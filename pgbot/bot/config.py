"""Bot settings. Distances are world units. Saved to %LOCALAPPDATA%\\pgbot\\settings.json and read on launch."""
import dataclasses
import json
import os
import pathlib
from dataclasses import dataclass, field

SETTINGS_FILE = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "settings.json"

# Modes (key, label, description). The description is shown under the selector.
MODES = [
    ("patrol", "Park & Patrol", "Farm around the start spot. Sweeps the open ground nearby when nothing shows up."),
    ("park", "Park", "Stay at the start spot and fight whatever comes into range. No patrol."),
    ("wander", "Wander", "Roam the whole planet and fight everything on the way."),
    ("seek_enemy", "Seek Enemy", "Hunt only the named enemies (full names), at their spawn areas when known. "
                                 "Attackers are still fought."),
    ("seek_blueprint", "Seek Blueprint", "Hunt the enemies that drop a blueprint, on its planet. Attackers are "
                                         "still fought."),
    ("bp_completion", "BP Completion", "Complete a star system's blueprints: hunts the enemies that drop the "
                                       "ones still missing, planet by planet. Progress is read in a hangar."),
    ("energy", "Collect Energy", "Fly between energy fields and collect orbs. Only attackers are fought."),
    ("fly", "Fly to Planet", "Fly to the chosen planet's orbit (through star gates if needed) and stop there."),
]
MODE_KEYS = [key for key, _, _ in MODES]
MODE_INPUTS = ("mode", "seek_enemies", "seek_blueprint", "fly_planet", "bp_system")  # saved with the settings


def _setting(default, label, group, unit="", minimum=None, maximum=None, step=None, percent=False, hint=""):
    """A user-facing setting: metadata for the settings tab lives on the dataclass field."""
    return field(default=default, metadata={
        "label": label, "group": group, "unit": unit, "min": minimum, "max": maximum, "step": step,
        "percent": percent, "hint": hint})


