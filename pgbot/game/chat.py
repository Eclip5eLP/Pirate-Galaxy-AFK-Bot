"""Read the in-game quick chat in its display order (read-only, from a separate process like every other read).

Found 2026-11-19 (docs/NOTES.md, decompiled client class_1114 "aob" / class_651 "byte"):
- The HUD's chat object (com/splitscreen/aob) has the quick chat panel in field aWi (class
  com/splitscreen/client/tools/chat/byte). Its ArrayList AC holds the shown lines, oldest first, exactly as on screen
  (new lines are appended, expired ones removed from the front). Each entry (byte$if) has the name of its MyGUI Edit
  widget (AU), a timer (AS, com/splitscreen/py, AtomicLong Xa = game clock ms + 300,000 when the line came in;
  reset to +500 ms while it fades out, flag AT) and its height.
- The client caches widgets by name hash: component com/splitscreen/switch -> as (instanceof) -> cE (j) -> dT
  (SoftReference) -> referent (TIntObjectHashMap: String.hashCode -> org/mygui/Edit). The Edit's int field dnf is the
  native MyGUI widget, and its text (UTF-16) is at [[[widget + 0x8] + 0x110] + 0x64] (same for all 9 lines checked).
- Line text: optional channel tag ("#b750ff[clan] "), optional sender place ("#888888[Gazica] ", only when the sender
  is in another zone, redrawn as they move), then "#DDDDFFsender: #FFFFFFmessage". No channel tag: Global.
- The heap holds several old chat objects (one per HUD); the live one is the one whose widget names are all in the
  live cache. It is looked up by a heap scan when the cached one stops matching.
Earlier attempts (UTF-8 copies of the lines found by their format) had no order: stale copies, no list.
"""
import re
import struct
import threading
import time
import collections

from ..mem import java
from ..mem.jvm import JVM
from ..mem.process import Process

CHAT_CLASS = "com/splitscreen/aob"
SCAN_S = 3.0           # how often the chat list is read
RESCAN_S = 30.0        # at most this often the heap is searched for the live chat object
KEEP = 60              # lines kept
LIFETIME_MS = 300_000  # quick chat lines stay this long (the timer is arrival + this)
TEXT_CHAIN = (0x8, 0x110, 0x64)
CHANNEL_NAMES = {"clan": "Clan", "alliance": "Alliance", "global": "Global"}
# The chat bar's channel list (aob.aWl, in the drop-down's order; aob.selectedChat is the shown one), by class:
# qq Alliance (#5050FF), yr Global, abq Clan (#b750ff), abe a private chat (#f68812), titled with the other player's
# name in properties "chat" -> properties "title" (decompiled class_1693, class_1499, class_1706, class_1682).
CHANNEL_CLASSES = {"com/splitscreen/qq": "Alliance", "com/splitscreen/yr": "Global", "com/splitscreen/abq": "Clan",
                   "com/splitscreen/abe": "Private"}
TAG = re.compile(r"#([0-9A-Fa-f]{6}) ?\[([^\]]{1,60})\] ?")
BODY = re.compile(r"#[0-9A-Fa-f]{6}([^:#]{1,40}?): ?#[0-9A-Fa-f]{6}(.*)", re.S)
COLOUR = re.compile(r"#[0-9A-Fa-f]{6}")


def parse(raw):
    """{"channel", "where", "sender", "text"} of a quick chat line's display text."""
    rest, channel, where = raw, None, None
    while True:
        m = TAG.match(rest)
        if not m:
            break
        if m.group(1).upper() == "888888":
            where = m.group(2)
        elif channel is None:
            channel = m.group(2)
        rest = rest[m.end():]
    body = BODY.match(rest)
    if body:
        sender, text = body.group(1).strip(), body.group(2)
    else:
        sender, text = "", COLOUR.sub("", rest)
    if channel is None:
        name = "Global"
    else:
        name = CHANNEL_NAMES.get(channel.lower(), "Private")
    return {"channel": name, "with": channel if name == "Private" else None, "where": where, "sender": sender,
            "text": text.replace("##", "#").strip()[:300]}


def _key(line):
    return line["channel"], line["sender"], line["text"]


