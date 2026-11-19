"""The farming bot. Reads the game from memory and acts through background input only.

Behaviour:
- Modes (config.MODES): Park & Patrol around the start spot, Park, Wander, Seek Enemy, Seek Blueprint, Collect Energy.
- Fights enemies, those attacking the ship first.
- Collects drops (cryonite, blueprints, other items) with the collector.
- Refills energy at energy orbs, using orb spots remembered per planet.
- Repairs with the repair item at low hull.
- After being shot down: rescue to starbase, repair in the hangar, starmap travel back, land, return to the spot.
Every action is checked in memory; repeated failures pause the bot with a warning.
"""
import math
import queue
import threading
import time
import traceback

from ..game import assets, location
from ..game.blueprints import BlueprintBook
from ..game.state import GameReader
from ..data import myzen
from ..gui.monitor import LogEvent
from ..input import awake, window
from ..input.background import BackgroundInput, GameWindowGone
from ..input.vdesktop import VirtualDesktops
from ..mem.jvm import JVM, SnapshotFailed
from ..mem.process import MemoryReadError, Process
from .chatsend import ChatSender
from .config import Config
from .orbs import OrbMemory
from .squadtrack import SquadTracks
from .gamestart import GameStarter, StartFailed
from .constants import *  # noqa: F401,F403
from .util import _load_state, _save_state, StopBot
from .camera import CameraMixin
from .actions import ActionsMixin
from .movement import MovementMixin
from .modes import ModesMixin
from .bpcompletion import BPCompletionMixin
from .danger import DangerMixin
from .learning import LearningMixin
from .evasion import EvasionMixin
from .farm import FarmMixin
from .fight import FightMixin
from .loot import LootMixin
from .drones import DronesMixin
from .travel import TravelMixin


