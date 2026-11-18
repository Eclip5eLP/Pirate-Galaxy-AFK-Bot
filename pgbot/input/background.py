"""Input to the game while another window has focus (confirmed 2026-11-11).

- Mouse: posted mouse messages to the game canvas reach the client's AWT mouse listener without focus.
- Keys: AWT only delivers keys to an active window, so the game is told it is active first
  (WM_ACTIVATEAPP, WM_NCACTIVATE, WM_ACTIVATE to the frame, WM_SETFOCUS to the canvas). This does not change the
  real foreground window, so the user's own typing still goes to their app.
- The game is marked active ONCE per window and left so (not active / inactive around every key and click): the
  view jumped to the game's desktop during farming (user, 2026-11-14), each time 0.04 s after a key, i.e. while the
  game was being marked active; Java 8's AWT sometimes answers that activation by requesting the real foreground.
  Re-marked only when the window changes (zone changes re-create it) or after the game was really in front (Windows
  then deactivated it). Clicks and drags go the same way (a press on a window Java thinks unfocused also asks for the
  foreground). release() marks it inactive when the bot stops.
- The client hides and re-shows its frame when it applies the screen mode (class `ev`), which happens on zone
  changes. That creates new window handles, so handles are looked up again before every action.
"""
import ctypes
import time
from ctypes import wintypes

from . import keyboard, mouse, window

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = ctypes.c_ssize_t

WM_ACTIVATE, WM_SETFOCUS, WM_KILLFOCUS, WM_NCACTIVATE, WM_ACTIVATEAPP = 0x0006, 0x0007, 0x0008, 0x0086, 0x001C


class GameWindowGone(Exception):
    """The game process has no visible window right now (for example during a screen mode change)."""


class BackgroundInput:
    def __init__(self, pid):
        self.pid = pid
        self.last_action = ("none", 0.0)  # (kind, time): what was last sent (to explain a focus change)
        self.game = None
        self.canvas = None
        self._marked = None  # (frame, canvas) handles last marked active
        self.refresh()

    def refresh(self):
        game = window.game_window(self.pid)
        if game is None:
            raise GameWindowGone(f"no visible window for pid {self.pid}")
        self.game = game
        self.canvas = game.children[0] if game.children else game

    def mark_active(self):
        _user32.SendMessageW(self.game.hwnd, WM_ACTIVATEAPP, 1, 0)
        _user32.SendMessageW(self.game.hwnd, WM_NCACTIVATE, 1, 0)
        _user32.SendMessageW(self.game.hwnd, WM_ACTIVATE, 1, 0)
        _user32.SendMessageW(self.canvas.hwnd, WM_SETFOCUS, 0, 0)
        time.sleep(0.05)

    def mark_inactive(self):
        """Undo mark_active, so the game and Windows agree again that another window is active."""
        _user32.SendMessageW(self.canvas.hwnd, WM_KILLFOCUS, 0, 0)
        _user32.SendMessageW(self.game.hwnd, WM_ACTIVATE, 0, 0)
        _user32.SendMessageW(self.game.hwnd, WM_NCACTIVATE, 0, 0)
        _user32.SendMessageW(self.game.hwnd, WM_ACTIVATEAPP, 0, 0)

    def _ensure_active(self):
        """Mark the game active only when needed (see the module notes)."""
        if window.foreground_hwnd() == self.game.hwnd:
            self._marked = None  # really in front; once it is not, Windows has deactivated it: mark again then
            return
        if self._marked != (self.game.hwnd, self.canvas.hwnd):
            self.mark_active()
            self._marked = (self.game.hwnd, self.canvas.hwnd)

    def release(self):
        """The bot stops: let the game know it is not active (it is not the foreground window)."""
        try:
            self.refresh()
            if self._marked is not None and window.foreground_hwnd() != self.game.hwnd:
                self.mark_inactive()
        except Exception:
            pass
        self._marked = None

    def _as_active(self, kind, send):
        """Send input while the game believes it is active (see the module notes)."""
        self.refresh()
        self.last_action = (kind, time.monotonic())
        self._ensure_active()
        send()
        time.sleep(0.1)  # processed before anything else is sent

    def _offset(self):
        """What to add to game client coordinates for a posted mouse message. The game reads the posted position as
        if it were on the screen and subtracts the window's screen position: in a 1280 x 720 window at (1080, 371)
        a hover at client (741, 12) did nothing, one posted at (1821, 383) showed the Settings tooltip at (741, 12),
        and starmap rows were selected only so (2026-11-18). Maximized at (0, 0) both are the same, so it never
        showed before. Posted positions are therefore client coordinates + the frame's screen origin (the canvas
        fills the frame's client area)."""
        return self.game.left, self.game.top

    def click(self, x, y, button="left", hold_s=0.05):
        """Click at game client coordinates."""
        self.refresh()
        dx, dy = self._offset()
        self._as_active(f"{button} click", lambda: mouse.post_click(self.canvas.hwnd, x + dx, y + dy, button, hold_s))

    def drag(self, x0, y0, x1, y1, button="right", steps=12):
        """Drag with a button held, in game client coordinates (turns the camera)."""
        self.refresh()
        dx, dy = self._offset()
        self._as_active("drag", lambda: mouse.post_drag(self.canvas.hwnd, x0 + dx, y0 + dy, x1 + dx, y1 + dy,
                                                        button, steps))

    def hover(self, x, y):
        """Move the cursor over game client coordinates without clicking (HUD buttons react to hover first)."""
        self.refresh()
        dx, dy = self._offset()
        mouse.post_move(self.canvas.hwnd, x + dx, y + dy)

    def wheel(self, notches):
        """Mouse wheel over the middle of the game view (zooms the camera). Like keys, the wheel only reaches the
        game while it believes it is active (posted alone it did nothing; marked active, -1 zoomed 650 -> 828)."""
        self.refresh()
        self._as_active("wheel", lambda: mouse.post_wheel(self.canvas.hwnd, self.canvas.left + self.canvas.width // 2,
                                                          self.canvas.top + self.canvas.height // 2, notches))

    def key(self, key, hold_s=0.06):
        self._as_active(f"key {key}", lambda: keyboard.post_key(self.canvas.hwnd, key, hold_s))
