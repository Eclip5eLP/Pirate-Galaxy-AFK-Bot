"""Blueprint drops and planet points from "The Hitchhiker's Guide to Pirate Galaxy" (prelude-myzen.co.uk).

Researched 2026-11-11 (docs/NOTES.md):
- Blueprint drops: one static page per item category, `PirateGalaxy/BPList/<category>.html` (links in `BPList.html`).
  Each blueprint is a `<tr>` with cells `BPcol name`, `ilvl`, `BP` (kind), `zone` (planet icons with the planet
  name as title) and `drop` (text like "R28 Avenger" or "R26-30 Rocketeer Marauder Paralyzor", one faction icon per
  enemy).
- Planet points: `PirateGalaxy/Planets.html` lists planet ids (radio values). Energy fields and landing zones are
  transparent 367x367 overlays `<id>_Energy.png` / `<id>_LZ.png` in the same frame as the game minimap (north up,
  whole map), so marker blob centres convert to world units like minimap pixels.

Everything is downloaded once (Update in the settings tab) and cached in %LOCALAPPDATA%\\pgbot\\myzen.json.
"""
import json
import os
import pathlib
import re
import time
import unicodedata
import urllib.error
import urllib.request

import cv2
import numpy as np
from bs4 import BeautifulSoup

BASE = "https://prelude-myzen.co.uk/"
CACHE = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "myzen.json"
USER_AGENT = "Mozilla/5.0 (pgbot data fetch)"
MAP_OFFSET, MAP_SPAN = 76800.0, 153600.0
LEVEL = re.compile(r"^R(\d+)(?:\s*\W\s*(\d+))?\s+(.*)$")


def normalize(name):
    """Lower case, no accents, letters and digits only: 'Vénar' -> 'venar'."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _get(path, attempts=4):
    """GET with retries: the site sometimes resets connections when asked many files in a row."""
    request = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise  # a real answer (404 etc.), retrying will not help
        except (urllib.error.URLError, OSError):
            if attempt == attempts - 1:
                raise
            time.sleep(3 * (attempt + 1))


def _text(data):
    """Pages are mostly UTF-8, some Windows-1252 (Planets.html)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


# Parsing -------------------------------------------------------------------------------------------------------

def drop_lines(cell):
    """The drop cell line by line (lines end at <br>): [{"name", "faction", "levels", "enemy", "event", "note"}].
    A line with a faction icon is an enemy; a line is an event way if it has an event note (Note.png title) or names
    an event source (Santa's Hidden Stash, Huge Mantis Egg, ...)."""
    lines, current = [], []
    for node in list(cell.children):
        if getattr(node, "name", None) == "br":
            lines.append(current)
            current = []
        else:
            current.append(node)
    lines.append(current)
    out = []
    for nodes in lines:
        faction, note, words = None, None, []
        for node in nodes:
            if getattr(node, "name", None) == "img":
                title, src = node.get("title") or "", (node.get("src") or "").lower()
                if "note" in src:
                    note = title
                elif "class" in node.attrs and title and faction is None:
                    faction = title
                continue
            if getattr(node, "name", None) == "em":
                continue
            text = node.get_text(" ", strip=True) if hasattr(node, "get_text") else str(node).strip()
            if text:
                words.append(text)
        text = " ".join(" ".join(words).split()).replace("\ufffd", "-")
        text = re.sub(r"\[or .*$", "", text).strip()  # "[or 5x14'925 Gold]": a gold price, no source
        if not text and not note:
            continue
        match = LEVEL.match(text)
        levels = (int(match.group(1)), int(match.group(2) or match.group(1))) if match else None
        name = match.group(3).strip() if match else text
        event = bool(note and EVENT_NOTE.search(note)) or any(src.lower() in name.lower() for src in EVENT_SOURCES)
        out.append({"name": name, "faction": faction, "levels": levels, "enemy": faction is not None and bool(name),
                    "event": event, "note": note})
    return out


