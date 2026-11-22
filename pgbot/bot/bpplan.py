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
            name, item = myzen.normalize(value), key[:-len(".name")]
            # A name can have several keys: "SC-X Thunderstorm" is the starter package (package.newplayer.ship.damage)
            # and the farmable ship (ship.lvl10raredamage). The real item wins over a package (2026-11-19).
            if name not in out or (out[name].startswith("package.") and not item.startswith("package.")):
                out[name] = item
    return out


# Locations that are no farmable planet on purpose: the Sirius rings (Ancient), and the skull blueprints of the XC
# Remnants (site: "Vega", "Oort"). Left out without a warning.
NOT_FARMED_AREAS = ("sirius ring", "vega", "oort", "sirius singularity")


def system_blueprints(system, skipped=None):
    """Site blueprints dropped by enemies on reachable planets of this star system (one entry per name).

    skipped: a list that receives (label, reason, warn) for every site entry of this system that is left out, so no
    blueprint is dropped silently (user, 2026-11-21: area locations dropped ~70 blueprints unnoticed). warn is True
    for the unexpected reason (a location that matches no planet); the others are left out on purpose."""
    want = myzen.normalize(system)
    out = {}
    note = skipped.append if skipped is not None else (lambda item: None)
    system_words = {want} | {myzen.normalize(p) for p in _system_planets(system)}
    for bp in (myzen.load() or {}).get("blueprints", []):
        places = bp.get("planets", [])
        known = [p for p in places if myzen.farmable(p)]  # not Colossus, Dante (conquest only)
        mine = [p for p in known if myzen.normalize(myzen.planet_bp_system(p) or "") == want]
        if not known:
            # A location the site data does not resolve to a planet (see myzen._expand_areas).
            text = " / ".join(places) or "no location"
            mentions = any(w and w in myzen.normalize(text) for w in system_words)
            if mentions and bp.get("enemies"):
                planned = any(a in text.lower() for a in NOT_FARMED_AREAS)
                note((myzen.label(bp), f"location '{text}' is no farmable planet" if planned else
                      f"location '{text}' matches no planet", not planned))
            continue
        if not mine:
            continue  # another system's blueprint
        if not bp.get("enemies"):
            note((myzen.label(bp), "no enemy drops it (event, shop or mission item)", False))
            continue
        if myzen.event_only(bp):
            note((myzen.label(bp), "special events only", False))  # user, 2026-11-17
            continue
        if (bp.get("kind") or "").lower() == "ancient bp" and want != myzen.normalize("Sirius"):
            # Ancient blueprints count for the other systems but drop only in Sirius (and the shop shows them only
            # after one part): left out of every other system's completion (user, 2026-11-19).
            note((myzen.label(bp), "Ancient blueprint (Sirius only)", False))
            continue
        if (bp.get("type") == "Color Pattern" or bp.get("category") == "Patterns") and                 "rare" in (bp.get("kind") or "").lower():
            note((myzen.label(bp), "rare color pattern (not obtainable normally)", False))  # user
            continue
        key = myzen.normalize(myzen.label(bp))
        entry = out.setdefault(key, {"label": myzen.label(bp), "short": bp.get("name"), "drops": [], "rare": False})
        entry["drops"].append(dict(bp, planets=mine))  # Earth in unrest: Tau Ceti
        entry["rare"] = entry["rare"] or "rare" in (bp.get("kind") or "").lower()
    if skipped is not None:
        # One line per blueprint (a blueprint can have several site rows); a planned row drops a reason line of the
        # same blueprint left out elsewhere only if the blueprint is not planned at all.
        seen, unique = set(), []
        for label, reason, warn in skipped:
            if myzen.normalize(label) in out or (label, reason) in seen:
                continue
            seen.add((label, reason))
            unique.append((label, reason, warn))
        skipped[:] = unique
    return out