@dataclass
class Config:
    # Mode (set from the main tab).
    mode: str = "patrol"
    seek_enemies: str = ""          # comma separated enemy names for Seek Enemy
    seek_blueprint: str = ""        # blueprint name for Seek Blueprint
    fly_planet: str = ""            # planet name (site data) for Fly to Planet
    bp_system: str = ""             # star system for BP Completion (empty: the current one)

    # Learning (docs/LEARNING.md).
    learning: bool = _setting(True, "Learn while playing", "Learning",
                              hint="The bot remembers how fights, routes, loot, drones and skills went and gets "
                                   "better over time. See the Learned tab. Off: it plays only by these settings.")
    # Game start (docs/ROADMAP.md 8).
    game_folder: str = _setting("", "Game folder", "Game start",
                                hint="Only if the game is installed somewhere else. Empty: the usual folder "
                                     "(AppData\\Roaming\\Splitscreen Studios\\Pirate Galaxy).")
    start_game: bool = _setting(True, "Start the game when it is not running", "Game start",
                                hint="Start opens the game on desktop 2 and logs in with the account the game "
                                     "remembers (Remember me must be on).")
    restart_after_crash: bool = _setting(True, "Start the game again after a crash", "Game start",
                                         hint="At most 3 times an hour.")
    pilot_row: int = _setting(1, "Pilot to log in", "Game start", "", 1, 10, 1,
                              hint="Its row in the game's pilot list (1 = top).")
    # Area.
    farm_planet: str = _setting("", "Farm planet", "Area",
                                hint="Planet to farm (site name, e.g. Terasa); the bot travels there, through star "
                                     "gates if needed. Empty: the planet the bot starts on. Seek Blueprint picks its "
                                     "own planet.")
    leash_radius: float = _setting(20000.0, "Farm radius", "Area", "units", 3000, 80000, 1000,
                                   hint="How far from the start spot the bot farms and patrols.")
    patrol_after_s: float = _setting(15.0, "Patrol after", "Area", "s", 0, 600, 5,
                                     hint="Time without enemies before patrolling starts.")
    home_tolerance: float = _setting(2500.0, "Return to start beyond", "Area", "units", 500, 20000, 500,
                                     hint="Park: fly back to the start spot when further away than this.")
    # Combat.
    engage_range: float = _setting(25000.0, "Engage range", "Combat", "units", 2000, 60000, 1000,
                                   hint="Enemies further away than this are ignored.")
    attack_neutral: bool = _setting(True, "Attack neutral enemies", "Combat",
                                    hint="Off: only aggressive enemies and attackers are fought.")
    min_enemy_level: int = _setting(0, "Lowest enemy level", "Combat", "", 0, 120, 1,
                                    hint="Skip enemies below this level (0 = no limit). Attackers are always fought.")
    max_enemy_level: int = _setting(0, "Highest enemy level", "Combat", "", 0, 120, 1,
                                    hint="Skip enemies above this level (0 = no limit). Attackers are always fought.")
    use_skills: bool = _setting(True, "Use attack skills and buffs", "Combat",
                                hint="Off: only blaster, collector and repair (those are always used).")
    ignore_names: str = _setting("Harvester", "Ignore enemies", "Combat",
                                 hint="Comma separated; a name matches enemies containing all its words. In Park, "
                                      "Park & Patrol and Wander they are never attacked unless they attack. The "
                                      "other modes skip them unless they hunt them (e.g. Harvesters for an "
                                      "Interceptor blueprint).")
    pull_carefully: bool = _setting(True, "Pull enemies one at a time", "Combat",
                                    hint="Prefer enemies with few others around them, so only one or two get "
                                         "aggroed at once.")
    max_pull: int = _setting(2, "Max enemies to pull at once", "Combat", "", 1, 8, 1,
                             hint="How many enemies the bot may aggro at once, by attacking or by flying past them "
                                  "(neutral ones count: they join when the ship comes close). Higher gets through "
                                  "crowded areas; use 1 for a ship that is weak for the area.")
    skills_min_energy: float = _setting(0.1, "Offensive skills above energy", "Combat", "%", 0.0, 1.0, 0.05,
                                        percent=True,
                                        hint="Below this share of max energy attack skills, traps and utility "
                                             "buffs are not used. Repair and defensive skills (protector, shield) "
                                             "are always used when needed.")
    # Danger.
    avoid_dangers: bool = _setting(True, "Avoid dangerous enemies", "Danger",
                                   hint="Keep away from Death Squads and the enemies named below when they are "
                                        "actually present. Routes through them are still allowed if no other "
                                        "way exists.")
    danger_names: str = _setting("", "Dangerous enemies", "Danger",
                                 hint="Comma separated; a name matches enemies containing all its words. No fight is "
                                      "started on them (they are fought when they attack). Death Squads are found "
                                      "by themselves (leader, Resurrector and their group on a patrol route).")
    ship: str = _setting("", "Ship", "Ship",
                         hint="The ship the bot flies (your own name or the ship's name, as listed in the hangar). "
                              "Empty: the current one. If another ship is active, the bot switches in the nearest "
                              "hangar first.")
    farming_mode: str = _setting("auto", "Farming mode", "Danger",
                                 hint="auto (from the level gap to the enemies around), or lower / same / high to "
                                      "force that mode's caution. Lower: one enemy at a time, fights only near full "
                                      "hull, early retreat.")
    danger_radius: float = _setting(5000.0, "Keep away by", "Danger", "units", 2000, 20000, 500,
                                    hint="Distance kept from a dangerous group.")
    danger_tier_gap: int = _setting(1, "Ignore dangers with a ship this far ahead", "Danger", "systems", 1, 5, 1,
                                    hint="Example: a Mizar ship on a Gemini planet is 1 system ahead.")
    seek_ignores_danger: bool = _setting(True, "Seek modes may attack dangerous targets", "Danger",
                                         hint="Seek Enemy and Seek Blueprint still go for their targets inside a "
                                              "dangerous group.")
    # Health.
    retreat_below: float = _setting(0.3, "Retreat below hull", "Health", "%", 0.05, 0.9, 0.05, percent=True,
                                    hint="Fly away from attackers below this while the repair skill is not ready.")
    engage_min_hull: float = _setting(0.5, "Start new fights only above hull", "Health", "%", 0.0, 1.0, 0.05,
                                      percent=True,
                                      hint="Below this, with repair cooling down, only enemies attacking the ship "
                                           "are fought.")
    repair_below: float = _setting(0.45, "Repair below hull", "Health", "%", 0.05, 0.99, 0.05, percent=True,
                                   hint="Use the repair skill when the hull drops below this.")
    # Energy (shares of the maximum energy, which grows with the pilot level).
    refill_below: float = _setting(0.18, "Refill below energy", "Energy", "%", 0.0, 0.95, 0.01, percent=True,
                                   hint="Go collecting energy orbs below this share of max energy.")
    refill_until: float = _setting(1.0, "Refill up to energy", "Energy", "%", 0.1, 1.0, 0.05, percent=True,
                                   hint="Stop collecting at this share of max energy.")
    stop_below: float = _setting(0.03, "Pause below energy", "Energy", "%", 0.0, 0.5, 0.01, percent=True,
                                 hint="Below this the bot only collects energy (fighting back only); it pauses only if the planet has no known energy field.")
    orb_wait_s: float = _setting(120.0, "Wait at an empty energy field", "Energy", "s", 10, 900, 10,
                                 hint="Orbs respawn quickly; after this long the bot tries another field.")
    use_drones: bool = _setting(True, "Use drones tactically", "Drones",
                                hint="Turn each drone on only when its effect is needed (energy drone while "
                                     "collecting energy, cryonite drone while picking up cryonite, stat drones in "
                                     "hard fights) and recharge them out of combat.")
    drone_recharge_below: float = _setting(0.6, "Recharge drones below charge", "Drones", "%", 0.0, 0.95, 0.05,
                                           percent=True, hint="Recharge a drone when its charge falls below this.")
    drone_recharge_min_energy: float = _setting(0.4, "Recharge drones only above energy", "Drones", "%", 0.1, 1.0,
                                                0.05, percent=True,
                                                hint="Never recharge a drone if energy would fall below this share "
                                                     "of max energy afterwards.")
    energy_fallback: bool = _setting(True, "Refill on Terasa when out of energy", "Energy",
                                     hint="Landed low on energy with no energy field in the landing zone: fly to Terasa "
                                          "(Antares), refill in its landing zone and come back. Ignored if Terasa is "
                                          "not unlocked (the bot then stops in orbit).")
    refill_on_land: bool = _setting(True, "Refill to full on landing", "Energy",
                                    hint="After every landing (also after a rescue), if an energy field is near the "
                                         "landing spot, fill energy to 100% before farming.")
    hyperjump: bool = _setting(True, "Hyperjump while travelling", "Energy",
                               hint="Skip planet-to-planet flights with the travel panel's Hyperjump button when the "
                                    "jump leaves energy at or above 'Refill below energy'.")
    use_site_energy_fields: bool = _setting(True, "Use energy fields from the site data", "Energy",
                                            hint="Also fly to energy fields listed on the Hitchhiker's Guide maps.")
    # Loot.
    collect_loot: bool = _setting(True, "Collect drops", "Loot")
    loot_radius: float = _setting(7500.0, "Loot radius", "Loot", "units", 500, 20000, 500,
                                  hint="Drops further away than this are ignored.")
    give_up_s: float = _setting(30.0, "Give up a pickup after", "Loot", "s", 5, 120, 5)
    # Session.
    max_runtime_min: int = _setting(0, "Stop after", "Session", "min", 0, 1440, 15,
                                    hint="Stop the bot after this many minutes (0 = no limit).")
    stuck_s: float = _setting(90.0, "Warn without progress after", "Session", "s", 30, 900, 10,
                              hint="Pauses after three times this.")
    # Advanced.
    tick_s: float = _setting(0.2, "Update interval", "Advanced", "s", 0.05, 2.0, 0.05)
    move_hop: float = _setting(2200.0, "Move click distance", "Advanced", "units", 500, 6000, 100,
                               hint="How far ahead of the ship a move click lands.")
    settle_s: float = _setting(5.0, "Wait after zone change", "Advanced", "s", 1, 30, 1,
                               hint="The game ignores keys right after arriving.")
    zone_timeout_s: float = _setting(40.0, "Zone change timeout", "Advanced", "s", 10, 180, 5)

    # Fixed values (not shown).
    friendly_factions: tuple = (2, 7)  # colonials, miners (preservation notes 13)
    patrol_radius_fraction: float = 0.6
    patrol_points: int = 8
    collect_range: float = 550.0

    # Files --------------------------------------------------------------------------------------------------

    @classmethod
    def load(cls, path=SETTINGS_FILE):
        """Saved settings over the defaults. Unknown or broken values are ignored."""
        config = cls()
        try:
            saved = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return config
        for item in dataclasses.fields(cls):
            if item.name in saved and (item.metadata or item.name in MODE_INPUTS):
                value = saved[item.name]
                default = getattr(config, item.name)
                try:
                    value = type(default)(value)
                except (TypeError, ValueError):
                    continue
                low, high = item.metadata.get("min"), item.metadata.get("max")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and (
                        (low is not None and value < low) or (high is not None and value > high)):
                    continue  # out of range, e.g. an old absolute energy value where a share is expected now
                setattr(config, item.name, value)
        if config.mode not in MODE_KEYS:
            config.mode = "patrol"
        _apply_folder(config)
        return config

    def save(self, path=SETTINGS_FILE):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        values = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)
                  if f.metadata or f.name in MODE_INPUTS}
        path.write_text(json.dumps(values, indent=2), encoding="utf-8")
        _apply_folder(self)

def _apply_folder(config):
    from ..game import location
    location.set_folder(config.game_folder)


def settings():
    """The user-facing settings in display order: [(group, [fields])]."""
    groups = {}
    for item in dataclasses.fields(Config):
        if item.metadata:
            groups.setdefault(item.metadata["group"], []).append(item)
    return list(groups.items())
