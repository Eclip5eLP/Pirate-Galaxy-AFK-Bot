"""Small helpers of the bot: route geometry, the state file, place names, StopBot."""
import json
import math

from ..game import assets
from ..data import myzen
from .constants import *  # noqa: F401,F403


def _closest_approach(route, threats, velocity=None, horizon=12.0, step=0.5):
    """Fly the ship along `route` at SHIP_SPEED while each threat (x, y, vx, vy) moves at its own velocity, or all at
    `velocity` if given (e.g. (0, 0) for staying put); return the closest distance during the flight (up to `horizon`
    seconds, threats keep moving if the ship arrives earlier) and the distance at the end."""
    moving = [(t[0], t[1], *(velocity if velocity is not None else (t[2:4] if len(t) >= 4 else (0.0, 0.0))))
              for t in threats]
    legs = list(zip(route, route[1:]))
    closest, distance = math.inf, math.inf
    t = 0.0
    while t <= horizon:
        travelled = SHIP_SPEED * t
        sx, sy = route[-1]
        for (ax, ay), (bx, by) in legs:
            leg = math.hypot(bx - ax, by - ay)
            if travelled <= leg:
                f = travelled / leg if leg else 1.0
                sx, sy = ax + (bx - ax) * f, ay + (by - ay) * f
                break
            travelled -= leg
        distance = min(math.hypot(sx - (x + vx * t), sy - (y + vy * t)) for x, y, vx, vy in moving)
        closest = min(closest, distance)
        t += step
    return closest, distance


def _point_along(route, distance):
    """The point `distance` units along a route (its end if the route is shorter)."""
    for (ax, ay), (bx, by) in zip(route, route[1:]):
        leg = math.hypot(bx - ax, by - ay)
        if distance <= leg:
            f = distance / leg if leg else 1.0
            return ax + (bx - ax) * f, ay + (by - ay) * f
        distance -= leg
    return route[-1]


def _segment_distance(point, a, b):
    """Distance from a point to the segment a-b."""
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(values):
    try:
        state = _load_state()
        state.update(values)
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _is_orb(unit):
    """An energy orb (class .../tb)."""
    return unit.class_name.endswith("/tb")


def _same_place(zone, name):
    """True if a zone id (or display name) and a display name are the same place: "Gemini3" ~ "Prosperous",
    "Vénar" ~ "venar"."""
    return myzen.normalize(assets.display_name(zone)) == myzen.normalize(name)


class StopBot(Exception):
    """Raised inside the loop when the bot must pause."""


class GameRestart(Exception):
    """Planned game restart (uptime, see GAME_RESTART_AFTER_H): raised in orbit, the run ends and the game starts
    again."""


class GameCrashed(Exception):
    """The game showed its crash message box: the run ends and the game is started again (not an OSError, so the
    loop's read-glitch handling does not swallow it)."""
