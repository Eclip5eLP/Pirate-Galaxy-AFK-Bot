"""BP Completion planning (user, 2026-11-16): which blueprints of a star system are still missing, where they drop.

Site data (myzen) lists every blueprint with its planets and the enemies that drop it; the game's memory has the item
templates (template id, key such as "$afterburner.84", parts needed) and the player's parts found (BlueprintBook).
A site blueprint is matched to a template by its full display name ("Symbolic Afterburner").
"""
import re

from ..data import myzen
from ..game import assets

_M = 1 << 32
_NUMBERED = re.compile(r"([a-z]+\.[a-z]*)(\d+)")  # "afterburner.94", "protect.ion94"


def _java_hash(text):
    h = 0
    for ch in text:
        h = (31 * h + ord(ch)) % _M
    return h


def predict_id(key, known):
    """Template id of an item key that is not in memory, from a known key of the same numbered series.

    Item ids are a Java string hash of a fixed text plus the item key (found 2026-11-17 from 566 known ids: within a
    series such as orbitalstrike.62 ... .90 the hidden part is the same, 522 of 566 predicted exactly; series that
    changed partway are covered by taking the known item with the nearest number). known: {key: template id}.
    None if no item of the series is known."""
    match = _NUMBERED.fullmatch(key)
    if not match:
        return None
    base, number = match.group(1), int(match.group(2))  # series "afterburner." / "protect.ion"
    best = None
    for other, other_id in known.items():
        m = _NUMBERED.fullmatch(other)
        if m and m.group(1) == base and (best is None or abs(int(m.group(2)) - number) < best[0]):
            best = (abs(int(m.group(2)) - number), other, other_id)
    if best is None:
        return None
    _, other, other_id = best
    hidden = ((other_id % _M) - _java_hash(other)) * pow(pow(31, len(other), _M), -1, _M) % _M
    value = (hidden * pow(31, len(key), _M) + _java_hash(key)) % _M
    return value - _M if value >= 1 << 31 else value


def lang_keys():
    """Normalized display name -> item key ("Deep Recurrent Aggrobeacon" -> "aggrobeacon.94"), from the game's
    language file (site names differ in spacing: "Aggro Beacon")."""
    assets.display_name("")  # loads the language file
    out = {}
    for key, value in assets._LANG.items():
        if key.endswith(".name"):
            out.setdefault(myzen.normalize(value), key[:-len(".name")])
    return out


def system_blueprints(system):
    """Site blueprints dropped by enemies on reachable planets of this star system (one entry per name)."""
    want = myzen.normalize(system)
    out = {}
    for bp in (myzen.load() or {}).get("blueprints", []):
        if not bp.get("enemies"):
            continue  # event, shop or mission items: no enemy to hunt
        if myzen.event_only(bp):
            continue  # special events only (user, 2026-11-17)
        if (bp.get("type") == "Color Pattern" or bp.get("category") == "Patterns") and \
                "rare" in (bp.get("kind") or "").lower():
            continue  # rare color patterns cannot be gotten normally (user)
        planets = [p for p in bp.get("planets", []) if myzen.planet_info(p)
                   and myzen.normalize(myzen.planet_system(p) or "") == want]
        if not planets:
            continue
        key = myzen.normalize(myzen.label(bp))
        entry = out.setdefault(key, {"label": myzen.label(bp), "short": bp.get("name"), "drops": [], "rare": False})
        entry["drops"].append(dict(bp, planets=planets))
        entry["rare"] = entry["rare"] or "rare" in (bp.get("kind") or "").lower()
    return out


def _name_variants(entry):
    """Normalized names the game may use for a site blueprint: the site writes "Rocket" (game: "Rockets"),
    "Minelayer" (game: "Mine"), ships with a "Spaceship" and color patterns with a "Color Pattern" suffix the game
    does not have, and "Attack Turret" for the game's "Attack Field Turret" (user checked the skipped ones)."""
    label = entry["label"]
    short = entry.get("short") or label  # the site's own name column: "Ink", "Mizar Monolith"
    names = [label, short, short + "s", label + "s", label.replace("Minelayer", "Mine"), re.sub(r" Spaceship$", "", label),
             re.sub(r" Color Pattern$", "", label),  # "Crossed Strings Color Pattern" -> "Crossed Strings"
             re.sub(r" (Attack|Repair) Turret$", r" \1 Field Turret", label)]  # site "Attack Turret"
    return [myzen.normalize(n) for n in names]


