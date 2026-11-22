"""Read the game's world state. Class and field names are the originals in client build 1002132.

Paths come from static analysis (docs/analysis-memory.md, docs/feasibility-autonomous.md) and need confirming
on the running game in phase 1.
"""
import math
from dataclasses import dataclass, field

from ..mem import java
from ..mem.jvm import JVM, TornRead

# Installed items sit under "active_item_" + the item's type id (client rk.if(add): add.Ev()), not a slot number:
# the Raven Controller has ids 1, 2, 4, 5, 31, 40, 52, 81 (2026-11-11). Ids up to ITEM_ID_LIMIT are looked up.
ITEM_ID_LIMIT = 256
ITEM_KEY = "active_item_{}"
STATUS_COOLDOWN = 3
SHIP_STATUS_LOADING = 4
# Object types seen only in one zone during the 2026-11-11 death test.
ZONE_MARKERS = (("com/splitscreen/abw", "hangar"), ("com/splitscreen/agn", "orbit"), ("com/splitscreen/ada", "orbit"),
                ("com/splitscreen/aiv", "starmap"))
UNIT_CLASS = "com/splitscreen/aeo"
STARBASE_MESSAGE_CLASS = "com/splitscreen/rm"
SKYBOX_CLASS = "com/splitscreen/qw"
STRIKE_CLASS = "com/splitscreen/rj"  # placed skill effects, e.g. Orbital Strike markers (2026-11-15)
MAX_REGISTRY_DEPTH = 8



# Landing zones that share another planet's orbit: zone id -> (orbit planet, card in the "Choose a zone" landing
# window). Earth in Unrest (user, 2026-11-16): landing in Earth's orbit opens the window, Earth left, it right.
SHARED_ORBIT = {"Sol3Invaded": ("Sol3", "right")}
SHARED_ORBIT_ZONES = {zone.lower(): zone for zone in SHARED_ORBIT}

@dataclass
class Unit:
    class_name: str
    marker: str | None
    name: str | None
    desc: str | None
    level: int | None
    faction: int | None
    hitpoints: float | None
    max_hitpoints: float | None
    hostile: bool
    aggression_range: float | None
    target_id: int | None
    is_player: bool
    exploded: bool
    is_item: bool
    value: int | None
    x: float
    y: float
    height: float | None = None
    distance: float = 0.0
    unit_id: int | None = None
    # Blueprint drops (client sz): "rare" = gold blueprint; "mesh" = item_blueprint[_decrypt][_rare|_cyan (clan)|
    # _ancient|_skull].model; "ancient" set for ancient ones.
    rare: bool = False
    ancient: bool = False
    mesh: str | None = None
    item_template: int | None = None  # blueprint drop: template id of its item ("item")
    item_name: str | None = None      # blueprint drop: item id ("itemname", e.g. $attackdroid.rare.draconis)


@dataclass
class Item:
    slot: int
    class_name: str
    name: str | None
    desc: str | None
    cooldown: float | None
    warmup: float | None
    active_time: float | None
    range: float | None
    status: int | None
    is_active: bool
    energy_cost: int | None
    last_use: int
    hotkey: str = ""  # "" = not on the action bar: never pressed
    on_enemy: bool = False
    on_self: bool = False
    on_item: bool = False
    offensive: bool = False
    template_id: int | None = None  # item template id (blueprint progress is kept per template id)
    # Repair items: hull points healed per second while active (property 791435772). A regeneration over the active
    # time, not one heal: $repair.86 has 44,406/s for 16 s on a 525,617-hull ship, 8.45%/s, matching the recorded
    # 8-9%/s (2026-11-16 play recordings); even a weak repair heals a ship from 1 to 100 when nothing hits it (user).
    heal_rate: float | None = None
    power: float | None = None      # "power" property (blaster: damage per shot; protector: shield points)

    @property
    def ready(self):
        """Status 3 means cooling down; 0 or unset (never used) means ready. Confirmed live 2026-11-11."""
        return self.status != STATUS_COOLDOWN


REPAIR_RATE_KEY = 791435772  # Java String.hashCode of the repair item's heal-per-second property (name unknown)


