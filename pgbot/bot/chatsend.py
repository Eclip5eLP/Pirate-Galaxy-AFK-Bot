"""Sending a chat message from the remote page (user, 2026-11-19), through the game's own chat box, like a player:
OS input only (clicks and typed characters), nothing sent over the network by the bot.

The quick chat sits above the action bar (seen on the server at 1280 x 720, 2026-11-19): a channel drop-down
(Alliance, Global, Clan, and private chats with other players) and right of it the text box; clicking the box lets
you type, Enter sends ("Quick Chat Interface: ENTER to open and send, arrow UP & DOWN to switch channels"), Esc
leaves the box. Typed Unicode works (tested on the server without sending: "test äé" in the box). The
channels are told apart by colour, in the drop-down and on its caption alike: Global white, Alliance blue (#5050FF),
Clan purple (#b750ff). The caller types only at a calm moment (not in a fight): with the box not focused, letters
would be the game's hotkeys.
"""
import time

import numpy as np

from ..input.capture import capture

CHAT_BOX = ("centre", 80, "bottom", -101)
CHANNEL_ARROW = ("centre", -59, "bottom", -101)
CHANNEL_CAPTION = (-142, -108, -65, -93)      # x0, y0 from the centre / bottom, x1, y1
CHANNEL_ROWS = (-140, -79, -50, 18, 4)        # x0, first row's y from the bottom, x1, row step, rows to look at
CHANNELS = ("Global", "Alliance", "Clan")
MAX_LEN = 200
ROW_HALF = 7


def colour_name(pixels):
    """Channel of a piece of text by its colour (BGR pixels of the area), None when there is no text."""
    flat = pixels.reshape(-1, 3).astype(int)
    text = flat[flat.max(axis=1) > 150]
    if len(text) < 20:
        return None
    b, g, r = text.mean(axis=0)
    if min(b, g, r) > 180:
        return "Global"
    if b > 180 and g < 130 and r < 120:
        return "Alliance"
    if b > 180 and g < 130 and 120 <= r <= 215:
        return "Clan"
    if r > 180 and b < 110 and 70 <= g <= 200:
        return "Private"  # a private chat: #f68812 (client class_1682)
    return "Other"


class ChatSender:
    def __init__(self, input, log=None):
        self.input = input
        self.log = log or (lambda text: None)

    def _point(self, spec):
        self.input.refresh()
        game = self.input.game
        horizontal, dx, vertical, dy = spec
        x = {"centre": game.width / 2, "right": game.width, "left": 0}[horizontal] + dx
        y = {"bottom": game.height, "top": 0, "middle": game.height / 2}[vertical] + dy
        return int(x), int(y)

    def _click(self, spec_or_point):
        x, y = self._point(spec_or_point) if isinstance(spec_or_point[0], str) else spec_or_point
        self.input.hover(x, y)
        time.sleep(0.2)
        self.input.click(x, y, "left", hold_s=0.1)

    def _screen(self):
        self.input.refresh()
        image = capture(self.input.game.hwnd)
        if image is None:
            raise RuntimeError("no screenshot of the game")
        return image

    def _caption(self, image):
        h, w = image.shape[:2]
        x0, y0, x1, y1 = CHANNEL_CAPTION
        return colour_name(image[h + y0:h + y1, w // 2 + x0:w // 2 + x1])

    def _pick_row(self, row):
        """A private chat: its row in the drop-down (the chat bar's channel list, read from memory, is in the same
        order), checked by the caption's colour afterwards. Several private chats share the colour, so the row comes
        from memory."""
        self._click(CHANNEL_ARROW)
        time.sleep(0.6)
        image = self._screen()
        h, w = image.shape[:2]
        x0, first, x1, step, _ = CHANNEL_ROWS
        y = h + first + row * step
        if y + ROW_HALF >= h:
            self._click(CHANNEL_ARROW)
            raise RuntimeError("that chat's row is below the window in the channel list")
        self._click((w // 2 + (x0 + x1) // 2, y))
        time.sleep(0.5)
        if self._caption(self._screen()) != "Private":
            raise RuntimeError("could not switch the chat to that private chat")

    def _pick_channel(self, channel):
        if self._caption(self._screen()) == channel:
            return
        self._click(CHANNEL_ARROW)
        time.sleep(0.6)
        image = self._screen()
        h, w = image.shape[:2]
        x0, first, x1, step, rows = CHANNEL_ROWS
        for row in range(rows):
            y = h + first + row * step
            if colour_name(image[y - ROW_HALF:y + ROW_HALF, w // 2 + x0:w // 2 + x1]) == channel:
                self._click((w // 2 + (x0 + x1) // 2, y))
                time.sleep(0.5)
                break
        else:
            self._click(CHANNEL_ARROW)  # close the list again
            raise RuntimeError(f"{channel} is not in the channel list")
        if self._caption(self._screen()) != channel:
            raise RuntimeError(f"could not switch the chat to {channel}")

    def send(self, channel, text, row=None):
        """Pick the channel, type the text into the chat box and press Enter. Raises RuntimeError on a failed step.
        A private chat ("Private: <player>") needs its row in the drop-down (from the chat reader)."""
        text = " ".join(str(text).split())[:MAX_LEN]
        if not text:
            raise RuntimeError("empty message")
        if channel.startswith("Private"):
            if row is None:
                raise RuntimeError("that private chat is not open in the game")
            self._pick_row(row)
        elif channel in CHANNELS:
            self._pick_channel(channel)
        else:
            raise RuntimeError(f"unknown channel {channel}")
        self._click(CHAT_BOX)
        time.sleep(0.4)
        try:
            self.input.type_text(text)
            time.sleep(0.3)
            self.input.key("ENTER")
            time.sleep(0.3)
        finally:
            # The box keeps the focus after Enter (the cursor stays, tested on the server 2026-11-19), so the bot's
            # next hotkeys would be typed into it; Esc takes the focus away (no menu opens).
            self.input.key("ESC")
        self.log(f"chat: typed {len(text)} characters into {channel}")
        return text
