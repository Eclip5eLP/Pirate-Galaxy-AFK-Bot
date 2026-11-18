"""Put windows on another virtual desktop (used only when the bot starts the game itself, docs/ROADMAP.md 8).

Two ways, the first preferred:
1. Windows' internal virtual desktop interface (IVirtualDesktopManagerInternal, undocumented): moves a window of any
   process to a desktop; the user's view never changes. Its interface ids change between Windows builds, so it is
   checked against the registry (desktop count and current desktop) before anything is moved; on a mismatch it
   counts as unavailable.
2. Switch the user's view to the target desktop with Win+Ctrl+Right, start the program there (new windows open on
   the active desktop), switch back with Win+Ctrl+Left. Documented means only, but the screen visibly switches.

Nothing here touches the game: it only moves windows / switches desktops.
"""
import ctypes
import time
import uuid
import winreg
from ctypes import wintypes

_ole32 = ctypes.WinDLL("ole32")
_user32 = ctypes.WinDLL("user32", use_last_error=True)
CLSCTX_LOCAL_SERVER = 0x4
COINIT_APARTMENTTHREADED = 0x2
REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_byte * 16)]

    @classmethod
    def parse(cls, text):
        value = cls()
        ctypes.memmove(value.data, uuid.UUID(text).bytes_le, 16)
        return value

    def __str__(self):
        return str(uuid.UUID(bytes_le=bytes(bytearray(self.data))))


CLSID_ImmersiveShell = GUID.parse("c2f03a33-21f5-47fa-b4bb-156362a2f239")
IID_IServiceProvider = GUID.parse("6d5140c1-7436-11ce-8034-00aa006009fa")
CLSID_VirtualDesktopManagerInternal = GUID.parse("c5e0cdca-7b6e-41b2-9fc4-d93975cc467b")
IID_IApplicationViewCollection = GUID.parse("1841c6d7-4f9d-42c0-af41-8747538f10e5")
IID_IObjectArray = GUID.parse("92ca9dcd-5622-4bba-a805-5e9f541bd8c9")
# Windows 11 24H2 / 25H2 (builds 26100 / 26200).
BUILDS = {
    "24H2": {"manager": GUID.parse("53f5ca0b-158f-4124-900c-057158060b27"),
             "desktop": GUID.parse("3f07f4be-b107-441a-af0f-39d82529072c")},
}

_HR = ctypes.HRESULT


def _method(ptr, index, *argtypes):
    vtable = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    return ctypes.WINFUNCTYPE(_HR, ctypes.c_void_p, *argtypes)(vtable[index])


def _release(ptr):
    if ptr:
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
            ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))[2])(ptr)


