##################################################################
# Copyright 2018 Open Source Geospatial Foundation and others    #
# licensed under MIT, Please consult LICENSE.txt for details     #
##################################################################

from pywps._compat import text_type, StringIO
import os
import shutil
import requests
import tempfile
import logging
import pywps.configuration as config
from pywps.inout.literaltypes import (LITERAL_DATA_TYPES, convert,
                                      make_allowedvalues, is_anyvalue,
                                      is_values_reference)
from pywps import OGCUNIT
from pywps.validator.mode import MODE
from pywps.validator.base import emptyvalidator
from pywps.validator import get_validator
from pywps.validator.literalvalidator import (validate_value,
                                              validate_anyvalue,
                                              validate_allowed_values,
                                              validate_values_reference)
from pywps.exceptions import NoApplicableCode, InvalidParameterValue, FileSizeExceeded, \
    FileURLNotSupported
from pywps._compat import PY2, urlparse
import base64
from collections import namedtuple
from copy import deepcopy
from io import BytesIO


_SOURCE_TYPE = namedtuple('SOURCE_TYPE', 'MEMORY, FILE, STREAM, DATA, URL')
SOURCE_TYPE = _SOURCE_TYPE(0, 1, 2, 3, 4)

LOGGER = logging.getLogger("PYWPS")


def _is_textfile(filename):
    try:
        # use python-magic if available
        import magic
        is_text = 'text/' in magic.from_file(filename, mime=True)
    except ImportError:
        # read the first part of the file to check for a binary indicator.
        # This method won't detect all binary files.
        blocksize = 512
        fh = open(filename, 'rb')
        is_text = b'\x00' not in fh.read(blocksize)
        fh.close()
    return is_text


def extend_instance(obj, cls):
    """Apply mixins to a class instance after creation."""
    base_cls = obj.__class__
    base_cls_name = obj.__class__.__name__
    obj.__class__ = type(base_cls_name, (cls, base_cls), {})


class UOM(object):
    """
    :param uom: unit of measure
    """

    def __init__(self, uom=''):
        self.uom = uom

    @property
    def json(self):
        return {"reference": OGCUNIT[self.uom],
                "uom": self.uom}

    def __eq__(self, other):
        return self.uom == other.uom