def parse_drop(cell):
    """Return (min level, max level, [enemy names]) from a drop cell, or None if it is not an enemy drop."""
    for em in cell.find_all("em"):
        em.decompose()
    text = " ".join(cell.get_text(" ", strip=True).split()).replace("�", "-")
    factions = [img.get("title") for img in cell.find_all("img") if img.get("title") and "class" in img.attrs]
    match = LEVEL.match(text)
    if match:
        low, high, rest = int(match.group(1)), int(match.group(2) or match.group(1)), match.group(3).strip()
    elif factions and text and len(text.split()) <= 3:
        low, high, rest = None, None, text  # an enemy without a level, e.g. "Minelayer"
    elif factions and len(factions) > 1:
        # Several enemies without levels, one faction icon each, one per line ("Avenger / Parasite / Detonator /
        # Pyro / Paralyzor" for the Mizar Master Blaster; dropped as "no enemy" before, so never hunted).
        names = []
        for icon in cell.find_all("img"):
            if not (icon.get("title") and "class" in icon.attrs):
                continue
            words = []
            for sibling in icon.next_siblings:
                if getattr(sibling, "name", None) in ("img", "br"):
                    break
                piece = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
                if piece:
                    words.append(piece)
            if words:
                names.append(" ".join(" ".join(words).split()))
        if len(names) != len(factions):
            return None
        return None, None, names, factions
    else:
        return None
    words = rest.split()
    # One faction icon per enemy: "Rocketeer Marauder Paralyzor" with three icons is three enemies.
    names = words if len(factions) > 1 and len(factions) == len(words) else [rest]
    return low, high, names, factions


def parse_blueprints(html, category):
    soup = BeautifulSoup(html, "lxml")
    result = []
    for row in soup.find_all("tr"):
        cells = {c: row.find("td", class_=c) for c in ("name", "ilvl", "BP", "zone", "drop")}
        if cells["name"] is None or cells["drop"] is None:
            continue
        # The name cell holds the name and, for some items (drones), a description line under it: the name only.
        first = cells["name"].find("span")
        name = " ".join((first or cells["name"]).get_text(" ", strip=True).split())
        if not name:
            continue
        kind = cells["BP"].find("img") if cells["BP"] else None
        planets = [img.get("title") for img in cells["zone"].find_all("img") if img.get("title")] if cells["zone"] else []
        # Notes on the drop (Note.png icons, read before parse_drop changes the cell): "Find during special event
        # (Xmas 2014, ...) mission: '...'" marks event-only blueprints (user, 2026-11-17).
        notes = [img.get("title") for img in cells["drop"].find_all("img")
                 if img.get("title") and "note" in (img.get("src") or "").lower()]
        lines = drop_lines(cells["drop"])
        drop = parse_drop(cells["drop"])
        normal = [line for line in lines if line["enemy"] and not line["event"]]
        if lines and any(line["event"] for line in lines):
            # A cell with event ways: only its normal enemy lines count (A110 Speed Drone: Custodian Commanders AND
            # Santa's Hidden Stash at Xmas; user, 2026-11-17).
            names = [line["name"] for line in normal]
            lows = [line["levels"][0] for line in normal if line["levels"]]
            highs = [line["levels"][1] for line in normal if line["levels"]]
            drop = (min(lows), max(highs), names, [line["faction"] for line in normal]) if names and lows else \
                ((None, None, names, [line["faction"] for line in normal]) if names else None)
        level = cells["ilvl"].get_text(strip=True) if cells["ilvl"] else ""
        result.append({
            "name": name,
            "category": category,
            "kind": kind.get("title") if kind is not None else "BP",
            "item_level": int(level) if level.isdigit() else None,
            "planets": planets,
            "enemies": drop[2] if drop else [],
            "factions": drop[3] if drop else [],
            "enemy_levels": [drop[0], drop[1]] if drop and drop[0] is not None else None,
            "drop_text": " ".join(cells["drop"].get_text(" ", strip=True).split()),
            "notes": notes,
            "event_sources": [line["name"] for line in lines if line["event"]],
        })
    return result


FIXED_TYPES = {"Cortex": "Cortex Part", "Drones": "Drone", "Patterns": "Color Pattern",
               "ShipUpgrades": "Ship Upgrade", "Ships": "Spaceship"}


