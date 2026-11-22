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
import math
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
# "R25 Mender", "R40-R44 Mender", "R43,44Detonator", "R38,R39 Pyro", "R67�69Sniper" (the site's dash arrives as a
# broken character; Mizar Magnet Sticky Bomb was hunted as an enemy named "R40�R44 Mender", 2026-11-20).
LEVEL = re.compile(r"^R(\d+)(?:\s*[^\w\s]\s*R?(\d+))?\s*([A-Za-z].*)$")


def normalize(name):
    """Lower case, no accents, letters and digits only: 'Vénar' -> 'venar'."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _get(path, attempts=4, timeout=60):
    """GET with retries: the site sometimes resets connections when asked many files in a row."""
    request = urllib.request.Request(BASE + path, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
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


def spot_labels(png_bytes, system=None):
    """[[x, y, level or None]] world points of the enemy level labels drawn on an overlay (sitelevels.py). A level
    that is surely a partial read becomes None: one digit among two-digit labels of the same image, or more than
    LABEL_BELOW_RANGE under the planet's star system range (Terasa "8", Mars "7": the left digit was not read)."""
    from . import sitelevels
    found = sitelevels.labels(png_bytes)
    image = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if not found or image is None:
        return []
    scale = MAP_SPAN / image.shape[1]
    levels = sorted(l for *_, l in found if l is not None)
    median = levels[len(levels) // 2] if levels else None
    low = next((lo for name, lo, _ in SYSTEM_LEVELS if system and normalize(name) == normalize(system)), None)
    out = []
    for x, y, level in found:
        if level is not None and level < 10 and median is not None and median >= 20:
            level = None
        if level is not None and low is not None and (level < low - LABEL_BELOW_RANGE or (level < 10 <= low)):
            level = None  # one digit outside Vega: a two-digit label with its "1" not read (Terasa "18" -> "8")
        out.append([round(x * scale - MAP_OFFSET), round(y * scale - MAP_OFFSET), level])
    return out


LABEL_BELOW_RANGE = 10


def add_spot_labels(progress=lambda text: None):
    """Add the level labels to a cache from before they were read (2026-11-20), without a full update: downloads
    only the overlays. Returns the number of overlays read."""
    data = load()
    if not data:
        return 0
    done, count = set(), 0
    for entry in data["planets"].values():
        if entry["id"] in done:
            continue
        done.add(entry["id"])
        for spot, info in entry.get("spots", {}).items():
            if "labels" in info:
                continue
            progress(f"Levels: {entry['label']} {spot}")
            try:
                png = _get(f"PirateGalaxy/MantisSpots/{entry['id']}/{entry['id']}_{spot}.png")
            except (OSError, urllib.error.HTTPError):
                continue
            info["labels"] = spot_labels(png, BLUEPRINT_SYSTEM.get(entry["id"], entry.get("system")))
            count += 1
            time.sleep(0.2)
    CACHE.write_text(json.dumps(data), encoding="utf-8")
    _loaded.clear()
    return count


# Download ------------------------------------------------------------------------------------------------------

# Level ranges per star system (GalacticBlueprints/systems.json); Sol in unrest counts as Sol for power tiers.
SYSTEM_LEVELS = [("Vega", 0, 14), ("Antares", 15, 24), ("Gemini", 25, 32), ("Mizar", 33, 42), ("Sol", 43, 59),
                 ("Draconis", 60, 69), ("Sirius", 70, 81), ("Tau Ceti", 82, 99), ("Oort", 100, 999)]


def update(progress=lambda text: None, fallback=None):
    """Download everything, save the cache and return it. If the site cannot be reached (or fails partway) and
    fallback is given, the backup it returns (bytes of a saved myzen.json) is used instead (user, 2026-11-21: the
    site was down). Raises OSError if neither works."""
    try:
        return _download(progress)
    except (OSError, ValueError) as error:  # URLError and timeouts are OSErrors
        if fallback is None:
            raise
        progress(f"Site not reachable ({type(error).__name__}); loading the backup")
        try:
            data = json.loads(fallback().decode("utf-8"))
        except Exception as backup_error:
            raise OSError(f"site: {error}; backup: {backup_error}") from None
        if not isinstance(data, dict) or not data.get("blueprints") or not data.get("planets"):
            raise OSError(f"site: {error}; backup: not site data") from None
        data["source"] = "backup"
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data), encoding="utf-8")
        _loaded.clear()
        return data