def registry_desktops():
    """(desktop ids in order, current id) from the registry; ([], None) if not available."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
            ids, _ = winreg.QueryValueEx(key, "VirtualDesktopIDs")
            try:
                current, _ = winreg.QueryValueEx(key, "CurrentVirtualDesktop")
            except OSError:
                current = None
    except OSError:
        return [], None
    desktops = [str(uuid.UUID(bytes_le=bytes(ids[i:i + 16]))) for i in range(0, len(ids) - 15, 16)]
    return desktops, (str(uuid.UUID(bytes_le=bytes(current))) if current else (desktops[0] if desktops else None))


class InternalDesktops:
    """The undocumented interface, verified. Raises OSError when it cannot be used on this Windows build."""

    def __init__(self):
        _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        shell = ctypes.c_void_p()
        hr = _ole32.CoCreateInstance(ctypes.byref(CLSID_ImmersiveShell), None, CLSCTX_LOCAL_SERVER,
                                     ctypes.byref(IID_IServiceProvider), ctypes.byref(shell))
        if hr != 0:
            raise OSError(hr, "ImmersiveShell not available")
        self._shell = shell
        query = _method(shell, 3, ctypes.POINTER(GUID), ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        views = ctypes.c_void_p()
        try:
            query(shell, ctypes.byref(IID_IApplicationViewCollection), ctypes.byref(IID_IApplicationViewCollection),
                  ctypes.byref(views))
        except OSError as error:
            raise OSError(-1, f"IApplicationViewCollection not available ({error})") from None
        self._views = views
        self._manager = None
        for name, ids in BUILDS.items():
            manager = ctypes.c_void_p()
            try:
                query(shell, ctypes.byref(CLSID_VirtualDesktopManagerInternal), ctypes.byref(ids["manager"]),
                      ctypes.byref(manager))
            except OSError:
                continue
            self._manager, self._ids, self.build = manager, ids, name
            break
        if self._manager is None:
            raise OSError(-1, "IVirtualDesktopManagerInternal: no known interface id fits this Windows build")
        self._verify()

    def _verify(self):
        """The layout is right only if what it reports matches the registry."""
        desktops, current = registry_desktops()
        count = ctypes.c_int()
        _method(self._manager, 3, ctypes.POINTER(ctypes.c_int))(self._manager, ctypes.byref(count))
        if not desktops or count.value != len(desktops):
            raise OSError(-1, f"desktop count does not match ({count.value} vs registry {len(desktops)})")
        ids = self.desktop_ids()
        if ids != desktops:
            raise OSError(-1, "desktop ids do not match the registry")
        if current is not None and self.current_id() != current:
            raise OSError(-1, "current desktop does not match the registry")

    def _desktop_ptrs(self):
        array = ctypes.c_void_p()
        _method(self._manager, 7, ctypes.POINTER(ctypes.c_void_p))(self._manager, ctypes.byref(array))
        try:
            n = ctypes.c_uint()
            _method(array, 3, ctypes.POINTER(ctypes.c_uint))(array, ctypes.byref(n))
            result = []
            for i in range(n.value):
                desktop = ctypes.c_void_p()
                _method(array, 4, ctypes.c_uint, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                    array, i, ctypes.byref(self._ids["desktop"]), ctypes.byref(desktop))
                result.append(desktop)
            return result
        finally:
            _release(array)

    @staticmethod
    def _desktop_id(desktop):
        value = GUID()
        _method(desktop, 4, ctypes.POINTER(GUID))(desktop, ctypes.byref(value))
        return str(value)

    def desktop_ids(self):
        ptrs = self._desktop_ptrs()
        try:
            return [self._desktop_id(p) for p in ptrs]
        finally:
            for p in ptrs:
                _release(p)

    def current_id(self):
        desktop = ctypes.c_void_p()
        _method(self._manager, 6, ctypes.POINTER(ctypes.c_void_p))(self._manager, ctypes.byref(desktop))
        try:
            return self._desktop_id(desktop)
        finally:
            _release(desktop)

    def move(self, hwnd, index):
        """Move a window to desktop number index (0 = first). Raises OSError if Windows refuses."""
        view = ctypes.c_void_p()
        _method(self._views, 6, wintypes.HWND, ctypes.POINTER(ctypes.c_void_p))(self._views, hwnd, ctypes.byref(view))
        if not view:
            raise OSError(-1, "no view for this window yet")
        ptrs = self._desktop_ptrs()
        try:
            if index >= len(ptrs):
                raise OSError(-1, f"there is no desktop {index + 1}")
            _method(self._manager, 4, ctypes.c_void_p, ctypes.c_void_p)(self._manager, view, ptrs[index])
        finally:
            for p in ptrs:
                _release(p)
            _release(view)


# Fallback: switch with the keyboard shortcut (SendInput).
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x2
VK_LWIN, VK_CONTROL, VK_LEFT, VK_RIGHT = 0x5B, 0x11, 0x25, 0x27


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _keys(*vks):
    events = []
    for vk in vks:
        events.append(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk)))
    for vk in reversed(vks):
        events.append(_INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP)))
    array = (_INPUT * len(events))(*events)
    _user32.SendInput(len(events), array, ctypes.sizeof(_INPUT))


def switch_by_keys(direction):
    """Win+Ctrl+Right / Left: the user's view moves one desktop."""
    _keys(VK_LWIN, VK_CONTROL, VK_RIGHT if direction > 0 else VK_LEFT)


def wait_current(desktop_id, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if registry_desktops()[1] == desktop_id:
            return True
        time.sleep(0.1)
    return False