def _system_planets(system):
    data = myzen.load() or {}
    return {v["id"] for v in data.get("planets", {}).values()
            if myzen.normalize(myzen.BLUEPRINT_SYSTEM.get(v["id"], v.get("system") or "")) == myzen.normalize(system)}


def _name_variants(entry):
    """Normalized names the game may use for a site blueprint: the site writes "Rocket" (game: "Rockets"),
    "Minelayer" (game: "Mine"), ships with a "Spaceship" and color patterns with a "Color Pattern" suffix the game
    does not have, and "Attack Turret" for the game's "Attack Field Turret" (user checked the skipped ones)."""
    label = entry["label"]
    short = entry.get("short") or label  # the site's own name column: "Ink", "Mizar Monolith"
    names = [label, short, short + "s", label + "s", label.replace("Minelayer", "Mine"), re.sub(r" Spaceship$", "", label),
             re.sub(r" Color Pattern$", "", label),  # "Crossed Strings Color Pattern" -> "Crossed Strings"
             re.sub(r" (Attack|Repair) Turret$", r" \1 Field Turret", label),  # site "Attack Turret"
             label + " Launcher",  # site "Standard Rocket" = game "Standard Rocket Launcher" (rocket.9, 2026-11-19)
             label.replace("Sanctuary", "Sanctuara")]  # the game spells the Ion Protector "Sanctuara" (protect.ion24)
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
        if template_id is None and item_key:
            # The game has the item, but its template number is in no data we have (Vega Ancient Protector and Shield:
            # not in the client's memory nor the capture; numbers come from the server). Planned anyway, not skipped
            # (user, 2026-11-19): progress from the game's pickup messages, by name ("key:" ids are no template ids).
            template_id = f"key:{item_key}"
        out.append((entry, template_id, needed))
    return out


def plan(system, templates, found, totals=None):
    """Missing blueprints of the system: {"missing": [entries], "unknown": [labels], "done": n}. found: template id
    -> parts found (the player's blueprint container; read in a hangar; no entry = 0 parts). An item whose parts needed are not known
    counts as missing (it is learned once a drop of it is seen)."""
    missing, unknown, done = [], [], 0
    totals = {myzen.normalize(name): value for name, value in (totals or {}).items()}
    for entry, template_id, needed in resolve(system, templates):
        if template_id is None:
            unknown.append(entry["label"])
            continue
        if not needed and totals:
            # Parts needed from the game's own pickup message ("collected (1/12)") when no data has the item.
            needed = next((totals[v] for v in _name_variants(entry) if v in totals), None)
        # The container holds entries only for blueprints with parts found: no entry is 0 parts (user, 2026-11-17).
        have = found.get(template_id, 0)
        if (needed and have >= needed) or have >= 999:  # 999: complete per the game's messages, parts not known
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


def seek_wants(entries, planet):
    """What each blueprint needs on a planet: [{"label", "names", "levels"}], one per drop there (levels None: any).
    The plan's one level range (seek_targets) lets every sought enemy through at every level of the range; these
    keep each blueprint's own (Mars, 2026-11-21: the AM Power Repair Target needs R50 Menders, the R45 ones cannot
    drop it)."""
    wants = []
    want = myzen.normalize(planet)
    for entry in entries:
        for drop in entry["drops"]:
            if any(myzen.normalize(p) == want for p in drop["planets"]):
                wants.append({"label": entry["label"], "names": myzen.full_enemy_names(drop),
                              "levels": tuple(drop["enemy_levels"]) if drop.get("enemy_levels") else None})
    return wants


def name_levels(wants):
    """Enemy name -> (low, high) over the blueprints it drops here, or None when one of them drops at any level."""
    out = {}
    for item in wants:
        for name in item["names"]:
            if name in out and out[name] is None:
                continue
            levels = item["levels"]
            if levels is None or name not in out:
                out[name] = levels
            else:
                out[name] = (min(out[name][0], levels[0]), max(out[name][1], levels[1]))
    return out
