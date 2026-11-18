"""Keyboard input to the game window: real key presses (focus needed) or window messages (no focus)."""
import ctypes
import time
from ctypes import wintypes

from .mouse import NotForeground

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]

KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101

VK = {**{str(d): 0x30 + d for d in range(10)}, **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
      "TAB": 0x09, "SPACE": 0x20, "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77, "ESC": 0x1B}


class _KeyInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KeyInput), ("padding", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _scan(key):
    return _user32.MapVirtualKeyW(VK[key.upper()], 0)


def _send(key, up):
    event = _Input(type=1)
    event.ki = _KeyInput(0, _scan(key), KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    if _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input)) != 1:
        raise OSError(ctypes.get_last_error(), "SendInput failed")


def send_key(game_hwnd, key, hold_s=0.06):
    """Real key press. Refuses unless the game is the foreground window."""
    if _user32.GetForegroundWindow() != game_hwnd:
        raise NotForeground("game window is not in the foreground; key not sent")
    _send(key, False)
    try:
        time.sleep(hold_s)
    finally:
        _send(key, True)


def post_key(hwnd, key, hold_s=0.06):
    """Key press as window messages; needs no focus."""
    vk, scan = VK[key.upper()], _scan(key)
    _user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1 | (scan << 16))
    time.sleep(hold_s)
    _user32.PostMessageW(hwnd, WM_KEYUP, vk, 1 | (scan << 16) | (1 << 30) | (1 << 31))
