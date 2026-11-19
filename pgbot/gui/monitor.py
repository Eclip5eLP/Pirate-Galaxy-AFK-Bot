"""Background thread that reads the game and publishes snapshots and log events. Read-only."""
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field

from ..game import assets, location, messages
from ..game.state import GameReader, World
from ..mem.jvm import JVM, SnapshotFailed
from ..mem.process import MemoryReadError, Process

MONITOR_START_GRACE_S = 120  # read errors this soon after a game process appears are its start, not a loss



BLUEPRINT_NAME = re.compile(r"blueprint '(.+?)'", re.I)
RARE_PICKUP_S = 6.0  # a gold drop next to the ship vanished this recently: the next blueprint message is that one


def _gold(unit):
    mesh = unit.mesh or ""
    return unit.rare or ("blueprint" in mesh and "_rare" in mesh)


@dataclass
class LogEvent:
    level: str  # "pickup", "blueprint", "blueprint_rare", "death", "warning", "error", "info"
    text: str
    time: float = field(default_factory=time.time)


@dataclass
class Snapshot:
    connected: bool
    world: World | None
    read_ms: float = 0.0


class Monitor:
    def __init__(self, interval_s=0.25):
        self.interval_s = interval_s
        self.snapshots = queue.Queue(maxsize=4)
        self.events = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self.latest = Snapshot(False, None)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def log(self, level, text):
        self.events.put(LogEvent(level, text))

    def _publish(self, snapshot):
        self.latest = snapshot
        try:
            self.snapshots.put_nowait(snapshot)
        except queue.Full:
            try:
                self.snapshots.get_nowait()
            except queue.Empty:
                pass
            self.snapshots.put_nowait(snapshot)

    def _run(self):
        while not self._stop.is_set():
            pids = location.game_pids()
            if len(pids) != 1:
                self._publish(Snapshot(False, None))
                self._stop.wait(2.0)
                continue
            first_seen = self.__dict__.setdefault("pid_seen", {}).setdefault(pids[0], time.monotonic())
            try:
                with Process(pids[0]) as process:
                    self._watch(JVM(process))
            except (MemoryReadError, OSError) as error:
                # Not an error when the game closed (a restart, a crash the bot handles) or is still starting: the
                # client's memory is not readable for its first moments (2026-11-19: "Lost connection" after every
                # start by the bot itself).
                if pids[0] not in location.game_pids():
                    self.log("info", "The game closed")
                elif time.monotonic() - first_seen > MONITOR_START_GRACE_S:
                    self.log("error", f"Lost connection to the game: {error}")
                self._publish(Snapshot(False, None))
                self._stop.wait(2.0)

    def _watch(self, jvm):
        reader = GameReader(jvm)
        watcher = messages.MessageWatcher()
        was_dead = None
        failures = 0
        rare_names = set()  # display names of gold blueprint drops seen
        near_gold = {}  # unit id -> True for gold drops within 2,000 of the ship last read
        gold_taken = -math.inf  # when such a drop last disappeared
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                world = reader.read()
                texts = jvm.consistent(lambda: messages.read_log(jvm))
                failures = 0
            except SnapshotFailed as error:
                failures += 1
                if failures == 20:
                    self.log("warning", f"Game data unreadable for a while: {error}")
                self._stop.wait(self.interval_s)
                continue
            if world is not None:
                now_near = {}
                for unit in world.units:
                    if unit.is_item and _gold(unit):
                        if unit.item_name:
                            rare_names.add(assets.display_name(unit.item_name).lower())
                        if math.hypot(unit.x - world.x, unit.y - world.y) < 2000:
                            now_near[unit.unit_id] = True
                if any(uid not in now_near for uid in near_gold):
                    gold_taken = time.monotonic()
                near_gold = now_near
            for message in watcher.new_messages(texts):
                if message.kind == "blueprint":
                    # Normal blueprint names in blue, gold ones in gold (user, 2026-11-12).
                    match = BLUEPRINT_NAME.search(message.text)
                    name = match.group(1).lower() if match else ""
                    rare = name in rare_names or time.monotonic() - gold_taken < RARE_PICKUP_S
                    if rare:
                        gold_taken = -math.inf
                    self.log("blueprint_rare" if rare else "blueprint", message.text)
                elif message.kind:
                    self.log("pickup", message.text)
            if world is not None:
                if was_dead is False and world.dead:
                    self.log("death", "Ship was shot down")
                was_dead = world.dead
            self._publish(Snapshot(True, world, (time.perf_counter() - started) * 1000))
            self._stop.wait(max(0.0, self.interval_s - (time.perf_counter() - started)))
