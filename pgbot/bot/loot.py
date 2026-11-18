"""Energy fields, loot and blueprint pickups, refilling energy.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game import assets
from ..data import myzen
from ..learn.learner import etype_of
from .constants import *  # noqa: F401,F403
from .util import _segment_distance, _is_orb


class LootMixin:
    def _field_near(self, world, reach, known_reach=None):
        """An energy field within reach: orbs in view, or a known field (remembered orbs, site data) within
        known_reach (default: reach)."""
        if any(_is_orb(u) and u.distance < reach for u in world.units):
            return True
        known_reach = reach if known_reach is None else known_reach
        return any(math.hypot(x - world.x, y - world.y) < known_reach for x, y in self._energy_spots(world))

    def _near_field(self, world):
        """At an energy field: orbs within 4,000 or a known field within 1,500."""
        return self._field_near(world, 4000, 1500)

    def _loot_safe(self, world, drop, now):
        """Loot comes before enemies that are not attacking, but not at any price (user, 2026-11-11: the bot flew
        into a group of ~5 enemies for a drop, and toward a squad into a tunnel). A drop is not collected while it
        or the straight way to it is inside an enemy's aggro range, or while that way is not safe from squads in
        view. Re-checked every tick, so it is collected as soon as it is safe (drops stay about 5 min)."""
        # Selecting a drop flies straight at it: neither the drop nor the way there may be inside an enemy's aggro
        # range (an enemy whose range already holds the ship does not block the way out of it).
        reason = None
        guards = [u for u in self._enemy_units(world) if not self._attacking(world, u)
                  and math.hypot(world.x - u.x, world.y - u.y) >= self._aggro_reach(u)
                  and _segment_distance((u.x, u.y), (world.x, world.y), (drop.x, drop.y)) < self._aggro_reach(u)]
        # While refilling, a blueprint the player has completed is energy (user: completed ones refill some energy):
        # fetched even with enemies around; only squads hold it back. Others are normal loot (gold ones come first
        # anyway). Completion is read from memory (BlueprintBook); unknown counts as not completed.
        energy_drop = self.refill_mode and self._completed_blueprint(drop)
        # Same rule as fights: against higher-level enemies nothing new is pulled while repair cools down. A loot run
        # past a Sniper right after a retreat (repair on cooldown, 79%) started the fight that killed the ship
        # (Vénar 16:10:06-26).
        repair = next((i for i in world.items if i.desc and "repair" in i.desc), None)
        allowed = 0 if (self.caution == "lower" and repair is not None and not repair.ready) else self.eff_max_pull
        if len(guards) > allowed and not energy_drop:
            reason = f"{len(guards)} enemies near it or on the way"
            # Clear them one by one, then collect (Mantis Hive: Cryonite waited behind 2-3 enemies that never left).
            for u in guards:
                self.path_blockers[u.unit_id] = (now, u.distance)
        elif self._threats() and not self._route_safe([(world.x, world.y), (drop.x, drop.y)]):
            reason = "a dangerous squad is near the way"
        if reason is None:
            return True
        key = (round(drop.x), round(drop.y))
        if now - self.loot_notes.get(key, -math.inf) > 10:  # log at most every 10 s per drop
            self.loot_notes[key] = now
            self._debug(f"not collecting {assets.display_name(drop.desc or drop.name)} at ({drop.x:.0f}, "
                        f"{drop.y:.0f}) yet: {reason}")
        return False

    def _completed_blueprint(self, drop):
        """True if the drop is a blueprint the player has completed (known from memory)."""
        if self.blueprints is None or drop.item_template is None:
            return False
        done = self.blueprints.completed(drop.item_template)
        if done and drop.item_template not in self.energy_bp_logged:
            self.energy_bp_logged.add(drop.item_template)
            self._debug(f"blueprint {drop.item_name} is completed: picking it up gives energy")
        return bool(done)

    def _pick_carefully(self, world, enemies):
        """Pull one enemy at a time: the nearest enemy that can be fought from a spot outside every other enemy's
        aggro range (the edge of a pack first), else the one pulling the fewest."""
        learner = self._learner("value")
        values = {}
        if learner is not None:
            for u in enemies:
                values[u.unit_id] = self._learn(lambda l: l.value(etype_of(u))) or 0.0
        # Learned value: a target worth more counts as up to half as far away (ties broken by worth, not detours).
        return min(enemies, key=lambda u: (self._pull_size(world, u), u.distance * (1 - 0.5 * values.get(u.unit_id, 0.0))))

    def _bp_hunters(self, world, enemies):
        """Enemies that often drop blueprints the player has completed (energy on pickup), for refilling when no
        energy field is at hand (switch "Completed blueprint hunting")."""
        learner = self._learner("bp_hunt")
        if learner is None or self.blueprints is None:
            return set()
        hunt = set()
        for u in enemies:
            rate = self._learn(lambda l: l.completed_rate(etype_of(u), self.blueprints.completed))
            if rate is not None and rate[0] >= 0.05:
                hunt.add(u.unit_id)
        if hunt:
            self._learn_note("bp-hunt", f"learning: refilling; also fighting {len(hunt)} enemies that often drop "
                                        f"completed blueprints", every=60)
        return hunt

    @staticmethod
    def _gold_blueprint(unit):
        """A gold (rare) blueprint drop: client sz sets "rare" and the item_blueprint_rare model for them."""
        mesh = getattr(unit, "mesh", None) or ""
        return unit.is_item and (getattr(unit, "rare", False) or ("blueprint" in mesh and "_rare" in mesh))

    def _collect(self, world, target, collector, status, gold=False):
        """Select a drop or orb, fly to it, and use the collector. Returns True when the object is gone.
        gold: a gold blueprint, tried three times as long before giving up."""
        cfg = self.config
        key = (round(target.x), round(target.y))
        now = time.monotonic()
        # The give-up timers count only time spent on this pickup: after a fight or other loot they start afresh.
        # They ran on through fights, so a gold blueprint was "12 s without progress" 4 s after the bot turned to it
        # (the timer dated from a Cryonite pickup before a fight) and was left behind (Molikar 16:01:33-48).
        tick = self.collect_tick
        if tick is None or tick[0] != key or now - tick[1] > COLLECT_GAP_S:
            self.collect = self.collect_progress = None
            self.select_tries.pop(key, None)
            if tick is None or now - tick[1] > COLLECT_GAP_S:
                self.collect_motion = None  # shared by nearby targets (see below), but not across a gap
        self.collect_tick = (key, now)
        if self.collect is None or self.collect[0] != key:
            self.collect = (key, now)
        elif now - self.collect[1] > cfg.give_up_s * (3 if gold else 1):
            self._give_up_pickup(target, key, now, gold, "Gave up collecting {name} for now")
            return False
        self.status = status
        label = f"{target.desc or ''} {target.name or ''} {assets.display_name(target.desc or target.name)}".lower()
        # Drones only for the moment of the pickup (user, 2026-11-16 recordings: on 2-5 s right before a pickup, off
        # right after; drone charge stayed at 1.0): the cryonite drone for cryonite, the energy drone for a completed
        # blueprint (only those give energy) or energy. Switched on once the drop is close.
        if target.distance < DRONE_PICKUP_NEAR:
            if "cryonite" in label:
                self.cryonite_pickup_until = now + DRONE_PICKUP_S
            elif "energy" in label or (getattr(target, "is_item", False) and self._completed_blueprint(target)):
                self.energy_pickup_until = now + DRONE_PICKUP_S
        # The collector beam running (item isActive; read live 2026-11-18: True while a drop is pulled in, False once
        # it arrives) is progress, however long it takes (some mission drops take longer than 12 s, user): no
        # give-up timer runs meanwhile, only a long safety limit.
        if collector is not None and collector.is_active and target.distance <= cfg.collect_range + 300:
            if self.collect_active is None or self.collect_active[0] != key:
                self.collect_active = (key, now)
            if now - self.collect_active[1] > COLLECT_ACTIVE_MAX_S:
                self._give_up_pickup(target, key, now, gold, f"Collector ran {COLLECT_ACTIVE_MAX_S:.0f} s on {{name}} "
                                                             f"without it arriving; retrying later")
                return False
            self.collect = (key, now)
            self.collect_progress = (key, target.distance, now)
            self.collect_motion = (world.x, world.y, now)
            self.select_tries.pop(key, None)
            return False
        self.collect_active = None
        nav = self._nav(world)
        # Selecting a drop flies STRAIGHT at it: only when that line keeps clear of walls; else along a route. And
        # when a straight approach gets no closer for COLLECT_STALL_S (Gazica 22:35: the ship pressed against a wall
        # 630 from it for 30 s and gave the blueprint up).
        if self.collect_progress is None or self.collect_progress[0] != key or \
                target.distance < self.collect_progress[1] - 200:
            self.collect_progress = (key, target.distance, now)
        stalled = now - self.collect_progress[2] > COLLECT_STALL_S
        # The ship itself not moving while collecting, whatever the target: two Energy Cells at about the same
        # distance took turns as "nearest", so the per-target give-up and stall timers restarted every switch, and
        # the ship sat pressed against something 1,155 from a cell for 4.5 min until the watchdog paused the bot
        # (Vénar 04:18:47-04:23:11). Give up everything within PICKUP_GIVE_UP_AREA of it for a while.
        motion = self.collect_motion
        # Standing in collector range is collecting, not stuck.
        if motion is None or math.hypot(world.x - motion[0], world.y - motion[1]) > 400 or \
                target.distance <= cfg.collect_range:
            self.collect_motion = (world.x, world.y, now)
        elif now - motion[2] > COLLECT_IDLE_S:
            if _is_orb(target):
                for u in world.units:
                    if _is_orb(u) and math.hypot(u.x - target.x, u.y - target.y) < PICKUP_GIVE_UP_AREA:
                        self.ignored[(round(u.x), round(u.y))] = now + PICKUP_RETRY_S[-1]
            self._give_up_pickup(target, key, now, gold, f"No progress collecting {{name}} for {COLLECT_IDLE_S:.0f} s; "
                                                         f"retrying later")
            return False
        start = (world.x, world.y)
        # A stalled straight approach takes a route at any distance (it was only beyond 1,200: the stall above
        # happened at 1,155); the route's stuck check backs the ship off the obstacle.
        # A route cannot help once the ship is at its end and that end is not the drop: a drop on open ground whose
        # planning cell counts as blocked (within 300 of a wall) gets a route to the nearest open cell, which can lie
        # outside collector range. The ship then sat there and the drop was never clicked (Molikar 19:24-19:27: a
        # gold blueprint 1,280 away, open ground on the fine map, route end = the ship's own spot). Then the drop is
        # selected and the game flies straight; a wall in the way ends in the idle give-up and a retry.
        route_end = nav.open_point(target.x, target.y) if nav is not None else None
        at_route_end = route_end is not None and \
            math.hypot(route_end[0] - target.x, route_end[1] - target.y) > 1 and \
            math.hypot(world.x - route_end[0], world.y - route_end[1]) < ROUTE_END_REACHED
        if nav is not None and target.distance > cfg.collect_range and not at_route_end and (
                stalled or target.distance > 1200 and (
                    not nav.line_clear(start, (target.x, target.y))
                    or nav.line_room(start, (target.x, target.y)) < COLLECT_WALL_ROOM)):
            self._move_toward(world, target.x, target.y, GOLD_LABEL if gold else "the pickup")
            return False
        selected = world.selected_target
        # The selection must be the drop itself: an enemy standing on a gold blueprint passed the position test, so
        # the drop was never clicked and the ship sat still for 47 s among 12 attackers until it died (12:51:44).
        if selected is not None and selected.unit_id is not None and target.unit_id is not None:
            is_selected = selected.unit_id == target.unit_id
        else:
            is_selected = selected is not None and abs(selected.x - target.x) < 150 and abs(selected.y - target.y) < 150
        if not is_selected:
            # A target that cannot be selected (the click picks another orb, e.g. one the game no longer lets you
            # take) is set aside: the bot clicked an Energy Cell 56 away for 4 min while the selection stayed on one
            # 200 away, standing still in collector range so no stall timer fired (Vénar 23:13:40-23:17:50, paused).
            first = self.select_tries.setdefault(key, now)
            if now - first > SELECT_GIVE_UP_S:
                self._give_up_pickup(target, key, now, gold, f"{{name}} at ({target.x:.0f}, {target.y:.0f}) cannot be "
                                                             f"selected; retrying later", level="debug")
                return False
            self._select(world, target)
            return False
        self.select_tries.pop(key, None)
        if target.distance > cfg.collect_range:
            return False
        if collector.ready:
            self._skill(collector)
            time.sleep(0.5)
        return False

    def _give_up_pickup(self, target, key, now, gold, text, level="warning"):
        """Set a pickup aside for a while, longer each time it fails again (never for good: it is retried while it
        is still there and in reach). text: log line, {name} is the drop's name."""
        fails = self.pickup_fails.get(key, 0)
        waits = GOLD_PICKUP_RETRY_S if gold else PICKUP_RETRY_S
        wait = waits[min(fails, len(waits) - 1)]
        self.pickup_fails[key] = fails + 1
        self.ignored[key] = now + wait
        line = text.format(name=assets.display_name(target.desc or target.name)) + f" (in {wait:.0f} s)"
        if level == "debug":
            self._debug(line)
        else:
            self._log(level, line)
        self.select_tries.pop(key, None)
        self.collect = self.collect_motion = self.collect_progress = self.collect_active = None

    def _energy_spots(self, world):
        """Remembered orb spots plus the site's energy fields for this planet, minus fields given up lately and
        fields inside danger zones."""
        now = time.monotonic()
        self.empty_spots = {k: v for k, v in self.empty_spots.items() if v > now}
        spots = [(s[0], s[1]) for s in self.orbs.spots.get(world.current_planet, [])]
        if self.config.use_site_energy_fields:
            info = myzen.planet_info(assets.display_name(world.current_planet))
            if info:
                spots += [tuple(p) for p in info["energy"]]
        return [s for s in spots if not self._in_danger(*s)
                and not any(math.hypot(s[0] - e[0], s[1] - e[1]) < 3000 for e in self.empty_spots)]

    def _refill(self, world, collector, share, forever=False):
        """Collect energy orbs. Returns True when refilling should end. forever: Collect Energy mode.
        At a field without orbs the bot waits for them to respawn (they come back quickly)."""
        now = time.monotonic()
        orbs = [u for u in world.units if _is_orb(u) and (round(u.x), round(u.y)) not in self.ignored
                and not self._in_danger(u.x, u.y)]
        full = share >= 0.995
        if orbs and collector and not full:
            target = min(orbs, key=lambda u: u.distance)
            if target.distance < 9000:
                self.at_field = target.distance < 4000
                self.orb_wait = None
                self._collect(world, target, collector, f"Collecting energy ({share * 100:.0f}%)")
                return False
        spots = self._energy_spots(world)
        if not spots:
            self._warn_once("no-orbs", "No energy field is known on this planet (or all are near danger)")
            return not forever
        spot = min(spots, key=lambda s: math.hypot(s[0] - world.x, s[1] - world.y))
        if math.hypot(spot[0] - world.x, spot[1] - world.y) < 1500:
            self.at_field = True
            if full:
                self.status = "Energy full; waiting at the field"
                return False
            # At the field and no orbs in reach: wait for the respawn; try another field after a while.
            if self.orb_wait is None or self.orb_wait[0] != spot:
                self.orb_wait = (spot, now)
            if now - self.orb_wait[1] > self.config.orb_wait_s:
                self._debug(f"no orbs at ({spot[0]:.0f}, {spot[1]:.0f}) for {self.config.orb_wait_s:.0f} s")
                self.empty_spots[spot] = now + 300
                self.orb_wait = None
                return False
            self.status = "Waiting for orbs to respawn"
            return False
        self.status = f"Flying to energy ({share * 100:.0f}%)"
        if not self._move_toward(world, spot[0], spot[1], "energy orbs"):
            self.empty_spots[spot] = now + 300
        return False
