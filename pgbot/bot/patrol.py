"""Patrol that follows the terrain: cover the open ground reachable from the farm spot.

The reachable area (flood fill on the collision map, within the farm radius) is split into blocks. Each block gets
one target point on open ground near its middle. The patrol always heads for the block not seen for the longest
time, the nearest one first among equals, so it sweeps the area instead of circling through walls.
"""
import math
import random

from ..game import assets

BLOCK_CELLS = 4        # block size in planning cells (600 units each): 2,400 world units
MIN_BLOCK_CELLS = 4    # ignore slivers of open ground smaller than this
SEEN_RADIUS = 2500     # a block counts as seen when the ship comes this close to its target point
WANDER_FORGET_S = 600  # wander: a block seen longer ago than this counts as new again
WANDER_NEAR, WANDER_FAR = 6000, 20000  # wander: next targets this far away
EDGE_MARGIN = 9000     # no patrol points this close to the map edge (Technatoria's landing pocket lies in a corner: the
                       # patrol hugged the border wall and stuck on it, 2026-11-19; open ground this close to the edge
                       # is 0-5 % of a map)


class Patrol:
    def __init__(self, nav, home, radius, wander=False):
        """wander: pick among the least recently seen blocks at random instead of nearest first."""
        self.nav = nav
        self.wander = wander
        self.prefer = None  # prefer(x, y) -> seconds, set by the bot (learned busy areas)
        self.random = random.Random()
        groups = {}
        for cell in nav.reachable(home, radius):
            groups.setdefault((cell[0] // BLOCK_CELLS, cell[1] // BLOCK_CELLS), []).append(cell)
        self.targets = {}
        for block, cells in groups.items():
            if len(cells) < MIN_BLOCK_CELLS:
                continue
            mx = sum(c[0] for c in cells) / len(cells)
            my = sum(c[1] for c in cells) / len(cells)
            # The open cell nearest the block's middle, preferring cells with open neighbours (away from walls).
            best = min(cells, key=lambda c: (c[0] - mx) ** 2 + (c[1] - my) ** 2 - 4 * _open_neighbours(nav, c))
            tx, ty = nav.to_world(best)
            if max(abs(tx), abs(ty)) > assets.MAP_OFFSET - EDGE_MARGIN:
                continue
            self.targets[block] = (tx, ty)
        self.seen = {block: 0.0 for block in self.targets}
        self.current = None
        self.heading = None  # wander: direction of travel, kept from target to target
        self.now = 0.0

    @classmethod
    def from_points(cls, nav, points, start=None):
        """Patrol a fixed list of world points (e.g. enemy spawn areas), each moved onto open ground. With `start`,
        points the ship cannot reach from there are left out (Mantis Hive: only 5,473 of 17,359 open cells connect
        to the landing area, and unreachable spawn areas were each tried and given up)."""
        patrol = cls.__new__(cls)
        patrol.nav, patrol.wander, patrol.random = nav, False, random.Random()
        patrol.targets = {}
        reach = nav.reachable(start, float("inf")) if start is not None else None
        patrol.unreachable = 0
        for index, (x, y) in enumerate(points):
            point = nav.open_point(x, y)
            if point is None:
                continue
            if reach is not None and nav.nearest_open(nav.to_cell(*point)) not in reach:
                patrol.unreachable += 1
                continue
            patrol.targets[index] = point
        patrol.seen = {block: 0.0 for block in patrol.targets}
        patrol.current = None
        patrol.heading = None
        patrol.now = 0.0
        return patrol

    def __len__(self):
        return len(self.targets)

    def update(self, x, y, now):
        """Mark blocks near the ship as seen."""
        self.now = now
        for block, (tx, ty) in self.targets.items():
            if math.hypot(tx - x, ty - y) < SEEN_RADIUS:
                self.seen[block] = now
        if self.current is not None and self.seen[self.current] == now:
            self.current = None

    def target(self, x, y, avoid=None, nearest=False, drop=None):
        """The point to fly to next: the least recently seen block, nearest first. avoid(x, y) -> True skips a
        block when choosing (dangerous area); None is returned when every block is avoided. drop(x, y) -> True
        abandons the current target (default: avoid); a target is otherwise kept until reached or given up."""
        drop = avoid if drop is None else drop
        if self.current is not None and drop is not None and drop(*self.targets[self.current]):
            self.current = None
        if self.current is None:
            blocks = [b for b in self.targets if avoid is None or not avoid(*self.targets[b])]
            if not blocks:
                return None

            def order(block):
                # prefer(x, y) -> seconds: busy areas (learned) count as seen that much longer ago.
                bonus = self.prefer(*self.targets[block]) if self.prefer else 0.0
                return self.seen[block] - bonus, math.hypot(self.targets[block][0] - x, self.targets[block][1] - y)
            if self.wander and not nearest:
                self.current = self._wander_pick(blocks, x, y)
            else:
                self.current = min(blocks, key=order)
            tx, ty = self.targets[self.current]
            length = math.hypot(tx - x, ty - y)
            if length > 1:
                self.heading = ((tx - x) / length, (ty - y) / length)
        return self.targets[self.current]

    def _wander_pick(self, blocks, x, y):
        """Keep going the same way: among blocks not seen lately and 6,000-20,000 away, the one most in line with
        the current direction of travel. Random picks anywhere on the map turned the ship around every few seconds
        (user, 2026-11-11)."""
        fresh = [b for b in blocks if self.seen[b] == 0.0 or self.now - self.seen[b] > WANDER_FORGET_S] or blocks

        def distance(b):
            return math.hypot(self.targets[b][0] - x, self.targets[b][1] - y)
        ring = [b for b in fresh if WANDER_NEAR <= distance(b) <= WANDER_FAR] or fresh
        if self.heading is None:
            return self.random.choice(ring)

        def alignment(b):
            tx, ty = self.targets[b]
            d = distance(b) or 1.0
            return ((tx - x) * self.heading[0] + (ty - y) * self.heading[1]) / d
        return max(ring, key=lambda b: (alignment(b) + self.random.uniform(0, 0.05)))

    def give_up(self, now):
        """The current target could not be reached: count it as seen and pick another."""
        if self.current is not None:
            self.seen[self.current] = now
            self.current = None


def _open_neighbours(nav, cell):
    cx, cy = cell
    count = 0
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < nav.blocked.shape[1] and 0 <= ny < nav.blocked.shape[0] and not nav.blocked[ny, nx]:
                count += 1
    return count
