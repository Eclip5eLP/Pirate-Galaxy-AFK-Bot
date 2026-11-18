"""Screenshot of the game window, also when it is on another desktop or behind other windows (PrintWindow with
PW_RENDERFULLCONTENT). Sends nothing to the game; it only copies what the window shows. Tested 2026-11-12: works
with the game on desktop 2 (3440x1440 client area)."""
import ctypes
from ctypes import wintypes

import numpy as np

PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def capture(hwnd):
    """BGR image (numpy array) of the window's client area, or None if it failed."""
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)) or rect.right <= 0 or rect.bottom <= 0:
        return None
    width, height = rect.right, rect.bottom
    window_dc = _user32.GetDC(hwnd)
    memory_dc = _gdi32.CreateCompatibleDC(window_dc)
    bitmap = _gdi32.CreateCompatibleBitmap(window_dc, width, height)
    try:
        _gdi32.SelectObject(memory_dc, bitmap)
        if not _user32.PrintWindow(hwnd, memory_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            return None
        header = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not _gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
            return None
        return np.frombuffer(buffer, np.uint8).reshape(height, width, 4)[:, :, :3].copy()
    finally:
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(memory_dc)
        _user32.ReleaseDC(hwnd, window_dc)
