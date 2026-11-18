"""Learned squad patrol loops, per planet.

A squad always flies the same patrol route and regroups when split (user, 2026-11-14). On Vénar one loops through the
landing pocket in front of both exits; judged only a few seconds ahead in a straight line, no way out ever looked safe
and the bot stayed in the pocket. Here each squad's centre is recorded once a second; to predict where it will be in t
seconds, the recorded moments where a squad was at about the same place, heading the same way, are looked up and
followed t seconds on. Saved in %LOCALAPPDATA%\\pgbot\\learned\\squads.json (read and written only by the bot).
"""
import json
import math
import os
import pathlib
import time

FILE = pathlib.Path(os.environ.get("PGBOT_LEARN_DIR") or
                    pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "learned") / "squads.json"
GROUP_R = 5000          # members this close together are one squad
SAMPLE_S = 1.0          # one sample per squad per second
MATCH_R = 2500          # a recorded moment matches when the squad was this close to where it is now ...
MATCH_COS = 0.6         # ... heading about the same way (when it moves)
MAX_SAMPLES = 2400      # per planet (about 40 min of one squad)
GAP_S = 3.0             # a longer gap in sightings starts a new track segment
SAVE_S = 60.0
MAX_MATCHES = 40       # recorded moments used per prediction (the latest)


class SquadTracks:
    def __init__(self, path=FILE, log=None):
        self.path = pathlib.Path(path)
        self.log = log or (lambda text: None)
        self.data = {}        # planet -> list of segments, each a list of [x, y, vx, vy]
        self.open = {}        # planet -> {squad slot: (last time, segment)}
        self.last_sample = 0.0
        self.saved = time.monotonic()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = {k: v for k, v in loaded.items() if isinstance(v, list)}
        except (OSError, ValueError):
            pass

    @staticmethod
    def groups(members):
        """Squads from members (x, y, vx, vy): (x, y, vx, vy) of each group's centre."""
        left = list(members)
        result = []
        while left:
            seed = left.pop(0)
            group = [seed] + [m for m in left if math.hypot(m[0] - seed[0], m[1] - seed[1]) < GROUP_R]
            left = [m for m in left if m not in group]
            n = len(group)
            result.append(tuple(sum(m[i] for m in group) / n for i in range(4)))
        return result

    def record(self, planet, members, now=None):
        """Once a second: add each squad's centre to the planet's track."""
        now = time.monotonic() if now is None else now
        if not planet or now - self.last_sample < SAMPLE_S:
            return
        self.last_sample = now
        segments = self.data.setdefault(planet, [])
        open_ = self.open.setdefault(planet, {})
        for key in [k for k, (seen, _) in open_.items() if now - seen > GAP_S]:
            del open_[key]
        for gx, gy, vx, vy in self.groups(members):
            # Continue the segment of the squad that was nearest a second ago (it moves at most ~1,000 per second).
            best = min(open_.items(), key=lambda kv: math.hypot(kv[1][1][-1][0] - gx, kv[1][1][-1][1] - gy),
                       default=None)
            if best is not None and math.hypot(best[1][1][-1][0] - gx, best[1][1][-1][1] - gy) < 3000:
                key, (_, segment) = best
            else:
                key, segment = len(segments), []
                segments.append(segment)
            segment.append([round(gx), round(gy), round(vx), round(vy)])
            open_[key] = (now, segment)
        total = sum(len(s) for s in segments)
        while total > MAX_SAMPLES and segments:
            dropped = segments.pop(0)
            total -= len(dropped)
            self.open[planet] = {}
        if now - self.saved > SAVE_S:
            self.save()

    def save(self):
        self.saved = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as error:
            self.log(f"squad tracks not saved: {error}")

    def matches(self, planet, squad, horizon):
        """Recorded moments where a squad was about here, heading the same way, with at least `horizon` seconds of
        track after them: [(segment, index, dx, dy)] (dx, dy: shift from the recorded squad to this one); the most
        recent MAX_MATCHES. Empty when not learned yet."""
        x, y, vx, vy = squad
        speed = math.hypot(vx, vy)
        steps = int(math.ceil(horizon / SAMPLE_S))
        found = []
        for segment in self.data.get(planet, []):
            for i in range(0, len(segment) - steps):
                sx, sy, svx, svy = segment[i]
                if abs(sx - x) > MATCH_R or abs(sy - y) > MATCH_R or math.hypot(sx - x, sy - y) > MATCH_R:
                    continue
                if speed > 100:
                    s_speed = math.hypot(svx, svy)
                    if s_speed < 100 or (svx * vx + svy * vy) / (s_speed * speed) < MATCH_COS:
                        continue
                found.append((segment, i, x - sx, y - sy))
        return found[-MAX_MATCHES:]

    @staticmethod
    def at(match, t):
        """Predicted squad centre t seconds after a match."""
        segment, i, dx, dy = match
        px, py = segment[min(len(segment) - 1, i + int(round(t / SAMPLE_S)))][:2]
        return px + dx, py + dy


