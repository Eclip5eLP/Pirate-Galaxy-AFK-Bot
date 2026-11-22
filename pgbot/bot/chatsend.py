"""Sending a chat message from the remote page (user, 2026-11-19), through the game's own chat box, like a player:
OS input only (clicks and keys), nothing sent over the network by the bot.

The quick chat sits above the action bar (server, 1280 x 720): a channel drop-down and right of it the text box.
Enter focuses the box when nothing has the focus and sends when it holds text; Esc leaves it.

The channel is chosen and checked in memory (user, 2026-11-21: "base it on memory"). The chat object
(com/splitscreen/aob, decompiled class_1114) keeps the drop-down's entries in aWl and the shown one's index in
selectedChat. With the input focused, the Up key selects selectedChat - 1 and Down selectedChat + 1, both wrapping
(key handler of "GGChatBarInput": key codes 38 / 40). Switching the channel keeps the typed text. So the bot types
first, presses Up / Down until selectedChat is the wanted entry (read after every press), then Enter. Before, the
channel was found by the colour of the drop-down's rows: the drop-down is see-through, and over Mars' red sky no row
matched ("Global is not in the channel list", 2026-11-21 00:36).

Read from the screen is only whether the typed text reached the box: that text lives in the native MyGUI widget, not
in Java memory. Typing first also proves the box has the focus before an arrow key is pressed (unfocused, arrows and
letters would be the game's keys). The caller types only at a calm moment (not in a fight).
"""
import time

from ..input.capture import capture

CHAT_BOX = ("centre", 80, "bottom", -101)
CHANNELS = ("Global", "Alliance", "Clan")
MAX_LEN = 200
TEXT_SEEN_MIN = 8  # typed text counts as in the chat box with this many more text pixels than before (one letter ~12)
KEY_WAIT_S = 0.3   # after an arrow key, before selectedChat is read again


class ChatSender:
    def __init__(self, input, bar, log=None):
        """bar: callable -> (channels, selected index) read from memory (game.chat.bar_state): channels as
        [{"row", "channel", "name"}] in the drop-down's order."""
        self.input = input
        self.bar = bar
        self.log = log or (lambda text: None)

    def _point(self, spec):
        self.input.refresh()
        game = self.input.game
        horizontal, dx, vertical, dy = spec
        x = {"centre": game.width / 2, "right": game.width, "left": 0}[horizontal] + dx
        y = {"bottom": game.height, "top": 0, "middle": game.height / 2}[vertical] + dy
        return int(x), int(y)

    def _click(self, spec):
        x, y = self._point(spec)
        self.input.hover(x, y)
        time.sleep(0.2)
        self.input.click(x, y, "left", hold_s=0.1)

    def _screen(self):
        self.input.refresh()
        image = capture(self.input.game.hwnd)
        if image is None:
            raise RuntimeError("no screenshot of the game")
        return image

    @staticmethod
    def _text_pixels(image):
        """Pixels of typed (light blue) text in the chat box: 12 for the empty box, 48 with "abc" (server, 1280 x 720,
        2026-11-20)."""
        h, w = image.shape[:2]
        area = image[h - 112:h - 92, w // 2 - 40:w // 2 + 300].astype(int)
        b, g, r = area[..., 0], area[..., 1], area[..., 2]
        return int(((b > 150) & (b > r + 50) & (g > 90)).sum())

    def _save_shot(self, step):
        """The screen at a step of a send, as logs/chat-<step>.png (the last send only)."""
        try:
            import cv2
            import pathlib
            folder = pathlib.Path(__file__).resolve().parent.parent.parent / "logs"
            folder.mkdir(exist_ok=True)
            cv2.imwrite(str(folder / f"chat-{step}.png"), self._screen())
        except Exception as error:
            self.log(f"chat: screenshot '{step}' failed ({error})")

    def _target(self, channel):
        """Index of the channel in the drop-down (memory)."""
        channels, _ = self.bar()
        names = [c["channel"] for c in channels]
        if channel not in names:
            raise RuntimeError(f"{channel} is not open in the game (chat bar: {', '.join(names) or 'empty'})")
        return names.index(channel), len(names)

    def _switch(self, target, count):
        """Up / Down until selectedChat is the target; True when it is (memory)."""
        for _ in range(count + 1):
            _, selected = self.bar()
            if selected == target:
                return True
            down = (target - selected) % count
            self.input.key("DOWN" if down <= count - down else "UP")
            time.sleep(KEY_WAIT_S)
        return self.bar()[1] == target

    def _clear(self, text):
        """Erase what was typed (a send that stops halfway must not leave the text for a later Enter)."""
        for _ in range(len(text) + 2):
            self.input.key("BACK", hold_s=0.02)

    def send(self, channel, text):
        """Type the text, switch the chat bar to the channel ("Global", "Alliance", "Clan" or "Private: <player>")
        and press Enter. Raises RuntimeError on a failed step (nothing sent then)."""
        text = " ".join(str(text).split())[:MAX_LEN]
        if not text:
            raise RuntimeError("empty message")
        if not (channel in CHANNELS or channel.startswith("Private: ")):
            raise RuntimeError(f"unknown channel {channel}")
        target, count = self._target(channel)
        self._click(CHAT_BOX)
        time.sleep(0.4)
        typed = False
        try:
            # The click alone did not always give the box the focus (2026-11-20 15:06 on Mars: the text went nowhere,
            # the Enter after it only opened the empty box). Enter focuses the box when nothing has the focus (game:
            # "ENTER to open and send"); on a focused empty box it does nothing.
            for attempt in range(2):
                self.input.key("ENTER")
                time.sleep(0.4)
                before = self._text_pixels(self._screen())
                self.input.type_text(text)
                time.sleep(0.4)
                if self._text_pixels(self._screen()) - before >= TEXT_SEEN_MIN:
                    typed = True
                    break
                self.log(f"chat: the text did not show in the chat box (try {attempt + 1})")
            if not typed:
                self._save_shot("missing")
                raise RuntimeError("the text did not reach the chat box; nothing sent")
            if not self._switch(target, count):
                shown = self.bar()[1]
                names = [c["channel"] for c in self.bar()[0]]
                self._clear(text)
                typed = False
                raise RuntimeError(f"could not switch the chat to {channel} (memory shows "
                                   f"{names[shown] if 0 <= shown < len(names) else shown}); nothing sent")
            self._save_shot("typed")
            self.input.key("ENTER")
            typed = False
            time.sleep(0.3)
            self._save_shot("entered")
        finally:
            if typed:
                self._clear(text)  # an error between typing and Enter
            # The box keeps the focus after Enter (the cursor stays, tested on the server 2026-11-19), so the bot's
            # next hotkeys would be typed into it; Esc takes the focus away (no menu opens).
            self.input.key("ESC")
        self.log(f"chat: typed {len(text)} characters into {channel} (chat bar entry {target + 1} of {count}, "
                 f"checked in memory)")
        return text