BACKUP_URL = "https://eclip5e.com/development/pgbot/api.php?a=sitedata"  # open to all, no sign-in (user)


def fetch_backup(url=BACKUP_URL, timeout=60):
    """The site data backup from the bot's web page, as bytes."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download(progress):
    data = {"fetched": time.strftime("%Y-%m-%d %H:%M"), "blueprints": [], "planets": {}}
    index = _text(_get("BPList.html", attempts=2, timeout=20))  # a site that is down fails fast (fallback)
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
            entry["spots"][spot] = {"centres": overlay_points(png), "cells": overlay_cells(png),
                                    "labels": spot_labels(png, BLUEPRINT_SYSTEM.get(site_id, system))}
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
            _loaded["data"] = _expand_areas(_repair_levels(json.loads(CACHE.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            _loaded["data"] = None
    return _loaded["data"]


LEVEL_TOKEN = r"[Rr](\d+)(?:\s*[^\w\s\[\]()]\s*[Rr]?(\d+))?"
LEVEL_INSIDE = re.compile(r"\s" + LEVEL_TOKEN + r"\s*[A-Za-z]")


def split_enemies(text):
    """[(name, (low, high) or None)] of one site enemy text that may hold several enemies run together, with bracket
    notes: "Repogrub R89,90 Interceptor" -> Repogrub (no level), Interceptor 89-90; "Ignomes [DS Commander] R93
    Abasis [DS Commander]" -> Ignomes, Abasis 93; "Signor [Comm.]" -> Signor (Sol drops, 2026-11-20)."""
    text = re.sub(r"\[[^\]]*\]", " ", text)
    # "(DS only)", "(at LZ only)", "(55/10)": notes; the enemy is hunted either way (a Death Squad Rocketeer is
    # still a Rocketeer).
    text = re.sub(r"\([^)]*\)", " ", text)
    # "R7 Scout, 1)" / "Custodian Comm., 1) 2)": footnote marks the site leaves on 29 patterns.
    text = re.sub(r"(\s*,?\s*\d+\s*\))+\s*$", "", text)
    out, pending, last = [], None, 0
    # A level token starts a new enemy; text between tokens is one enemy name.
    for match in re.finditer(r"(?:^|(?<=\s))" + LEVEL_TOKEN + r"(?=\s|[A-Za-z]|$)", text):
        name = " ".join(text[last:match.start()].split())
        if name:
            out.append((name, pending))
        pending = (int(match.group(1)), int(match.group(2) or match.group(1)))
        last = match.end()
    name = " ".join(text[last:].split())
    if name:
        out.append((name, pending))
    return out


def _repair_levels(data):
    """Enemy names that still carry their level ("R40�R44 Mender", parsed with the old LEVEL pattern): name
    and levels split, as a fresh download would. The levels are set only if every enemy of the drop has them (a
    "Mender" next to "R52-60 Sniper" keeps the drop open to any level)."""
    for bp in (data or {}).get("blueprints", []):
        enemies = bp.get("enemies") or []
        if not any(LEVEL.match(e or "") or LEVEL_INSIDE.search(e or "") or re.search(r"[\[()]|^[Rr]\d", e or "")
                   for e in enemies):
            continue
        names, lows, highs, all_levelled = [], [], [], True
        for enemy in enemies:
            for name, levels in split_enemies(enemy or ""):
                names.append(name)
                if levels:
                    lows.append(levels[0])
                    highs.append(levels[1])
                else:
                    all_levelled = False
        bp["enemies"] = names
        if bp.get("enemy_levels") is None and all_levelled and lows:
            bp["enemy_levels"] = [min(lows), max(highs)]
    return data


def _split_known(text, vocabulary):
    """Split an enemy text the site ran together ("Custodian Comm. Repogrub", "Rocketeer Rocket Turret") into known
    enemy names, longest first; [text] if it does not split into known names completely."""
    words, out, i = text.split(), [], 0
    known = {normalize(v): v for v in vocabulary}
    while i < len(words):
        for j in range(len(words), i, -1):
            name = " ".join(words[i:j])
            if normalize(name) in known:
                out.append(known[normalize(name)])
                i = j
                break
        else:
            return [text]
    return out or [text]


