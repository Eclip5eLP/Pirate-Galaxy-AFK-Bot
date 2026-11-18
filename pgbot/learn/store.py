"""Where learned data lives: %LOCALAPPDATA%\\pgbot\\learned\\.

- shared.json: what does not depend on the ship (enemy behaviour, drops, routes, places).
- ship_<key>.json: per ship type (combat estimates, situations, drones, skills, last configuration).
- switches.json: the Learned tab's switches (per area, and per entry: enemies, planets, drones).
Files are written atomically (temp file + rename); a broken or missing file starts that part fresh.
"""
import hashlib
import json
import os
import pathlib
import threading
import time

# PGBOT_LEARN_DIR: checks point it at a temp folder so they never touch the real learned data.
LEARN_DIR = pathlib.Path(os.environ.get("PGBOT_LEARN_DIR") or
                         pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "learned")

AREAS = {
    # key: (label, description) shown in the Learned tab
    "fight_risk": ("Fights", "Skips fights it expects to lose too much hull in. Can also fight an enemy from the "
                             "danger list if this ship handles it well."),
    "squads": ("Squads", "Learns which enemies fly together as squads on each planet, so it keeps away from them "
                         "also while they stand still."),
    "pulls": ("Pulling", "Decides how many enemies to take on at once from past fights, instead of the "
                         "Max enemies to pull setting."),
    "retreat": ("Retreating", "Keeps fighting what it can still win and retreats from fights it would lose, "
                              "counting what each enemy has left and who is about to join. Always retreats below "
                              "12% hull."),
    "routes": ("Routes", "Flies around places where the ship keeps getting stuck, but still uses them if there "
                         "is no other way."),
    "patrol": ("Patrolling", "Goes back sooner to places where enemies show up often."),
    "bp_hunt": ("Energy from blueprints", "When energy runs low, also hunts enemies that often drop blueprints you "
                                          "have already completed, since those give energy."),
    "value": ("Loot", "Prefers enemies that give more cryonite and XP when the choice is otherwise equal."),
    "drones": ("Drones", "Stops turning on drones that cost more to recharge than they give back."),
    "skills": ("Skills", "Uses the attack skills that do the most damage for their energy first, shields when "
                         "a lot of damage is coming in, turrets, drones and traps where they pay off, buffs in "
                         "fights long enough to use them, and doesn't waste the repair."),
}


def _key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


