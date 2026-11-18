"""Route planning on a planet's collision map (`<planet>.Collision.png`, black = open, white = blocked).

The map covers the same 153,600 world units as the minimap image (planet cfg ScaleX/ScaleZ), so world (x, y) maps
to pixel ((x + 76800) / 150, (y + 76800) / 150) on the 1024 px map. Checked 2026-11-11: the ship and all 29
nearby objects were on open cells.
"""
import heapq
import math

import cv2
import numpy as np

from . import assets

MOVES = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
         (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]  # (dx, dy, cost) of the 8 grid steps
GRID = 256                  # planning grid: 153600 / 256 = 600 world units per cell
CLEARANCE_PX = 2            # wall margin on the 1024 px map (about 300 world units)
PREFER_PX = 4               # cells within about 600 of a wall are open but cost extra (routes keep to the middle)
NEAR_WALL_COST = 1.5        # extra cost per near-wall cell


def planet_scale(prefix):
    """ScaleX / ScaleY / ScaleZ from the planet's <prefix>.cfg (landscape size and height range)."""
    data = assets.find_asset(f"{prefix}.cfg")
    values = {}
    if data is None:
        return values
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("Scale") and "=" in line:
            key, value = line.split("=", 1)
            try:
                values[key.strip()] = float(value.strip())
            except ValueError:
                pass
    return values