def _expand_areas(data):
    """Drop locations that are no single planet, resolved to planets (user, 2026-11-21: "Sol Solo Shield" was
    collected on Mars but never planned; the site gives its place as "Sol System"). 120+ rare blueprints were left
    out of every system's plan this way:
    - "Moon and Earth", "Baumar and Molikar": both planets;
    - "Sol System", "Mizar System": the system's farm planets (planets with site drops) where the enemy is known to
      appear (in another drop there or the planet's spawn overlays), else all of them. The site's level range is a
      whole-system range, so such drops get no level filter;
    - a misspelling such as "Klabesh": the planet with a near-identical name.
    Bare system names ("Vega", "Oort": skull blueprints of XC Remnants) and the Sirius rings stay as they are.
    The original text is kept as "area"."""
    import difflib
    if not data:
        return data
    planets = data.get("planets", {})
    by_key = {normalize(v["id"]): v for v in planets.values()}
    names = {}  # display name used in drops -> planet entry
    for bp in data.get("blueprints", []):
        for place in bp.get("planets", []):
            info = planets.get(normalize(place)) or by_key.get(normalize(place))
            if info and normalize(info["id"]) not in CONQUEST_ONLY:
                names.setdefault(place, info)
    # Enemy names the site ran together: "Marauder, Resurrector" (comma) is split at the comma; "Rocketeer Rocket
    # Turret" with factions Mantis, Imperial into names of entries that have one faction per name, when that gives
    # exactly one name per faction. Other run-together texts stay (a looser split broke "Custodian Comm." apart).
    vocabulary = set()
    for bp in data.get("blueprints", []):
        enemies, factions = bp.get("enemies") or [], bp.get("factions") or []
        if len(enemies) == len(factions):
            vocabulary.update(e for e in enemies if "," not in e)
    for bp in data.get("blueprints", []):
        enemies = [part.strip() for e in bp.get("enemies") or [] for part in e.split(",") if part.strip()]
        factions = bp.get("factions") or []
        if len(enemies) < len(factions):
            split = [n for e in enemies for n in _split_known(e, vocabulary - set(enemies))]
            if len(split) == len(factions):
                enemies = split
        if enemies:
            bp["enemies"] = enemies
    # Enemies seen per planet (drop lists).
    on_planet = {}
    for bp in data.get("blueprints", []):
        for place in bp.get("planets", []):
            if place in names:
                on_planet.setdefault(place, set()).update(normalize(e) for e in bp.get("enemies") or [])

    def appears(enemy, place):
        if normalize(enemy) in on_planet.get(place, set()):
            return True
        spots = names[place].get("spots") or {}
        return any(normalize(enemy).endswith(normalize(spot)) or normalize(spot).endswith(normalize(enemy))
                   for spot in spots)

    for bp in data.get("blueprints", []):
        places = bp.get("planets") or []
        if not bp.get("enemies") or all(p in names for p in places):
            continue
        resolved, system_wide = [], None
        for place in places:
            if place in names:
                resolved.append(place)
                continue
            parts = [x.strip() for x in re.split(r"\s+and\s+", place) if x.strip()]
            if len(parts) > 1 and all(x in names for x in parts):
                resolved += parts
                continue
            match = re.fullmatch(r"(.+?)\s+System", place.strip())
            if match:
                system = match.group(1)
                # Blueprint system, not travel system: Earth in unrest is Tau Ceti's (BLUEPRINT_SYSTEM).
                farm = [n for n, info in names.items()
                        if normalize(BLUEPRINT_SYSTEM.get(info["id"], info["system"])) == normalize(system)]
                if farm:
                    system_wide = system
                    fits = [n for n in farm if any(appears(e, n) for e in bp["enemies"])]
                    resolved += fits or farm
                    continue
            close = difflib.get_close_matches(place, list(names), n=1, cutoff=0.85)
            if close:
                resolved.append(close[0])
        if resolved:
            bp["area"] = places
            bp["planets"] = list(dict.fromkeys(resolved))
            if system_wide:
                bp["enemy_levels"] = None
    return data


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
    """The star system a planet is reached in (travel: gates, orbits). Earth in unrest is Sol's (Earth's orbit)."""
    info = planet_info(display_name)
    return info.get("system") if info else None


# Zones that share another system's orbit but belong to a later system for blueprints and enemy levels (user,
# 2026-11-20): Earth in unrest is reached through Earth's orbit in Sol, but it is a Tau Ceti zone (enemies R89-93,
# the site lists it under Sol only because of the shared place). Site id -> system.
BLUEPRINT_SYSTEM = {"AEarth": "Tau Ceti"}
# Conquest-only planets (user, 2026-11-21): no landing for farming, never part of a blueprint plan.
CONQUEST_ONLY = {"colossus", "dante"}


