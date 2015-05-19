from abc import ABCMeta, abstractmethod, abstractproperty
import os
from pywps._compat import PY2
from pywps.exceptions import NotEnoughStorage, NoApplicableCode


class STORE_TYPE:
    PATH = 0
# TODO: cover with tests
class StorageAbstract(object):
    """Data storage abstract class
    """

    __metaclass__ = ABCMeta

    @abstractmethod
    def store(self, output):
        """
        :param output: of type IOHandler
        :returns: (type, store, url) where
            type - is type of STORE_TYPE - number
            store - string describing storage - file name, database connection
            url - url, where the data can be downloaded
        """
        pass

class DummyStorage(StorageAbstract):
    """Dummy empty storage implementation, does nothing

    Default instance, for non-reference output request

    >>> store = DummyStorage()
    >>> assert store.store
    """

    def __init__(self, config=None):
        """
        :param config: storage configuration object
        """
        self.config = config

    def store(self, ouput):
        pass


class FileStorage(StorageAbstract):
    """File storage implementation, stores data to file system

    >>> import ConfigParser
    >>> config = ConfigParser.RawConfigParser()
    >>> config.add_section('FileStorage')
    >>> config.set('FileStorage', 'target', './')
    >>> config.add_section('server')
    >>> config.set('server', 'outputurl', 'http://foo/bar/filestorage')
    >>>
    >>> store = FileStorage(config = config)
    >>>
    >>> class FakeOutput(object):
    ...     def __init__(self):
    ...         self.file = self._get_file()
    ...     def _get_file(self):
    ...         tiff_file = open('file.tiff', 'w')
    ...         tiff_file.close()
    ...         return 'file.tiff'
    >>> fake_out = FakeOutput()
    >>> (type, path, url) = store.store(fake_out)
    >>> type == STORE_TYPE.PATH
    True
    """

    def __init__(self, config):
        """
        :param config: storage configuration object
        """
        self.target = config.get_config_value('server', 'outputPath')
        self.output_url = '%s:%s%s' % (
            config.get_config_value('wps', 'serveraddress'),
            config.get_config_value('wps', 'serverport'),
            config.get_config_value('server', 'outputUrl')
        )

    def store(self, output):
        import shutil, tempfile, math

        file_name = output.file

        file_block_size = os.stat(file_name).st_blksize
        avail_size = get_free_space(self.target)
        file_size = os.stat(file_name).st_size

        # calculate space used according to block size
        actual_file_size = math.ceil(file_size / float(file_block_size)) * file_block_size

        if avail_size < actual_file_size:
            raise NotEnoughStorage('Not enough space in %s to store %s' % (self.target, file_name))

        (prefix, suffix) = os.path.splitext(file_name)
        if not suffix:
            suffix = output.output_format.get_extension()
        (file_dir, file_name) = os.path.split(prefix)
        output_name = tempfile.mkstemp(suffix=suffix, prefix=file_name,
                                       dir=self.target)[1]

        shutil.copy2(output.file, os.path.join(self.target, output_name))

        just_file_name = os.path.basename(output_name)

        if PY2:
            from urlparse import urljoin
            url = urljoin(self.output_url, just_file_name)
        else:
            from urllib.parse import urljoin
            url = urljoin(self.output_url, just_file_name)

        return (STORE_TYPE.PATH, output_name, url)


def get_free_space(folder):
    """ Return folder/drive free space (in bytes)
    """
    import platform

    if platform.system() == 'Windows':
        import ctypes

        free_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(folder), None, None, ctypes.pointer(free_bytes))
        return free_bytes.value
    else:
        return os.statvfs(folder).f_bfree