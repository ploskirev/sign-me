"""Small, bundled Cocoa bridge. Qt retains ownership of its window and content view."""
import ctypes
from pathlib import Path
import sys


class MacBackdrop:
    def __init__(self):
        self.library = None
        if sys.platform != 'darwin':
            return
        root = Path(__file__).parent
        path = root / 'libtalon_backdrop.dylib' if getattr(sys,'frozen',False) else root / '.build/native/libtalon_backdrop.dylib'
        try:
            self.library = ctypes.CDLL(str(path))
        except OSError:
            return
        for name,result in (('talon_install_backdrop',ctypes.c_int),('talon_backdrop_ready',ctypes.c_int),
                            ('talon_window_number',ctypes.c_long)):
            function = getattr(self.library,name)
            function.argtypes = [ctypes.c_void_p]
            function.restype = result
        self.library.talon_set_backdrop_dark.argtypes = [ctypes.c_void_p,ctypes.c_int]
        self.library.talon_set_backdrop_dark.restype = None

    def install(self, window):
        return bool(self.library and self.library.talon_install_backdrop(int(window.winId())))

    def ready(self, window):
        return bool(self.library and self.library.talon_backdrop_ready(int(window.winId())))

    def window_number(self, window):
        return self.library.talon_window_number(int(window.winId())) if self.library else 0

    def set_dark(self, window, dark):
        if self.library:
            self.library.talon_set_backdrop_dark(int(window.winId()),int(dark))
