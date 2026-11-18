"""Tuning constants and UI positions of the bot (moved from core.py; each keeps its reasoning)."""
import os
import pathlib

from .config import SETTINGS_FILE


# UI positions measured on a 3440x1440 borderless window (docs/phase1-findings.md), as offsets from edges.
RESCUE_TOAST = ("centre", -85, "bottom", -133)
RESCUE_STARBASE_ROW = ("centre", 0, "bottom", -286)
HANGAR_MAINTENANCE_TAB = ("right", -364, "top", 46)
HANGAR_REPAIR_BUTTON = ("right", -77, "top", 133)
# Reference images (from the working repair of 2026-11-12 11:18) and the window area to find them in, by window
# width and height: the hangar tab bar with Maintenance active, and the repair dialog's OK / Cancel row.
UI_DIR = pathlib.Path(__file__).resolve().parent.parent / "input" / "ui"
UI_CHECKS = {
    "maintenance_tabs": lambda w, h: (w - 700, 20, w - 150, 75),  # wide: some hangars have a CONQUEST tab in front
    "repair_buttons": lambda w, h: (w // 2 - 170, h // 2 + 15, w // 2 + 170, h // 2 + 80),
    "gate_tab": lambda w, h: (w - 560, 25, w - 400, 75),
    "gate_jump": lambda w, h: (w - 140, 385, w - 20, 435),
    "drone_setup_title": lambda w, h: (w // 2 - 170, h // 2 - 215, w // 2 + 170, h // 2 - 160),
    "drone_charge_button": lambda w, h: (w // 2 + 170, h // 2 + 110, w // 2 + 300, h // 2 + 165),
    "landing_choose_zone": lambda w, h: (w // 2 - 200, h // 2 - 240, w // 2 + 200, h // 2 - 140),
    "hangar_tab_active": lambda w, h: (w - 700, 20, w - 150, 75),
}
# BP Completion (user, 2026-11-16): the player's blueprint progress is only in memory in a hangar. Orbits with a
# hangar have it in the location menu under the minimap: the menu button, then the "Hangar" row (Vénar orbit).
HANGAR_KEY = "F7"  # in an orbit with a hangar: into the hangar (user, 2026-11-17)
HANGAR_KEY_WAIT_S = 10
# Ship switching in a hangar (2026-11-17, game input tested with the user): Space opens the panel, the HANGAR tab, then
# "Ships"; one click on a row switches the active ship at once. Rows 39 px apart, 8 visible; the list scrolls only with
# its scrollbar (a click on the track pages, a drag moves the thumb). Row order and names come from memory
# (pgbot/game/ships.py), the result is checked in memory (the active ship's type).
HANGAR_TAB = ("right", -284, "top", 46)
HANGAR_SHIPS = ("right", -500, "top", 72)
SHIP_ROW = ("right", -240, "top", 77)
SHIP_ROW_STEP = 39
SHIP_ROWS_VISIBLE = 8
SHIP_SCROLL_X = -15  # from the right edge
SHIP_TRACK = (80, 378)  # scrollbar track, top to bottom
SHIP_SWITCH_TRIES = 4
HANGAR_FALLBACKS = ("Gemini Starbase", "Kalabesh")  # hangars every player can reach, in this order (user)
BP_REPLAN_S = 900  # re-plan the missing blueprints this often (and whenever new hangar data was read)
BP_LEVEL_MARGIN = 2  # planets whose enemies are at most this many levels above the ship come first
# The hangar CONFIGURATION panel makes the client fetch a fresh blueprint container from the server, but the copy can
# show up only after the hangar is left (2026-11-18 00:06:47, 3 s after F5; 2026-11-17 19:12:51 it came 16 s after
# entering). So the hangar only waits for the item catalogue, and the orbit waits for a copy that was not in memory
# when the panel opened, then plans. The old read timed out at 20 s while the catalogue took about 20 s, then
# started over (catalogue requested again), and planned on old counts (2026-11-18 00:06:04, 00:06:24).
BP_CATALOG_WAIT_S = 90  # wait this long in a hangar for the item catalogue
BP_FRESH_WAIT_S = 60  # wait this long in orbit for the server's copy of the blueprint container
# "Landing - Choose a zone" window in an orbit shared by two zones (Earth / Earth in Unrest, user 2026-11-16): two
# cards side by side, centred on screen; a click on a card's picture lands there.
LANDING_CARD = {"left": ("centre", -142, "middle", -60), "right": ("centre", 142, "middle", -60)}
LANDING_WAIT_S = 12
UI_MATCH_MIN = 0.8
UI_HOVER_S = 0.3  # cursor rests on a UI element this long before the press
UI_HOLD_S = 0.12  # UI button press length
REPAIR_CONFIRM_OK = ("centre", -62, "middle", 48)
STARMAP_ROW = ("right", -424, "top", 82)
STARMAP_ROW_STEP = 49
SELECT_MIN_DIST = 350  # an enemy closer than this is under the ship: a click there selects nothing
SELECT_BACK_OFF = 900  # all attackers that close: back off this far to aim
SELECT_GIVE_UP_S = 6  # a pickup that stays unselectable this long is set aside for 2 min
STARMAP_SCROLLBAR = ("right", -262, "top", 120)  # top of the destination list's scrollbar thumb
STARMAP_SCROLL_STEP = 150  # pixels the thumb is dragged per page
STARMAP_MAX_PAGES = 6
STARMAP_TRAVEL = ("right", -60, "top", 406)
# Star gates (user demo 2026-11-12): each system has one gate orbit in its starmap ("DraconisGate"; in Gemini the
# starbase is the gate). There Space opens the STARGATE tab: a list of the unlocked systems in progression order and
# a Jump button; a jump loads and puts the ship at the target system's gate orbit. Rows are found by their name
# image (pgbot/input/ui/gate_row_<system>.png, from the demo; 1.00 own row vs <= 0.57 others).
GATE_JUMP = ("right", -73, "top", 410)
GATE_LIST_AREA = lambda w, h: (w - 540, 55, w - 330, 560)  # noqa: E731
GATE_JUMP_TIMEOUT_S = 60
NO_GATE_SYSTEMS = {"Sirius"}  # no gate travel (user: reached through a Draconis wormhole, changes weekly, for clans)
GATE_AT_STARBASE = {"Gemini"}  # systems whose starbase is the gate (user demo); others are learned from the starmap
MINIMAP = ("centre", 239, "bottom", -122)
MINIMAP_SIZE = 113

# Screen areas never clicked when aiming at the 3D world: HUD bottom, top bar, open map panel on the right.
SAFE_MARGIN_BOTTOM = 270
SAFE_MARGIN_TOP = 70
RIGHT_PANEL = (520, 460)  # width, height of the top-right panel area


STATE_FILE = SETTINGS_FILE.parent / "state.json"  # what the bot remembers between runs (last planet farmed)


RESCUE_UNLOCK_S = 11  # the rescue toast unlocks about 10 s after being shot down
DANGER_MEMORY_S = 90  # a dangerous squad member out of sight is still avoided this long
DANGER_TARGET_MARGIN = 1000  # targets, loot and patrol points must be this much beyond the keep-away distance
DANGER_POSITION_S = 30  # a member's last known position counts this long after it was last seen
SQUAD_RADIUS = 2500  # enemies this close to a dangerous enemy count as its squad
DS_RADIUS = 3000     # Death Squad: units this close to the leader (Custodian) are the squad
DS_MIN_SIZE = 4      # ... with a Resurrector and at least this many in all (usually 6; some may be out of view)
# Squads by behaviour (user, 2026-11-14: a squad is a group flying its patrol route together, regrouping when split).
# The listed names miss most (Tau Ceti's "Resurrector" is not "Mantis Resurrector"; a Signor squad): two Vénar
# deaths to unrecognised squads (21:56, 22:45) and one to four Signors (22:41).
# Off for now (user, 2026-11-14 23:18): on Vénar the Assassins roam in groups along the squad route and were taken for
# squads, so the bot avoided nearly everything. Squads are the danger list (names) again; movement and learned squad
# types are still recorded (Learned: Squads) to build a better recognition offline, but not acted on.
SQUAD_BY_MOVEMENT = False
SQUAD_MOVE_WINDOW_S = 3.0  # moving together this long
SQUAD_MOVE_MIN = 200       # u/s (Vénar squads ~380)
SQUAD_MOVE_COS = 0.8       # same heading
SQUAD_MOVE_GROUP = 3       # at least this many
SQUAD_MOVE_R = 3000        # this close together
SQUAD_MOVE_SHIP = 5000     # and this far from the ship (enemies charging the ship also move together)
FLEE_SHARE = 0.6  # always evade when a squad member is this close (share of the keep-away distance)
EVADE_EXIT = 1.3  # evading ends when no member has been within this share of the keep-away distance for 3 s
EVADE_MIN_S = 5  # evade at least this long once started
SQUAD_FIGHT_NEAR_SHARE = 1.6  # no new fights while a squad member is within this share of the keep-away distance
CAST_MARGIN_S = 0.3  # no other key until a skill's warm-up is over plus this
SELECT_EVERY_S = 1.0  # at most one select click per unit this often
CLOSE_SELECT = 700  # enemies closer than this sit on the own ship on screen: shoot back at others first
FIRE_SPOT_KEEP = 800  # a firing spot is kept until its target has moved this far from where it was
STRIKE_RADIUS = 3000  # enemy Orbital Strike blast: the "range" 3000 every Orbital Strike has in the game's item data;
# the user's recording agrees (all hits within 3,000 of the marker, e.g. -28% at 2,448; none from 3,034-3,444), and a
# dodge to ~2,980 still died on Earth in Unrest (2026-11-16 18:53, level 89 enemies, one hit from 100%)
STRIKE_MARGIN = 800  # dodge this much further out: the player gets 3,150-4,400 away (2026-11-16 recordings)
SIGNOR_FINISH_SHARE = 0.25  # a Signor below this hp share is not finished while a live strike marker is near
ORBITAL_SPLASH = 1800  # an area attack skill (Orbital Strike) hits others within about this of its target
BREAKAWAY_ATTACKERS = 3  # this many attackers: retreat fire goes at the crowd, not the weakest
FIGHT_ON_HULL = 0.5  # with at least this hull, learning's "lost" alone does not start a retreat
FIGHT_ON_LOOK_S = 10.0  # ...unless the damage now reaches the retreat line within this time
SELECT_KEEP_AFTER_PRESS_S = 2.0  # the selection is not changed this soon after a skill press (it cancels the cast)
PULL_MARGIN = 1500  # a pull spot keeps every other enemy at least its aggro range + this away
ROUTE_START_SLACK = 300  # inside a squad's keep-away distance a route may not come closer than now minus this
PULL_NEAR_MARGIN = 500  # others count as near the fight within their aggro range + this
PULL_CLOSING_MIN = 100  # only attackers coming closer faster than this (u/s) are pulled (Snipers stay put)
SELECT_TRUST_S = 1.5  # a selection click this recent counts as selected (memory shows it a moment later)
PULL_DISTANCES = (3000, 4500, 6000)  # pull spots this far from the ship (the player moved 550-7,150 away)
PULL_ARRIVE = 800  # a pull ends this close to its spot
PULL_MAX_S = 15.0  # ...or after this long
PULL_MEMORY_S = 120.0  # an attacker is pulled once in this time
STRIKE_LABEL = "out of an Orbital Strike"
STRIKE_KEEP_S = 5.5  # a dodge spot is kept this long for its marker (markers live 4.6-5.0 s)
STUN_LABEL = "out of a Stun Turret's reach"
STUN_REACH_MARGIN = 1000  # Stun Turret reach = its aggro range (3,000) + this; the data has no stun radius
STRIKE_SQUAD_KEEP = 3000  # strike dodge spots stay this far from squad members (or no closer than the ship is)
STRIKE_LOG_NEAR = 4000  # strikes going off this close are logged with the hull change (to learn the radius)
SPAWN_NEAR = 6000       # an enemy first seen this close was just put down (units are known 20,000+ away)
SPAWN_GROUP = 2500      # newcomers this close together are one drop
SPAWN_MIN_UNITS = 3     # this many in SPAWN_WINDOW_S is a squad dropping in (single respawns are normal enemies)
SPAWN_WINDOW_S = 4.0
SITE_SQUAD_NEAR = 5000  # a group drop this far from every site-data squad area is a normal respawn
SPAWN_HEIGHT = 800      # an enemy this far above the ship is falling (normal heights differ by ~150)
SPAWN_GRACE_S = 10.0    # after a zone change everything is new
SPAWN_AVOID_S = 20.0    # the drop spot is avoided this long (then squad recognition has it)
SPAWN_KEEP = 8000      # near a known squad spawn point: new fights only with afterburner and repair ready
SPAWN_MERGE = 6000     # drops this close to a known spawn point are the same point (members fall in at angles, spread ~5,000)
PATROL_SQUAD_SHARE = 2.0  # patrol points are not picked within this share of the keep-away distance of a squad in view
RETREAT_SPAWN_REACH = 12000  # learned squad spawn points this close are fled from like threats when retreating
RETREAT_SQUAD_SHARE = 1.5  # retreats flee from squads only within this share of the keep-away distance
CHASE_SPEED = 400   # a threat closing faster than this is chasing the ship
CHASE_KEEP_S = 3.0  # while chased, an escape spot is kept at least this long (a straight run)
GETAWAY_MIN = 3000  # an escape spot this far ahead is a getaway: the afterburner goes (a short step aside is none)
ENERGY_PLANET = "Terasa"  # energy fallback (user): its landing zone has energy; Antares
ENERGY_PLANET_FIELD_RANGE = 12000  # landing zone is inside the field (user); the site stores the field as one point ~9,800 away
ENERGY_TRIP_DONE = 0.95  # refilled this full on Terasa: back to the previous planet
BP_HUNT_MIN_ENERGY = 0.25   # no blueprint-hunt fights below this energy share
BP_HUNT_FIELD_REACH = 15000  # nor with an energy field this close (collect there instead)
GHOST_IGNORE_S = 120  # an attacker that cannot be selected and does no damage is ignored this long
SELECT_STEP = 900  # sideways step when an attacker cannot be selected (a new view of it)
WEAPON_GAP_S = 0.2  # extra wait between the blaster's cycle and an attack skill (and back)
# Client skill rules (2026-11-12, ako.do / akn): every item use starts a global cooldown of 1,500 ms / cooldown
# factor (the Speed Actuator raises the factor) during which no other item can be used, and an item in its warm-up
# blocks all others. The blaster is exempt from the check but its shots start the global cooldown too.
GLOBAL_COOLDOWN_S = 1.5
ATTACK_MEMORY_S = 4.0  # an enemy still counts as attacking this long after it last aimed at the ship
GCD_MARGIN_S = 0.1
DEFENSIVE_WORDS = ("protect", "shield", "barrier", "absorb", "heal")  # defensive buffs (repair is handled apart)
# When defensive skills are used, per caution profile: hull below this, or this many attackers.
# Smart healing out of combat (user, 2026-11-12): at an energy field, while actually collecting there, repair
# below FIELD_HEAL_HULL (players heal during the energy downtime); in lower / same level mode also repair before
# starting a new fight below PRE_FIGHT_HEAL, and stay at the field (energy full) until repair is back, at most
# FIELD_HEAL_WAIT_S.
FIELD_HEAL_HULL = 0.9
# Drones (user, 2026-11-12): not all on at once, each only when its effect is needed, since active drones drain
# (3 h from full while on a planet) and recharging costs max(1000, 250 x rank) x 0.55 x factor x missing charge.
# HUD drone icons: 2 columns right of the skill bar (game 3440 x 1440: slot 0 at (1900, 1368), 26 px apart); a
# right-click toggles a drone, a left-click opens the Drone Setup window (rows, Charge button, close X).
DRONE_HUD_STEP = 26
DRONE_ARROW_CLOSED_DX = 163  # arrow picture's left edge right of the window centre when the panel is closed
DRONE_BACKOFF_S = 120.0  # something looked wrong: leave drones alone this long, doubling up to DRONE_BACKOFF_MAX_S
DRONE_BACKOFF_MAX_S = 900.0
DRONE_RECHARGE_CLEAR = 4000  # recharge only with no enemy this close (or within its aggro range + 1,500)
DRONE_WINDOW_ROW = ("centre", -152, "middle", -156)
DRONE_WINDOW_ROW_STEP = 39
DRONE_CHARGE_BUTTON = ("centre", 234, "middle", 135)
DRONE_WINDOW_CLOSE = ("centre", 278, "middle", -187)
DRONE_MIN_TOGGLE_S = 3.0  # a drone is not switched again within this time (no flicker); the user toggles 2-5 s
DRONE_HOLD_S = 2.0  # a drone stays on this long after its reason ended (user: off right after the pickup)
DRONE_PICKUP_NEAR = 1500  # a pickup drone goes on once the drop is this close
DRONE_PICKUP_S = 3.0  # ...and is wanted this long after the last approach tick
DRONE_COMBAT_HOLD_S = 15.0  # other drones (combat, blueprint) stay on this long after their reason ended
DRONE_COMBAT_MIN_TOGGLE_S = 10.0  # ...and are not switched again within this time
DRONE_RECHARGE_RETRY_S = 300.0
COMBAT_BOOSTS = ("armorboost", "damageboost", "hitchanceboost", "criticalhitboost", "cooldownboost", "rangeboost",
                 "speedboost")
ZOOM_WARN_AFTER_S = 30  # still zoomed in this long after a zone change: warn once
ZOOM_KEEP_EVERY_S = 3  # zoomed in (below ZOOM_FAR): zoom out again at most this often
ZOOM_TERRAIN_EVERY_S = 10  # zoom out for a camera in terrain at most this often
ZOOM_FAR = 3500  # camera distance counted as zoomed out (the farthest seen is 4,132)
ZOOM_RETRIES = 3  # more wheel turns if the camera did not move out
ZOOM_NOTCHES = 3  # wheel notches to zoom out after landing
NO_CLICK_RELAX_S = 2.0  # no clickable point this long: click without the terrain-sight test
NO_CLICK_GIVE_UP_S = 8.0  # ... and this long: give the destination up
LAND_FIELD_RANGE = 8000  # refill on landing only if an energy field is this close to the landing spot
PRE_FIGHT_HEAL = {"lower": 0.75, "same": 0.6}  # heal before a new fight below this hull. Same was raised to 0.75 for
# a while (2026-11-16 21:10-22:25): the heal then used the repair right before the next fight (cooldown 66 s), the ship
# fought at 100% without repair (22:24, 5 attackers, dead); deaths did not drop (3 in 33 min before, 7 in 75 min after)
FIELD_HEAL_WAIT_S = 90.0
# Lower level was too cautious on Vénar (ship 82 vs 85: shield at 2 attackers, repair at 90%, retreat at 83% hull;
# user, 2026-11-12): now 70% / 3 attackers, as in same level.
DEFENSE_HULL = {"lower": 0.7, "same": 0.7, "high": 0.6}
DEFENSE_ATTACKERS = {"lower": 3, "same": 3, "high": 5}
SKILL_CONFIRM_S = 2.5  # a press counts as taken once the skill reads cooling down or running within this time
SKILL_FAILS = 3  # presses in a row that did not take ...
SKILL_PAUSE_S = 8  # ... pause that skill this long (60 s took the Orbital Strike out of whole fights; user: far too
# long, the failures are short-lived: target out of range or the press lost to another key)
SEEK_AGGRESSIVE_RANGE = 8000  # seek modes also fight aggressive enemies this close
BLOCKER_MEMORY_S = 3  # enemies blocking every way stay "in the way" this long
REPAIR_CAST_S = 2.5  # after a repair press no other key until the repair shows as used, at most this long
REPAIR_TRIES = 2  # repair presses within REPAIR_TRY_WINDOW_S that did not take: treat repair as unavailable
REPAIR_TRY_WINDOW_S = 8
FLEE_HOP = 3500  # escape clicks land this far ahead (afterburner speed); at 5,000 half were ignored (near the horizon)
FLEE_CLICK_S = 0.5  # ... and are sent this often
ADVICE_STEADY_S = 3.0  # a retreat turns into a fight again only after 'won' held this long
LEARN_FIGHT_ON_FLOOR = 0.5  # below this hull with LEARN_FIGHT_ON_MAX_ATTACKERS+ attackers a learned 'won' cannot keep the ship fighting
LEARN_FIGHT_ON_MAX_ATTACKERS = 3
RETREAT_KEEP_SPOT_S = 3.0  # a retreat's escape spot is kept at least this long (no zigzag between sides)
RETREAT_RESUME_HULL = 0.8  # a retreat ends once the hull is back above this, even with attackers following
STANDOFF_SHARE = 0.85  # fight from this share of the blaster range (2,720 with a 3,200 blaster)
# Aggro (user, 2026-11-11): an enemy attacks when the ship comes within its aggression range or when it is attacked
# itself; its group does not join. The range is read per enemy (`aggressionrange`, Rocketeer 2,500).
AGGRO_DEFAULT = 2500  # aggression range when an enemy does not report one
ROUTE_NEAR = 6000  # only enemies along this much of a route decide whether it may pass through them
AGGRO_MARGIN = 500  # keep this much beyond an enemy's aggression range when it is not meant to be pulled
TRAP_MIN_ENEMIES = 3  # Magnet Trap only with this many enemies near where it lands ...
TRAP_RADIUS = 1500  # ... within this distance (its stun area; not measured yet)
GOLD_LOOT_RADIUS = 12000  # gold blueprints are fetched from at least this far
HOSTILE_ATTACK_RANGE = 4000  # hull falling, no attacker visible: enemies this close are taken as attackers
IMPLIED_ATTACK_S = 5.0
COLLECT_STALL_S = 4.0  # a straight approach to a drop that gets no closer this long switches to a route
COLLECT_IDLE_S = 12.0  # the ship not moving this long while collecting: the pickup is given up
PICKUP_GIVE_UP_AREA = 1500  # ...with every energy orb this close to it (orbs only: drops are retried on their own)
# A pickup given up is retried after these waits, longer each time it fails again (drops stay about 5 min; user,
# 2026-11-18: a gold blueprint and lots of Cryonite were left behind for good after one 2-min give-up).
PICKUP_RETRY_S = (15.0, 30.0, 60.0, 120.0)
GOLD_PICKUP_RETRY_S = (8.0, 15.0, 30.0, 60.0)
COLLECT_GAP_S = 3.0  # a pickup not worked on this long (fighting, other loot) starts its give-up timers afresh
COLLECT_ACTIVE_MAX_S = 60.0  # the collector beam running this long without the drop arriving: given up
ROUTE_END_REACHED = 700  # the ship this close to a route's end that is not the drop: the drop is clicked directly
COLLECT_WALL_ROOM = 450  # a straight approach must keep this far from mapped walls
GOLD_STALL_S = 6.0  # under attack, a gold blueprint run that gets no closer for this long yields to fighting
GOLD_RETRY_S = 15.0  # ... for this long
GOLD_LABEL = "the gold blueprint"  # move label: the way there may pass enemies (not squads)
CRITICAL_ENERGY = 0.10  # below this share nothing but squads stops the flight to energy (no energy = no skills)
SQUAD_FIGHT_ETA_S = 20  # no new fights when a squad in view would reach the keep-away distance within this time
CAMERA_DEG_PER_PX = 0.458  # yaw per pixel of right-button drag (camera_drag_test, 2026-11-11)
CAMERA_MAX_DRAG_PX = 450  # one drag turns at most about 200 degrees
# Camera tilt (user, 2026-11-17: Baumar's narrow canyons; the bot only turned and zoomed, at ~12 deg above the horizon
# walls hide the ground). A vertical right-drag tilts: 100 px down = +20.5 deg (camera_drag_test, 2026-11-11); the rate
# and direction are measured again on every tilt and kept (state.json "camera_pitch_per_px").
CAMERA_PITCH_PER_PX = 0.205  # degrees of pitch per pixel dragged down (first guess)
CAMERA_PITCH_MAX = 65.0  # steepest tilt used (a top-down view loses sight of enemies ahead)
CAMERA_PITCH_STEP = 15.0  # tilt this much at a time
CAMERA_PITCH_RELAX_S = 30.0  # clear view this long: tilt back toward the normal angle
CAMERA_TILT_EVERY_S = 2.0  # at most one tilt this often
CAMERA_STEP_DEG = 60  # turns toward targets and move directions go at most this far at a time
CAMERA_FACE_RANGE = 12000  # turn to look at targets within this distance that are not on screen
OUTNUMBERED_RETREAT_HULL = 0.7  # with 3+ attackers and repair not ready, retreat below this hull (normal profile)
# Caution by the level gap between the ship and the enemies around (user: careful near the enemy level, bolder when
# well above it; at 82 vs ~71 it fled at 50% hull, which was far too early).
# Farming modes by level gap (ship level - median enemy level), user 2026-11-12: below 0 "lower" (enemies one by
# one or death is likely), 0-9 "same" (still careful, smart fights, dangers avoided), 10+ "high" (outlevels them).
LOWER_GAP = 0  # gap below this: lower level
HIGH_GAP = 10  # gap this or more: high level
MODE_NAMES = {"lower": "Lower level farming", "same": "Same level farming", "high": "High level farming"}
LOWER_SKIP_NAMES = ("harvester",)  # never started in lower level mode (8+ Interceptor waves); fought if attacking
# Afterburner gain (user, 2026-11-16): its boost depends on the ship's hull, so the upgraded armor (919,813 hull) made the
# same item add ~6-10% speed (1,030 vs ~970 u/s) instead of ~95%. Measured live (boosted speed / normal top speed);
# below AB_WEAK_GAIN a squad cannot be outrun, so squads are kept AB_WEAK_KEEP times farther away and new fights stop
# earlier when one closes in.
AB_WEAK_GAIN = 1.6  # weak below this: the upgraded ship measures x1.06-1.40, the old one ~x1.9
AB_GAIN_PLAUSIBLE = (1.0, 3.0)  # readings outside this are bad samples, not the item
AB_WEAK_KEEP = 1.4
AB_WEAK_ETA_S = 35
LEVEL_MEMORY_S = 60  # enemy levels seen this recently decide the profile
# A retreat ends when no attacker is this close (or repair is ready). Was 5,000: Assassins fire from 5,000+ (Vénar
# 21:36:34: the retreat ended at 52% hull with 4 attackers, the nearest 5,055 away; the bot turned back, 7 attackers,
# dead in 4 s). Same as the distance the retreat flees from.
RETREAT_CLEAR = 8000
RETREAT_LOOKAHEAD_S = 5  # retreat when the hull would fall below the retreat line within this time
UNSTICK_S = 3  # fly the unstick hop for at most this long
ROUTE_AHEAD_S = 8  # route checks look this far ahead (they are repeated on every move)
ROUTE_STATIC_S = 6  # ... and assume a squad may stay where it is for this long
FAR_ATTACKER_WAIT_S = 20.0  # an attacker out of reach behind other enemies is waited for this long ...
FAR_ATTACKER_IGNORE_S = 60.0
FAR_ATTACKER_APPROACH_S = 5.0  # ... or only this long if it is not coming any closer  # ... then left alone this long unless it comes within range
MAX_RESTARTS_PER_HOUR = 3  # game restarts after crashes
# Taking off to another planet (_leave_planet): the game allows it only a few seconds after the last fight (user).
LEAVE_CALM_S = 6.0          # nothing fought the ship for this long before F5
LEAVE_CLEAR_MARGIN = 1500.0 # take off no closer than this to an enemy's aggro range
LEAVE_RETRY_S = 15.0        # F5 again when the zone did not change this long after a calm press
LEAVE_RETRY_KNOWN_S = 8.0   # the same when the game's combat flag said the press could work
LEARN_PATROL_BONUS_S = 240  # a busy area (learned) counts as seen this much longer ago, at most
LEARN_SAFE_MARGIN = 0.1     # learned fights must leave the hull this far above the retreat line
LEARN_PULL_EXTRA = 2        # learned pulls may exceed the max pull setting by this many
PATROL_RETRIES = 15  # patrol points tried per tick when squads block the way (6 often all lay past the squad)
PATROL_SEARCH_BUDGET_S = 0.4  # at most this long per tick trying other patrol points (the bot is blind meanwhile)
PATROL_BLOCKED_KEEP_S = 5.0  # patrol points found blocked by a squad are skipped this long (searched over ticks)
SQUAD_WAIT_S = 30  # hold position this long for a squad blocking the way before choosing another target
ESCAPE_SAFE_SHARE = 1.4  # an escape spot is good enough when it ends this share of the keep-away distance away
STUCK_MEMORY_S = 45  # stuck marks (assumed walls) are forgotten after this long
SHIP_SPEED = 850  # flight speed in units/s (trace 2026-11-11: about 880; squads move 600-1,000)
EVADE_MIN_CLOSING = 300  # u/s: slower drift toward the ship does not trigger the early warning
EVADE_LOOKAHEAD_S = 3  # evade when a squad member would be inside the keep-away distance within this time
EVADE_WARNING_SHARE = 1.5  # ... but never further out than this share of the keep-away distance (7,500 at 5,000)
SQUAD_RECENT_S = 60  # a squad seen this recently keeps fights and patrol points off its routes
DANGER_ROUTE_MARGIN = 1500  # routes go around squads with this much room
# Passing a squad (user, 2026-11-14: its patrol route cannot always be avoided, e.g. through the Vénar landing pocket;
# 5,000-7,000 from it is fine for a player): with the squad's way known (site route or learned track), the whole
# crossing is checked against where the squad will be, up to SQUAD_PREDICT_S ahead.
SQUAD_PREDICT_S = 45
SQUAD_PASS_SHARE = 1.0   # keep the keep-away distance from the predicted squad centre, plus the squad's spread
SQUAD_SPEED_DEFAULT = 400  # u/s when a squad's speed is not measured yet (Vénar: ~380)
ESCAPE_MIN_SHARE = 0.5  # an escape spot snapped to less than this share of its intended distance is skipped
ESCAPE_END_CLEAR = 4000  # enemies this close to an escape's end spot count against it
HIT_RECENT_S = 5.0  # fight back only while the hull dropped within this many seconds
NON_SHOOTERS = ("repogrub",)  # target the ship but never shoot (user): not attackers
ESCAPE_ZONE_COST = 8000    # an escape through another enemy's aggro range costs like this much more flight
SQUAD_SPREAD_MAX = 1500    # at most this much is added to the keep-away distance for a squad's size
AWAY_CHECK_S = 1.5  # an escape must not bring the ship closer to any squad member's current position within this time
AWAY_SLACK = 100  # ... by more than this
KEEP_WHEN_BLOCKED = ("the next patrol point", "the farm spot", "the last enemy sighting", "energy orbs",
                     "range of the target")
BLOCKED_GIVE_UP_S = 12  # a destination still blocked by enemies after this long is given up (e.g. turrets)
NARROW_ROOM = 1200  # stuck marks closer than this to a wall are not used for routing
ROUTE_FLIP_DEG = 100  # a re-plan turning the ship around by more than this keeps the current route ...
ROUTE_FLIP_MIN = 2000  # ... if that still has at least this far to go
GIVE_UP_S = 15  # give up a move target after this long without getting 300 units closer
HULL_FULL_SHARE = 0.99  # hull counts as fully repaired from this share of the computed maximum
READ_ERRORS_MAX = 30  # failed memory reads within a minute before the game counts as lost
# PGBOT_LOG_DIR: checks write their logs elsewhere (they filled logs/ next to the real runs).
LOG_DIR = pathlib.Path(os.environ.get("PGBOT_LOG_DIR") or pathlib.Path(__file__).resolve().parent.parent.parent / "logs")
