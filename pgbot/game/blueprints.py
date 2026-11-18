"""Which blueprints the player has completed, read from the game's memory (read-only).

Found 2026-11-12 (docs/NOTES.md):
- Blueprint progress lives in a blueprint container (client class amj, a persistent object with a PropertyMap that
  the server fills). Per item template id it holds foundcount_<id> = parts found. Two containers are loaded; the
  player's is the one with entries for the ship's installed items (the other is presumably the clan's).
- An item template (any object with the field templateId) holds unlockcount = parts needed. Templates are server
  data in client memory, not in the game files.
- A blueprint drop carries the template id of its item in the property "item".
- Completed = parts found >= parts needed (the client's own test).

Neither the container nor the templates are reachable from anything the bot reads, so they are found by searching
the heap for the container's class pointer or the template id. That takes a few seconds, so it runs in a background
thread with its own reader; the bot asks completed(id) and gets True, False or None (not known yet).
"""
import json
import math
import os
import pathlib
import queue
import re

import numpy as np
import struct
import threading
import time

from . import assets, messages
from ..mem import java
from ..mem.jvm import JVM

CONTAINER_CLASS = "com/splitscreen/amj"
TEMPLATE_BASE = "com/splitscreen/hi"
CHUNK = 1 << 20
RESCAN_S = 600  # look for the container again this often (the server may replace it)
MAX_PARTS = 1000  # a parts count above this is no parts count
# The container in memory is a copy the client fetched from the server (getBlueprintMemory, a server call made when
# some panels are built); pickups never update it. Found 2026-11-17: it still said A110 Speed Drone 3/10 when the
# game said "collected (6/10)", and three "missing" blueprints were "Converted known blueprint". The game's pickup
# messages carry the live count, so they are read too and kept here between runs (counts only go up).
SEEN_FILE = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "bp_seen.json"
COLLECTED = re.compile(r"^Blueprint '(.+)' collected\s*\((\d+)\s*/\s*(\d+)\)", re.I)
KNOWN = re.compile(r"^Converted known blueprint '(.+)' into", re.I)
RETRY_S = 10  # after a failed read, try again this much later
HANGAR_SCAN_S = 12  # after rescan(): search every 2 s this long (the server's fresh copy arrives after the panel opens)


