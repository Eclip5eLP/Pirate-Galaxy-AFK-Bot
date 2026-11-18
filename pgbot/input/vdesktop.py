"""Query which virtual desktop a window is on, using the documented IVirtualDesktopManager COM interface.

Read-only: this module never moves windows (moving another process's window needs undocumented APIs).
"""
import ctypes
import uuid
from ctypes import wintypes

_ole32 = ctypes.WinDLL("ole32")
CLSCTX_ALL = 0x17
COINIT_APARTMENTTHREADED = 0x2


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]

    @classmethod
    def parse(cls, text):
        value = cls()
        ctypes.memmove(value.data, uuid.UUID(text).bytes_le, 16)
        return value

    def __str__(self):
        return str(uuid.UUID(bytes_le=bytes(bytearray(self.data))))


CLSID_VirtualDesktopManager = GUID.parse("aa509086-5ca9-4c25-8f95-589d3c07b48a")
IID_IVirtualDesktopManager = GUID.parse("a5cd92ff-29be-454c-8d04-d82879fb3f1b")

_IS_ON_CURRENT = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.POINTER(wintypes.BOOL))
_GET_DESKTOP_ID = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND, ctypes.POINTER(GUID))


class VirtualDesktops:
    def __init__(self):
        _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        self._ptr = ctypes.c_void_p()
        result = _ole32.CoCreateInstance(ctypes.byref(CLSID_VirtualDesktopManager), None, CLSCTX_ALL,
                                         ctypes.byref(IID_IVirtualDesktopManager), ctypes.byref(self._ptr))
        if result != 0:
            raise OSError(result, "IVirtualDesktopManager not available")
        vtable = ctypes.cast(ctypes.cast(self._ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                             ctypes.POINTER(ctypes.c_void_p))
        # IUnknown takes slots 0-2, then IsWindowOnCurrentVirtualDesktop, GetWindowDesktopId, MoveWindowToDesktop.
        self._is_on_current = _IS_ON_CURRENT(vtable[3])
        self._get_desktop_id = _GET_DESKTOP_ID(vtable[4])

    def is_on_current(self, hwnd):
        value = wintypes.BOOL()
        result = self._is_on_current(self._ptr, hwnd, ctypes.byref(value))
        if result != 0:
            raise OSError(result, "IsWindowOnCurrentVirtualDesktop failed")
        return bool(value.value)

    def desktop_id(self, hwnd):
        value = GUID()
        result = self._get_desktop_id(self._ptr, hwnd, ctypes.byref(value))
        if result != 0:
            raise OSError(result, "GetWindowDesktopId failed")
        return str(value)