class IOHandler(object):
    """Base IO handling class subclassed by specialized versions: FileHandler, UrlHandler, DataHandler, etc.

    If the specialized handling class is not known when the object is created, instantiate the object with IOHandler.
    The first time the `file`, `url` or `data` attribute is set, the associated subclass will be automatically
    registered. Once set, the specialized subclass cannot be switched.

    :param workdir: working directory, to save temporal file objects in.
    :param mode: ``MODE`` validation mode.


    `file` : str
      Filename on the local disk.
    `url` : str
      Link to an online resource.
    `stream` : FileIO
      A readable object.
    `data` : object
      A native python object (integer, string, float, etc)
    `base64` : str
      A base 64 encoding of the data.

    >>> # setting up
    >>> import os
    >>> from io import RawIOBase
    >>> from io import FileIO
    >>>
    >>> ioh_file = IOHandler(workdir=tmp)
    >>> assert isinstance(ioh_file, IOHandler)
    >>>
    >>> # Create test file input
    >>> fileobj = open(os.path.join(tmp, 'myfile.txt'), 'w')
    >>> fileobj.write('ASDF ASFADSF ASF ASF ASDF ASFASF')
    >>> fileobj.close()
    >>>
    >>> # testing file object on input
    >>> ioh_file.file = fileobj.name
    >>> assert isinstance(ioh_file, FileHandler
    >>> assert ioh_file.file == fileobj.name
    >>> assert isinstance(ioh_file.stream, RawIOBase)
    >>> # skipped assert isinstance(ioh_file.memory_object, POSH)
    >>>
    >>> # testing stream object on input
    >>> ioh_stream = IOHandler(workdir=tmp)
    >>> assert ioh_stream.workdir == tmp
    >>> ioh_stream.stream = FileIO(fileobj.name,'r')
    >>> assert isinstance(ioh_stream, StreamHandler)
    >>> assert open(ioh_stream.file).read() == ioh_file.stream.read()
    >>> assert isinstance(ioh_stream.stream, RawIOBase)
    """
    prop = None

    def __init__(self, workdir=None, mode=MODE.NONE):
        # Internal defaults for class and subclass properties.
        self._workdir = None
        self._reset_cache()

        # Set public defaults
        self.workdir = workdir
        self.valid_mode = mode

        # TODO: Clarify intent
        self.as_reference = False
        self.inpt = {}
        self.uuid = None  # request identifier
        self.data_set = False

        # This creates dummy property setters and getters for `file`, `data`, `url`, `stream` that
        #  1. register the subclass methods according to the given property
        #  2. replace the property setter by the subclass property setter
        #  3. set the property
        self._create_fset_properties()

    def _reset_cache(self):
        """Sets all internal objects to None."""
        self._file = None
        self._data = None
        self._post_data = None
        self._stream = None
        self._url = None
        self.data_set = False

    def _check_valid(self):
        """Validate this input using given validator
        """

        validate = self.validator
        _valid = validate(self, self.valid_mode)
        if not _valid:
            self.data_set = False
            raise InvalidParameterValue('Input data not valid using '
                                        'mode {}'.format(self.valid_mode))
        self.data_set = True

    @property
    def workdir(self):
        return self._workdir

    @workdir.setter
    def workdir(self, path):
        """Set working temporary directory for files to be stored in."""
        if path is not None:
            if not os.path.exists(path):
                os.makedirs(path)

        self._workdir = path

    @property
    def validator(self):
        """Return the function suitable for validation
        This method should be overridden by class children

        :return: validating function
        """

        return emptyvalidator

    @property
    def source_type(self):
        """Return the source type."""
        # For backward compatibility only. source_type checks could be replaced by `isinstance`.
        return getattr(SOURCE_TYPE, self.prop.upper())

    def _set_default_value(self, value=None, value_type=None):
        """Set default value based on input data type."""
        value = value or getattr(self, '_default')
        value_type = value_type or getattr(self, '_default_type')

        if value:
            if value_type == SOURCE_TYPE.DATA:
                self.data = value
            elif value_type == SOURCE_TYPE.MEMORY:
                raise NotImplementedError
            elif value_type == SOURCE_TYPE.FILE:
                self.file = value
            elif value_type == SOURCE_TYPE.STREAM:
                self.stream = value
            elif value_type == SOURCE_TYPE.URL:
                self.url = value

    def _build_file_name(self, href=''):
        """Return a file name for the local system."""
        url_path = urlparse(href).path or ''
        file_name = os.path.basename(url_path).strip() or 'input'
        (prefix, suffix) = os.path.splitext(file_name)
        suffix = suffix or self.extension
        if prefix and suffix:
            file_name = prefix + suffix
        input_file_name = os.path.join(self.workdir, file_name)

        # build tempfile in case of duplicates
        if os.path.exists(input_file_name):
            input_file_name = tempfile.mkstemp(
                suffix=suffix, prefix=prefix + '_',
                dir=self.workdir)[1]

        return input_file_name

    @property
    def extension(self):
        """Return the file extension for the data format, if set."""
        if getattr(self, 'data_format', None):
            return self.data_format.extension
        else:
            return ''

    def clone(self):
        """Create copy of yourself
        """
        return deepcopy(self)

    @staticmethod
    def _create_fset_properties():
        """Create properties that when set for the first time, will determine
        the instance's handler class.

        Example
        -------
        >>> h = IOHandler()
        >>> isinstance(h, DataHandler)
        False
        >>> h.data = 1 # Mixes the DataHandler class to IOHandler. h inherits DataHandler methods.
        >>> isinstance(h, DataHandler)
        True

        Note that trying to set another attribute (e.g. `h.file = 'a.txt'`) will raise an AttributeError.
        """
        for cls in (FileHandler, DataHandler, StreamHandler, UrlHandler):
            def fset(s, value, kls=cls):
                """Assign the handler class and set the value to the attribute.

                This function will only be called once. The next `fset` will
                use the subclass' property.
                """
                # Add cls methods to this instance.
                extend_instance(s, kls)

                # Set the attribute value through the associated cls property.
                setattr(s, kls.prop, value)

            setattr(IOHandler, cls.prop, property(fget=lambda x: None, fset=fset))


