"""Keep the display on while the bot runs.

The game stops drawing and stops taking input when Windows turns the display off (2026-11-15: display off at
03:01:19 after the 15 min idle timeout, the game's frame froze right after while its memory kept changing). The bot's
input is posted to the game window, which Windows does not count as user activity, so the idle timer runs out even
while the bot plays.

SetThreadExecutionState is a per-thread request, not a settings change: it holds only while the calling thread lives
and asks for it, and Windows drops it when the thread ends. The power plan is not touched.
"""
import ctypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
_kernel32.SetThreadExecutionState.restype = ctypes.c_uint32

ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_CONTINUOUS = 0x80000000


def keep_display_on():
    """Ask Windows to keep the display (and the PC) on for as long as this thread runs. True if accepted."""
    return _kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED) != 0


def allow_display_off():
    """Withdraw the request (call from the same thread)."""
    _kernel32.SetThreadExecutionState(ES_CONTINUOUS)