class BlueprintBook:
    def __init__(self, process, log=lambda text: None, owns_process=False):
        """owns_process: close the process handle when the worker ends (the bot gives the book its own handle)."""
        self.process = process
        self.owns_process = owns_process
        self.log = log
        self.container = None  # address of the player's blueprint container
        self.parts_needed = {}  # template id -> unlockcount (None: no template found)
        self.found = {}  # template id -> parts found, as last read
        self.installed = []  # template ids of the ship's installed items (to recognise the player's container)
        self._requests = queue.Queue()
        self._pending = set()
        self._stop = threading.Event()
        self._last_scan = -math.inf
        self.tracked = set()  # template ids whose parts found are kept up to date (BP Completion)
        self.container_seen = None  # monotonic time the player's container was last read
        self.templates = {}  # template id -> {"desc": "$orbitalstrike.86", "needed": 14} (catalog(), BP Completion)
        self.seen = self._load_seen()  # display name -> parts found per the game's pickup messages
        # The container is a copy the client fetched from the server: one each time the hangar CONFIGURATION panel
        # (class avw) is built, and older copies stay in memory (one from hours earlier was read all night, found
        # 2026-11-17). Which copy is newest cannot be told by reference, but counts only go up: every copy is read
        # and the highest count per blueprint kept.
        self.copies = []  # addresses of the player's container copies in memory
        self.scanned_at = None  # monotonic time of the last full search (the hangar read waits for one)
        self._scan_until = -math.inf  # search again every 2 s until then (entering a hangar)
        self._want_catalog = threading.Event()
        self.catalog_done = threading.Event()
        self._thread = threading.Thread(target=self._work, name="blueprints", daemon=True)

    # Called from the bot thread ---------------------------------------------------------------------------------

    def start(self, installed):
        self.installed = [t for t in installed if t is not None]
        self._thread.start()

    def stop(self):
        self._stop.set()

    def request_catalog(self):
        """Read every item template in memory (id, key, parts needed) in the worker; catalog_done is set when ready.
        Also keeps the parts found of all of them up to date from then on (BP Completion)."""
        self.catalog_done.clear()
        self._want_catalog.set()

    def rescan(self):
        """Look for container copies now and again every 2 s for HANGAR_SCAN_S (entering a hangar: the panel fetches
        a fresh copy from the server a moment after it opens)."""
        self._last_scan = -math.inf
        self._scan_until = time.monotonic() + HANGAR_SCAN_S

    def keep_scanning(self, seconds):
        """Search for container copies every 2 s for at least `seconds` more (waiting for the server's copy)."""
        self._scan_until = max(self._scan_until, time.monotonic() + seconds)

    def track(self, ids):
        """Keep the parts found of these template ids up to date (read whenever the container is in memory: in a
        hangar; the last values are kept otherwise)."""
        self.tracked.update(i for i in ids if i is not None)

    def progress(self, template_id):
        """(parts found, parts needed) for a catalogued template, or None."""
        entry = self.templates.get(template_id)
        if entry is None or not entry.get("needed"):
            return None
        return self._found(template_id), entry["needed"]

    def completed(self, template_id):
        """True if the player has completed this blueprint, False if not, None if not known (yet)."""
        if template_id is None:
            return None
        needed = self.parts_needed.get(template_id, "?")
        if needed == "?":
            if template_id not in self._pending:
                self._pending.add(template_id)
                self._requests.put(template_id)
            return None
        found = self._found(template_id)
        if needed is None or found is None:
            return None
        return found >= needed

    def found_now(self):
        """Parts found per template id, with the game's pickup messages applied (for planning)."""
        return {t: self._found(t) or 0 for t in set(self.found) | set(self.templates)}

    def _found(self, template_id):
        """Parts found: the container's copy, raised by the game's pickup messages since."""
        found = self.found.get(template_id)
        entry = self.templates.get(template_id)
        name = assets.display_name(entry["desc"]) if entry else None
        live = self.seen.get(name) if name else None
        if live is None:
            return found
        if live == "known":
            return max(found or 0, self.parts_needed.get(template_id) or (entry or {}).get("needed") or 0)
        return max(found or 0, live)

    @staticmethod
    def _load_seen():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _read_messages(self, jvm):
        """Pickup messages in the game's message log: live parts counts and completed ("known") blueprints."""
        changed = False
        for text in jvm.consistent(lambda: messages.read_log(jvm)):
            match = COLLECTED.match(text)
            if match:
                name, count, total = match.group(1), int(match.group(2)), int(match.group(3))
                old = self.seen.get(name)
                if count >= total > 0:  # the last part (10/10): complete from now on (user, 2026-11-17)
                    if old != "known":
                        self.seen[name], changed = "known", True
                    continue
                if old != "known" and (old is None or count > old):
                    self.seen[name], changed = count, True
                continue
            match = KNOWN.match(text)
            if match and self.seen.get(match.group(1)) != "known":
                self.seen[match.group(1)], changed = "known", True
        if changed:
            try:
                SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                SEEN_FILE.write_text(json.dumps(self.seen, indent=1, sort_keys=True), encoding="utf-8")
            except OSError:
                pass

    # Worker thread (own reader; reads only) ---------------------------------------------------------------------

    def _work(self):
        """Never dies on a failed read: the game changes its memory while it is read (2026-11-12: the class list was
        read while classes were being loaded, a null pointer ended the thread, and the book stayed empty for the
        whole run). Failed steps are retried a little later."""
        try:
            self._loop()
        finally:
            if self.owns_process:
                self.process.close()

    def _loop(self):
        jvm = None
        while not self._stop.is_set():
            try:
                if jvm is None:
                    jvm = JVM(self.process)
                since = time.monotonic() - self._last_scan
                if self.container is None or since > RESCAN_S or (time.monotonic() < self._scan_until and since > 2.0):
                    self._find_container(jvm)
            except Exception as error:
                self.log(f"blueprint book: search failed, retrying ({error})")
                self._last_scan = time.monotonic() - RESCAN_S + RETRY_S  # try again in RETRY_S
                self._stop.wait(RETRY_S)
                continue
            if self._want_catalog.is_set():
                try:
                    self._read_catalog(jvm)
                    self._find_container(jvm)  # again with the templates known: the freshest copy has most parts
                    self._refresh_found(jvm, list(self.templates))
                    self._want_catalog.clear()
                    self.catalog_done.set()
                except Exception as error:
                    self.log(f"blueprint book: catalog failed, retrying ({error})")
                    self._stop.wait(RETRY_S)
                continue
            try:
                template_id = self._requests.get(timeout=2.0)
            except queue.Empty:
                try:
                    self._refresh_found(jvm)
                except Exception:
                    pass
                try:
                    self._read_messages(jvm)
                except Exception:
                    pass
                continue
            try:
                self.parts_needed[template_id] = self._parts_needed(jvm, template_id)
                self._refresh_found(jvm, [template_id])
                self._pending.discard(template_id)
            except Exception as error:
                # Asked again on the next drop of this type.
                self.log(f"blueprint book: template {template_id} failed, will retry ({error})")
                self.parts_needed.pop(template_id, None)
                self._pending.discard(template_id)

    def _chunks(self, jvm):
        """(address, bytes) over the readable heap. A 1 MB read fails if any page in it is not readable, and skipping
        the whole MB lost most objects (399 of 512 MB "unreadable" on 2026-11-16: the blueprint container and most
        item templates seemed to come and go). Failed chunks are read again in 64 KB, then 4 KB pieces."""
        address = jvm.heap_start - jvm.heap_start % 8
        while address < jvm.heap_end and not self._stop.is_set():
            size = min(CHUNK, jvm.heap_end - address)
            size -= size % 8
            yield from self._read_pieces(address, size, (CHUNK, 1 << 16, 1 << 12))
            address += size

    def _read_pieces(self, address, size, sizes):
        try:
            yield address, self.process.read(address, size)
            return
        except Exception:
            pass
        smaller = [s for s in sizes if s < size]
        if not smaller:
            return
        step = smaller[0]
        for piece in range(address, address + size, step):
            yield from self._read_pieces(piece, min(step, address + size - piece), smaller)

    def _scan(self, jvm, pattern, offset):
        """Addresses of objects whose 4 bytes at `offset` equal `pattern`, over the readable heap."""
        hits = []
        for address, data in self._chunks(jvm):
            start = 0
            while True:
                i = data.find(pattern, start)
                if i < 0:
                    break
                oop = address + i - offset
                if oop % 8 == 0:
                    hits.append(oop)
                start = i + 1
        return hits

    def _find_container(self, jvm):
        info = jvm.find_class(CONTAINER_CLASS)
        if info is None:
            self._last_scan = time.monotonic()
            return
        copies, best, best_parts = [], None, -1
        for oop in self._scan(jvm, struct.pack("<I", info.address), jvm._klass_ptr_at):
            try:
                if jvm.class_name(oop) != CONTAINER_CLASS:
                    continue
                props = java.Properties(jvm, jvm.get(oop, "properties"))
            except Exception:
                continue
            if self.installed and not any(f"foundcount_{t}" in props for t in self.installed):
                continue  # not the player's (a clan's)
            copies.append(oop)
            parts = sum(int(props.get(f"foundcount_{t}") or 0) for t in self.templates)
            if parts > best_parts:
                best, best_parts = oop, parts
        if copies and (best != self.container or len(copies) != len(self.copies)):
            self.log(f"blueprint book: {len(copies)} container cop{'y' if len(copies) == 1 else 'ies'}, newest at "
                     f"{best:#x} ({best_parts} parts)")
        self.copies, self.container = copies, best
        self._refresh_found(jvm, list(set(self.templates) | set(self.parts_needed) | self.tracked))
        self._last_scan = self.scanned_at = time.monotonic()

    def _parts_needed(self, jvm, template_id):
        base = jvm.find_class(TEMPLATE_BASE)
        offset = jvm.field_of(base, "templateId").offset
        for oop in self._scan(jvm, struct.pack("<i", template_id), offset):
            try:
                if not jvm.has_field(oop, "templateId") or jvm.get(oop, "templateId") != template_id:
                    continue
                if not jvm.has_field(oop, "properties"):
                    continue
                needed = java.Properties(jvm, jvm.get(oop, "properties")).get("unlockcount")
            except Exception:
                continue
            if needed:
                return int(needed)
        return None

    def _template_classes(self, jvm):
        """Klass addresses of the item template class and all its subclasses (238 in 2026-11)."""
        base = jvm.find_class(TEMPLATE_BASE)
        jvm._refresh_class_names()
        found = {base.address}
        for address in list(jvm._by_name.values()):
            chain, current = [], address
            try:
                for _ in range(16):
                    if current in found:
                        found.update(chain)
                        break
                    chain.append(current)
                    info = jvm.klass(current)
                    if not info.super_address:
                        break
                    current = info.super_address
            except Exception:
                continue
        return found

    def _read_catalog(self, jvm):
        """One pass over the heap: every object whose class is an item template class (klass pointer test on each
        8-byte aligned slot with numpy), then its templateId, key ("desc") and parts needed ("unlockcount")."""
        classes = np.array(sorted(self._template_classes(jvm)), dtype=np.uint32)
        offset = jvm._klass_ptr_at
        templates = {}
        for address, data in self._chunks(jvm):
            words = np.frombuffer(data, dtype=np.uint32)[offset // 4::2]
            for index in np.nonzero(np.isin(words, classes))[0]:
                oop = address + int(index) * 8
                try:
                    template_id = jvm.get(oop, "templateId")
                    props = java.Properties(jvm, jvm.get(oop, "properties"))
                    desc, needed = props.get("desc"), props.get("unlockcount")
                    if not isinstance(desc, str):
                        # Color pattern templates (class abv) keep their key under "name" ("$color.30" = Ink) and
                        # have no unlockcount (found 2026-11-17: the catalogue skipped them, Ink unidentified).
                        name = props.get("name")
                        desc = name if isinstance(name, str) and name.startswith("$") else None
                        if desc and not isinstance(needed, (int, float)):
                            needed = 1  # client (class_1620): unlockcount <= 0 or unset -> 1 part
                except Exception:
                    continue
                if not isinstance(template_id, int) or not isinstance(desc, str):
                    continue
                entry = templates.setdefault(template_id, {"desc": desc, "needed": None})
                if isinstance(needed, (int, float)) and 0 < needed <= MAX_PARTS and not entry["needed"]:
                    entry["needed"] = int(needed)  # some templates hold a long timestamp-like value there
        self.templates = templates
        for template_id, entry in templates.items():
            if entry["needed"]:
                self.parts_needed[template_id] = entry["needed"]
        self._refresh_found(jvm, list(templates))
        self.log(f"blueprint book: {len(templates)} item templates read "
                 f"({sum(1 for e in templates.values() if e['needed'])} with a parts count)")

    def _refresh_found(self, jvm, ids=None):
        """Parts found from every copy in memory, the highest per blueprint (counts only go up)."""
        maps = []
        for oop in list(self.copies):
            try:
                if jvm.class_name(oop) == CONTAINER_CLASS:
                    maps.append(java.Properties(jvm, jvm.get(oop, "properties")))
                    continue
            except Exception:
                pass
            self.copies.remove(oop)  # collected or moved by the garbage collector: found again by the next search
        if not maps:
            self.container = None
            return
        if self.container not in self.copies:
            self.container = self.copies[0]
        for template_id in (ids if ids is not None else list(self.parts_needed) + list(self.tracked)):
            value = max(int(m.get(f"foundcount_{template_id}") or 0) for m in maps)
            self.found[template_id] = max(self.found.get(template_id) or 0, value)
        self.container_seen = time.monotonic()