class FileHandler(IOHandler):
    prop = 'file'

    @property
    def file(self):
        """Return filename."""
        return self._file

    @file.setter
    def file(self, value):
        """Set file name"""
        self._reset_cache()
        self._file = os.path.abspath(value)
        self._check_valid()

    @property
    def data(self):
        """Read file and return content."""
        if self._data is None:
            with open(self.file, mode=self._openmode()) as fh:
                self._data = fh.read()
        return self._data

    @property
    def base64(self):
        """Return base64 encoding of data."""
        data = self.data.encode() if not isinstance(self.data, bytes) else self.data
        return base64.b64encode(data)

    @property
    def stream(self):
        """Return stream object."""
        from io import FileIO
        if getattr(self, '_stream', None) and not self._stream.closed:
            self._stream.close()

        self._stream = FileIO(self.file, mode='r', closefd=True)
        return self._stream

    @property
    def mem(self):
        """Return memory object."""
        raise NotImplementedError

    @property
    def url(self):
        """Return url to file."""
        import pathlib
        return pathlib.PurePosixPath(self.file).as_uri()

    def _openmode(self, data=None):
        openmode = 'r'
        if not PY2:
            # in Python 3 we need to open binary files in binary mode.
            checked = False
            if hasattr(self, 'data_format'):
                if self.data_format.encoding == 'base64':
                    # binary, when the data is to be encoded to base64
                    openmode += 'b'
                    checked = True
                elif 'text/' in self.data_format.mime_type:
                    # not binary, when mime_type is 'text/'
                    checked = True
            # when we can't guess it from the mime_type, we need to check the file.
            # mimetypes like application/xml and application/json are text files too.
            if not checked and not _is_textfile(self.file):
                openmode += 'b'
        return openmode


class DataHandler(FileHandler):
    prop = 'data'

    def _openmode(self, data=None):
        openmode = 'w'
        if not PY2 and isinstance(data, bytes):
            # on Python 3 open the file in binary mode if the source is
            # bytes, which happens when the data was base64-decoded
            openmode += 'b'
        return openmode

    @property
    def data(self):
        """Return data."""
        return getattr(self, '_data', None)

    @data.setter
    def data(self, value):
        self._reset_cache()
        self._data = value
        self._check_valid()

    @property
    def file(self):
        """Return file name storing the data.

        Requesting the file attributes writes the data to a temporary file on disk.
        """
        if self._file is None:
            self._file = self._build_file_name()
            with open(self._file, self._openmode(self.data)) as fh:
                fh.write(self.data)

        return self._file

    @property
    def stream(self):
        """Return a stream representation of the data."""
        if not PY2 and isinstance(self.data, bytes):
            return BytesIO(self.data)
        else:
            return StringIO(text_type(self.data))


class StreamHandler(DataHandler):
    prop = 'stream'

    @property
    def stream(self):
        """Return the stream."""
        return self._stream

    @stream.setter
    def stream(self, value):
        """Set the stream."""
        self._reset_cache()
        self._stream = value
        self._check_valid()

    @property
    def data(self):
        """Return the data from the stream."""
        if self._data is None:
            self._data = self.stream.read()
        return self._data


class UrlHandler(FileHandler):
    prop = 'url'

    @property
    def url(self):
        """Return the URL."""
        return self._url

    @url.setter
    def url(self, value):
        """Set the URL value."""
        self._reset_cache()
        self._url = value
        self._check_valid()

    @property
    def file(self):
        if self._file is not None:
            return self._file

        self._file = self._build_file_name(href=self.url)

        max_byte_size = self.max_input_size()

        # Create request
        try:
            reference_file = self._openurl(self.url, self.post_data)
            data_size = reference_file.headers.get('Content-Length', 0)
        except Exception as e:
            raise NoApplicableCode('File reference error: {}'.format(e))

        error_message = 'File size for input "{}" exceeded. Maximum allowed: {} megabytes'.format(
            self.inpt.get('identifier', '?'), max_byte_size)

        if int(data_size) > int(max_byte_size):
            raise FileSizeExceeded(error_message)

        try:
            with open(self._file, 'wb') as f:
                data_size = 0
                for chunk in reference_file.iter_content(chunk_size=1024):
                    data_size += len(chunk)
                    if int(data_size) > int(max_byte_size):
                        raise FileSizeExceeded(error_message)
                    f.write(chunk)

        except Exception as e:
            raise NoApplicableCode(e)

        return self._file

    @property
    def post_data(self):
        return self._post_data

    @post_data.setter
    def post_data(self, value):
        self._post_data = value

    @staticmethod
    def _openurl(href, data=None):
        """Open given href.
        """
        LOGGER.debug('Fetching URL {}'.format(href))
        if data is not None:
            req = requests.post(url=href, data=data, stream=True)
        else:
            req = requests.get(url=href, stream=True)

        return req

    @staticmethod
    def max_input_size():
        """Calculates maximal size for input file based on configuration
        and units.

        :return: maximum file size in bytes
        """
        ms = config.get_config_value('server', 'maxsingleinputsize')
        return config.get_size_mb(ms) * 1024**2