def template_ids(templates):
    """Normalized display name -> template id, from the book's catalogue {id: {"desc": key, ...}}."""
    out = {}
    for template_id, entry in templates.items():
        name = assets.display_name(entry.get("desc"))
        if name:
            out.setdefault(myzen.normalize(name), template_id)
    return out


_STATIC = None


def static_ids():
    """Item key -> {"id", "needed"} for ~1,100 items, from server data captured by the preservation project
    (reports/gamedata/equipment.csv, 2026-10); for items the client never loads and that are no numbered series, e.g.
    the Mizar Monolith Attack Field Turret (attackturret.rare.mizar)."""
    global _STATIC
    if _STATIC is None:
        import json
        import pathlib
        try:
            path = pathlib.Path(__file__).resolve().parent.parent / "data" / "item_ids.json"
            _STATIC = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STATIC = {}
    return _STATIC


def resolve(system, templates):
    """[(entry, template id or None, parts needed or None)] for the system's enemy-dropped blueprints: the id from
    the catalogue (items the client has loaded), else computed from the item key (language file) and a known item of
    the same series."""
    names = template_ids(templates)
    keys = lang_keys()
    known = {e["desc"].lstrip("$"): i for i, e in templates.items() if isinstance(e.get("desc"), str)}
    out = []
    for key, entry in sorted(system_blueprints(system).items()):
        template_id = names.get(key)
        item_key = next((keys[v] for v in _name_variants(entry) if v in keys), None)
        static = static_ids().get(item_key) if item_key else None
        if template_id is None and item_key:
            template_id = known.get(item_key) or (static or {}).get("id") or predict_id(item_key, known)
        needed = (templates.get(template_id) or {}).get("needed") if template_id is not None else None
        if needed is None and static and static.get("id") == template_id:
            needed = static.get("needed")
        out.append((entry, template_id, needed))
    return out


def plan(system, templates, found):
    """Missing blueprints of the system: {"missing": [entries], "unknown": [labels], "done": n}. found: template id
    -> parts found (the player's blueprint container; read in a hangar; no entry = 0 parts). An item whose parts needed are not known
    counts as missing (it is learned once a drop of it is seen)."""
    missing, unknown, done = [], [], 0
    for entry, template_id, needed in resolve(system, templates):
        if template_id is None:
            unknown.append(entry["label"])
            continue
        # The container holds entries only for blueprints with parts found: no entry is 0 parts (user, 2026-11-17).
        have = found.get(template_id, 0)
        if needed and have >= needed:
            done += 1
            continue
        missing.append(dict(entry, template_id=template_id, progress=(have, needed)))
    return {"missing": missing, "unknown": unknown, "done": done}


def by_planet(missing):
    """Planet -> missing entries that drop there, most entries first."""
    planets = {}
    for entry in missing:
        for drop in entry["drops"]:
            for planet in drop["planets"]:
                planets.setdefault(planet, [])
                if entry not in planets[planet]:
                    planets[planet].append(entry)
    return dict(sorted(planets.items(), key=lambda item: -len(item[1])))


def seek_targets(entries, planet):
    """(enemy names, (min level, max level)) to hunt on a planet for these blueprints."""
    names, lows, highs = set(), [], []
    want = myzen.normalize(planet)
    for entry in entries:
        for drop in entry["drops"]:
            if not any(myzen.normalize(p) == want for p in drop["planets"]):
                continue
            names.update(myzen.full_enemy_names(drop))
            if drop.get("enemy_levels"):
                lows.append(drop["enemy_levels"][0])
                highs.append(drop["enemy_levels"][1])
    return sorted(names), ((min(lows), max(highs)) if lows else None)