def parse_types(index_html):
    """{category slug: item type name} from the icon titles in BPList.html ("Rare Repair Droids" -> "Repair Droid")."""
    types = dict(FIXED_TYPES)
    for slug, title in re.findall(r'class="ImgBP[_s]* ([A-Za-z]+)_rare"[^>]*title="([^"]*)"', index_html):
        name = re.sub(r"\s*\(.*\)", "", title).replace("Rare ", "", 1).strip()
        types[slug] = name[:-1] if name.endswith("s") else name
    return types


# Drop sources that exist only during special events (user, 2026-11-17); a blueprint dropping only from these (or
# with a "special event" note on the site) cannot be farmed normally.
EVENT_SOURCES = ("Huge Mantis Egg", "Rogue Methanoid", "Prototype Factory", "Christmas Memory", "Sirius Pirate Droid",
                 "Santa's Hidden Stash", "Wrecked Escape Pod", "Gamma-Bamba Pumpkin", "Halloween Mantis",
                 "Reward Capsule", "Colonization Node Wreckage", "Thanksgiving Wares Transporter", "Spaceball")


EVENT_NOTE = re.compile(r"\bevents?\b|xmas|christmas|halloween|easter|anniversary|thanksgiving|birthday|spaceball",
                        re.IGNORECASE)


def event_only_old(blueprint):
    """True for blueprints only available during special events: a "special event" note on the site, or all its drop
    sources event ones (a blueprint that also drops from normal enemies stays farmable)."""
    # The note wording varies ("Find during special event (Xmas ...) mission", "Find in special event (10th
    # Anniversary 2019) mission", "Use A036 Locator Drone to find Santa's Hidden Stash during special event (Xmas
    # 2012)"; user): any mention of an event or a known occasion counts.
    if any(EVENT_NOTE.search(note or "") for note in blueprint.get("notes") or []):
        return True
    def eventish(text):
        return any(source.lower() in (text or "").lower() for source in EVENT_SOURCES)
    enemies = blueprint.get("enemies") or []
    if enemies:
        return all(eventish(e) for e in enemies)  # also normal enemies drop it: farmable
    return eventish(blueprint.get("drop_text"))


def event_only(blueprint):
    """True for blueprints only available during special events: event ways only, no normal enemy (drop lines parsed
    one by one; a blueprint that also drops from normal enemies stays farmable, user 2026-11-17)."""
    enemies = blueprint.get("enemies") or []
    if "event_sources" in blueprint:
        if enemies:
            return all(any(src.lower() in e.lower() for src in EVENT_SOURCES) for e in enemies)
        return bool(blueprint["event_sources"]) or event_only_old(blueprint)
    return event_only_old(blueprint)


def label(blueprint):
    """Full item name as the game shows it: name plus item type ("Gemini Geiger Blaster", "AM Awe Rocket")."""
    kind = blueprint.get("type") or blueprint["category"]
    return f"{blueprint['name']} {kind}"


def parse_planets(html):
    """[(site id, label, system)] from Planets.html."""
    soup = BeautifulSoup(html, "lxml")
    planets = []
    for radio in soup.find_all("input", attrs={"name": "Planet"}):
        label = ""
        for sibling in radio.next_siblings:
            if getattr(sibling, "name", None) in ("input", "br"):
                break
            text = sibling.get_text(" ", strip=True) if hasattr(sibling, "get_text") else str(sibling).strip()
            if text:
                label = (label + " " + text).strip()
        system_icon = radio.find_next("img")
        system = system_icon.get("title", "") if system_icon is not None else ""
        planets.append((radio.get("value"), label or radio.get("value"), system.replace(" System", "").strip()))
    return planets


SYSTEM_SECTIONS = {"Tau Ceti": "Tau"}  # the start page's section id per system, where it is not the system name


def parse_hangars(html):
    """{system: [place names with a hangar]} from the start page's "Planet Systems" sections (a "Hangar available"
    icon in a place's row; user, 2026-11-17: Mizar has hangars at Ob, Cosmo and the Mizar Starbase only)."""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for system, _, _ in SYSTEM_LEVELS:
        div = soup.find("div", id=SYSTEM_SECTIONS.get(system, system))
        if div is None:
            continue
        names = []
        for row in div.find_all("tr"):
            name = row.find("span", class_="style8")
            if name is not None and row.find("img", title="Hangar available") is not None:
                names.append(" ".join(name.get_text(" ", strip=True).split()))
        out[system] = names
    return out