class SimpleHandler(DataHandler):
    """Data handler for Literal In- and Outputs

    >>> class Int_type(object):
    ...     @staticmethod
    ...     def convert(value): return int(value)
    >>>
    >>> class MyValidator(object):
    ...     @staticmethod
    ...     def validate(inpt): return 0 < inpt.data < 3
    >>>
    >>> inpt = SimpleHandler(data_type = Int_type)
    >>> inpt.validator = MyValidator
    >>>
    >>> inpt.data = 1
    >>> inpt.validator.validate(inpt)
    True
    >>> inpt.data = 5
    >>> inpt.validator.validate(inpt)
    False
    """

    def __init__(self, workdir=None, data_type=None, mode=MODE.NONE):
        DataHandler.__init__(self, workdir=workdir, mode=mode)
        if data_type not in LITERAL_DATA_TYPES:
            raise ValueError('data_type {} not in {}'.format(data_type, LITERAL_DATA_TYPES))
        self.data_type = data_type

    @DataHandler.data.setter
    def data(self, value):
        """Set data value. Inputs are converted into target format.
        """
        if self.data_type and value is not None:
            value = convert(self.data_type, value)

        DataHandler.data.fset(self, value)


class BasicIO:
    """Basic Input/Output class
    """
    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 min_occurs=1, max_occurs=1, metadata=[]):
        self.identifier = identifier
        self.title = title
        self.abstract = abstract
        self.keywords = keywords
        self.min_occurs = int(min_occurs)
        self.max_occurs = int(max_occurs)
        self.metadata = metadata


class BasicLiteral:
    """Basic literal Input/Output class
    """

    def __init__(self, data_type="integer", uoms=None):
        assert data_type in LITERAL_DATA_TYPES
        self.data_type = data_type
        # list of uoms
        self.uoms = []
        # current uom
        self._uom = None

        # add all uoms (upcasting to UOM)
        if uoms is not None:
            for uom in uoms:
                if not isinstance(uom, UOM):
                    uom = UOM(uom)
                self.uoms.append(uom)

        if self.uoms:
            # default/current uom
            self.uom = self.uoms[0]

    @property
    def uom(self):
        return self._uom

    @uom.setter
    def uom(self, uom):
        if uom is not None:
            self._uom = uom


class BasicComplex(object):
    """Basic complex input/output class

    """

    def __init__(self, data_format=None, supported_formats=None):
        self._data_format = data_format
        self._supported_formats = ()
        if supported_formats:
            self.supported_formats = supported_formats

        if data_format:
            self.data_format = data_format
        elif self.supported_formats:
            # not an empty list, set the default/current format to the first
            self.data_format = supported_formats[0]

    def get_format(self, mime_type):
        """
        :param mime_type: given mimetype
        :return: Format
        """

        for frmt in self.supported_formats:
            if frmt.mime_type == mime_type:
                return frmt
        else:
            return None

    @property
    def validator(self):
        """Return the proper validator for given data_format
        """
        return self.data_format.validate

    @property
    def supported_formats(self):
        return self._supported_formats

    @supported_formats.setter
    def supported_formats(self, supported_formats):
        """Setter of supported formats
        """

        def set_format_validator(supported_format):
            if not supported_format.validate or \
               supported_format.validate == emptyvalidator:
                supported_format.validate =\
                    get_validator(supported_format.mime_type)
            return supported_format

        self._supported_formats = tuple(map(set_format_validator, supported_formats))

    @property
    def data_format(self):
        return self._data_format

    @data_format.setter
    def data_format(self, data_format):
        """self data_format setter
        """
        if self._is_supported(data_format):
            self._data_format = data_format
            if not data_format.validate or data_format.validate == emptyvalidator:
                data_format.validate = get_validator(data_format.mime_type)
        else:
            raise InvalidParameterValue("Requested format {}, {}, {} not supported".format(
                                        data_format.mime_type,
                                        data_format.encoding,
                                        data_format.schema),
                                        'mimeType')

    def _is_supported(self, data_format):

        if self.supported_formats:
            for frmt in self.supported_formats:
                if frmt.same_as(data_format):
                    return True

        return False


