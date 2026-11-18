"""Read images from the game's local data archives (ZIP files XORed with 0x55). Read-only.

Extracted files are cached outside the repo in %LOCALAPPDATA%\\pgbot\\cache.
"""
import io
import re
import os
import pathlib
import zipfile

from . import location

CACHE = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "cache"
XOR_TABLE = bytes(b ^ 0x55 for b in range(256))


def _open_archive(path):
    data = path.read_bytes().translate(XOR_TABLE)
    if data[:2] != b"PK":
        return None
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None


def find_asset(name):
    """Return the bytes of a game asset by file name (case-insensitive), or None. Results are cached on disk."""
    cached = CACHE / name.lower()
    if cached.exists():
        return cached.read_bytes()
    wanted = name.lower()
    data_dir = location.game_data()
    candidates = [data_dir / "core"] + sorted((data_dir / "files").iterdir())
    for path in candidates:
        if not path.is_file():
            continue
        archive = _open_archive(path)
        if archive is None:
            continue
        for info in archive.infolist():
            if info.filename.lower() == wanted:
                data = archive.read(info)
                CACHE.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(data)
                return data
    return None


_LANG = None


def display_name(internal):
    """English display name for a planet or place (e.g. Gemini3 -> Prosperous), from the game's language file."""
    global _LANG
    if internal is None:
        return None
    if _LANG is None:
        _LANG = {}
        lang_dir = location.game_data() / "lang"
        # lang_*.properties and lang1_*.properties: the second one has the newer ships and items (RavenDynamics).
        for path in sorted(lang_dir.glob("lang*_en.properties")):
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    # Java properties escapes: "V\\u00e9nar" in the file is Vénar (the starmap search never matched).
                    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value.strip())
                    _LANG.setdefault(key.strip().lower(), value)
    key = internal.lstrip("$").lower()
    return _LANG.get(f"{key}.name") or _LANG.get(key) or internal.lstrip("$")


def enemy_names():
    """All enemy display names from the language file ("Mantis Harvester", "Imperial Harvester", ...)."""
    display_name("")  # loads the language file
    names = set()
    for key, value in _LANG.items():
        if key.startswith("alien.") and (key.endswith(".name") or key.count(".") == 1):
            if len(value) > 3 and value[0].isalpha() and " " in value:
                names.add(value)
    return sorted(names)


# Map image coordinates (client class_1353): u = (world + 76800) / 153600 on both axes.
MAP_OFFSET = 76800.0
MAP_SPAN = 153600.0


def world_to_map(x, y, width, height):
    return (x + MAP_OFFSET) / MAP_SPAN * width, (y + MAP_OFFSET) / MAP_SPAN * height