class NavMap:
    def __init__(self, planet_map_name):
        """planet_map_name: the planet's minimap file name, e.g. gemini3.Minimap.jpg."""
        prefix = planet_map_name.split(".")[0]
        data = assets.find_asset(f"{prefix}.Collision.png")
        if data is None:
            raise FileNotFoundError(f"no collision map for {prefix}")
        raw = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
        if raw.shape[0] % GRID or raw.shape[0] != raw.shape[1]:
            # Arkon's map is 1,025 px: the grid needs a multiple of 256 (route planning failed to load there).
            size = max(GRID, round(raw.shape[0] / GRID) * GRID)
            raw = cv2.resize(raw, (size, size), interpolation=cv2.INTER_NEAREST)
        self.size = raw.shape[0]
        self.blocked_fine = raw > 127
        factor = self.size // GRID

        def padded(px):
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (px * 2 + 1, px * 2 + 1))
            return (cv2.dilate(self.blocked_fine.astype(np.uint8), kernel) > 0).reshape(GRID, factor, GRID, factor)
        # A planning cell is blocked only if all of it lies within 300 of a wall. Earlier rules closed narrow
        # passages: "any part within 600" left 7 of 10 Mantis Signor areas unreachable (2026-11-11), and "most of it
        # within 300" still split Mantis Hive's open area into 10 pieces and Gemini3's into 5 (user: the bot thinks it
        # cannot pass some passages, 2026-11-12); this rule gives one connected area on both. Waypoints use each cell's
        # point farthest from the walls (cell_points), so routes through a passage keep to its middle.
        self.blocked = padded(CLEARANCE_PX).all(axis=(1, 3))
        self.near_wall = padded(PREFER_PX).any(axis=(1, 3)) & ~self.blocked
        self.cell_world = assets.MAP_SPAN / GRID
        # Distance to the nearest mapped wall in world units, per fine pixel (for unstick hops).
        self.fine_world = assets.MAP_SPAN / self.size
        self.learned_cost = {}  # (cell x, cell y) -> extra route cost (learned trouble spots, never a hard block)
        self.learned_mask = np.zeros_like(self.blocked)
        self.clearance = cv2.distanceTransform((~self.blocked_fine).astype(np.uint8), cv2.DIST_L2, 5) * self.fine_world
        # Per cell: the fine pixel with the most room to the walls (world point), used as the cell's waypoint.
        cells = self.clearance.reshape(GRID, factor, GRID, factor).transpose(0, 2, 1, 3).reshape(GRID, GRID, -1)
        best = cells.argmax(axis=2)
        gy, gx = np.mgrid[0:GRID, 0:GRID]
        self.cell_points = np.stack(((gx * factor + best % factor + 0.5) * self.fine_world - assets.MAP_OFFSET,
                                     (gy * factor + best // factor + 0.5) * self.fine_world - assets.MAP_OFFSET),
                                    axis=2)
        # Terrain heights: <planet>.0.0.jp2, 16 bit, scaled to 0..ScaleY from the planet's own cfg. Gemini3 has 19,200
        # (object heights = map height + 125-140); Draconis2 (Mantis Hive) has 38,400: with the Gemini value the ground
        # sat 12,000 under the ship and no click point was ever found (2026-11-11).
        scale = planet_scale(prefix)
        self.height_scale = scale.get("ScaleY", 19200.0)
        if abs(scale.get("ScaleX", assets.MAP_SPAN) - assets.MAP_SPAN) > 1:
            raise ValueError(f"{prefix} spans {scale['ScaleX']:.0f} units; only {assets.MAP_SPAN:.0f} is supported")
        self.heights = None
        heights = assets.find_asset(f"{prefix}.0.0.jp2")
        if heights is not None:
            image = cv2.imdecode(np.frombuffer(heights, np.uint8), cv2.IMREAD_UNCHANGED)
            if image is not None:
                self.heights = image.astype(np.float32) * (self.height_scale / 65535.0)

    def ground_height(self, x, y):
        """Terrain height at a world point, or None if the heightmap is not available."""
        if self.heights is None:
            return None
        size = self.heights.shape[0] - 1
        px = min(size, max(0, round((x + assets.MAP_OFFSET) / assets.MAP_SPAN * size)))
        py = min(size, max(0, round((y + assets.MAP_OFFSET) / assets.MAP_SPAN * size)))
        return float(self.heights[py, px])

    # Coordinates --------------------------------------------------------------------------------------------

    def to_cell(self, x, y):
        cx = int((x + assets.MAP_OFFSET) / self.cell_world)
        cy = int((y + assets.MAP_OFFSET) / self.cell_world)
        return min(GRID - 1, max(0, cx)), min(GRID - 1, max(0, cy))

    def to_world(self, cell):
        """The cell's waypoint: its point farthest from the walls (the centre could lie in a wall in a passage)."""
        cx, cy = cell
        x, y = self.cell_points[cy, cx]
        return float(x), float(y)

    def sight_clear(self, eye, point, margin=80.0, skip_end=400.0):
        """True if the straight line from the camera eye to a 3D point (x, height, world y) stays above the terrain.
        The last `skip_end` units are not checked (the point itself sits on or near the ground). Ground clicks with
        terrain in between land on that terrain (2026-11-11: camera inside the tunnel wall, ship pressed into it)."""
        if self.heights is None:
            return True
        (ex, ey, ez), (px, py, pz) = eye, point
        length = math.sqrt((px - ex) ** 2 + (py - ey) ** 2 + (pz - ez) ** 2)
        steps = max(2, int(length / 150))
        for i in range(1, steps):
            t = i / steps
            if length * (1 - t) < skip_end:
                break
            x, h, z = ex + (px - ex) * t, ey + (py - ey) * t, ez + (pz - ez) * t
            if self.ground_height(x, z) > h - margin:
                return False
        return True

    def fire_line_clear(self, a, b):
        """True if the straight line between two world points crosses no wall on the collision map itself (no
        clearance margin). The padded planning grid called most lines in tunnels "blocked", and the bot then flew to
        the enemy itself (2026-11-11)."""
        (ax, ay), (bx, by) = a, b
        steps = max(1, int(math.hypot(bx - ax, by - ay) / (self.fine_world / 2)))
        for i in range(steps + 1):
            t = i / steps
            px = int((ax + (bx - ax) * t + assets.MAP_OFFSET) / self.fine_world)
            py = int((ay + (by - ay) * t + assets.MAP_OFFSET) / self.fine_world)
            if 0 <= px < self.size and 0 <= py < self.size and self.blocked_fine[py, px]:
                return False
        return True

    def open_spot_near(self, x, y, radius=1200, avoid=()):
        """The point within `radius` with the most room to the mapped walls (not right next to the start: a real
        hop, not a nudge in place), outside the circles in `avoid` ((x, y, r) world units). None if nothing is open."""
        step = self.fine_world
        px, py = (x + assets.MAP_OFFSET) / step, (y + assets.MAP_OFFSET) / step
        r = int(radius / step)
        x0, x1 = max(0, int(px) - r), min(self.size, int(px) + r + 1)
        y0, y1 = max(0, int(py) - r), min(self.size, int(py) + r + 1)
        window = self.clearance[y0:y1, x0:x1].copy()
        ys, xs = np.mgrid[y0:y1, x0:x1]
        dist = np.hypot(xs - px, ys - py)
        window[dist > r] = 0
        window[dist < r * 0.4] *= 0.5  # a real hop, not a nudge in place
        for ax, ay, ar in avoid:
            apx, apy = (ax + assets.MAP_OFFSET) / step, (ay + assets.MAP_OFFSET) / step
            window[np.hypot(xs + 0.5 - apx, ys + 0.5 - apy) < ar / step] = 0  # cell centres, as returned
        if window.max() <= 0:
            return None
        iy, ix = np.unravel_index(int(window.argmax()), window.shape)
        return (x0 + ix + 0.5) * step - assets.MAP_OFFSET, (y0 + iy + 0.5) * step - assets.MAP_OFFSET

    def room(self, x, y):
        """Distance from a world point to the nearest mapped wall (0 outside the map or on a wall)."""
        px, py = int((x + assets.MAP_OFFSET) / self.fine_world), int((y + assets.MAP_OFFSET) / self.fine_world)
        if not (0 <= px < self.size and 0 <= py < self.size):
            return 0.0
        return float(self.clearance[py, px])

    def is_blocked(self, x, y):
        return bool(self.blocked[self.to_cell(x, y)[1], self.to_cell(x, y)[0]])

    def nearest_open(self, cell, limit=20):
        cx, cy = cell
        if not self.blocked[cy, cx]:
            return cell
        for radius in range(1, limit):
            for dx in range(-radius, radius + 1):
                for dy in (-radius, radius):
                    for x, y in ((cx + dx, cy + dy), (cx + dy, cy + dx)):
                        if 0 <= x < GRID and 0 <= y < GRID and not self.blocked[y, x]:
                            return x, y
        return None

    def open_point(self, x, y):
        """The world point itself if open, else the centre of the nearest open cell (None if none is close)."""
        if not self.is_blocked(x, y):
            return x, y
        cell = self.nearest_open(self.to_cell(x, y))
        return self.to_world(cell) if cell is not None else None

    def reachable(self, start, radius):
        """Open cells connected to start (flood fill) whose centre lies within radius of start."""
        origin = self.nearest_open(self.to_cell(*start))
        if origin is None:
            return set()
        limit = (radius / self.cell_world) ** 2
        seen = {origin}
        queue = [origin]
        while queue:
            cx, cy = queue.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = cx + dx, cy + dy
                if (nx, ny) in seen or not (0 <= nx < GRID and 0 <= ny < GRID) or self.blocked[ny, nx]:
                    continue
                if (nx - origin[0]) ** 2 + (ny - origin[1]) ** 2 > limit:
                    continue
                seen.add((nx, ny))
                queue.append((nx, ny))
        return seen

    def line_room(self, a, b):
        """Smallest distance to a mapped wall along the straight line a-b (world units)."""
        (ax, ay), (bx, by) = a, b
        steps = max(1, int(math.hypot(bx - ax, by - ay) / (self.fine_world * 2)))
        return min(self.room(ax + (bx - ax) * i / steps, ay + (by - ay) * i / steps) for i in range(steps + 1))

    def line_clear(self, a, b, blocked=None):
        """True if the straight segment between two world points avoids blocked cells (of `blocked`, default the
        terrain grid)."""
        grid = self.blocked if blocked is None else blocked
        (ax, ay), (bx, by) = a, b
        steps = max(1, int(math.hypot(bx - ax, by - ay) / (self.cell_world / 2)))
        for i in range(steps + 1):
            t = i / steps
            cx, cy = self.to_cell(ax + (bx - ax) * t, ay + (by - ay) * t)
            if grid[cy, cx]:
                return False
        return True

    # Planning -----------------------------------------------------------------------------------------------

    def path(self, start, goal, extra_blocked=(), avoid=()):
        """World waypoints from start to goal around blocked terrain, or None if unreachable.
        avoid: circles (x, y, radius) to route around, e.g. dangerous enemy groups. A circle holding the start or
        the goal is ignored, so the ship can always leave it."""
        blocked = self.blocked
        if extra_blocked or avoid:
            blocked = blocked.copy()
            self._mark(blocked, extra_blocked)
            ys, xs = np.mgrid[0:GRID, 0:GRID]
            for x, y, radius in avoid:
                if math.hypot(start[0] - x, start[1] - y) < radius or math.hypot(goal[0] - x, goal[1] - y) < radius:
                    continue
                cx = (x + assets.MAP_OFFSET) / self.cell_world - 0.5
                cy = (y + assets.MAP_OFFSET) / self.cell_world - 0.5
                blocked |= (xs - cx) ** 2 + (ys - cy) ** 2 <= (radius / self.cell_world) ** 2
        start_cell = self.nearest_open(self.to_cell(*start))
        goal_cell = self.nearest_open(self.to_cell(*goal))
        if start_cell is None or goal_cell is None:
            return None
        came = {start_cell: None}
        cost = {start_cell: 0.0}
        frontier = [(0.0, start_cell)]
        while frontier:
            _, cell = heapq.heappop(frontier)
            if cell == goal_cell:
                break
            for nx, ny, new_cost in self._steps(blocked, cell, cost[cell]):
                if new_cost < cost.get((nx, ny), math.inf):
                    cost[(nx, ny)] = new_cost
                    came[(nx, ny)] = cell
                    heuristic = math.hypot(goal_cell[0] - nx, goal_cell[1] - ny)
                    heapq.heappush(frontier, (new_cost + heuristic, (nx, ny)))
        return self._route(came, blocked, start, goal, goal_cell)

    def _mark(self, blocked, points):
        """Close the cell of each (x, y) in `blocked` (one cell: 3x3, 1,800 units, closed whole passages)."""
        for x, y in points:
            cx, cy = self.to_cell(x, y)
            blocked[cy, cx] = True

    def _steps(self, blocked, cell, base):
        """(x, y, cost) of each open neighbour of `cell` reached from it at cost `base` (8 directions, no corner
        cutting; cells near walls and learned trouble spots cost extra)."""
        cx, cy = cell
        for dx, dy, step in MOVES:
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < GRID and 0 <= ny < GRID) or blocked[ny, nx]:
                continue
            if dx and dy and (blocked[cy, nx] or blocked[ny, cx]):
                continue  # no corner cutting
            yield nx, ny, base + step + (NEAR_WALL_COST if self.near_wall[ny, nx] else 0.0) \
                + self.learned_cost.get((nx, ny), 0.0)

    def _route(self, came, blocked, start, goal, goal_cell):
        if goal_cell not in came:
            return None
        cells = []
        cell = goal_cell
        while cell is not None:
            cells.append(cell)
            cell = came[cell]
        cells.reverse()
        # A goal inside terrain is replaced by the nearest open cell, so the ship never pushes into a wall.
        end = goal if not self.is_blocked(*goal) else self.to_world(goal_cell)
        points = [start] + [self.to_world(c) for c in cells[1:-1]] + [end]
        # Straightening may not cut through near-wall cells either (it would pull routes back against walls).
        return self._smooth(points, blocked | self.near_wall | self.learned_mask)

    def tree(self, start, extra_blocked=(), limit=None):
        """One search from start to every open cell (within `limit` world units of travel, if given), for many
        routes from the same start: routes() then costs nothing per goal. The escape search planned 37 routes one by
        one (1.3 s), up to four times per retreat step, and the ship stood still 5 s at a time until it died
        (2026-11-12 17:35)."""
        blocked = self.blocked
        if extra_blocked:
            blocked = blocked.copy()
            self._mark(blocked, extra_blocked)
        start_cell = self.nearest_open(self.to_cell(*start))
        came = {}
        if start_cell is None:
            return came, blocked
        came[start_cell] = None
        cost = {start_cell: 0.0}
        frontier = [(0.0, start_cell)]
        max_cost = limit / self.cell_world * 2 if limit else math.inf
        while frontier:
            c, cell = heapq.heappop(frontier)
            if c > cost.get(cell, math.inf) or c > max_cost:
                continue
            for nx, ny, new_cost in self._steps(blocked, cell, c):
                if new_cost < cost.get((nx, ny), math.inf):
                    cost[(nx, ny)] = new_cost
                    came[(nx, ny)] = cell
                    heapq.heappush(frontier, (new_cost, (nx, ny)))
        return came, blocked

    def route(self, tree, start, goal):
        """Route from the tree's start to goal (None if the tree does not reach it)."""
        came, blocked = tree
        goal_cell = self.nearest_open(self.to_cell(*goal))
        if goal_cell is None:
            return None
        return self._route(came, blocked, start, goal, goal_cell)

    def _smooth(self, points, blocked):
        """Drop waypoints that a straight clear line can skip. Checked on the same grid the route was planned on:
        smoothing on the bare terrain grid cut routes straight back through the squad circles and stuck marks they
        were planned around, so the route around a squad failed its safety check and the bot waited."""
        if len(points) <= 2:
            return points
        result = [points[0]]
        index = 0
        while index < len(points) - 1:
            next_index = len(points) - 1
            while next_index > index + 1 and not self.line_clear(points[index], points[next_index], blocked):
                next_index -= 1
            result.append(points[next_index])
            index = next_index
        return result
