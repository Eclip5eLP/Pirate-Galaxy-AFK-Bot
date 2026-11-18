"""Mouse input to the game window.

Window messages (post_*) reach the game without focus and without moving the user's cursor.
Whether the game reacts to them is tested in phase 2.
"""
import ctypes
import time
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
MK_LBUTTON, MK_RBUTTON = 0x0001, 0x0002

BUTTONS = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
}


class _MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MouseInput), ("padding", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.GetForegroundWindow.restype = wintypes.HWND

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
SEND_FLAGS = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP), "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)}


class NotForeground(Exception):
    """The game is not the foreground window, so real input would land elsewhere."""


def _send(dx, dy, flag):
    event = _Input(type=0)
    event.mi = _MouseInput(dx, dy, 0, flag, 0, 0)
    if _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
        raise OSError(ctypes.get_last_error(), "SendInput failed")


def _send_button(flag):
    _send(0, 0, flag)


def _send_move(screen_x, screen_y, steps=6):
    """Move the cursor with real mouse-move events, in a few steps from where it is now."""
    start = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(start))
    left, top = _user32.GetSystemMetrics(76), _user32.GetSystemMetrics(77)
    width, height = _user32.GetSystemMetrics(78), _user32.GetSystemMetrics(79)
    for i in range(1, steps + 1):
        x = start.x + (screen_x - start.x) * i / steps
        y = start.y + (screen_y - start.y) * i / steps
        nx = round((x - left) * 65535 / max(1, width - 1))
        ny = round((y - top) * 65535 / max(1, height - 1))
        _send(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)
        time.sleep(0.015)


def send_move(game_hwnd, screen_x, screen_y):
    """Move the real cursor over the game without clicking. Refuses unless the game is in the foreground."""
    if _user32.GetForegroundWindow() != game_hwnd:
        raise NotForeground("game window is not in the foreground; cursor not moved")
    _send_move(screen_x, screen_y)


def cursor_position():
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def send_click(game_hwnd, screen_x, screen_y, button="left", hold_s=0.1, settle_s=0.2, restore=True):
    """Real click at screen coordinates. Refuses unless the game window is in the foreground.

    Moves the user's cursor; it is put back afterwards when restore is True.
    """
    if _user32.GetForegroundWindow() != game_hwnd:
        raise NotForeground("game window is not in the foreground; click not sent")
    old = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(old))
    down, up = SEND_FLAGS[button]
    try:
        _send_move(screen_x, screen_y)
        time.sleep(settle_s)
        if _user32.GetForegroundWindow() != game_hwnd:
            raise NotForeground("focus changed before the click; click not sent")
        _send_button(down)
        time.sleep(hold_s)
        _send_button(up)
    finally:
        if restore:
            time.sleep(settle_s)
            _send_move(old.x, old.y)


def _lparam(x, y):
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def _post(hwnd, message, wparam, lparam):
    if not _user32.PostMessageW(hwnd, message, wparam, lparam):
        raise OSError(ctypes.get_last_error(), "PostMessage failed")


def post_drag(hwnd, x0, y0, x1, y1, button="right", steps=12, step_s=0.015):
    """Drag with a button held from (x0, y0) to (x1, y1) in client coordinates, using window messages.
    Holding a mouse button and dragging turns the game camera (user, 2026-11-11)."""
    down, up, flag = BUTTONS[button]
    _post(hwnd, WM_MOUSEMOVE, 0, _lparam(x0, y0))
    time.sleep(0.02)
    _post(hwnd, down, flag, _lparam(x0, y0))
    time.sleep(0.03)
    for i in range(1, steps + 1):
        x = x0 + (x1 - x0) * i / steps
        y = y0 + (y1 - y0) * i / steps
        _post(hwnd, WM_MOUSEMOVE, flag, _lparam(x, y))
        time.sleep(step_s)
    _post(hwnd, up, 0, _lparam(x1, y1))


def post_click(hwnd, x, y, button="left", hold_s=0.05):
    """Click at client coordinates (x, y) of hwnd using window messages."""
    down, up, flag = BUTTONS[button]
    position = _lparam(x, y)
    _post(hwnd, WM_MOUSEMOVE, 0, position)
    time.sleep(0.02)
    _post(hwnd, down, flag, position)
    time.sleep(hold_s)
    _post(hwnd, up, 0, position)


WM_MOUSEWHEEL = 0x020A


def post_wheel(hwnd, screen_x, screen_y, notches):
    """Turn the mouse wheel over hwnd by window messages: notches > 0 away from the user, < 0 toward the user.
    WM_MOUSEWHEEL carries screen coordinates."""
    for _ in range(abs(int(notches))):
        delta = 120 if notches > 0 else -120
        _post(hwnd, WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, _lparam(screen_x, screen_y))
        time.sleep(0.08)


def post_move(hwnd, x, y):
    """Move the cursor over client coordinates (x, y) of hwnd by a window message (hover, no click)."""
    _post(hwnd, WM_MOUSEMOVE, 0, _lparam(x, y))