class SiteRoutes:
    """A planet's squad patrol routes from the site data (coarse cells about 1,700 units apart, no order). A squad
    is followed along the cells in the direction it flies; at forks both ways are kept (it may take either)."""

    STEP = 1674.0          # site overlay cell spacing (world units)
    ON_ROUTE_R = 4000      # a squad this close to a route cell is taken to be on it
    MAX_FRONT = 12

    def __init__(self, cells):
        self.cells = [tuple(c) for c in cells]
        self.grid = {}
        if self.cells:
            self.ox, self.oy = self.cells[0][0] % self.STEP, self.cells[0][1] % self.STEP
            for c in self.cells:
                self.grid[self._key(*c)] = c

    def _key(self, x, y):
        return round((x - self.ox) / self.STEP), round((y - self.oy) / self.STEP)

    def _nearest(self, x, y):
        kx, ky = self._key(x, y)
        reach = int(self.ON_ROUTE_R / self.STEP) + 1
        best, best_d = None, self.ON_ROUTE_R
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                c = self.grid.get((kx + dx, ky + dy))
                if c is not None:
                    d = math.hypot(c[0] - x, c[1] - y)
                    if d < best_d:
                        best, best_d = (kx + dx, ky + dy), d
        return best

    def paths(self, squad, distance):
        """Polylines a squad (x, y, vx, vy) may follow for `distance` units along the routes; [] if not on a route."""
        if not self.grid:
            return []
        x, y, vx, vy = squad
        start = self._nearest(x, y)
        if start is None:
            return []
        speed = math.hypot(vx, vy)
        headings = [(vx / speed, vy / speed)] if speed > 100 else [(1, 0), (-1, 0), (0, 1), (0, -1)]
        result = []
        # Level by level, at most MAX_FRONT ways at once (one per cell), so every fork is followed.
        front = [([(x, y)], start, h, frozenset([start]), 0.0) for h in headings]
        while front:
            nxt = {}
            for points, key, (hx, hy), visited, length in front:
                if length >= distance:
                    result.append(points)
                    continue
                options = []
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nk = (key[0] + dx, key[1] + dy)
                        if (dx or dy) and nk in self.grid and nk not in visited:
                            d = math.hypot(dx, dy)
                            cos = (dx * hx + dy * hy) / d
                            if cos > 0.2:
                                options.append((cos, nk, (dx / d, dy / d)))
                if not options:
                    result.append(points)  # the route ends or turns back here: the squad stays about here
                    continue
                options.sort(reverse=True)
                for cos, nk, (dx, dy) in options[:3]:
                    c = self.grid[nk]
                    nh = (hx * 2 + dx, hy * 2 + dy)
                    n = math.hypot(*nh) or 1.0
                    state = (points + [c], nk, (nh[0] / n, nh[1] / n), visited | {nk},
                             length + math.hypot(c[0] - points[-1][0], c[1] - points[-1][1]))
                    if nk not in nxt or cos > nxt[nk][0]:
                        nxt[nk] = (cos, state)
            front = [state for _, state in sorted(nxt.values(), key=lambda v: -v[0])[:self.MAX_FRONT]]
        return result


def point_along(points, distance):
    """The point `distance` units along a polyline (its end if shorter)."""
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        leg = math.hypot(bx - ax, by - ay)
        if distance <= leg:
            f = distance / leg if leg else 1.0
            return ax + (bx - ax) * f, ay + (by - ay) * f
        distance -= leg
    return points[-1]