def farmable(display_name):
    """A planet the bot may farm on (known to the site data and not conquest-only)."""
    info = planet_info(display_name)
    return bool(info) and normalize(info["id"]) not in CONQUEST_ONLY


def planet_bp_system(display_name):
    """The star system a planet's blueprints count for (BP Completion): planet_system, except the zones above."""
    info = planet_info(display_name)
    if not info:
        return None
    return BLUEPRINT_SYSTEM.get(info.get("id"), info.get("system"))


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


def enemy_areas(display_name, full_names, levels=None):
    """Spawn areas of these enemies on a planet, from the site overlays. With levels (low, high) and level labels
    read from the overlay: the labelled spawn points at those levels plus the unread ones (None); an overlay
    without labels gives its area centres."""
    info = planet_info(display_name)
    if not info:
        return []
    points = []
    spots = info.get("spots", {})
    for name in full_names:
        spot = spots.get(spot_id(name))
        if spot is None and is_squad_leader(name):
            # The site has no spot list for Death Squad leaders; its Death Squad overlay is where they fly (user,
            # 2026-11-20: Antares' Custodian Commanders are single units, but the site's "DS" is them).
            spot = spots.get("DeathSquad")
        if not spot:
            continue
        labels = spot.get("labels") or []
        if levels and labels:
            # The site writes each spawn's level next to its icon (user, 2026-11-20): Lustra's R25 Custodian
            # Commander is the south-east one, the other five are R23.
            found = [(x, y) for x, y, level in labels if level is None or levels[0] <= level <= levels[1]]
        else:
            found = [tuple(p) for p in spot["centres"]]
        points.extend(p for p in found if p not in points)
    return points


SPAWN_OWN_LABEL = 6000     # an area centre with its enemy's level label this close is that labelled spawn
SPAWN_NEAR_LABEL = 10000   # an unlabelled spawn takes the level of the nearest label of any enemy this close
FOCUS_RADIUS = 25000       # spawn values within this distance add up (the bot fights whatever it meets around)
FOCUS_SHARE = 0.4          # areas worth at least this share of the best are patrolled


def _overlay(spots, name):
    spot = spots.get(spot_id(name))
    if spot is None and is_squad_leader(name):
        spot = spots.get("DeathSquad")
    return spot


def enemy_spawns(display_name, full_name):
    """[(x, y, level or None)] spawn points of an enemy on a planet: its labelled spawns, plus area centres without a
    label of their own. A spawn without a level takes the nearest label of any enemy around: spawns in one place
    share their level (Mars: the R50 corner in the north-east holds Pyros, Assassins, Resurrectors, an Imperial
    Sniper and the Menders' area, whose overlay has no labels; user, 2026-11-21)."""
    info = planet_info(display_name)
    spot = _overlay(info.get("spots", {}), full_name) if info else None
    if not spot:
        return []
    labels = [tuple(p) for p in spot.get("labels") or []]
    points = list(labels)
    for cx, cy in spot.get("centres") or []:
        if not any(math.hypot(cx - x, cy - y) <= SPAWN_OWN_LABEL for x, y, _ in labels):
            points.append((cx, cy, None))
    every = [(x, y, level) for other in info["spots"].values() for x, y, level in other.get("labels") or []
             if level is not None]
    out = []
    for x, y, level in points:
        if level is None:
            # The level most labels around show, the nearest one on a tie: Mars' Mender area in the north-east has
            # a Transporter R45 label closest, but the Imperial Sniper and the Resurrector beside it are R50.
            near = sorted((math.hypot(px - x, py - y), lv) for px, py, lv in every)
            near = [lv for d, lv in near if d <= SPAWN_NEAR_LABEL]
            if near:
                level = max(dict.fromkeys(near), key=near.count)
        out.append((round(x), round(y), level))
    return out


def spawn_values(display_name, wants):
    """[(x, y, value)] spawn points of the sought enemies, valued by the blueprints they can drop: each blueprint
    adds 1 / (its number of spawns) to every spawn of its enemies at its levels (unknown level: counted). A
    blueprint that only few spawns drop weighs on those; one that every spawn drops weighs little anywhere.
    Returns [] when no blueprint is bound to some spawns only (the level filter leaves every spawn in)."""
    names = sorted({n for item in wants for n in item["names"]})
    spawns = {name: enemy_spawns(display_name, name) for name in names}
    values, bound = {}, False
    for item in wants:
        points = [(x, y) for name in item["names"] for x, y, level in spawns[name]
                  if item["levels"] is None or level is None or item["levels"][0] <= level <= item["levels"][1]]
        every = sum(len(spawns[name]) for name in item["names"])
        if not points:
            continue
        if len(points) < every:
            bound = True
        for point in points:
            values[point] = values.get(point, 0.0) + 1.0 / len(points)
    return [(x, y, v) for (x, y), v in values.items()] if bound else []