class BasicBoundingBox(object):
    """Basic BoundingBox input/output class
    """

    def __init__(self, crss=None, dimensions=2):
        self.crss = crss or ['epsg:4326']
        self.crs = self.crss[0]
        self.dimensions = dimensions

    @property
    def ll(self):
        data = getattr(self, 'data', None)
        if data:
            return data[:2]
        return []

    @property
    def ur(self):
        data = getattr(self, 'data', None)
        if data:
            return data[2:]
        return []


class LiteralInput(BasicIO, BasicLiteral, SimpleHandler):
    """LiteralInput input abstract class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 data_type="integer", workdir=None, allowed_values=None,
                 uoms=None, mode=MODE.NONE,
                 min_occurs=1, max_occurs=1, metadata=[],
                 default=None, default_type=SOURCE_TYPE.DATA):
        BasicIO.__init__(self, identifier, title, abstract, keywords,
                         min_occurs, max_occurs, metadata)
        BasicLiteral.__init__(self, data_type, uoms)
        SimpleHandler.__init__(self, workdir, data_type, mode=mode)

        if default_type != SOURCE_TYPE.DATA:
            raise InvalidParameterValue("Source types other than data are not supported.")

        self.any_value = False
        self.values_reference = None
        self.allowed_values = []

        if allowed_values:
            if not isinstance(allowed_values, (tuple, list)):
                allowed_values = [allowed_values]
            self.any_value = any(is_anyvalue(a) for a in allowed_values)
            for value in allowed_values:
                if is_values_reference(value):
                    self.values_reference = value
                    break
            self.allowed_values = make_allowedvalues(allowed_values)

        self._default = default
        self._default_type = default_type

        if default is not None:
            self.data = default

    @property
    def validator(self):
        """Get validator for any value as well as allowed_values
        :rtype: function
        """

        if self.any_value:
            return validate_anyvalue
        elif self.values_reference:
            return validate_values_reference
        elif self.allowed_values:
            return validate_allowed_values
        else:
            return validate_value


class LiteralOutput(BasicIO, BasicLiteral, SimpleHandler):
    """Basic LiteralOutput class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 data_type=None, workdir=None, uoms=None, validate=None,
                 mode=MODE.NONE):
        BasicIO.__init__(self, identifier, title, abstract, keywords)
        BasicLiteral.__init__(self, data_type, uoms)
        SimpleHandler.__init__(self, workdir=None, data_type=data_type,
                               mode=mode)

        self._storage = None

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, storage):
        self._storage = storage

    @property
    def validator(self):
        """Get validator for any value as well as allowed_values
        """

        return validate_anyvalue


class BBoxInput(BasicIO, BasicBoundingBox, DataHandler):
    """Basic Bounding box input abstract class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=[], crss=None,
                 dimensions=None, workdir=None,
                 mode=MODE.SIMPLE,
                 min_occurs=1, max_occurs=1, metadata=[],
                 default=None, default_type=SOURCE_TYPE.DATA):
        BasicIO.__init__(self, identifier, title, abstract, keywords,
                         min_occurs, max_occurs, metadata)
        BasicBoundingBox.__init__(self, crss, dimensions)
        DataHandler.__init__(self, workdir=workdir, mode=mode)

        if default_type != SOURCE_TYPE.DATA:
            raise InvalidParameterValue("Source types other than data are not supported.")

        self._default = default
        self._default_type = default_type

        self._set_default_value(default, default_type)


class BBoxOutput(BasicIO, BasicBoundingBox, DataHandler):
    """Basic BoundingBox output class
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None, crss=None,
                 dimensions=None, workdir=None, mode=MODE.NONE):
        BasicIO.__init__(self, identifier, title, abstract, keywords)
        BasicBoundingBox.__init__(self, crss, dimensions)
        DataHandler.__init__(self, workdir=workdir, mode=mode)
        self._storage = None

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, storage):
        self._storage = storage