class Bot(
    CameraMixin, ActionsMixin, MovementMixin, ModesMixin, BPCompletionMixin, DangerMixin, LearningMixin,
    EvasionMixin, FarmMixin, FightMixin, LootMixin, DronesMixin, TravelMixin,
):
    def __init__(self, config=None):
        self.config = config or Config.load()
        self._status_hold = None  # status text kept while waiting for a far attacker (see _fight)
        self.running = False
        self.paused = False
        self._status = "Stopped"
        self._debug_file = None
        self.route = None
        self.route_check = lambda x, y: False  # set each farm tick: point on a Death Squad route while one is about
        self.target_id = None  # enemy being fought
        self.saved_planet = None  # last planet written to the state file
        self.enemy_blocked = False  # the last move call found only ways through enemies (they are cleared first)
        self.progress = None  # [goal, best distance, time of best] of the current move
        self.progress_touch = 0.0
        self.last_blocked_note = 0.0
        self.path_blockers = {}
        self.blocked_since = {}  # move goal -> first time it was found blocked by enemies
        self.last_fight_time = 0.0  # last tick spent in range of a target, firing
        self.leaving = None         # status text while taking off for another planet, else None
        self.leave_fight_at = 0.0   # last time something attacked the ship while leaving
        self.take_off_at = None     # last F5 while leaving
        self.stop_in_orbit = False  # "Stop in orbit": finish the fight and loot, take off, stop in orbit
        self.reland = False         # out of energy, no field close: to orbit and land again (landing zone check)
        self.energy_trip = None     # refill trip to Terasa: what to go back to (destination, home planet)
        self.cast_until = 0.0  # no skill presses before this (another skill is warming up)
        self.repair_cast = None  # (hotkey, time pressed) until the repair shows as used or REPAIR_CAST_S pass
        self.repair_tries = []  # times of repair presses that have not shown as used yet
        self.select_times = {}  # unit id -> time of the last select click
        self.weapon_range = 3000.0  # blaster range, updated every farm tick
        self.trap = None  # Magnet Trap item, if the ship has one
        self.skill_pending = {}  # hotkey -> time of a press not yet seen to take
        self.weapon_hold = 0.0  # no blaster shot before this (an attack skill is firing)
        self.last_any_skill = -math.inf  # time of the last skill press of any kind
        self.at_field = False  # at an energy field and collecting there (last tick); for smart healing
        self.drone_wanted_at = {}  # drone index -> last time its effect was needed
        self.drone_toggled_at = {}  # drone index -> last toggle click
        self.drone_toggle_fails = 0
        self.drone_toggle_want = {}  # drone index -> state the last click asked for, until memory shows it
        self.drones_paused_until = 0.0  # something looked wrong: no drone clicks before this
        self.drone_backoff = DRONE_BACKOFF_S
        self.drone_recharge_at = {}  # drone index -> last recharge attempt
        self.cryonite_pickup_until = 0.0
        self.gate_starbases = set(_load_state().get("gate_starbases", []))  # systems whose starbase is the gate
        self.zoom_direction = _load_state().get("zoom_direction")  # wheel notch sign that zooms out (learned)
        self.no_click_since = None  # since when no move click could be placed (camera facing the way)
        self.just_landed = False  # arrived on the planet; the next farm tick decides on refilling
        self.land_refill = False  # refilling to 100% after landing (ignores "Refill up to energy")
        self.field_heal_since = None  # energy full, waiting at the field for repair (lower / same level)
        self.collect_progress = None  # (drop key, best distance, time of best) of the current pickup
        self.gold_progress = None  # (drop key, best distance, time of best) of the current gold blueprint run
        self.attacked_at = {}  # unit id -> last time it aimed at the ship
        self.gcd_until = 0.0  # no skill other than the blaster before this (global cooldown)
        self.cooldown_factor = 1.0
        self.defensive = []  # defensive buffs of the ship this tick (protector, shield, ...)
        self.offensive_ok = True  # energy allows offensive skills this tick
        self.repair_wanted = False  # this tick: repair is due, other skills wait
        self.fire_spot = None  # (target id, spot, target position when chosen)
        self.refill_mode = False  # refilling energy this tick
        self.blueprints = None  # BlueprintBook: completed blueprints (read from memory in the background)
        self.blueprints_started = False
        self.energy_bp_logged = set()
        self.energy_critical = False  # energy below CRITICAL_ENERGY this tick
        cfg = self.config
        # Caution profile (see _update_caution); the settings themselves until enemies have been seen.
        self.caution = "same"
        self.level_samples = []  # (time, enemy level) of enemies near the ship
        self.bp = None  # BP Completion state (see _bp_tick)
        self.bp_hangar_wanted = False  # BP Completion: visit a hangar to read the blueprint progress
        self.last_terrain_zoom = -math.inf  # last zoom-out for a camera in terrain
        self.speed_track = []  # (time, x, y, afterburner active) for the afterburner gain
        self.speed_normal = []  # recent normal speeds (u/s)
        self.speed_boosted = []  # recent afterburner speeds (u/s)
        saved = _load_state().get("afterburner_gain") or {}
        self.ab_gain = saved.get("gain")  # boosted / normal top speed; None until measured
        self.ab_gain_hull = saved.get("hull") if isinstance(saved.get("hull"), list) else None  # [hull, afterburner]
        if self.ab_gain_hull is None:
            self.ab_gain = None
        self.eff_max_pull = cfg.max_pull
        self.eff_retreat_below = cfg.retreat_below
        self.eff_engage_min = cfg.engage_min_hull
        self.eff_outnumbered = (3, OUTNUMBERED_RETREAT_HULL)
        self.skill_fails = {}  # hotkey -> presses in a row that did not take
        self.skill_paused = {}  # hotkey -> paused until
        self.path_blockers = {}  # enemy id -> time it was found blocking every way to a destination
        self.gold_logged = set()
        self.last_range_note = 0.0
        self.learner = None  # Learner while "Learn while playing" is on (docs/LEARNING.md)
        self.learn_failed = False
        self.learn_route_at = {}
        self.learn_notes = {}
        self.learn_verdicts = {}
        self.last_press = -math.inf
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self.tick_at = None  # watchdog: last loop tick or bot-thread log line
        self._guard_thread = None
        self.last_zone = None
        # Kept for the bot's lifetime (not reset per run). These used to be created on first use.
        self.input = None  # BackgroundInput while a game runs
        self.chat_queue = []  # chat messages from the remote page (entries kept by the GUI), sent at a calm moment
        self.travel_seen = None  # (monotonic time the trip was first seen, trip seconds) while travelling
        self.hyperjump_tries, self.hyperjump_next = 0, 0.0  # at most two hyperjump tries per trip
        self.patrol_centre = None  # (x, y) the patrol circles around on the current planet; None until landed
        self.start_destination = None
        self.read_errors = []  # times of recent memory read errors
        self._pitch_rate = None  # measured camera pitch per pixel of a vertical drag
        self._last_zoom_distance = None
        self._zoom_warned = None  # zone_since of the last "cannot zoom out" warning
        self.press_ready = {}  # hotkey -> {item desc: ready} at the press
        self.last_items = []
        self.key_crossed = {}
        self.ship_type_now = None
        self.hotkey_tried = {}
        self._hotkey_cache = None
        self.stuck_shot_at = -1e9
        self.no_new_fights = False
        self.travel_to = None
        self.bp_hangar_target = None  # BP Completion: the place whose hangar is being flown to
        self.bp_hangars_tried = set()
        self.bp_fallbacks_tried = set()
        self.bp_hangar_since = None  # when the current hangar read started
        self.bp_copies_before = set()  # container copies in memory when the hangar panel was opened
        self.bp_fresh_wait = None  # since when the orbit waits for the server's copy (after a hangar visit)
        self.ship_switch_failed = None
        self.ship_list = None  # hangar ship list, once read
        self._forced_logged = False
        self.enemy_tracks = {}
        self.member_last_velocity = {}
        self._getaway_note = 0.0
        self._spawn_zone = None
        self.falling_seen = {}
        self._falling_note = 0.0
        self._squad_cells = {}
        self._spawn_cache = None
        self.dodge_spot = None
        self._low_energy_note = 0.0
        self.far_given_up = {}  # unit id -> ignored until
        self.far_wait = {}
        self.advice_won_since = None
        self.patrol_blocked_at = -1e9
        self.select_fail_hull = 0
        self.energy_pickup_until = 0.0
        self.unit_tracks = {}
        self.last_skill_press = -math.inf
        self.last_hit = -1e9
        self.implied_attackers = {}  # unit id -> counted as attacking until
        self.collect_motion = None
        self.select_tries = {}
        self.collect_tick = None    # (drop key, time) of the last _collect call: timers restart after a gap
        self.collect_active = None  # (drop key, since): the collector beam is pulling this drop in
        self.pickup_fails = {}      # drop key -> times given up (each retry waits longer)
        self.starmap_seen = 0
        self._reset_run_state()

    # Controls (called from the GUI thread) ------------------------------------------------------------------

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.paused = False
        self.stop_in_orbit = False
        self.reland = False
        self.energy_trip = None
        self.running = True
        self.status = "Starting"
        self._thread = threading.Thread(target=self._run, name="bot", daemon=True)
        self._thread.start()
        self.tick_at = None  # set by the loop's first tick (starting the game takes longer than WATCHDOG_S)
        threading.Thread(target=self._watchdog, name="bot-watchdog", daemon=True).start()

    def stop(self):
        self._stop.set()
        self.running = False
        self.status = "Stopped"
        try:
            self.squad_tracks.save()
        except Exception:
            pass

    def set_stop_in_orbit(self, on):
        """Stop once the ship is in orbit: on a planet the fight and the loot near the ship are finished first, then it
        takes off when out of combat (user, 2026-11-14)."""
        if not self.running or on == self.stop_in_orbit:
            return
        self.stop_in_orbit = on
        self._log("info", "Stopping in orbit: finishing up, then taking off" if on else "Stop in orbit cancelled")

    def toggle_pause(self):
        if self.running:
            self.paused = not self.paused
            self.status = "Paused" if self.paused else "Resuming"

    def drain_events(self):
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        hold = self._status_hold
        if hold is not None:
            value = hold
        if value != self._status:
            self._debug(f"status: {value}")
        self._status = value

    def _debug(self, text):
        """Detailed run log for analysis (logs/ is gitignored)."""
        if self.tick_at is not None and threading.current_thread() is self._thread:
            # A long step that keeps acting (the starmap search for the gate clicks row after row: 34 s, 15:03:16)
            # is not stuck; only a loop that neither ticks nor logs is.
            self.tick_at = time.monotonic()
        try:
            if self._debug_file is None:
                LOG_DIR.mkdir(exist_ok=True)
                self._debug_file = open(LOG_DIR / f"bot-{time.strftime('%Y%m%d-%H%M%S')}.log", "a", encoding="utf-8")
            self._debug_file.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            self._debug_file.flush()
        except OSError:
            pass

    def _log(self, level, text):
        self._debug(f"[{level}] {text}")
        self._events.put(LogEvent(level, text))

    # Thread -------------------------------------------------------------------------------------------------

    def _watchdog(self):
        """Log every thread's stack when the bot loop has neither finished a tick nor logged anything for WATCHDOG_S (2026-11-19: the loop
        hung twice on Technatoria right after a select click in a fight, 13:57 and 14:24; under Wine the threads
        show nothing). Only logs; once per hang."""
        import sys
        dumped = False
        while not self._stop.is_set():
            self._stop.wait(5.0)
            if self.tick_at is None or self.paused:
                dumped = False
                continue
            stuck = time.monotonic() - self.tick_at
            if stuck < WATCHDOG_S:
                dumped = False
                continue
            if dumped:
                continue
            dumped = True
            names = {t.ident: t.name for t in threading.enumerate()}
            lines = [f"watchdog: no loop tick for {stuck:.0f} s; thread stacks:"]
            for ident, frame in sys._current_frames().items():
                lines.append(f"--- thread {names.get(ident, ident)}")
                lines += [l.rstrip() for l in traceback.format_stack(frame)[-12:]]
            self._log("error", "Bot loop stuck (stacks in the debug log)")
            self._debug(chr(10).join(lines))

    def _run(self):
        restarts = []
        if not awake.keep_display_on():  # the game freezes when Windows turns the display off (pgbot/input/awake.py)
            self._log("warning", "Windows did not accept the keep-display-on request; the game may freeze when "
                                 "the display turns off")
        try:
            while not self._stop.is_set():
                pid = self._game_or_start()
                if pid is None:
                    return
                try:
                    self._run_game(pid)
                    return
                except (MemoryReadError, OSError) as error:
                    if pid in location.game_pids():  # gone: "The game closed; starting it again" below says it
                        self._log("error", f"Lost the game: {error}")
                if not (self.config.start_game and self.config.restart_after_crash) or self._stop.is_set():
                    return
                # The client crashes by itself after hours (2026-11-14 18:19, Direct3D 9): start it again.
                now = time.monotonic()
                restarts = [t for t in restarts if now - t < 3600] + [now]
                if len(restarts) > MAX_RESTARTS_PER_HOUR:
                    self._log("error", f"The game closed {len(restarts)} times within an hour; not starting it again")
                    return
                if not self._wait_game_gone(60):
                    self._log("error", "The game stopped responding but is still open; not starting a second one")
                    return
                if self.blueprints is not None:
                    self.blueprints.stop()
                    self.blueprints = None
                self._log("warning", "The game closed; starting it again")
        except (MemoryReadError, OSError) as error:
            self._log("error", f"Lost the game: {error}")
        except Exception as error:  # keep the GUI alive and show what went wrong
            self._log("error", f"Bot crashed: {error}")
            traceback.print_exc()
        finally:
            awake.allow_display_off()
            self.running = False
            self.status = "Stopped"
            self._stop.set()
            if self.blueprints is not None:
                self.blueprints.stop()
            try:
                self.orbs.save()
            except Exception:
                pass
            if self.learner is not None:
                try:
                    self.learner.store.save(force=True)
                except Exception:
                    pass

    def _run_game(self, pid):
        self.pid = pid
        with Process(self.pid) as process:
            self.jvm = JVM(process)
            self.reader = GameReader(self.jvm)
            self.input = BackgroundInput(self.pid)
            self.desktops = VirtualDesktops()
            self.orbs = OrbMemory()
            # Its own read-only handle: the bot's reads must never share state with the background thread.
            self.blueprints = BlueprintBook(Process(self.pid), log=self._debug, owns_process=True)
            # A new book for each game run, so it must be started again: after the game crash of 2026-11-19 04:21 the
            # flag was still set from the first run, the new book never started, the catalogue never came and BP
            # Completion waited in the hangar until the watchdog paused the bot (04:31).
            self.blueprints_started = False
            if self._guard_thread is None or not self._guard_thread.is_alive():
                self._guard_thread = threading.Thread(target=self._guard, name="window-guard", daemon=True)
                self._guard_thread.start()
            try:
                self._setup()
                self._loop()
            finally:
                self.input.release()  # the game is told it is not active again

    def _game_or_start(self):
        """The running game's process id; else the game is started and logged in (setting), else None."""
        pids = location.game_pids()
        if len(pids) == 1:
            return pids[0]
        if len(pids) > 1:
            client = GameStarter._game_pid()
            if client is not None:
                return client
            self._log("error", "Several game processes and none with a game window; bot not started")
            return None
        if location.problem():
            self._log("error", f"Game not running, and {location.problem()[0].lower()}{location.problem()[1:]}")
            return None
        if not self.config.start_game:
            self._log("error", "Game not found; bot not started")
            return None
        self.status = "Starting the game"
        readers = {}

        def read_world(pid):
            try:
                if pid not in readers:
                    process = Process(pid)
                    readers[pid] = (process, GameReader(JVM(process)))
                return readers[pid][1].read()
            except Exception:
                return None

        try:
            return GameStarter(self._log, self._debug, self._stop, read_world, self.config.pilot_row).start()
        except StartFailed as error:
            if str(error) != "stopped":
                self._log("error", f"Could not start the game: {error}")
            return None
        finally:
            for process, _ in readers.values():
                try:
                    process.close()
                except Exception:
                    pass

    def _wait_game_gone(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self._stop.is_set():
            if not location.game_pids():
                return True
            self._stop.wait(1.0)
        return not location.game_pids()

    def _guard(self):
        """Keep the game behind the user's windows if it is on the user's desktop and raises itself."""
        desktops = VirtualDesktops()
        # The game taking the foreground while the user works elsewhere is logged with the bot's last input, to find
        # the cause if it still happens (user, 2026-11-14: the view jumped to desktop 2 during farming; clicks are now
        # sent while the game is marked active, see input/background.py).
        was_foreground = False
        while not self._stop.is_set():
            game = window.game_window(self.pid)
            foreground = window.foreground_hwnd()
            is_foreground = bool(game) and foreground == game.hwnd
            if is_foreground and not was_foreground:
                kind, at = self.input.last_action if self.input is not None else ("none", 0.0)
                self._debug(f"the game window came to the front (last bot input: {kind}, "
                            f"{time.monotonic() - at:.2f} s before)")
            if was_foreground and not is_foreground:
                # The user was in the game and left: Windows deactivated it, so the next input must mark it active
                # again. Missed when it happened between two bot inputs: keys and clicks stopped reaching the game,
                # the ship stood still with repair "ready" and died, and the rescue failed (2026-11-15 02:34).
                inp = self.input
                if inp is not None:
                    inp._marked = None
            was_foreground = is_foreground
            if game and foreground and foreground != game.hwnd:
                try:
                    on_current = desktops.is_on_current(game.hwnd)
                except OSError:
                    on_current = True
                if on_current and window.is_above(game.hwnd, foreground):
                    window.send_to_bottom(game.hwnd)
            self._stop.wait(0.03)

    def _start_bp(self, world):
        """Set up BP Completion off a planet; False (and paused) when it cannot start."""
        try:
            self.active_mode = self._mode_key()
            self._setup_bp(world)
            return True
        except StopBot as reason:
            self._log("error", f"Paused: {reason}")
            self.paused = True
            return False

    def _setup(self):
        world = self._read()
        if world is None or not world.current_planet or (world.zone == "planet" and world.x is None):
            self._log("error", f"Start the bot in the game (now: {world.zone if world else 'no game data'})")
            self._stop.set()
            return
        self.home_planet = world.current_planet
        place = (self._location(world) or "").lower()
        if world.zone != "planet" and (self._is_starbase(world) or "gate" in place or "gate" in
                                       (world.current_planet or "").lower()):
            # After a rescue the current "planet" is the starbase (DraconisStarbase = Woe Fields), which is no travel
            # destination: the bot paused with "Woe Fields not found in the destination list" (2026-11-12). Go back
            # to the planet farmed last instead. The same at a gate (after a jump the current planet still names
            # the old gate: the bot set off back to the Antares gate, 2026-11-12).
            last = _load_state().get("last_planet")
            if last:
                self.home_planet = last
        farm = myzen.planet_info(self.config.farm_planet.strip()) if self.config.farm_planet.strip() else None
        self.start_destination = farm["label"] if farm and self.config.mode not in ("seek_blueprint", "fly", "bp_completion") \
            else None
        if self.config.mode == "fly":
            fly = myzen.planet_info(self.config.fly_planet.strip())
            self.start_destination = fly["label"] if fly else None
        if world.zone == "planet":
            self.patrol_centre = (world.x, world.y)
        else:
            # Started in a hangar, orbit or the starmap: go to the last planet and farm where the ship lands.
            self.patrol_centre = None
            target = self.start_destination or assets.display_name(self.home_planet)
            if self.config.mode not in ("seek_blueprint", "bp_completion"):  # they name their planet below
                self._log("info", f"Not on a planet; flying to {target} first")
        self.last_zone = world.zone
        self._reset_run_state()
        try:
            game = window.game_window(self.pid)
            if game and self.desktops.is_on_current(game.hwnd):
                self._log("warning", "The game is on your current desktop. Move it to another desktop (Win+Tab) "
                                     "to avoid flashes.")
        except OSError:
            pass
        if self.config.mode == "bp_completion" and (world.zone != "planet" or world.dead):
            # Started off a planet, or shot down (the rescue goes to a hangar, not through the planet's mode setup):
            # set up now (hangar visit or the plan's planet).
            self._start_bp(world)
        if self.config.mode == "seek_blueprint" and world.zone != "planet":
            # Seek Blueprint started off a planet: choose its planet now (it used to fly to the last planet first).
            try:
                self._setup_blueprint(world)
            except StopBot as reason:
                self._log("error", f"Paused: {reason}")
                self.paused = True

    def _reset_run_state(self):
        """Per-run state (set at every start; also once when the bot is made, so nothing that runs before the
        first start, or a check, finds it missing)."""
        self.zone_since = time.monotonic()
        self.ghosts = {}           # unit id -> until: "attackers" that cannot be selected and do no damage
        self.unit_seen = {}        # unit id -> monotonic time first seen in this zone (squads dropping in)
        self.new_arrivals = []     # (time, x, y, desc) of enemies that appeared close by
        self.spawn_threat = None   # (x, y, until): a squad dropping in there
        self.strike_watch = {}     # marker (x, y) -> distance from the ship when last seen
        self.strike_pending = []   # (log time, text, hull before)
        self.pull = None           # pulling attackers to a clear spot: {ids, spot, start, seen, name}
        self.pulled = {}           # attacker id -> time it was considered for a pull (once per PULL_MEMORY_S)
        self.target_id = None
        self.target_failures = 0
        self.ignored = {}
        self.collect = None
        self.refilling = False
        self.warned = set()
        self.last_move = 0.0
        self.last_enemy_seen = time.monotonic()
        self.last_enemy_spot = None
        self.navmaps = {}
        self.last_skill = {}
        self.recent_errors = []
        self.route = None
        self.route_goal = (0.0, 0.0)
        self.route_time = 0.0
        self.move_history = []
        self.stuck_spots = []
        self.progress = None  # [goal, best distance, time of best]
        self.patrol_index = 0
        self.patrol = None
        self.patrol_key = None
        self.empty_spots = {}
        self.orb_wait = None
        self.energy_seen = 0
        self.dangers = []        # [x, y, last seen] of dangerous groups
        self.danger_ids = set()
        self.danger_members = {}  # unit id -> (x, y, last seen)
        self.squad_tracks = SquadTracks(log=self._debug)  # learned squad patrol loops per planet
        self.site_routes = {}     # planet name -> SiteRoutes (site data squad routes)
        self.squad_planet = (None, None)  # (planet map, planet name) of the current farm tick
        self.danger_logged = []
        self.flee_point = None
        self.flee_time = 0.0
        self.evade_since = None
        self.evade_clear = None
        self.member_tracks = {}  # squad member id -> [(time, x, y)] over the last ~3 s
        self.last_trace = 0.0
        self.last_squad_seen = -math.inf
        self.squad_waits = {}  # move goal -> time the bot started holding for a squad blocking the way there
        self.squad_blocked = False  # the last move call held position for a squad
        self.enemy_blocked = False  # the last move call found only ways through enemies (they are cleared first)
        self.progress_touch = 0.0
        self.last_blocked_note = 0.0
        self.unstick = None  # ((x, y), until): short hop away from a wall
        self.last_turn = 0.0
        self.last_camera_check = 0.0
        self.last_tilt = 0.0
        self.pitch_home = None  # the angle the camera had on arriving (tilted back to in the open)
        self.camera_clear_since = None
        self.hull_history = []  # (time, hull share) over the last seconds, for the damage rate
        self.retreating = False
        self.learner = None  # Learner while "Learn while playing" is on (docs/LEARNING.md)
        self.learn_failed = False
        self.learn_route_at = {}  # planet map -> when learned route costs were last applied
        self.learn_notes = {}  # note key -> time of the last debug line
        self.learn_verdicts = {}  # enemy type -> (time, verdict) for the danger list
        self.last_press = -math.inf  # last blaster / attack skill press (learning: who is being fought)
        self.last_range_note = 0.0
        self.last_eta_note = 0.0
        self.loot_notes = {}  # drop position -> time of the last 'not collecting yet' log line
        self.route_cells_planet = None
        self.route_cells = set()
        self.dead_since = None
        self.started = time.monotonic()
        self.active_mode = None
        # Display name of a planet to travel to (Seek Blueprint, Farm planet). Started off a planet with a farm planet
        # set: go there directly (it landed on the last planet first).
        self.destination = self.start_destination
        self.seek = None         # {"names": [...], "levels": (low, high) or None}
        self.progress_mark = None
        self.progress_time = time.monotonic()

    def _loop(self):
        while not self._stop.is_set():
            if self.paused:
                self.status = "Paused"
                self._stop.wait(0.3)
                continue
            started = time.monotonic()
            limit = self.config.max_runtime_min
            if limit and started - self.started > limit * 60:
                self._log("info", f"Stopped after {limit} min as set")
                self.stop()
                break
            try:
                world = self._read()
                if world is not None:
                    self._step(world)
            except StopBot as reason:
                if not self._stop.is_set() and str(reason) != "paused by user":
                    self._log("error", f"Paused: {reason}")
                    self.paused = True
            except GameWindowGone:
                self.status = "Waiting for the game window"
            except SnapshotFailed:
                pass
            except (MemoryReadError, OSError) as error:
                # A single failed read while the game is still running is a passing glitch (an object or class being
                # set up as it is read: 2026-11-12 10:06, as a blueprint drop appeared, ended the run with "Lost the
                # game"). Stop only when the game process is gone or reads keep failing.
                if not location.game_pids() or self.pid not in location.game_pids():
                    raise
                now = time.monotonic()
                self.read_errors = [t for t in self.read_errors if now - t < 60] + [now]
                self._debug(f"read failed, skipping this tick: {error}")
                if len(self.read_errors) >= READ_ERRORS_MAX:
                    raise
            except Exception as error:
                # One bad tick should not end the run; repeated errors pause it.
                now = time.monotonic()
                self.recent_errors = [t for t in self.recent_errors if now - t < 60] + [now]
                self._debug(traceback.format_exc())
                self._log("error", f"Error: {type(error).__name__}: {error}")
                if len(self.recent_errors) >= 5:
                    self._log("error", "Paused: repeated errors")
                    self.paused = True
            self.tick_at = time.monotonic()
            self._stop.wait(max(0.0, self.config.tick_s - (time.monotonic() - started)))

    # Reading ------------------------------------------------------------------------------------------------

    def _read(self):
        try:
            return self.reader.read()
        except SnapshotFailed:
            return None

    def _wait(self, condition, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if self.paused:
                raise StopBot("paused by user")
            world = self._read()
            if world is not None and condition(world):
                return world
            time.sleep(0.25)
        if self._stop.is_set():
            raise StopBot("stopped")
        return None

    # Decisions ----------------------------------------------------------------------------------------------

    def _start_blueprint_book(self, world):
        """Start the blueprint reader once the ship's items are known, in any zone: BP Completion reads the progress
        in a hangar, and the reader was only started on a planet (2026-11-17 00:09: nothing read in the hangar)."""
        if self.blueprints is not None and not self.blueprints_started and world.items:
            self.blueprints.start([i.template_id for i in world.items])
            self.blueprints_started = True

    def _step(self, world):
        self._apply_hotkeys(world)
        self._send_chats(world)
        if not world.travelling:
            self.travel_seen, self.hyperjump_tries, self.hyperjump_next = None, 0, 0.0
        self._start_blueprint_book(world)
        if world.zone != self.last_zone:
            if world.zone == "planet" and self.last_zone not in (None, "planet"):
                self.just_landed = True  # handled by the first farm tick (refill on landing)
            self.last_zone = world.zone
            self.zone_since = time.monotonic()
            self.take_off_at = None
            self.pitch_home = None  # the new zone's camera angle is taken on the first tilt
        settled = time.monotonic() - self.zone_since >= self.config.settle_s
        self._check_progress(world)

        if world.dead:
            self.status = "Dead - rescuing"
            if self.dead_since is None:
                self._learn(lambda learner: learner.died())
            self._rescue(world)
        elif world.zone == "loading" or world.zone == "unknown":
            self.status = "Loading"
        elif world.travelling:
            # The entry picked in the starmap; the current planet is still the origin while travelling.
            to = self.travel_to
            self.status = f"Travelling to {assets.display_name(to) if to else self._home_name()}"
            self._hyperjump(world)
        elif not settled:
            self.status = "Waiting after zone change"
        elif self.stop_in_orbit and world.zone in ("orbit", "starmap"):
            self.status = "Stopped in orbit"
            self._log("info", "In orbit; stopped (Stop in orbit)")
            self.stop()
        elif world.zone == "hangar":
            if world.current_planet and world.current_planet.lower() not in self._hangar_orbits()["yes"]:
                self._learn_hangar(world.current_planet, True)
            if self.config.mode == "bp_completion":
                # Not set up yet (2026-11-18 00:01: a restart while shot down was rescued into the hangar and left it
                # without a plan; the landing guard stopped it in orbit at Molikar).
                if self.bp is None and not self._start_bp(world):
                    return
                if not self._bp_hangar_read(world):
                    return
                # Plan right here, so the hangar is left for the plan's planet (it used to land on the old planet first
                # just to plan, then take off again: Vénar 00:49:40 for a Mizar run).
                self._bp_tick(world)
                if self.bp is not None and self.bp["planned"] is None:
                    # Not planned yet (the item data was still being read): stay in the hangar until it is. Leaving
                    # unplanned kept the hangar trip's destination, and the ship tried to land on Ob (L34-43) instead
                    # of going back to Baumar (2026-11-17 19:07).
                    return
            if self._ship_wanted(world):
                self._switch_ship(world)
                return
            if self.config.mode != "bp_completion":
                self.bp_hangar_wanted = False
            self._leave_hangar(world)
        elif world.zone in ("orbit", "starmap"):
            if world.zone == "orbit":
                self.reland = False  # landing again now; the landing check decides on energy
            if self.config.mode == "bp_completion" and self.bp is None and not self._start_bp(world):
                return
            if self.config.mode == "bp_completion" and self.bp_fresh_wait is not None and not self._bp_fresh_read():
                return  # just out of a hangar: plan only with the server's copy of the progress
            if self.config.mode == "bp_completion" and not self.bp_hangar_wanted:
                # Plan before choosing where to go, so the orbit never falls back to an old destination (a hangar
                # trip's planet) while the plan is still missing.
                self._bp_tick(world)
                if self.bp["planned"] is None:
                    return
            try:
                self._from_orbit(world) if world.zone == "orbit" else self._starmap(world)
            except StopBot as error:
                if self.energy_trip is None or ENERGY_PLANET.lower() not in self._home_name().lower():
                    raise
                # Terasa (or Antares) not unlocked for this pilot, or the way there failed: no fallback.
                _save_state({"energy_planet_unreachable": True})
                self.energy_trip = None
                self._log("warning", f"{ENERGY_PLANET} cannot be reached ({error}); stopping in orbit")
                self.status = "Stopped in orbit"
                self.stop()
        elif world.zone == "planet":
            if world.current_planet and world.current_planet != self.saved_planet and self._is_home(world.current_planet):
                self.saved_planet = world.current_planet
                _save_state({"last_planet": world.current_planet})
            self._apply_mode(world)
            if self.config.mode == "bp_completion":
                self._bp_tick(world)
            # Taking off is refused in combat (user, 2026-11-14), so leaving runs inside the farm tick: attackers are
            # fought, then the ship moves clear and takes off once calm (_leave_planet). It used to press F5 and wait
            # 40 s doing nothing, also while being shot.
            if self.stop_in_orbit:
                self.leaving = "Going to orbit to stop"
            elif self.reland:
                self.leaving = "Going to orbit to land again (no energy close by)"
            elif self.config.mode == "fly":
                self.leaving = f"Taking off for {self._home_name()}"
            elif self.bp_hangar_wanted or self._ship_wanted(world):
                self.bp_hangar_wanted = True
                self.leaving = "Going to the hangar" + (" to switch ships" if self._ship_wanted(world) else
                                                        " to read the blueprint progress")
            elif not self._is_home(world.current_planet):
                self.leaving = f"Leaving for {self._home_name()}"
            else:
                self.leaving = None
            self._farm(world)

    def queue_chat(self, entry):
        """A chat message from the remote page: sent by _send_chats at the next calm moment."""
        self.chat_queue.append(entry)

    def _send_chats(self, world):
        """Type queued chat messages into the game's chat box (chatsend.py) when nothing else needs the keyboard:
        not dead, not loading, not in a fight (letters are hotkeys outside the box), on a planet, in orbit or
        travelling. A message that finds no such moment within CHAT_WAIT_S fails."""
        if not self.chat_queue or self.input is None:
            return
        now = time.time()
        entry = self.chat_queue[0]
        calm = (not world.dead and world.in_battle is not True and time.monotonic() - self.last_fight_time > 5
                and (world.travelling or world.zone in ("planet", "orbit"))
                and time.monotonic() - self.zone_since >= self.config.settle_s)
        if not calm:
            if now - entry["t"] > CHAT_WAIT_S:
                self.chat_queue.pop(0)
                entry.update(state="failed", detail="no calm moment to type it")
            return
        self.chat_queue.pop(0)
        entry["state"] = "typing"
        try:
            ChatSender(self.input, self._debug).send(entry["channel"], entry["text"], entry.get("row"))
            entry.update(state="typed", typed_at=time.time())
        except Exception as error:
            entry.update(state="failed", detail=str(error)[:200])
            self._log("warning", f"Chat message not sent: {error}")
            self._snapshot("chat")

    def _check_progress(self, world):
        mark = (world.xp, world.cryonite, round((world.x or 0) / 500), round((world.y or 0) / 500), world.zone)
        now = time.monotonic()
        waiting = self.status.startswith("Waiting") or self.status.startswith("Energy full")
        if mark != self.progress_mark or waiting or world.travelling:
            self.progress_mark = mark
            self.progress_time = now
            return
        idle = now - self.progress_time
        if idle > self.config.stuck_s * 3:
            raise StopBot(f"no progress for {idle:.0f} s")
        if idle > self.config.stuck_s:
            self._warn_once(f"stuck-{int(self.progress_time)}", f"No progress for {idle:.0f} s")
