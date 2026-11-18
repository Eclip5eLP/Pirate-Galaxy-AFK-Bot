"""The player's ships, read from the game's memory (read-only).

Found 2026-11-17 (docs/NOTES.md), in a hangar:
- The hangar's ship list (Space -> HANGAR tab -> Ships) is a view object of class avw$38; its field ciI holds an nm whose
  field "value" is an array of rl objects, one per row, in the order shown on screen.
- An rl is a ship entry: property "name" is the ship type's name key ("$ship.raider.name", the true ship name, shown
  in the smaller second line), and the player's own name for the ship (the bigger first line; players can rename
  ships) is under a property whose name hashes to CUSTOM_NAME_KEY (the name itself is not known).
- The container with the blueprint progress also lists the inventory (PropertyMap field Ia -> pl.VW), ships included,
  but with type data only (owned_ships below).
"""
import struct

from ..mem import java
from . import assets
from .state import _displayed_level

SHIP_LIST_VIEW = "com/splitscreen/avw$38"
SHIP_ENTRY = "com/splitscreen/rl"
CUSTOM_NAME_KEY = 1294862529  # java hash of the custom-name property's (unknown) name


def ship_type(entry_name):
    """"$ship.raider.name" -> "$ship.raider" (the active ship's World.ship_type)."""
    return entry_name[:-len(".name")] if entry_name and entry_name.endswith(".name") else entry_name


def hangar_ship_list(jvm, scan):
    """[{"index", "type", "name", "custom"}] of the rows of the hangar's ship list, in screen order, or [] if the list
    is not in memory. scan(pattern, offset) -> object addresses (BlueprintBook._scan)."""
    view_class = jvm.find_class(SHIP_LIST_VIEW)
    if view_class is None:
        return []
    best = []
    for view in scan(struct.pack("<I", view_class.address), jvm._klass_ptr_at):
        try:
            if jvm.class_name(view) != SHIP_LIST_VIEW:
                continue
            rows = jvm.oop_array(jvm.get(jvm.get(view, "ciI"), "value"))
        except Exception:
            continue
        entries = []
        for index, row in enumerate(rows):
            try:
                if not row or jvm.class_name(row) != SHIP_ENTRY:
                    continue
                props = java.Properties(jvm, jvm.get(row, "properties"))
                raw = props._values.get(CUSTOM_NAME_KEY)
                custom = java.unbox(jvm, raw) if isinstance(raw, int) else raw
                kind = ship_type(props.get("name"))
                entries.append({"index": index, "type": kind, "name": assets.display_name(kind),
                                "custom": custom if isinstance(custom, str) else None})
            except Exception:
                continue
        if len(entries) > len(best):
            best = entries
    return best


def label(entry):
    """How a ship is shown in the settings: "<own name> (<true name>)", or the true name if not renamed."""
    custom, name = entry.get("custom"), entry.get("name")
    return f"{custom} ({name})" if custom and custom != name else name


def find(entries, wanted):
    """The entry the setting means: by label, own name, true name or type key (first match)."""
    wanted = (wanted or "").strip()
    if not wanted:
        return None
    for test in (lambda e: label(e) == wanted, lambda e: e.get("custom") == wanted,
                 lambda e: e.get("name") == wanted, lambda e: e.get("type") == wanted):
        hit = next((e for e in entries if test(e)), None)
        if hit:
            return hit
    return None


def owned_ships(jvm, container):
    """[{"desc", "name", "level" (displayed), "template_id"}] of the ships in the container's inventory (type data
    only: no own names, no armor)."""
    if not container:
        return []
    items = java.array_list(jvm, jvm.get(jvm.get(jvm.get(container, "properties"), "Ia"), "VW"))
    ships = []
    for item in items:
        try:
            props = java.Properties(jvm, jvm.get(item, "properties"))
        except Exception:
            continue
        desc = props.get("desc")
        if not isinstance(desc, str) or not desc.startswith("$ship."):
            continue
        armor = props.get("armor") or 0
        level = props.get(f"armorshiplevel{armor}") if armor and armor > 0 else None
        displayed = _displayed_level(level if level is not None else props.get("level"))
        ships.append({"desc": desc, "name": assets.display_name(desc), "level": displayed,
                      "template_id": jvm.get(item, "templateId") if jvm.has_field(item, "templateId") else None})
    ships.sort(key=lambda s: (s["level"] or 0))
    return ships