class ComplexInput(BasicIO, BasicComplex, IOHandler):
    """Complex input abstract class

    >>> ci = ComplexInput()
    >>> ci.validator = 1
    >>> ci.validator
    1
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 workdir=None, data_format=None, supported_formats=None,
                 mode=MODE.NONE,
                 min_occurs=1, max_occurs=1, metadata=[],
                 default=None, default_type=SOURCE_TYPE.DATA):

        BasicIO.__init__(self, identifier, title, abstract, keywords,
                         min_occurs, max_occurs, metadata)
        IOHandler.__init__(self, workdir=workdir, mode=mode)
        BasicComplex.__init__(self, data_format, supported_formats)

        self._default = default
        self._default_type = default_type

    def file_handler(self, inpt):
        """<wps:Reference /> handler.
        Used when href is a file url."""
        extend_instance(self, FileHandler)

        # check if file url is allowed
        self._validate_file_input(href=inpt.get('href'))
        # save the file reference input in workdir
        tmp_file = self._build_file_name(href=inpt.get('href'))

        try:
            inpt_file = urlparse(inpt.get('href')).path
            inpt_file = os.path.abspath(inpt_file)
            os.symlink(inpt_file, tmp_file)
            LOGGER.debug("Linked input file {} to {}.".format(inpt_file, tmp_file))
        except Exception:
            # TODO: handle os.symlink on windows
            # raise NoApplicableCode("Could not link file reference: {}".format(e))
            LOGGER.warn("Could not link file reference")
            shutil.copy2(inpt_file, tmp_file)

        return tmp_file

    def url_handler(self, inpt):
        # That could possibly go into the data property...
        if inpt.get('method') == 'POST':
            if 'body' in inpt:
                self.post_data = inpt.get('body')
            elif 'bodyreference' in inpt:
                self.post_data = requests.get(url=inpt.get('bodyreference')).text
            else:
                raise AttributeError("Missing post data content.")

        return inpt.get('href')

    def process(self, inpt):
        """Subclass with the appropriate handler given the data input."""
        href = inpt.get('href', None)
        self.inpt = inpt

        if href:
            if urlparse(href).scheme == 'file':
                self.file = self.file_handler(inpt)

            else:
                # No file download occurs here. The file content will
                # only be retrieved when the file property is accessed.
                self.url = self.url_handler(inpt)

        else:
            self.data = inpt.get('data')

    @staticmethod
    def _validate_file_input(href):
        href = href or ''
        parsed_url = urlparse(href)
        if parsed_url.scheme != 'file':
            raise FileURLNotSupported('Invalid URL scheme')
        file_path = parsed_url.path
        if not file_path:
            raise FileURLNotSupported('Invalid URL path')
        file_path = os.path.abspath(file_path)
        # build allowed paths list
        inputpaths = config.get_config_value('server', 'allowedinputpaths')
        allowed_paths = [os.path.abspath(p.strip()) for p in inputpaths.split(':') if p.strip()]
        for allowed_path in allowed_paths:
            if file_path.startswith(allowed_path):
                LOGGER.debug("Accepted file url as input.")
                return
        raise FileURLNotSupported()


class ComplexOutput(BasicIO, BasicComplex, IOHandler):
    """Complex output abstract class

    >>> # temporary configuration
    >>> import ConfigParser
    >>> from pywps.storage import *
    >>> config = ConfigParser.RawConfigParser()
    >>> config.add_section('FileStorage')
    >>> config.set('FileStorage', 'target', './')
    >>> config.add_section('server')
    >>> config.set('server', 'outputurl', 'http://foo/bar/filestorage')
    >>>
    >>> # create temporary file
    >>> tiff_file = open('file.tiff', 'w')
    >>> tiff_file.write("AA")
    >>> tiff_file.close()
    >>>
    >>> co = ComplexOutput()
    >>> co.file ='file.tiff'
    >>> fs = FileStorage(config)
    >>> co.storage = fs
    >>>
    >>> url = co.url # get url, data are stored
    >>>
    >>> co.stream.read() # get data - nothing is stored
    'AA'
    """

    def __init__(self, identifier, title=None, abstract=None, keywords=None,
                 workdir=None, data_format=None, supported_formats=None,
                 mode=MODE.NONE):
        BasicIO.__init__(self, identifier, title, abstract, keywords)
        IOHandler.__init__(self, workdir=workdir, mode=mode)
        BasicComplex.__init__(self, data_format, supported_formats)

        self._storage = None

    @property
    def storage(self):
        return self._storage

    @storage.setter
    def storage(self, storage):
        # don't set storage twice
        if self._storage is None:
            self._storage = storage

    # TODO: refactor ?
    def get_url(self):
        """Return URL pointing to data
        """
        (outtype, storage, url) = self.storage.store(self)
        return url


if __name__ == "__main__":
    import doctest
    from pywps.wpsserver import temp_dir

    with temp_dir() as tmp:
        os.chdir(tmp)
        doctest.testmod()