DRONE_BOOSTS = ("armorboost", "damageboost", "speedboost", "blueprintboost", "hitchanceboost", "criticalhitboost",
                "cryoniteboost", "rangeboost", "cooldownboost", "energyboost", "energycapboost")


@dataclass
class Drone:
    """One drone slot (client aai in the ship's droneContainer aah; the drone itself is aag). Read-only."""
    index: int
    desc: str | None
    active: bool
    charge: float           # energyStatus, 1 = full, 0 = empty (an empty drone stays active but has no effect)
    energy_factor: float    # totalEnergyFactor (recharge cost factor)
    boosts: dict            # boost name -> value, e.g. {"cryoniteboost": 0.1}

    def recharge_cost(self, rank_raw):
        """Client aag.char(): max(1000, 250 x rank) x 0.55 x totalEnergyFactor x (1 - energyStatus). Matched the
        Drone Setup window (6 energy for 0.99973 charge, factor 1.8), 2026-11-12."""
        return int(max(1000, 250 * (rank_raw or 0)) * 0.55 * self.energy_factor * (1 - self.charge))


@dataclass
class World:
    zone: str = "unknown"
    dead: bool = False
    travelling: bool = False
    travel_s: float | None = None  # player "travel": the trip's length in seconds, set at the jump
    current_planet: str | None = None
    pilot_level: int | None = None
    own_id: int | None = None
    xp: int | None = None
    heading: tuple | None = None
    planet_map: str | None = None
    energy: int | None = None
    energy_cap: int | None = None   # base cap from the rank; drones or boosts can raise the real cap
    rank_raw: int | None = None
    cryonite: int | None = None
    # Ship property "battle": the game's own combat state (the HUD panels turn to "alert" with it, atp$3); taking off is
    # refused while it is set. None when not read.
    in_battle: bool | None = None
    is_planet: bool = False
    at_starbase: bool = False       # orbit of a starbase (current_planet still names the last planet there)
    has_minimap: bool = False
    ship_class: str | None = None
    ship_name: str | None = None
    ship_type: str | None = None    # type id like $ship.lvl25damage (the base level is in the id)
    hitpoints: float | None = None
    max_hitpoints: float | None = None
    ship_level: int | None = None   # displayed level, armor upgrades included
    gcd_remaining: float = 0.0      # seconds until the global skill cooldown is over (0 when free)
    cooldown_factor: float = 1.0    # skill cooldown speed (Speed Actuator raises it; the global cooldown is 1.5 s / it)
    armor_level: int = 0
    ship_height: float | None = None
    x: float | None = None
    y: float | None = None
    selected_target: Unit | None = None
    minimap_zoom: float | None = None
    minimap_center: tuple | None = None
    units: list = field(default_factory=list)
    # Enemy Orbital Strike markers not yet gone off: (x, y). A Signor's strike puts down a marker (class rj, mesh
    # weapon_orbitalstrike_idle_red_FX.model, hitpoints 500 -> 0 when it goes off) and hits around it a few seconds
    # later (seen 2026-11-15 12:49:23-31, about 8 s). The player's own strike uses the mesh without "_red".
    strikes: list = field(default_factory=list)
    own_strikes: list = field(default_factory=list)  # the player's own Orbital Strike markers (mesh without "_red")
    items: list = field(default_factory=list)
    slots_known: bool = False  # hotkeys come from the ship's slot list in memory (else the old rule + learned keys)
    drones: list = field(default_factory=list)
    skybox: str | None = None  # the zone's skybox name, e.g. "draconis" (names the star system in orbits)
    orbit_place: str | None = None  # name from the orbit's message, e.g. "antaresgate" (orbits only)  # Drone per equipped slot, by index