def hangars(system):
    """Places of a star system with a hangar (site data), or [] if not known."""
    data = load() or {}
    return list((data.get("hangars") or {}).get(system, []))


def parse_spot_lists(html):
    """{site id: [spot names]} from Planets.html: enemy area overlays per planet, including commented-out lists
    (the site hides some, e.g. the Death Squad routes on Prosperous, but the images exist)."""
    form = BeautifulSoup(html, "lxml").find("form", attrs={"name": "MantisSpots"})
    names = [item.get("value") for item in form.find_all("input")] if form else []
    block = re.search(r"planet_checks\s*=\s*\{(.*?)\n\t*\};", html, re.S)
    result = {}
    if not block:
        return result
    for match in re.finditer(r'"?([A-Za-z0-9-]+)"?\s*:\s*\{(.*?)\}', block.group(1), re.S):
        indexes = {int(n) for lst in re.findall(r"mantis:\s*\[([^\]]*)\]", match.group(2))
                   for n in re.findall(r"\d+", lst)}
        result[match.group(1)] = [names[i] for i in sorted(indexes) if i < len(names)]
    return result


def overlay_cells(png_bytes, step=4):
    """World points of the covered pixels of an overlay, on a coarse grid (step pixels, about 1,700 units)."""
    image = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        return []
    mask = image[:, :, 3] > 0
    size = image.shape[1]
    scale = MAP_SPAN / size
    cells = []
    for y in range(0, size, step):
        for x in range(0, size, step):
            if mask[y:y + step, x:x + step].any():
                cells.append((round((x + step / 2) * scale - MAP_OFFSET), round((y + step / 2) * scale - MAP_OFFSET)))
    return cells


def overlay_points(png_bytes):
    """World points of the marker blobs in an overlay image."""
    image = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        return []
    mask = (image[:, :, 3] > 0).astype(np.uint8)
    count, _, stats, centres = cv2.connectedComponentsWithStats(mask)
    scale = MAP_SPAN / image.shape[1]
    return [(round(cx * scale - MAP_OFFSET), round(cy * scale - MAP_OFFSET))
            for (cx, cy), stat in zip(centres[1:], stats[1:]) if stat[cv2.CC_STAT_AREA] >= 20]


# Download ------------------------------------------------------------------------------------------------------

# Level ranges per star system (GalacticBlueprints/systems.json); Sol in unrest counts as Sol for power tiers.
SYSTEM_LEVELS = [("Vega", 0, 14), ("Antares", 15, 24), ("Gemini", 25, 32), ("Mizar", 33, 42), ("Sol", 43, 59),
                 ("Draconis", 60, 69), ("Sirius", 70, 81), ("Tau Ceti", 82, 99), ("Oort", 100, 999)]


def update(progress=lambda text: None):
    """Download everything, save the cache and return it. Raises OSError if the site cannot be reached."""
    data = {"fetched": time.strftime("%Y-%m-%d %H:%M"), "blueprints": [], "planets": {}}
    index = _text(_get("BPList.html"))
    types = parse_types(index)
    categories = sorted(set(re.findall(r"PirateGalaxy/BPList/([A-Za-z]+)\.html", index)))
    for number, category in enumerate(categories, 1):
        progress(f"Blueprints {number}/{len(categories)}: {types.get(category, category)}")
        html = _text(_get(f"PirateGalaxy/BPList/{category}.html"))
        for blueprint in parse_blueprints(html, category):
            blueprint["type"] = types.get(category, category)
            data["blueprints"].append(blueprint)
        time.sleep(0.2)
    progress("Hangars")
    try:
        data["hangars"] = parse_hangars(_text(_get("")))
    except OSError:
        data["hangars"] = {}
    planets_html = _text(_get("PirateGalaxy/Planets.html"))
    planets = parse_planets(planets_html)
    spot_lists = parse_spot_lists(planets_html)
    for number, (site_id, name, system) in enumerate(planets, 1):
        progress(f"Planets {number}/{len(planets)}: {name}")
        entry = {"id": site_id, "label": name, "system": system, "energy": [], "landing": [], "spots": {}}
        for kind, key in (("Energy", "energy"), ("LZ", "landing")):
            try:
                entry[key] = overlay_points(_get(f"PirateGalaxy/{site_id}_{kind}.png"))
            except urllib.error.HTTPError:
                pass
            time.sleep(0.1)
        for spot in spot_lists.get(site_id, []):
            try:
                png = _get(f"PirateGalaxy/MantisSpots/{site_id}/{site_id}_{spot}.png")
            except urllib.error.HTTPError:
                continue
            entry["spots"][spot] = {"centres": overlay_points(png), "cells": overlay_cells(png)}
            time.sleep(0.3)
        # "AX-03/BX-12/..." is one map shared by several planets.
        for part in name.split("/"):
            data["planets"][normalize(part)] = entry
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data), encoding="utf-8")
    _loaded.clear()
    return data


