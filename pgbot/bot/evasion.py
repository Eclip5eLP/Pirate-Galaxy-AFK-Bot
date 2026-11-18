"""Getting away: squad escapes, spawn points, strikes, stun turrets.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game import assets
from ..data import myzen
from .constants import *  # noqa: F401,F403
from .util import _closest_approach, _point_along, _segment_distance, _load_state, _save_state


class EvasionMixin:
    def _avoid_point(self, world, squads_only=False):
        """Predicate for patrol targets: near a squad that was actually seen. Known Death Squad routes from the site
        are not avoided: squads pass through tunnels a ship must use too."""
        # Only squads in view count. Skipping targets near old sightings or on squad routes pushed every new target
        # into the landing pocket, and the bot never left it (user, 2026-11-11).
        threats = self._threats()
        # Twice the keep-away distance: points 8,000-9,000 from a squad standing at its spawn point kept being picked
        # (the learned patrol bonus likes that area), every way to them passed the squad, and the bot went back and
        # forth between them and the far side of the map without fighting (user, Vénar 2026-11-15 14:22-14:26).
        reach = self.danger_radius * PATROL_SQUAD_SHARE + DANGER_TARGET_MARGIN
        # Seek targets are the sought enemies' spawn areas: enemies there are the point, not a reason to skip them.
        zones = [] if squads_only or self.seek is not None else self._path_zones(world)

        def avoid(x, y):
            return any(math.hypot(x - t[0], y - t[1]) < reach for t in threats) or \
                any(math.hypot(x - zx, y - zy) < zr for zx, zy, zr in zones)
        return avoid

    def _flee(self, world, items, threats=None, status="Avoiding a dangerous squad", velocity=None, keep_s=1.0):
        """Fly to an escape spot chosen against where the threats are heading (a squad by default). The spot is kept
        until it is reached, 5 s pass, or the threats get closer to it than the ship. Afterburner when close."""
        now = time.monotonic()
        self.status = status
        self.target_id = None
        if threats is None:
            threats = self._threats()
            if not threats:
                threats = [(d[0], d[1]) for d in self.dangers]
        threats = [tuple(t) + (0.0, 0.0) if len(t) == 2 else tuple(t) for t in threats]
        if velocity is not None:
            threats = [(t[0], t[1], *velocity) for t in threats]
        if not threats:
            return
        nearest = min(math.hypot(world.x - t[0], world.y - t[1]) for t in threats)
        reached = self.flee_point is not None and math.hypot(self.flee_point[0] - world.x,
                                                             self.flee_point[1] - world.y) < 1200
        # keep_s: a spot is kept at least this long before "overtaken" / "toward" may replace it. Retreating from
        # attackers all round (Vénar 21:32:23-28, 6-7 Assassins) re-picked every second between opposite sides and the
        # ship hardly moved; squads keep 1 s (they must be dodged as soon as they turn).
        # A squad chasing the ship: hold the spot longer (a straight run). Re-picked every second, the escape went
        # west, north, west while a squad with Signors kept 1,700-3,000 behind and killed the ship (Vénar 17:38:51-39:13).
        t = min(threats, key=lambda t: math.hypot(world.x - t[0], world.y - t[1]))
        gap = math.hypot(world.x - t[0], world.y - t[1]) or 1.0
        if (t[2] * (world.x - t[0]) + t[3] * (world.y - t[1])) / gap > CHASE_SPEED:
            keep_s = max(keep_s, CHASE_KEEP_S)
        settled = self.flee_point is not None and now - self.flee_time < keep_s
        overtaken = not settled and self.flee_point is not None and min(
            math.hypot(self.flee_point[0] - t[0], self.flee_point[1] - t[1]) for t in threats) < nearest
        # Re-checked every second: an escape toward a member's current position is dropped at once (run 21:10:43:
        # the squad turned into the tunnel the escape crossed, and the spot was kept until it was too late).
        toward = self.flee_point is not None and not settled and now - self.flee_time > 1.0 and not self._heads_away(
            world, self.route if self.route and math.hypot(self.route[-1][0] - self.flee_point[0],
                                                           self.route[-1][1] - self.flee_point[1]) < 800
            else [(world.x, world.y), self.flee_point], threats)
        if self.flee_point is None or reached or overtaken or toward or now - self.flee_time > 5:
            self.flee_point, self.flee_time = self._flee_target(world, threats), now
        afterburner = next((i for i in items if i.desc and "afterburner" in i.desc), None)
        # The afterburner is for a fast getaway (user, 2026-11-15): used when the bot is actually running away - its
        # escape spot lies GETAWAY_MIN or more ahead - not by enemy distance, closing speed or hull (those rules fired
        # it on squads still far off, then too late for a chasing one, then at 90% hull mid-retreat).
        running = self.flee_point is not None and (
            math.hypot(self.flee_point[0] - world.x, self.flee_point[1] - world.y) >= GETAWAY_MIN
            # Fleeing from a squad or attackers already within the keep-away distance is running away too: a squad
            # 1,400 away got short escape spots, no afterburner, and caught the ship (Vénar 17:30:25-46, death).
            or nearest < self.danger_radius)
        if afterburner and afterburner.ready and not afterburner.is_active and running:
            if self._getaway_note < now - 10:
                self._getaway_note = now
                self._debug(f"afterburner: running away ({status})")
            self._skill(afterburner)
        if self.flee_point is not None:
            if not self._move_toward(world, *self.flee_point, "safety"):
                self.flee_point = None

    def _watch_spawns(self, world):
        """(x, y) of a squad dropping in near the ship, else None. Two tells: enemies that were not there a moment
        ago appearing within SPAWN_NEAR (the client knows units 20,000+ away, so a close newcomer was just put down)
        - and enemies far above the ship's height (falling). Either counts only for units arriving together (see
        _drop_group). The spot is avoided for SPAWN_AVOID_S."""
        now = time.monotonic()
        if self.caution == "high":  # high level farming dodges nothing (user, 2026-11-17)
            self.spawn_threat = None
            return None
        if self._spawn_zone != self.zone_since:  # a new zone (or a rescue): everything is new again
            self._spawn_zone = self.zone_since
            self.unit_seen, self.new_arrivals, self.falling_seen = {}, [], {}
        # Attack Droids are put down by others and fly high (13:30:35-48: one or two at a time seen "falling");
        # they are no squad.
        # Harvesters put down Interceptors in groups (user, 2026-11-15): no squad either.
        enemies = [u for u in self._enemy_units(world)
                   if not any(k in (u.desc or "") for k in ("attackdroid", "interceptor"))]
        settled = now - self.zone_since > SPAWN_GRACE_S
        for u in enemies:
            if u.unit_id is None or u.unit_id in self.unit_seen:
                continue
            self.unit_seen[u.unit_id] = now
            if settled and u.distance < SPAWN_NEAR:
                self.new_arrivals.append((now, u.x, u.y, u.desc or ""))
        self.new_arrivals = [a for a in self.new_arrivals if now - a[0] < SPAWN_WINDOW_S]
        found = None
        # Every enemy drops in from the sky, at an angle from a random direction; normal enemies one by one somewhere
        # in their area, a squad together at its fixed spawn point (user, 2026-11-15). So only units arriving
        # together count: SPAWN_MIN_UNITS within SPAWN_GROUP of each other, or a Custodian with a Resurrector.
        arrivals = [(a[1], a[2], a[3]) for a in self.new_arrivals]
        found = self._drop_group(arrivals, "appeared")
        if found is None and settled and world.ship_height is not None:
            # Members do not all fall at the same moment (13:20:01: the Custodian first, the squad recognised 5 s
            # later at 3,700): falling units are kept for SPAWN_WINDOW_S and grouped together.
            recent = self.falling_seen
            for u in enemies:
                if u.height is not None and u.distance < SPAWN_NEAR and u.height - world.ship_height > SPAWN_HEIGHT:
                    recent[u.unit_id] = (now, u.x, u.y, u.desc or "")
            for key in [k for k, v in recent.items() if now - v[0] > SPAWN_WINDOW_S]:
                recent.pop(key)
            falling = [(v[1], v[2], v[3]) for v in recent.values()]
            found = self._drop_group(falling, "falling")
            if found is None and falling and now - self._falling_note > SPAWN_WINDOW_S:
                self._falling_note = now
                self._debug(f"enemies falling, not together: "
                            f"{', '.join(assets.display_name(d or '?') for _, _, d in falling)} (normal respawns)")
        if found is not None:
            x, y, why = found
            # The site data knows where this planet's squads roam: a group dropping in far from all of it is a
            # spot's group respawn (3 Endurers on Earth in Unrest were taken for a squad and learned as two spawn
            # points 9,000-13,000 from any squad area; user, 2026-11-16) - not learned as a spawn point. Still moved
            # away from: 4 level 89-90 enemies dropping on the ship at once killed it (Earth in Unrest 19:14:10-57).
            squad = self._squad_area_near(world, x, y)
            if self.spawn_threat is None or now > self.spawn_threat[2]:
                what = "Squad" if squad else "Enemy group"
                self._log("warning", f"{what} dropping in at ({x:.0f}, {y:.0f}), "
                                     f"{math.hypot(world.x - x, world.y - y):.0f} away: {why}; moving away")
            self.spawn_threat = (x, y, now + SPAWN_AVOID_S)
            # Only a real Death Squad (Custodian + Resurrector) marks a spawn point: 3-4 ordinary Marauders and
            # Rocketeers arriving together made 24 "spawn points" all over Molikar (2026-11-17; the site's squad
            # area covers the whole planet there, so that check let everything through).
            if squad and "Custodian" in why and "Resurrector" in why:
                self._note_spawn_point(world, x, y)
        if self.spawn_threat is None or now > self.spawn_threat[2]:
            return None
        x, y, _ = self.spawn_threat
        if math.hypot(world.x - x, world.y - y) > self.danger_radius:
            return None  # far enough: normal squad handling takes over from here
        return (x, y)

    @staticmethod
    def _drop_group(units, verb):
        """(x, y, why) of a squad arriving together among units [(x, y, desc)], else None."""
        for x, y, _ in units:
            group = [u for u in units if math.hypot(u[0] - x, u[1] - y) < SPAWN_GROUP]
            descs = [d for _, _, d in group]
            together = any("custodian" in d for d in descs) and any("resurrector" in d for d in descs)
            if len(group) >= SPAWN_MIN_UNITS or together:
                names = ", ".join(sorted({assets.display_name(d or "?") for d in descs}))
                return (sum(u[0] for u in group) / len(group), sum(u[1] for u in group) / len(group),
                        f"{len(group)} enemies {verb} together ({names})")
        return None

    def _squad_area_near(self, world, x, y):
        """True if the site data puts a death squad area within SITE_SQUAD_NEAR of (x, y), or has no squad data for
        this planet (then every group drop counts)."""
        planet = assets.display_name(getattr(world, "current_planet", None) or "")
        cache = self._squad_cells
        if planet not in cache:
            try:
                cache[planet] = myzen.death_squad_cells(planet) or []
            except Exception:
                cache[planet] = []
        cells = cache[planet]
        return not cells or any(math.hypot(x - cx, y - cy) < SITE_SQUAD_NEAR for cx, cy in cells)

    def _spawn_points(self, world):
        """Known squad spawn points of this planet (learned from drops seen, kept between runs)."""
        if self.caution == "high":  # high level farming dodges nothing, retreats included (user, 2026-11-17)
            return []
        planet = assets.display_name(world.current_planet or "")
        cache = self._spawn_cache
        if cache is None or cache[0] != planet or time.monotonic() - cache[1] > 10:
            points = [tuple(p) for p in _load_state().get("spawn_points", {}).get(planet, [])]
            self._spawn_cache = cache = (planet, time.monotonic(), points)
        return cache[2]

    def _note_spawn_point(self, world, x, y):
        """Remember where a squad dropped in (merged with a known point within SPAWN_MERGE)."""
        planet = assets.display_name(world.current_planet or "")
        state = _load_state().get("spawn_points", {})
        points = [list(p) for p in state.get(planet, [])]
        for p in points:
            if math.hypot(p[0] - x, p[1] - y) < SPAWN_MERGE:
                p[0], p[1] = (p[0] * 3 + x) / 4, (p[1] * 3 + y) / 4  # drift toward the drops seen
                break
        else:
            points.append([x, y])
            self._log("info", f"Learned a squad spawn point on {planet} at ({x:.0f}, {y:.0f}); keeping clear of it")
        state[planet] = [[round(p[0]), round(p[1])] for p in points]
        _save_state({"spawn_points": state})
        self._spawn_cache = None

    def _escape_ready(self, world):
        """Afterburner and repair both ready: a squad dropping in close could be outrun."""
        afterburner = next((i for i in world.items if i.desc and "afterburner" in i.desc), None)
        repair = next((i for i in world.items if i.desc and "repair" in i.desc), None)
        return (afterburner is None or afterburner.ready) and (repair is None or repair.ready)

    def _near_spawn(self, world, x, y):
        if self.caution == "high":  # high level farming dodges nothing (user, 2026-11-17)
            return False
        return any(math.hypot(x - sx, y - sy) < SPAWN_KEEP for sx, sy in self._spawn_points(world))

    def _note_strikes(self, world):
        """Learn the blast radius: when an enemy marker within STRIKE_LOG_NEAR goes off, log the ship's distance from
        it and the hull change over the next second (the radius is in no game data)."""
        now = time.monotonic()
        hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else None
        current = {(round(x), round(y)): math.hypot(world.x - x, world.y - y) for x, y in getattr(world, "strikes", ())}
        for spot, distance in self.strike_watch.items():
            if spot not in current and distance < STRIKE_LOG_NEAR:
                self.strike_pending.append((now + 1.0, f"enemy Orbital Strike at {spot} went off {distance:.0f} "
                                                       f"from the ship", hull))
        self.strike_watch = current
        for item in [p for p in self.strike_pending if now >= p[0]]:
            self.strike_pending.remove(item)
            before = item[2]
            change = f"hull {before * 100:.0f}% -> {hull * 100:.0f}%" if before is not None and hull is not None else ""
            self._debug(f"{item[1]}; {change}")

    @staticmethod
    def _is_stun_turret(unit):
        return "stunturret" in (unit.desc or "")

    def _stun_zones(self, world):
        """(x, y, reach) of living Stun Turrets: their area stun (user) reaches about their aggro range; the data has
        no stun radius, so STUN_REACH_MARGIN is added."""
        return [(u.x, u.y, max(u.aggression_range or 0, 3000) + STUN_REACH_MARGIN)
                for u in self._enemy_units(world) if self._is_stun_turret(u)]

    def _leave_stun_zone(self, world):
        """Inside a Stun Turret's reach: move to the nearest spot outside every turret's reach (a clear straight line
        first). True while moving out."""
        zones = self._stun_zones(world)
        inside = [z for z in zones if math.hypot(world.x - z[0], world.y - z[1]) < z[2]]
        if not inside:
            return False
        zx, zy, zr = min(inside, key=lambda z: math.hypot(world.x - z[0], world.y - z[1]))
        nav = self._nav(world)
        start = (world.x, world.y)
        best, best_d, best_clear = None, math.inf, False
        for step in range(16):
            angle = step * math.pi / 8
            tx, ty = zx + math.cos(angle) * (zr + 400), zy + math.sin(angle) * (zr + 400)
            point = nav.open_point(tx, ty) if nav is not None else (tx, ty)
            if point is None or any(math.hypot(point[0] - x, point[1] - y) < r for x, y, r in zones):
                continue
            clear = nav is None or nav.line_clear(start, point)
            if not clear and best_clear:
                continue
            d = math.hypot(point[0] - world.x, point[1] - world.y)
            if (clear and not best_clear) or d < best_d:
                best, best_d, best_clear = point, d, clear
        if best is None:
            return False
        if self.status != "Leaving a Stun Turret's reach":
            self._debug(f"other enemies attack inside a Stun Turret's reach ({zx:.0f}, {zy:.0f}); moving to "
                        f"({best[0]:.0f}, {best[1]:.0f})")
        self.status = "Leaving a Stun Turret's reach"
        return bool(self._move_toward(world, *best, STUN_LABEL, face=False))

    def _energy_planet_ok(self, world):
        """Terasa can serve as the energy fallback: travel by gate possible from here and not found unreachable before
        (the player may not have unlocked it; then the fallback is ignored, user)."""
        if _load_state().get("energy_planet_unreachable") or not self.config.energy_fallback:
            return False
        here = self._system_of(world.current_planet)
        return here not in NO_GATE_SYSTEMS and myzen.planet_info(ENERGY_PLANET) is not None

    def _dodge_strike(self, world):
        """Move out of every enemy Orbital Strike marker's blast (STRIKE_RADIUS + STRIKE_MARGIN). Of 16 spots around
        the nearest marker just outside all blasts, the one closest to the ship is taken. True while dodging."""
        reach = STRIKE_RADIUS + STRIKE_MARGIN
        near = [s for s in getattr(world, "strikes", ()) if math.hypot(world.x - s[0], world.y - s[1]) < reach]
        if not near:
            return False
        sx, sy = min(near, key=lambda s: math.hypot(world.x - s[0], world.y - s[1]))
        nav = self._nav(world)
        start = (world.x, world.y)
        squad_now = self._threats()
        # Keep the spot chosen for this marker (markers live ~5 s and never move): re-chosen every tick, the spot
        # changed with the moving enemies and the ship zigzagged, covering ~1,100 in 6 s instead of reaching a spot
        # 3,900 away (Vénar 21:11:48-54, hit, death). Dropped only if a blast or a squad member now covers it.
        kept = self.dodge_spot
        if kept is not None and kept[0] == (round(sx), round(sy)) and time.monotonic() - kept[2] < STRIKE_KEEP_S:
            px, py = kept[1]
            if not any(math.hypot(px - x, py - y) < reach for x, y in world.strikes) and not any(
                    math.hypot(px - t[0], py - t[1]) < STRIKE_SQUAD_KEEP for t in squad_now):
                return self._dodge_move(world, kept[1])
        # Not back into the fight: a dodge to the spot nearest the ship took it back toward the attackers it was
        # retreating from (Vénar 14:53:24, death 14:53:40). Enemies near a spot cost like an escape through them, and
        # during a retreat the spot leans toward the retreat's target.
        enemies_now = [(u.x, u.y) for u in self._enemy_units(world)]
        heading = self.flee_point if self.retreating and self.flee_point is not None else None
        # A straight, clear line first: the 13:22 dodge ran into a wall (user saw it live) and stayed in the blast.
        # Only if no spot has one, the one with the shortest way around walls.
        best, best_d, best_clear = None, math.inf, False
        for step in range(16):
            angle = step * math.pi / 8
            tx, ty = sx + math.cos(angle) * (reach + 300), sy + math.sin(angle) * (reach + 300)
            point = nav.open_point(tx, ty) if nav is not None else (tx, ty)
            if point is None or any(math.hypot(point[0] - x, point[1] - y) < reach for x, y in world.strikes):
                continue
            # Not toward a squad member: out of the blast but into the squad is no escape.
            if any(math.hypot(point[0] - t[0], point[1] - t[1]) < min(STRIKE_SQUAD_KEEP, math.hypot(
                    world.x - t[0], world.y - t[1])) for t in squad_now):
                continue
            clear = nav is None or nav.line_clear(start, point)
            if clear:
                d = math.hypot(point[0] - world.x, point[1] - world.y)
            elif best_clear:
                continue
            else:
                route = nav.path(start, point, [])
                if route is None:
                    continue
                d = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))
            d += ESCAPE_ZONE_COST * sum(1 for ex, ey in enemies_now
                                        if math.hypot(point[0] - ex, point[1] - ey) < ESCAPE_END_CLEAR)
            if heading is not None:
                d += 0.5 * math.hypot(point[0] - heading[0], point[1] - heading[1])
            if (clear and not best_clear) or d < best_d:
                best, best_d, best_clear = point, d, clear
        if best is None:
            return False
        if self.status != "Dodging an Orbital Strike":
            self._debug(f"enemy Orbital Strike at ({sx:.0f}, {sy:.0f}), "
                        f"{math.hypot(world.x - sx, world.y - sy):.0f} away; moving to ({best[0]:.0f}, {best[1]:.0f})")
        self.dodge_spot = ((round(sx), round(sy)), best, time.monotonic())
        return self._dodge_move(world, best)

    def _dodge_move(self, world, best):
        """Fly to the strike dodge spot, shooting back meanwhile. True while dodging."""
        self.status = "Dodging an Orbital Strike"
        if not self._move_toward(world, *best, STRIKE_LABEL, face=False):
            # No way out: fight on rather than stand still (13:21:58-13:22:13: the dodge was refused every tick
            # because its way passed an enemy; the ship stood still, not firing, 76% -> 36%).
            return False
        # Keep shooting while dodging, like the retreat: Signors cast a strike every few seconds, and dodging without
        # firing left two Signors and a Rocketeer to wear the ship down at ~10%/s without losses of their own (Vénar
        # 17:20:15-39, death; every strike dodged).
        attackers = [u for u in self._enemy_units(world) if self._attacking(world, u)]
        if attackers:
            blaster, _, _, attacks, _ = self._items(world)
            self._shoot_back(world, attackers, blaster, attacks)
        return True

    def _flee_target(self, world, threats):
        """Score reachable open spots in 16 directions at 6,000 / 9,000 / 12,000: distance from where the squad will
        be (current positions moved along its heading for the flight time), minus part of the flight length. Routes
        passing near the squad are rejected."""
        nav = self._nav(world)
        start = (world.x, world.y)
        # First choice: escapes that also lead away from where the members are now. Forecasts alone failed: squads
        # follow curved routes, and an escape "past" a squad heading elsewhere met it when it turned (run 21:10:43).
        radius = self.danger_radius
        # One search from the ship serves every candidate route of every pass (it took 5 s per step before).
        now = time.monotonic()
        marks = [(sx, sy) for sx, sy, t in self.stuck_spots if now - t < STUCK_MEMORY_S]
        tree = nav.tree(start, marks) if nav is not None else None
        for keep, away in ((radius * 0.6, True), (radius * 0.3, True), (radius * 0.3, False), (0.0, False)):
            best = self._best_escape(world, nav, start, threats, keep, away, tree)
            if best is not None:
                break
        if best is None and getattr(world, "strikes", None):
            # Nothing passes the strike-line test (a marker on one side, a squad on the other): a line past the edge
            # of a blast beats no escape at all ("flee to None" for 2 s next to a marker, Vénar 03:55:24-25).
            best = self._best_escape(world, nav, start, threats, 0.0, False, tree, strike_lines=False)
        heads = ", ".join(f"({t[2]:.0f}, {t[3]:.0f})" for t in threats[:6])
        self._debug(f"flee to {best and (round(best[0]), round(best[1]))}, members moving {heads} u/s (keep {keep:.0f})")
        return best

    def _heads_away(self, world, route, threats):
        """True if flying the route for AWAY_CHECK_S brings the ship no closer to the current position of any threat
        within twice the keep-away distance."""
        ahead = _point_along(route, SHIP_SPEED * AWAY_CHECK_S)
        reach = self.danger_radius * 2
        for t in threats:
            now_distance = math.hypot(world.x - t[0], world.y - t[1])
            if now_distance < reach and math.hypot(ahead[0] - t[0], ahead[1] - t[1]) < now_distance - AWAY_SLACK:
                return False
        return True

    def _best_escape(self, world, nav, start, threats, keep, away=False, tree=None, strike_lines=True):
        """Best reachable escape spot. For each candidate the ship is moved along its route at SHIP_SPEED and the
        squad along its current heading, second by second (up to 12 s); the two must never come closer than `keep`.
        Choice: the SHORTEST flight among spots that end safely (ESCAPE_SAFE_SHARE of the keep-away distance from
        the squad, also if it stays put), with a penalty on squad routes: a short step aside, not a retreat across
        the map or into the landing pocket (user, 2026-11-11). No bonus toward the ship's target: squads follow
        curved routes, and that would lean escapes toward them (user). If no spot ends safely: the one ending
        furthest from the squad."""
        on_route = self._ds_route_check(world)
        now = time.monotonic()
        marks = [(sx, sy) for sx, sy, t in self.stuck_spots if now - t < STUCK_MEMORY_S]
        strikes = getattr(world, "strikes", None) or []
        safe_enough = self.danger_radius * ESCAPE_SAFE_SHARE
        # (Escapes are not judged against the squad's predicted way: near spots all failed against a looping squad
        # and the bot ran very far after it had passed, user 2026-11-14 23:35. The short dodge was fine.)
        # Other enemies along the way count heavily: a retreat from 8 Assassins fled through more of them, 14
        # attackers, dead (Vénar 23:33).
        bystanders = self._aggro_zones(world, margin=False)
        # Where the escape ends counts too: fleeing a squad, the ship ended next to a Sniper group with repair on
        # cooldown and died (Vénar 14:47:23-43). Every enemy within ESCAPE_END_CLEAR of the end costs like a zone.
        # Bystanders only: squad members and attackers are what the escape is judged against already.
        others = [(u.x, u.y) for u in self._enemy_units(world)
                  if u.unit_id not in self.danger_ids and not self._attacking(world, u)]
        # Not into or through a Stun Turret's reach: a squad escape went past one 1,178 away, the ship stood stunned
        # for 34 s while the squad's Marauders shot it, 80% -> dead (Earth in Unrest 19:23:04-38). Leaving a reach
        # the ship is already in is fine (the line starts inside it).
        stuns = self._stun_zones(world)
        best, best_cost = None, math.inf
        fallback, fallback_score = None, -math.inf
        for step in range(16):
            angle = step * math.pi / 8
            for distance in (4000, 6000, 9000, 12000, 16000):
                tx, ty = world.x + math.cos(angle) * distance, world.y + math.sin(angle) * distance
                point = nav.open_point(tx, ty) if nav is not None else (tx, ty)
                if point is None:
                    continue
                # Beyond the map edge a candidate snaps back to the open ground at the edge - next to the ship, the
                # "cheapest" escape: the ship sat at the map's bottom edge for 16 s while a squad came to it (Vénar
                # 15:34:17-33, death). Candidates that end up much closer than intended are no escape.
                if math.hypot(point[0] - world.x, point[1] - world.y) < distance * ESCAPE_MIN_SHARE:
                    continue
                # Not into a live Orbital Strike's blast, nor through it: a squad escape ran past a marker 2,260 away
                # as the strike went off, 51% -> 1% (Vénar 03:30:56-31:01, death). Leaving a blast the ship is already
                # in is fine (the line starts inside it), as long as it does not cross the marker.
                reach = STRIKE_RADIUS + STRIKE_MARGIN
                if any(math.hypot(point[0] - sx, point[1] - sy) < reach for sx, sy in strikes):
                    continue
                if strike_lines and any(_segment_distance((sx, sy), start, point) < reach
                                        and math.hypot(start[0] - sx, start[1] - sy) >= reach for sx, sy in strikes):
                    continue
                if strike_lines and any(_segment_distance((sx, sy), start, point) < STRIKE_RADIUS * 0.5
                                        for sx, sy in strikes):
                    continue
                if any(math.hypot(point[0] - zx, point[1] - zy) < zr for zx, zy, zr in stuns):
                    continue
                if any(_segment_distance((zx, zy), start, point) < zr and math.hypot(start[0] - zx, start[1] - zy) >= zr
                       for zx, zy, zr in stuns):
                    continue
                # Escapes respect recent stuck marks (2026-11-11: the retreat kept choosing the way through the wall
                # the ship was pressed against).
                if nav is not None and (marks or not nav.line_clear(start, point)):
                    route = nav.route(tree, start, point) if tree is not None else nav.path(start, point, marks)
                    if route is None:
                        continue
                else:
                    route = [start, point]
                length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))
                if away and not self._heads_away(world, route, threats):
                    continue
                closest, final = _closest_approach(route, threats)
                if closest < keep:
                    continue
                staying = min(math.hypot(point[0] - t[0], point[1] - t[1]) for t in threats)
                ends = min(final, staying)
                crowd = self._zones_crossed(route, bystanders) if bystanders else 0
                crowd += sum(1 for ox, oy in others if math.hypot(point[0] - ox, point[1] - oy) < ESCAPE_END_CLEAR)
                cost = length + (3000 if on_route(*point) else 0) + ESCAPE_ZONE_COST * crowd
                # keep 0 is the last pass (every escape comes close to the squad somewhere): there the shortest one
                # that ends safely may lead straight through it (Vénar 2026-11-15 12:30:03, the escape turned from
                # west to east through a 10-unit squad 1,300 away, dead 9 s later). So that pass takes the escape
                # that stays furthest from the squad on the way (fallback below).
                if keep > 0 and ends >= safe_enough and cost < best_cost:
                    best, best_cost = point, cost
                score = min(closest, ends) - 0.35 * length - ESCAPE_ZONE_COST * crowd
                if score > fallback_score:
                    fallback, fallback_score = point, score
        return best if best is not None else fallback

    def _ds_route_check(self, world):
        """Predicate: a point lies on a Death Squad patrol route of this planet (site data) while a squad was seen
        in the last SQUAD_RECENT_S. Routes stay passable; this only keeps fights, patrol points and escape spots
        off them while a squad is about."""
        now = time.monotonic()
        if not self.config.avoid_dangers or now - self.last_squad_seen > SQUAD_RECENT_S:
            return lambda x, y: False
        planet = assets.display_name(world.current_planet)
        if self.route_cells_planet != planet:
            self.route_cells_planet = planet
            self.route_cells = {(round(cx / 1674), round(cy / 1674)) for cx, cy in myzen.death_squad_cells(planet)}
        cells = self.route_cells

        def on_route(x, y):
            gx, gy = round(x / 1674), round(y / 1674)
            return any((gx + dx, gy + dy) in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1))
        return on_route
