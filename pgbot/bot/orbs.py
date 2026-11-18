"""Remember where energy orbs were seen on each planet, so the bot can go back to refill."""
import json
import math
import os
import pathlib

STORE = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "orb_spots.json"
MERGE_DISTANCE = 1500.0


class OrbMemory:
    def __init__(self, path=STORE):
        self.path = pathlib.Path(path)
        try:
            self.spots = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.spots = {}
        self._dirty = False

    def remember(self, planet, x, y):
        if not planet or x is None or y is None or math.isnan(x) or math.isnan(y):
            return
        spots = self.spots.setdefault(planet, [])
        for spot in spots:
            if math.hypot(spot[0] - x, spot[1] - y) < MERGE_DISTANCE:
                spot[2] += 1
                return
        spots.append([round(x), round(y), 1])
        self._dirty = True

    def nearest(self, planet, x, y):
        spots = self.spots.get(planet, [])
        if not spots:
            return None
        spot = min(spots, key=lambda s: math.hypot(s[0] - x, s[1] - y))
        return spot[0], spot[1]

    def save(self):
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.spots), encoding="utf-8")
        self._dirty = False