class Store:
    def __init__(self, directory=LEARN_DIR):
        self.dir = pathlib.Path(directory)
        self.lock = threading.RLock()
        self.shared = self._load("shared.json")
        self.switches = self._load("switches.json")
        self.ship_key = None
        self.ship = {}
        self.dirty = set()
        self.last_save = time.monotonic()
        for area in AREAS:
            self.switches.setdefault(area, True)
        self.switches.setdefault("disabled", {})
        for part in ("enemies", "routes", "places", "behaviour", "drops", "blueprints", "squads"):
            self.shared.setdefault(part, {})
        if self.shared.get("squads_version") != 2:
            # Squads learned so far rated ordinary Vénar enemies (Assassins, Snipers, ...) as squads: cleared (v1: before the
            # site-route rule; v2: the per-type learning was switched off, user 2026-11-15).
            self.shared["squads"] = {}
            self.shared["squads_version"] = 2
        if self.shared.get("drops_version") != 2:
            # Before version 2 drops were never linked to enemies (they have no unit id): kills only, which made
            # every per-kill value too low. Start the loot part fresh once.
            self.shared["drops"], self.shared["blueprints"] = {}, {}
            self.shared["drops_version"] = 2
            self.dirty.add("shared")

    def _load(self, name):
        try:
            data = json.loads((self.dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        temp = self.dir / (name + ".tmp")
        temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, self.dir / name)

    def use_ship(self, ship_type):
        """Switch to a ship's file (saving the previous one)."""
        if ship_type == self.ship_key or not ship_type:
            return
        with self.lock:
            if self.ship_key is not None:
                self.save(force=True)
            self.ship_key = ship_type
            self.ship = self._load(f"ship_{_key(ship_type)}.json")
            self.ship["ship"] = ship_type
            for part in ("enemies", "situations", "drones", "skills", "config"):
                self.ship.setdefault(part, {})
            self.ship.setdefault("repair", {"n": 0.0, "heal": 0.0})
            # Once (levels_version 1): attack skills are now learned per farming level. Data from before was almost
            # all from Gazica with the ship above the enemies ("high"), so it is kept as that level's.
            if self.ship.get("levels_version") != 1:
                for entry in self.ship["skills"].values():
                    if entry.get("n") and "by_level" not in entry:
                        entry["by_level"] = {"high": [entry["n"], entry.get("drop", 0.0)]}
                self.ship["levels_version"] = 1

    def ship_files(self):
        """[(ship type, data)] of every ship learned so far (for the Learned tab)."""
        result = []
        for path in sorted(self.dir.glob("ship_*.json"), key=lambda p: p.stat().st_mtime):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("ship"):
                result.append((data["ship"], data))
        return result

    def copy(self):
        """A detached copy for reading in another thread (the Learned tab); switches still go to this store."""
        with self.lock:
            other = Store.__new__(Store)
            other.dir, other.lock, other.dirty, other.last_save = self.dir, threading.RLock(), set(), time.monotonic()
            other.ship_key = self.ship_key
            other.shared = json.loads(json.dumps(self.shared))
            other.ship = json.loads(json.dumps(self.ship))
            other.switches = json.loads(json.dumps(self.switches))
            return other

    def mark(self, part):
        self.dirty.add(part)

    def save(self, force=False):
        """Writes changed parts; at most once a minute unless forced."""
        with self.lock:
            if not force and time.monotonic() - self.last_save < 60:
                return
            self.last_save = time.monotonic()
            try:
                if "shared" in self.dirty:
                    self._write("shared.json", self.shared)
                if "ship" in self.dirty and self.ship_key:
                    self._write(f"ship_{_key(self.ship_key)}.json", self.ship)
                if "switches" in self.dirty:
                    self._write("switches.json", self.switches)
                self.dirty.clear()
            except OSError:
                pass  # tried again at the next save

    # Switches ---------------------------------------------------------------------------------------------------

    def on(self, area, entry=None):
        if not self.switches.get(area, True):
            return False
        if entry is not None and entry in self.switches["disabled"].get(area, []):
            return False
        return True

    def set_switch(self, area, value, entry=None):
        with self.lock:
            if entry is None:
                self.switches[area] = bool(value)
            else:
                disabled = set(self.switches["disabled"].get(area, []))
                if value:
                    disabled.discard(entry)
                else:
                    disabled.add(entry)
                self.switches["disabled"][area] = sorted(disabled)
            self.mark("switches")
            self.save(force=True)

    def reset(self, area):
        """Forget one area (Learned tab: Reset)."""
        with self.lock:
            if area in ("fight_risk", "pulls", "retreat"):
                self.ship["enemies"], self.ship["situations"] = {}, {}
                self.shared["behaviour"] = {}
                self.mark("ship")
                self.mark("shared")
                for path in self.dir.glob("ship_*.json"):
                    if self.ship_key is None or path.name != f"ship_{_key(self.ship_key)}.json":
                        try:
                            data = json.loads(path.read_text(encoding="utf-8"))
                            data["enemies"], data["situations"] = {}, {}
                            self._write(path.name, data)
                        except (OSError, ValueError):
                            pass
            elif area == "routes":
                self.shared["routes"] = {}
                self.mark("shared")
            elif area == "squads":
                self.shared["squads"] = {}
                self.mark("shared")
            elif area == "patrol":
                self.shared["places"] = {}
                self.mark("shared")
            elif area in ("bp_hunt", "value"):
                self.shared["drops"], self.shared["blueprints"] = {}, {}
                self.mark("shared")
            elif area in ("drones", "skills"):
                self.ship[area] = {}
                self.mark("ship")
            self.save(force=True)