class GameReader:
    def __init__(self, jvm: JVM):
        self.jvm = jvm
        self._unit_klasses = {}

    def _is_unit(self, oop):
        """True for objects whose class extends aeo (ships, NPCs, orbs, drops)."""
        jvm = self.jvm
        info = jvm.klass_of(oop)
        known = self._unit_klasses.get(info.address)
        if known is not None:
            return known
        base = jvm.find_class(UNIT_CLASS)
        current, result = info, False
        while current is not None:
            if base is not None and current.address == base.address:
                result = True
                break
            current = jvm.klass(current.super_address) if current.super_address else None
        self._unit_klasses[info.address] = result
        return result

    def read(self):
        """Return a World, or None while the client has no game screen. Raises SnapshotFailed on persistent errors."""
        return self.jvm.consistent(self._read)

    def _components(self, registry):
        """Return {class name: object} for a registry and its parents. Nearer registries win."""
        jvm = self.jvm
        result = {}
        current = registry
        for _ in range(MAX_REGISTRY_DEPTH):
            if not current:
                break
            for oop in java.tint_object_map(jvm, jvm.get(current, "components")).values():
                if oop:
                    result.setdefault(jvm.class_name(oop), oop)
            current = jvm.get(current, "parent")
        return result

    def _drones(self, ship_props):
        """The ship's drones: property "droneContainer" (aah) holds slot objects (aai: index, isActive, drone), the
        drone (aag) its charge and boosts. Found 2026-11-12; toggles show at once, a recharge sets energyStatus 1."""
        jvm = self.jvm
        drones = []
        try:
            container = ship_props.raw("droneContainer")
            if not container:
                return drones
            slots = java.Properties(jvm, jvm.get(container, "properties"))
            for oop in slots._values.values():
                if not oop or jvm.class_name(oop) != "com/splitscreen/aai":
                    continue
                slot = java.Properties(jvm, jvm.get(oop, "properties"))
                drone = slot.raw("drone")
                if not drone:
                    continue
                props = java.Properties(jvm, jvm.get(drone, "properties"))
                boosts = {k: float(props.get(k)) for k in DRONE_BOOSTS if k in props and props.get(k)}
                drones.append(Drone(int(slot.get("index") or 0), _text(props.get("desc")), slot.get("isActive") is True,
                                    float(props.get("energyStatus") or 0.0),
                                    float(props.get("totalEnergyFactor") or 1.0) if "totalEnergyFactor" in props
                                    else 1.0, boosts))
        except (KeyError, AttributeError, TypeError, ValueError):
            return []
        return sorted(drones, key=lambda d: d.index)

    def _global_cooldown(self, ship, world):
        """The ship's global skill cooldown (client akn on field aaa): every item use starts 1,500 ms / speed factor
        (akn.Lf, called from ako.do), during which no other item can be used; the end time is a py timer (Xa) on the
        game clock pw.WU in ms. Read-only; left at 0 if anything is missing."""
        jvm = self.jvm
        try:
            if not jvm.has_field(ship, "aaa"):
                return
            cooldowns = jvm.get(ship, "aaa")
            if not cooldowns:
                return
            world.cooldown_factor = float(jvm.get(cooldowns, "aFY") or 1.0)
            end = jvm.get(jvm.get(jvm.get(cooldowns, "aFW"), "Xa"), "value")
            clock = jvm.get_static("com/splitscreen/pw", "WU")
            world.gcd_remaining = max(0.0, (end - clock) / 1000.0)
        except (KeyError, AttributeError, TypeError):
            pass

    def _position(self, unit_oop):
        jvm = self.jvm
        voyager = jvm.get(unit_oop, "amE")
        if not voyager:
            return None, None
        position = jvm.get(voyager, "djD")
        if not position:
            return None, None
        return jvm.get(position, "bx"), jvm.get(position, "by")

    def _height(self, props):
        """Render height from the `worldposition` property (3D: bx = x, by = height, UQ = world y)."""
        point = props.raw("worldposition")
        return self.jvm.get(point, "by") if point else None

    def _unit(self, oop, markers, own_x, own_y):
        jvm = self.jvm
        props = java.Properties(jvm, jvm.get(oop, "properties"))
        x, y = self._position(oop)
        distance = math.hypot(x - own_x, y - own_y) if None not in (x, y, own_x, own_y) else math.inf
        marker = markers.get(oop, 0)
        return Unit(
            class_name=jvm.class_name(oop),
            marker=java.string(jvm, marker) if marker else None,
            name=_text(props.get("name")),
            desc=_text(props.get("desc")),
            level=_displayed_level(props.get("level")),
            faction=_number(props.get("faction")),
            hitpoints=_number(props.get("hitpoints")),
            max_hitpoints=_number(props.reference("hitpoints")),
            hostile=props.get("hostile") is True,
            aggression_range=_number(props.get("aggressionrange")),
            target_id=_number(props.get("currentTarget")),
            is_player="avatar" in props,
            exploded=props.get("exploded") is True,
            is_item=props.get("isitem") is True,
            value=_number(props.get("value")),
            x=x if x is not None else math.nan,
            y=y if y is not None else math.nan,
            height=self._height(props),
            distance=distance,
            unit_id=jvm.get(oop, "aae") if jvm.has_field(oop, "aae") else None,
            rare=props.get("rare") is True,
            ancient=props.get("ancient") is True,
            mesh=_text(props.get("mesh")),
            item_template=_number(props.get("item")) if props.get("isitem") is True else None,
            item_name=_text(props.get("itemname")),
        )

    def _items(self, ship_props):
        jvm = self.jvm
        items = []
        for slot in range(0, ITEM_ID_LIMIT):
            oop = ship_props.raw(ITEM_KEY.format(slot))
            if not oop:
                continue
            if not jvm.has_field(oop, "properties"):
                continue
            props = java.Properties(jvm, jvm.get(oop, "properties"))
            items.append(Item(
                slot=slot,
                class_name=jvm.class_name(oop),
                name=_text(props.get("name")),
                desc=_text(props.get("desc")),
                cooldown=_number(props.get("cooldown")),
                warmup=_number(props.get("warmup")),
                active_time=_number(props.get("activetime")),
                range=_number(props.get("range")),
                status=_number(props.get("status")),
                is_active=props.get("isActive") is True,
                energy_cost=_number(props.get("use")),
                last_use=jvm.get(oop, "aqo") if jvm.has_field(oop, "aqo") else 0,
                on_enemy=props.get("onenemy") is True,
                on_self=props.get("onself") is True,
                on_item=props.get("onitem") is True,
                offensive=props.get("offensive") is True,
                template_id=jvm.get(oop, "templateId") if jvm.has_field(oop, "templateId") else None,
                heal_rate=_number(java.unbox(jvm, props._values.get(REPAIR_RATE_KEY, 0))),
                power=_number(props.get("power")),
            ))
        # Hotkeys are fixed slots per ship; an empty slot does not shift the others (user, 2026-11-17: the Mantis Sol
        # Sniper has no Attack Droid, so its Orbital Strike is key 7, not 6). The item "id" is its type: 1, 2, 4, 5 are
        # the four slots every ship has (blaster, collector, repair, afterburner); a class adds a block of four types
        # (sniper 50-53: sniper blaster, attack droid, orbital strike, attack buff), slots 5-8 in that order. Ships that
        # mix classes (Item Slot Kits, the Raven) keep the order of their items.
        # The action bar is the ship's slot list (ship properties "slots.<item type>", empty slots included), in type
        # order: keys 1-8 (read live 2026-11-19: GC-1 Fusion Storm slots 1,2,3,4,5,10,12,13 = blaster, collector,
        # rocket, repair, afterburner, aim, perforator, thermoblast = keys 1-8; the hybrid 1,2,3,4,5,10,22,23).
        slots = [t for t in range(ITEM_ID_LIMIT) if f"slots.{t}" in ship_props]
        if slots:
            for item in items:
                item.hotkey = str(slots.index(item.slot) + 1) if item.slot in slots[:8] else ""
            self.slots_known = True
            return items
        self.slots_known = False
        common = {1: 1, 2: 2, 4: 3, 5: 4}
        blocks = {item.slot // 10 for item in items if item.slot not in common}
        by_rule = len(blocks) == 1 and all(item.slot in common or item.slot >= 10 for item in items)
        for index, item in enumerate(items[:8]):
            if by_rule:
                item.hotkey = str(common[item.slot] if item.slot in common else 5 + item.slot % 10)
            else:
                item.hotkey = str(index + 1)
        return items

    def _read(self):
        jvm = self.jvm
        try:
            registry = jvm.get_static("com/splitscreen/atp", "cL")
        except KeyError:
            return None
        if not registry:
            return None
        components = self._components(registry)
        session = components.get("com/splitscreen/gq")
        if not session:
            return None
        world = World()
        avatar = jvm.get(session, "avatar")
        if avatar:
            world.pilot_level = _displayed_level(java.Properties(jvm, jvm.get(avatar, "properties")).get("rank"))
        player_oop = jvm.get(session, "GI")
        flags = java.Properties(jvm, jvm.get(session, "GK"))
        world.is_planet = flags.get("isPlanet") is True
        world.has_minimap = flags.get("minimap") is True
        world.travelling = flags.get("ontravel") is True
        starmap = flags.get("starmapready") is True
        if not player_oop:
            return world
        player = java.Properties(jvm, player_oop)
        world.current_planet = _text(player.get("currentplanet"))
        world.xp = _number(player.get("xp"))
        world.travel_s = _number(player.get("travel"))
        world.energy = _number(player.get("currency.reload"))
        world.rank_raw = _number(player.get("rank"))
        if world.rank_raw is not None:
            # Client aju.this(): max(1000, 250 * rank) * (1 + best drone energy-cap bonus + energycapboost).
            # Confirmed by the user: 6,250 at level 26 (raw rank 25). Drone/boost bonuses are not read yet.
            world.energy_cap = max(1000, 250 * world.rank_raw)
        world.cryonite = _number(player.get("currency.buy"))
        ship = player.raw("ship")
        if ship:
            ship_props = java.Properties(jvm, jvm.get(ship, "properties"))
            world.ship_class = jvm.class_name(ship)
            world.ship_name = _text(ship_props.get("desc")) or _text(ship_props.get("name"))
            world.ship_type = _text(ship_props.get("desc"))
            world.hitpoints = _number(ship_props.get("hitpoints"))
            # Armor upgrades (client interface qy): armor level A = "armor"; the ship level is "armorshiplevel<A>"
            # when set, else "level"; the hull maximum is the base hitpoints x "armorfactor<A>" (1 without armor).
            # Raven Controller, armor 5: level 81 (shown 82), 6,458 x 81.39 = 525,617, live hull 525,606.
            armor = _number(ship_props.get("armor")) or 0
            world.armor_level = armor
            level = ship_props.get(f"armorshiplevel{armor}") if armor > 0 else None
            world.ship_level = _displayed_level(level if level is not None else ship_props.get("level"))
            factor = _number(ship_props.get(f"armorfactor{armor}")) if armor > 0 else None
            base = _number(ship_props.reference("hitpoints"))
            world.max_hitpoints = base * factor if base and factor else base
            world.dead = ship_props.get("down") is True or ship_props.get("exploded") is True
            battle = ship_props.get("battle")
            world.in_battle = battle if isinstance(battle, bool) else None
            self._global_cooldown(ship, world)
            world.drones = self._drones(ship_props)
            # Client aju.this(): the cap rises by the best energycapboost of the active, charged drones (on a planet).
            bonus = max((d.boosts.get("energycapboost", 0.0) for d in world.drones if d.active and d.charge > 0),
                        default=0.0)
            if world.energy_cap and bonus:
                world.energy_cap = int(world.energy_cap * (1 + bonus))
            if ship_props.get("status") == SHIP_STATUS_LOADING:
                world.zone = "loading"
            world.x, world.y = self._position(ship)
            world.own_id = jvm.get(ship, "aae") if jvm.has_field(ship, "aae") else None
            world.ship_height = self._height(ship_props)
            voyager = jvm.get(ship, "amE")
            heading = jvm.get(voyager, "djE") if voyager else 0
            if heading:
                world.heading = (jvm.get(heading, "bx"), jvm.get(heading, "by"))
            world.items = self._items(ship_props)
            world.slots_known = getattr(self, "slots_known", False)

        markers = {}
        minimap = components.get("com/splitscreen/ans")
        if minimap:
            markers = java.hashtable(jvm, jvm.get(minimap, "aLL"))
            world.minimap_zoom = jvm.get(minimap, "aLQ")
            center = jvm.get(minimap, "aLN")
            if center:
                world.minimap_center = (jvm.get(center, "bx"), jvm.get(center, "by"))
        # Every object the server published in this zone. The minimap's own list only holds what it is drawing.
        store = jvm.get(session, "GH")
        objects = java.array_list(jvm, jvm.get(store, "Jt")) if store else []
        world.units = []
        classes = set()
        for oop in objects:
            if not oop or oop == ship:
                continue
            class_name = jvm.class_name(oop)
            classes.add(class_name)
            if self._is_unit(oop) or class_name == STRIKE_CLASS:
                unit = self._unit(oop, markers, world.x, world.y)
                if math.isnan(unit.x) or math.isnan(unit.y):  # just published objects may have no position yet
                    continue
                if unit.mesh and "orbitalstrike_idle" in unit.mesh:
                    if unit.hitpoints is None or unit.hitpoints > 0:
                        (world.strikes if "_red" in unit.mesh else world.own_strikes).append((unit.x, unit.y))
                    continue
                world.units.append(unit)
            elif class_name == "com/splitscreen/ahd":
                world.planet_map = _text(java.Properties(jvm, jvm.get(oop, "properties")).get("map"))
            elif class_name == SKYBOX_CLASS and jvm.has_field(oop, "properties"):
                # The zone's skybox names the star system ("Skybox.draconis"): the surest sign of a gate arrival.
                props = java.Properties(jvm, jvm.get(oop, "properties"))
                for raw in props._values.values():
                    value = java.unbox(jvm, raw)
                    if isinstance(value, str) and value.startswith("Skybox."):
                        world.skybox = value[len("Skybox."):]
            elif class_name == STARBASE_MESSAGE_CLASS and jvm.has_field(oop, "properties"):
                # Starbase orbits carry an orbit message like "$geministarbase.orbit.msg" (seen 2026-11-11).
                props = java.Properties(jvm, jvm.get(oop, "properties"))
                for raw in props._values.values():
                    value = java.unbox(jvm, raw)
                    if isinstance(value, str) and "starbase" in value.lower():
                        world.at_starbase = True
                    if isinstance(value, str) and value.startswith("$") and not value.startswith("$chat"):
                        # The orbit's own name ("$antaresgate.orbit.msg", "$draconisgate"): right after a gate jump
                        # the player's current planet still names the old gate until the starmap opens (2026-11-12).
                        name = value[1:]
                        world.orbit_place = name[:-len(".orbit.msg")] if name.endswith(".orbit.msg") else name
        zone = _zone(world.is_planet, starmap, classes)
        if zone == "unknown" and world.orbit_place:
            # The Mizar starbase orbit has neither orbit marker class, only its orbit message ("$mizarstarbase"):
            # read as "unknown", the bot showed "Loading" for an hour after a rescue (2026-11-17 07:06-08:06).
            zone = "orbit"
        # Ship status 4 shows during zone loads but also stays set in the hangar, so a known zone wins.
        if zone != "unknown" or world.zone != "loading":
            world.zone = zone
        selected = flags.raw("selectedtarget")
        if selected:
            world.selected_target = self._unit(selected, markers, world.x, world.y)
        if world.zone == "planet" and world.current_planet and world.planet_map:
            # A zone sharing another planet's orbit (Earth in Unrest, 2026-11-16): the player's current planet still
            # names the orbit ("Sol3"), the surface's map names the zone ("sol3invaded.minimap.jpg").
            zone_id = SHARED_ORBIT_ZONES.get(world.planet_map.split(".")[0].lower())
            if zone_id and SHARED_ORBIT[zone_id][0] == world.current_planet:
                world.current_planet = zone_id
        return world


def _zone(is_planet, starmap, classes):
    """Zone from session flags and the zone's object types, as observed in the 2026-11-11 death test."""
    if is_planet:
        return "planet"
    if starmap:
        return "starmap"
    for marker, zone in ZONE_MARKERS:
        if marker in classes:
            return zone
    return "unknown"


def _displayed_level(raw):
    """The game stores levels and ranks zero-based and shows them plus one (confirmed by the user)."""
    number = _number(raw)
    return number + 1 if number is not None else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    return value if isinstance(value, str) else None


__all__ = ["GameReader", "Item", "TornRead", "Unit", "World"]