class ChatReader:
    """Reads the quick chat list. update() may scan the heap (a few seconds): call it from a worker thread."""

    def __init__(self, process):
        self.process = process
        self.jvm = JVM(process)
        self.chat = None          # address of the live chat object
        self.scanned_at = -1e9
        self.seen = {}            # (widget name, text) -> first seen (time.time())
        self.lines = collections.deque(maxlen=KEEP)

    def _widgets(self):
        """{name hash: org/mygui/Edit} of the live widget cache."""
        from .state import GameReader
        jvm = self.jvm
        components = GameReader(jvm)._components(jvm.get_static("com/splitscreen/atp", "cL"))
        holder = jvm.get(jvm.get(components["com/splitscreen/switch"], "as"), "cE")
        ref = jvm.get(holder, "dT")
        referent = jvm.get(ref, "referent") if ref else 0
        return java.tint_object_map(jvm, referent) if referent else {}

    def _entries(self, chat, cache):
        """[(name, timer ms, fading, Edit)] of a chat object's quick chat list, or None if it is not the live one."""
        jvm = self.jvm
        if jvm.class_name(chat) != CHAT_CLASS:
            return None
        out = []
        for entry in java.array_list(jvm, jvm.get(jvm.get(chat, "aWi"), "AC")):
            name = java.string(jvm, jvm.get(entry, "AU"))
            edit = cache.get(java.java_hash(name)) if name else None
            if not edit:
                return None
            timer = jvm.get(jvm.get(jvm.get(entry, "AS"), "Xa"), "value")
            out.append((name, timer, bool(jvm.get(entry, "AT")), edit))
        return out

    def _find_chat(self, cache):
        from .blueprints import BlueprintBook
        jvm = self.jvm
        self.scanned_at = time.monotonic()
        info = jvm.find_class(CHAT_CLASS)
        if info is None:
            return None
        best, best_t = None, -1
        for oop in BlueprintBook(self.process)._scan(jvm, struct.pack("<I", info.address), jvm._klass_ptr_at):
            try:
                entries = self._entries(oop, cache)
            except Exception:
                continue
            if entries:
                newest = max(t for _, t, _, _ in entries)
                if newest > best_t:
                    best, best_t = oop, newest
        return best

    def _text(self, edit):
        address = self.jvm.get(edit, "dnf")
        for offset in TEXT_CHAIN:
            address = struct.unpack("<I", self.process.read(address + offset, 4))[0]
            if not address:
                return None
        data = self.process.read(address, 1200)
        end = 0
        while end + 1 < len(data) and data[end:end + 2] != b"\0\0":
            end += 2
        return data[:end].decode("utf-16-le", "replace")

    def channels(self):
        """[{"row", "channel", "name"}] of the chat bar's drop-down, in its order: "Global", "Alliance", "Clan", or
        "Private: <player>" for a private chat (name: the player). [] before the chat object is found."""
        jvm = self.jvm
        if self.chat is None:
            return []
        out = []
        for row, oop in enumerate(java.array_list(jvm, jvm.get(self.chat, "aWl"))):
            kind = CHANNEL_CLASSES.get(jvm.class_name(oop))
            name = None
            if kind == "Private":
                try:
                    inner = java.Properties(jvm, jvm.get(oop, "properties")).raw("chat")
                    name = java.Properties(jvm, jvm.get(inner, "properties")).get("title") if inner else None
                    name = java.string(jvm, name) if isinstance(name, int) else name
                except Exception:
                    name = None
            if kind:
                out.append({"row": row, "channel": f"Private: {name}" if kind == "Private" else kind, "name": name})
        return out

    def update(self):
        """Read the list; new lines are appended to self.lines in display order. Returns them."""
        cache = self._widgets()
        entries = None
        if self.chat is not None:
            try:
                entries = self._entries(self.chat, cache)
            except Exception:
                entries = None
        if entries is None:
            if time.monotonic() - self.scanned_at < RESCAN_S:
                return []
            self.chat = self._find_chat(cache)
            if self.chat is None:
                return []
            entries = self._entries(self.chat, cache) or []
        now = time.time()
        shown = []
        for name, timer, fading, edit in entries:
            try:
                raw = self._text(edit)
            except Exception:
                continue
            # A fading or reused widget can point at freed memory (garbage text, 14:03 on the page): only real
            # chat lines count.
            if not raw or not raw.startswith("#") or not BODY.search(raw):
                continue
            line = parse(raw)
            line["t"] = round((timer - LIFETIME_MS) / 1000 if timer and not fading else now, 1)
            shown.append(line)
        # Each zone change builds a new HUD whose chat is refilled with the same history (new widgets, new timers,
        # the place tags redrawn): those lines came up again and again (14:03:19, 14:03:57, 14:05:15). The list on
        # screen is always some lines already known followed by new ones, so the new ones are what comes after the
        # longest overlap between the end of the known lines and the start of the list.
        known = [_key(line) for line in self.lines]
        keys = [_key(line) for line in shown]
        overlap = 0
        for size in range(min(len(keys), len(known)), 0, -1):
            if keys[:size] == known[-size:]:
                overlap = size
                break
        if overlap == 0 and known:
            recent = set(known)
            for i, key in enumerate(keys):  # a list that starts before the known end: skip the known part
                if key not in recent:
                    overlap = i
                    break
            else:
                overlap = len(keys)
        new = shown[overlap:]
        self.lines.extend(new)
        return new


class ChatWatcher:
    """Reads the chat every SCAN_S in its own daemon thread; recent() never blocks (for the remote page)."""

    def __init__(self, pid):
        self.pid = pid
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._recent = []
        self._channels = []
        self.errors = 0
        self.thread = threading.Thread(target=self._run, name="chat", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()

    def recent(self, count=30):
        with self._lock:
            return list(self._recent[-count:])

    def channels(self):
        with self._lock:
            return list(self._channels)

    def _run(self):
        try:
            process = Process(self.pid)
        except OSError:
            return
        try:
            reader = ChatReader(process)
            while not self._stop.is_set():
                try:
                    reader.update()
                    channels = reader.channels()
                    with self._lock:
                        self._recent = list(reader.lines)
                        self._channels = channels
                except Exception:
                    self.errors += 1
                self._stop.wait(SCAN_S)
        except Exception:
            self.errors += 1
        finally:
            process.close()