_loaded = {}


def load():
    """The cached data, or None if it was never downloaded."""
    if "data" not in _loaded:
        try:
            _loaded["data"] = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _loaded["data"] = None
    return _loaded["data"]


# Lookups -------------------------------------------------------------------------------------------------------

def blueprint_names():
    """Blueprints that enemies drop, by full item name."""
    data = load()
    if not data:
        return []
    return sorted({label(bp) for bp in data["blueprints"] if bp["enemies"]})


def blueprint_drops(name):
    """All drop entries for a blueprint (one per row on the site)."""
    data = load()
    if not data:
        return []
    return [bp for bp in data["blueprints"] if label(bp) == name and bp["enemies"]]


def full_enemy_names(drop):
    """Enemy names with their faction as the game shows them: "Avenger" + Mantis -> "Mantis Avenger"."""
    factions = drop["factions"] or [""]
    names = []
    for i, name in enumerate(drop["enemies"]):
        faction = factions[i] if i < len(factions) else factions[0]
        names.append(name if not faction or name.lower().startswith(faction.lower()) else f"{faction} {name}")
    return names


def planet_info(display_name):
    """Site data for a planet by its in-game display name, or None."""
    data = load()
    if not data or not display_name:
        return None
    key = normalize(display_name)
    if key in data["planets"]:
        return data["planets"][key]
    for entry in data["planets"].values():
        if normalize(entry["id"]) == key:
            return entry
    return None


def planet_system(display_name):
    info = planet_info(display_name)
    return info.get("system") if info else None


def tier(level):
    """Index of the star system whose level range holds this level (0 = Vega), or None."""
    if level is None:
        return None
    for index, (_, low, high) in enumerate(SYSTEM_LEVELS):
        if low <= level <= high:
            return index
    return None


def spot_id(full_name):
    """Overlay name for an enemy: "Mantis Rocketeer" -> "Rocketeer", "Imperial Harvester" -> "ImpHarvester"."""
    words = full_name.split()
    if not words:
        return ""
    prefix = {"mantis": "", "imperial": "Imp", "ancient": "Anc", "raider": "Rai"}.get(words[0].lower())
    if prefix is None:
        return "".join(words)
    return prefix + "".join(w[:1].upper() + w[1:] for w in words[1:])


def enemy_areas(display_name, full_names):
    """Centres of the spawn areas of these enemies on a planet, from the site overlays."""
    info = planet_info(display_name)
    if not info:
        return []
    points = []
    for name in full_names:
        spot = info.get("spots", {}).get(spot_id(name))
        if spot:
            points.extend(tuple(p) for p in spot["centres"])
    return points


def death_squad_cells(display_name):
    """Coarse world cells on the Death Squad patrol routes of a planet (site overlay), or []."""
    info = planet_info(display_name)
    spot = info.get("spots", {}).get("DeathSquad") if info else None
    return [tuple(p) for p in spot["cells"]] if spot else []


def _words(text):
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text or "").lower())


def enemy_matches(unit_name, level, wanted_names, levels=None, exact=True):
    """True if an in-game enemy name is one of the wanted full names and the level fits.
    exact=False also accepts names that contain all words of a wanted name (site names the game lacks)."""
    if not unit_name:
        return False
    if levels and level is not None and not (levels[0] <= level <= levels[1]):
        return False
    name = _words(unit_name)
    for wanted in wanted_names:
        parts = _words(wanted)
        if parts == name or (not exact and parts and all(part in name for part in parts)):
            return True
    return False
