"""Modes and their destinations (Farm, Fly, Seek Blueprint), landing; ship switching.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""

import time

from ..game import assets
from ..game.state import SHARED_ORBIT
from ..data import myzen
from .config import MODES
from ..game import ships
from .constants import *  # noqa: F401,F403
from .util import _load_state, _save_state, _same_place, StopBot


class ModesMixin:
    # Modes --------------------------------------------------------------------------------------------------

    def _is_home(self, planet, orbit=False):
        """True for the planet the bot should be on (start planet, or the Seek Blueprint destination). orbit: `planet`
        names an orbit or starmap entry, which for a zone sharing another planet's orbit (Earth in Unrest) is that
        planet's ("Sol3")."""
        home = self._home_zone()
        if orbit and home in SHARED_ORBIT:
            return planet == SHARED_ORBIT[home][0]
        if self.destination:
            return _same_place(planet, self.destination)
        return planet == self.home_planet

    def _home_zone(self):
        """Internal id of the home zone when it shares an orbit (from the destination's display name too), else the
        home planet."""
        if self.destination:
            wanted = myzen.normalize(self.destination)
            zones = list(SHARED_ORBIT) + [orbit for orbit, _ in SHARED_ORBIT.values()]
            return next((z for z in zones if myzen.normalize(assets.display_name(z)) == wanted), None)
        return self.home_planet

    def _land(self):
        """F6 lands; in an orbit shared by two zones the game first asks which ("Choose a zone"): click home's card,
        or the other one's when home is the plain planet (Earth itself)."""
        home = self._home_zone()
        card = SHARED_ORBIT[home][1] if home in SHARED_ORBIT else None
        if card is None and any(orbit == home for orbit, _ in SHARED_ORBIT.values()):
            card = "left"  # the plain planet (Earth) is the left card
        for attempt in range(3):
            self._key("F6")
            deadline = time.monotonic() + LANDING_WAIT_S
            clicked = False
            while time.monotonic() < deadline:
                time.sleep(0.8)
                world = self._read()
                if world is not None and world.zone in ("planet", "loading"):
                    self._wait(lambda w: w.zone == "planet", self.config.zone_timeout_s, "planet")
                    self.zone_since = time.monotonic()
                    return
                if card and not clicked and self._ui_shows("landing_choose_zone"):
                    self._log("info", f"Landing: choosing {self._home_name()} ({card} card)")
                    self._click(LANDING_CARD[card])
                    clicked = True
        raise self._fail("F6-no-planet", "F6 did not lead to the planet")

    @staticmethod
    def _is_starbase(world):
        """The ship is at (or its current planet names) a starbase, not a planet one can travel to."""
        return world.at_starbase or "starbase" in (world.current_planet or "").lower()

    def _home_name(self):
        return self.destination or assets.display_name(self.home_planet)

    def _mode_key(self):
        cfg = self.config
        return cfg.mode, cfg.seek_enemies, cfg.seek_blueprint, cfg.farm_planet, cfg.fly_planet, cfg.bp_system

    def _apply_mode(self, world):
        """Set up the selected mode when the run starts or the mode changes in the GUI."""
        cfg = self.config
        key = self._mode_key()
        if key == self.active_mode:
            return
        self.active_mode = key
        self.seek = None
        self.destination = None
        self.target_id = None
        self.patrol = None
        self.last_enemy_spot = None
        if cfg.mode in ("park", "patrol") and world.zone == "planet" and world.x is not None:
            # The farm spot is where the mode is chosen: the bot flew back across the map to its start spot when
            # switched to Park mid-run (user, 2026-11-11).
            self.patrol_centre = (world.x, world.y)
        label = dict((k, name) for k, name, _ in MODES).get(cfg.mode, cfg.mode)
        if cfg.mode == "seek_enemy":
            names = [n.strip() for n in cfg.seek_enemies.split(",") if n.strip()]
            if not names:
                self.active_mode = None
                raise StopBot("Seek Enemy: enter the enemy names first")
            self.seek = {"names": names, "levels": None, "exact": True}
            label += f": {', '.join(names)}"
        elif cfg.mode == "fly":
            self._setup_fly()
            label += f": {self.destination}"
            self._log("info", f"Mode: {label}")
            return
        elif cfg.mode == "bp_completion":
            self._setup_bp(world)
            return
        elif cfg.mode == "seek_blueprint":
            label += f": {cfg.seek_blueprint}"
            self._log("info", f"Mode: {label}")
            try:
                self._setup_blueprint(world)
            except StopBot:
                self.active_mode = None  # retry after the user fixes it and resumes
                raise
            return
        self._log("info", f"Mode: {label}")
        planet = cfg.farm_planet.strip()
        if planet:
            info = myzen.planet_info(planet)
            if info is None:
                self.active_mode = None
                raise StopBot(f"Farm planet '{planet}' is not in the site data (Settings, Update game data)")
            if info.get("system") in NO_GATE_SYSTEMS:
                self.active_mode = None
                raise StopBot(f"{planet} is in {info['system']}, which star gates do not reach: fly there yourself and "
                              f"start the bot on that planet")
            self.destination = info["label"]  # also when already there: it is what "home" means now
            if not _same_place(world.current_planet, info["label"]):
                self.patrol_centre = None
                self._log("info", f"Travelling to {info['label']} ({info.get('system')}) to farm there")

    def _setup_fly(self):
        """Fly to Planet: the planet from the mode input (site data name); Sirius is not reachable by gate."""
        name = self.config.fly_planet.strip()
        info = myzen.planet_info(name) if name else None
        if info is None:
            self.active_mode = None
            raise StopBot(f"Fly to Planet: '{name}' is not a known planet (pick one from the list)")
        if info.get("system") in NO_GATE_SYSTEMS:
            self.active_mode = None
            raise StopBot(f"{info['label']} is in {info['system']}, which star gates do not reach")
        self.destination = info["label"]
        self.patrol_centre = None

    def _setup_blueprint(self, world):
        cfg = self.config
        if myzen.load() is None:
            raise StopBot("Seek Blueprint needs the site data: Settings, Update game data")
        drops = myzen.blueprint_drops(cfg.seek_blueprint)
        if not drops:
            raise StopBot(f"No enemy drop known for '{cfg.seek_blueprint}'")
        here_name = assets.display_name(world.current_planet)
        here = myzen.normalize(here_name)
        system = self._system_of(world.current_planet)
        # Prefer drops on this planet, then on another planet of this star system, then anywhere (star gates).
        local = [d for d in drops if any(myzen.normalize(p) == here for p in d["planets"])]
        in_system = [d for d in drops if any(myzen.planet_system(p) == system for p in d["planets"]
                                             if myzen.planet_info(p))]
        anywhere = [d for d in drops if any(myzen.planet_info(p) and myzen.planet_system(p) not in NO_GATE_SYSTEMS
                                            for p in d["planets"])]
        chosen = local or in_system[:1] or anywhere[:1]
        if not chosen:
            raise StopBot(f"{cfg.seek_blueprint}: no planet with known site data drops it")
        names = sorted({n for d in chosen for n in myzen.full_enemy_names(d)})
        lows = [d["enemy_levels"][0] for d in chosen if d["enemy_levels"]]
        highs = [d["enemy_levels"][1] for d in chosen if d["enemy_levels"]]
        levels = (min(lows), max(highs)) if lows else None
        known = {myzen.normalize(n) for n in assets.enemy_names()}
        # Exact names where the game knows them; word matching for site names the game spells differently.
        self.seek = {"names": names, "levels": levels, "exact": all(myzen.normalize(n) in known for n in names)}
        where = ", ".join(chosen[0]["planets"]) or "unknown planet"
        level_text = (f"R{levels[0]}" + (f"-{levels[1]}" if levels[1] != levels[0] else "") + " ") if levels else ""
        self._log("info", f"{cfg.seek_blueprint} drops from {level_text}{' / '.join(names)} on {where}")
        if local:
            # Farm right here: this planet is home now (else the old home - Terasa - made the bot leave Mantis Hive
            # as "the wrong planet" right after landing, 2026-11-12 16:48).
            self.destination = here_name
            self.home_planet = world.current_planet
            return
        candidates = [p for p in chosen[0]["planets"] if myzen.planet_info(p)
                      and myzen.planet_system(p) not in NO_GATE_SYSTEMS]
        if not candidates:
            raise StopBot(f"{cfg.seek_blueprint} drops only in Sirius, which star gates do not reach: fly there "
                          f"yourself and start the bot on that planet")
        target = next((p for p in candidates if myzen.planet_system(p) == system), candidates[0])
        self.destination = target
        self.patrol_centre = None
        there = myzen.planet_system(target)
        via = f" (star gate {system} -> {there})" if system and there and there != system else ""
        self._log("info", f"Travelling to {target} for this blueprint{via}")

    # Ship ---------------------------------------------------------------------------------------------------

    def _ship_wanted(self, world):
        """True if the Ship setting names a ship other than the active one (or one not seen yet in a hangar list)."""
        wanted = (self.config.ship or "").strip()
        if not wanted or self.ship_switch_failed == wanted or not world.ship_type:
            return False
        known = self.ship_list or _load_state().get("owned_ships") or []
        entry = ships.find(known, wanted)
        if entry is None:
            return not self.ship_list  # read the hangar list first
        return entry["type"] != world.ship_type

    def _ship_list(self):
        if self.blueprints is None:
            return []
        book = self.blueprints
        return ships.hangar_ship_list(self.jvm, lambda pattern, offset: book._scan(self.jvm, pattern, offset))

    def _switch_ship(self, world):
        """In a hangar: open the ship list, scroll to the wanted ship's row and click it; checked in memory."""
        wanted = self.config.ship.strip()
        self.status = f"Switching to {wanted}"
        if not self._ui_shows("hangar_tab_active"):
            if not self._hangar_panel_open():
                self._key("SPACE")
                time.sleep(1.5)
            self._click_tab("hangar", HANGAR_TAB)
            time.sleep(1.0)
        self._click(HANGAR_SHIPS)
        time.sleep(1.0)
        entries = self._ship_list()
        if not entries:
            time.sleep(1.5)
            entries = self._ship_list()
        self.ship_list = entries
        if entries:
            _save_state({"owned_ships": entries})
        entry = ships.find(entries, wanted)
        if entry is None:
            self.ship_switch_failed = wanted
            self._log("warning", f"Ship '{wanted}' is not in the hangar's ship list; flying the current one")
            self._key("SPACE")
            return
        if entry["type"] == world.ship_type:
            self._key("SPACE")
            return
        count, index = len(entries), entry["index"]
        visible = SHIP_ROWS_VISIBLE
        span = max(1, count - visible)
        top, bottom = SHIP_TRACK
        length = bottom - top
        thumb = max(20.0, length * visible / count) if count > visible else length
        x = self._point(("right", SHIP_SCROLL_X, "top", 0))[0]
        aim = min(max(index - 3, 0), span) if count > visible else 0
        for attempt in range(SHIP_SWITCH_TRIES):
            if count > visible:
                # To the top (pages on the track above the thumb), then the thumb down to the wanted offset.
                for _ in range(count // visible + 2):
                    self._click(("right", SHIP_SCROLL_X, "top", top + 12))
                    time.sleep(0.15)
                start = top + thumb / 2
                self.input.drag(x, start, x, start + (length - thumb) * aim / span, "left")
                time.sleep(0.6)
            row = index - aim
            if 0 <= row < visible + 1:
                base = self._point(SHIP_ROW)
                self.input.hover(base[0], base[1] + row * SHIP_ROW_STEP)
                time.sleep(UI_HOVER_S)
                self.input.click(base[0], base[1] + row * SHIP_ROW_STEP, "left", hold_s=UI_HOLD_S)
            time.sleep(1.5)
            now_world = self._read()
            active = now_world.ship_type if now_world else None
            if active == entry["type"]:
                self._log("info", f"Switched to {ships.label(entry)}")
                self._key("SPACE")
                return
            # Which row was hit tells where the list really stands: correct the aim by the difference.
            hit = [e["index"] for e in entries if e["type"] == active]
            if hit:
                clicked = min(hit, key=lambda i: abs(i - index))
                aim = min(max(aim + (clicked - index), 0), span)
            self._debug(f"ship switch: wanted row {index}, got {active}; aiming at offset {aim}")
        self.ship_switch_failed = wanted
        self._log("warning", f"Could not switch to {ships.label(entry)}; flying the current ship")
        self._key("SPACE")