def focus_areas(display_name, wants, areas):
    """The patrol areas near the most valuable spawns (spawn_values summed within FOCUS_RADIUS): those worth at least
    FOCUS_SHARE of the best. None when nothing to focus on. Mars, 2026-11-21: four of ten blueprints drop only in
    the R50 north-east; the patrol over all 34 areas met no R50 Custodian Commander in 10 h."""
    values = spawn_values(display_name, wants)
    if not values or not areas:
        return None

    def worth(ax, ay):
        return sum(v for x, y, v in values if math.hypot(x - ax, y - ay) <= FOCUS_RADIUS)
    scored = [(a, worth(*a)) for a in areas]
    best = max(w for _, w in scored)
    if best <= 0:
        return None
    focused = [a for a, w in scored if w >= FOCUS_SHARE * best]
    # Valuable spawns with no area of their own nearby are patrol points too: Mars' second R50 Custodian Commander
    # (53378, -48395) has a level label but no area centre, so the patrol never went there (2026-11-21).
    for x, y, _ in values:
        if all(math.hypot(x - ax, y - ay) > SPAWN_OWN_LABEL for ax, ay in focused) and worth(x, y) >= FOCUS_SHARE * best:
            focused.append((x, y))
    return focused


def is_squad_leader(name):
    """A Death Squad leader by name (the Custodians; see bot/danger.py)."""
    return "custodian" in (name or "").lower()


def death_squad_cells(display_name):
    """Coarse world cells on the Death Squad patrol routes of a planet (site overlay), or []."""
    info = planet_info(display_name)
    spot = info.get("spots", {}).get("DeathSquad") if info else None
    return [tuple(p) for p in spot["cells"]] if spot else []


def _words(text):
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", text or "").lower())


# Units that appear only when their spawner is attacked (user, 2026-11-19): Harvesters put down Interceptors, Mantis
# Menders and Imperial Repair Nodes Attack Drones. A blueprint that drops from them is farmed by attacking the spawner.
# (spawned unit, spawner, faction or None for any)
SPAWNED_BY = [("interceptor", "Harvester", None), ("attack drone", "Repair Node", "imperial"),
              ("attack drone", "Mender", "mantis")]


def spawners(names, known=None):
    """Spawner names for spawned units among `names` ("Mantis Interceptor" -> "Mantis Harvester", "Imperial Attack
    Drone" -> "Imperial Repair Node"). With `known` (the game's enemy names, normalized), a spawner the game does not
    have is left out."""
    out = []
    for name in names:
        low = name.lower()
        for spawned, spawner, faction in SPAWNED_BY:
            if spawned in low and (faction is None or low.startswith(faction)):
                candidate = re.sub(spawned, spawner, name, flags=re.I).strip()
                if (known is None or normalize(candidate) in known) and candidate not in out:
                    out.append(candidate)
    return out


def enemy_matches(unit_name, level, wanted_names, levels=None, exact=True):
    """True if an in-game enemy name is one of the wanted full names and the level fits.
    exact=False also accepts names that contain all words of a wanted name (site names the game lacks)."""
    if not unit_name:
        return False
    if levels and level is not None and not (levels[0] <= level <= levels[1]):
        return False
    name = _words(unit_name)
    for wanted in wanted_names:
        parts = _wanted_words(wanted)
        if len(parts) == len(name) and all(_word_fits(part, word) for part, word in zip(parts, name)):
            return True
        if not exact and parts and all(any(_word_fits(part, word) for word in name) for part in parts):
            return True
    return False


def _wanted_words(text):
    """Words of a site enemy name; a word cut short with a dot stays marked ("Custodian Comm." -> custodian, comm.)."""
    return re.findall(r"[a-z0-9]+\.?", unicodedata.normalize("NFKD", text or "").lower())


def _word_fits(part, word):
    # The site shortens names ("Custodian Comm." = the game's "Custodian Commander", 2026-11-20): a cut word matches
    # the words it starts.
    return word.startswith(part[:-1]) if part.endswith(".") else word == part
