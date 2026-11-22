"""Find the game's windows and their screen geometry."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass

_user32 = ctypes.WinDLL("user32", use_last_error=True)

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetForegroundWindow.restype = wintypes.HWND


def make_dpi_aware():
    """Use physical pixels for all coordinates. Call once at startup."""
    try:
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor aware v2
    except AttributeError:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


@dataclass
class Window:
    hwnd: int
    class_name: str
    title: str
    left: int
    top: int
    width: int
    height: int
    visible: bool
    children: list


def _describe(hwnd, children):
    name = ctypes.create_unicode_buffer(256)
    title = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, name, 256)
    _user32.GetWindowTextW(hwnd, title, 256)
    rect = wintypes.RECT()
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return Window(hwnd, name.value, title.value, origin.x, origin.y, rect.right, rect.bottom,
                  bool(_user32.IsWindowVisible(hwnd)), children)


def _children(hwnd):
    found = []

    def callback(child, _):
        found.append(child)
        return True

    _user32.EnumChildWindows(hwnd, WNDENUMPROC(callback), 0)
    return [_describe(child, []) for child in found]


def windows_of(pid):
    """Return the top-level windows of a process with their child windows."""
    found = []

    def callback(hwnd, _):
        owner = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            found.append(hwnd)
        return True

    _user32.EnumWindows(WNDENUMPROC(callback), 0)
    return [_describe(hwnd, _children(hwnd)) for hwnd in found]


def game_window(pid):
    """The visible top-level window with the largest client area: the game's main frame."""
    candidates = [w for w in windows_of(pid) if w.visible and w.width > 0 and w.height > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda w: w.width * w.height)


# Native crashes of the client can end in a Visual C++ runtime message box instead of the process exiting
# (2026-11-20 21:18, server: "Runtime Error! ... requested the Runtime to terminate it in an unusual way"); the process
# then stays open with its memory frozen until the box is closed. Seen under Wine as a '#32770' dialog of the game
# process titled "Microsoft Visual C++ Runtime Library" with a Static "Runtime Error!..." text.
_CRASH_TITLES = ("runtime library", "program error", "fatal error")
_CRASH_TEXTS = ("runtime error", "terminate it in an unusual way", "has stopped working", "fatal error")


def crash_dialog(pid):
    """The game's crash message box (a visible dialog with a runtime error text), or None."""
    for w in windows_of(pid):
        if not w.visible or w.class_name != "#32770":
            continue
        texts = [w.title.lower()] + [c.title.lower() for c in w.children]
        if any(t in texts[0] for t in _CRASH_TITLES) or any(m in t for t in texts for m in _CRASH_TEXTS):
            return w
    return None


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
PROCESS_TERMINATE = 0x0001


_kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_kernel32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_age_s(pid):
    """Seconds since the process started, or None if not known."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return None
        now = wintypes.FILETIME()
        _kernel32.GetSystemTimeAsFileTime(ctypes.byref(now))

        def value(ft):
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        created = value(times[0])
        return (value(now) - created) / 1e7 if created else None
    finally:
        _kernel32.CloseHandle(handle)


def end_process(pid):
    """End a process that crashed and did not exit (only after its crash box was found). True if ended."""
    handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


def foreground_hwnd():
    return _user32.GetForegroundWindow()


_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetTopWindow.restype = wintypes.HWND
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, wintypes.UINT]
GW_HWNDNEXT = 2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0001, 0x0002, 0x0010, 0x0200
HWND_NOTOPMOST = -2


def is_above(hwnd, other):
    """True if hwnd is stacked above other in the window z-order."""
    current = _user32.GetTopWindow(None)
    while current:
        if current == hwnd:
            return True
        if current == other:
            return False
        current = _user32.GetWindow(current, GW_HWNDNEXT)
    return False


HWND_BOTTOM = 1


def send_to_bottom(hwnd):
    """Put hwnd below all other top-level windows without activating anything; clears always-on-top."""
    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
    _user32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
    _user32.SetWindowPos(hwnd, ctypes.c_void_p(HWND_BOTTOM), 0, 0, 0, 0, flags)
