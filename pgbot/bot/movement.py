"""Moving on a planet: route planning, being stuck, selecting units.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game import assets
from ..game.navmap import NavMap
from .constants import *  # noqa: F401,F403
from .util import _point_along, _segment_distance


class MovementMixin:
    def _nav(self, world):
        name = world.planet_map
        if not name:
            return None
        if name not in self.navmaps:
            try:
                self.navmaps[name] = NavMap(name)
            except (FileNotFoundError, ValueError) as error:
                self._warn_once(f"nav-{name}", f"No terrain map for this planet ({error}); flying straight")
                self.navmaps[name] = None
        nav = self.navmaps[name]
        if nav is not None and time.monotonic() - self.learn_route_at.get(name, -math.inf) > 60:
            self.learn_route_at[name] = time.monotonic()
            learner = self._learner("routes", name)
            costs = self._learn(lambda l: l.route_costs(name)) if learner is not None else None
            nav.learned_cost = costs or {}
            nav.learned_mask[:] = False
            for cx, cy in nav.learned_cost:
                nav.learned_mask[cy, cx] = True
            if costs:
                self._learn_note(f"routes-{name}", f"learning: {len(costs)} trouble spot(s) on this planet get a route "
                                                   f"cost")
        return nav

    def _check_stuck(self, world, nav):
        """Called while moving. Detect no progress and remember the blocked spot ahead of the ship."""
        now = time.monotonic()
        if now - self.last_move > 2.5:
            # No move clicked lately (e.g. waiting for a squad to pass): standing still is intended, not stuck.
            self.move_history.clear()
            return False
        self.move_history.append((now, world.x, world.y))
        # Samples come about once a second, so keep 5 s and judge over at least 3.5 s.
        self.move_history = [p for p in self.move_history if now - p[0] <= 5.0]
        oldest = self.move_history[0]
        if now - oldest[0] < 3.5 or math.hypot(world.x - oldest[1], world.y - oldest[2]) > 400:
            return False
        self.move_history.clear()
        self.route = None
        # What the game showed: 22:20:51-22:21:03 the ship did not move for 12 s despite move clicks every second and
        # the afterburner, with no stunner near (user); a screenshot of the game window (logs/, at most every 20 s).
        if now - self.stuck_shot_at > 20:
            self.stuck_shot_at = now
            self._snapshot("stuck")
        if self._nearest_member(world) < self.danger_radius:
            # Not moving next to a squad is the Custodian Commander's stun, not a wall: mark nothing.
            self._debug(f"not moving at ({world.x:.0f}, {world.y:.0f}) next to a squad (stunned?)")
            return True
        heading = world.heading or (0, 0)
        length = math.hypot(*heading) or 1
        spot = (world.x + heading[0] / length * 700, world.y + heading[1] / length * 700)
        self.stuck_spots = [s for s in self.stuck_spots if now - s[2] < STUCK_MEMORY_S][-20:] + [(*spot, now)]
        # Learning records it as one occasion; a route cost needs repeats on separate occasions (docs/LEARNING.md).
        self._learn(lambda learner: learner.stuck(world.planet_map, *spot, nav))
        self._debug(f"stuck at ({world.x:.0f}, {world.y:.0f}); marking ({spot[0]:.0f}, {spot[1]:.0f}) as blocked")
        # No permanent "learned walls": at the Prosperous tunnel neck the ship was stuck because the camera sat inside
        # the wall and clicks hit the wall (user, 2026-11-11), not because of a missing wall; learning it would have
        # blocked the only way out of the landing area.
        # Back off: one short hop to the spot with the most room to the mapped walls nearby. Not based on the
        # heading: at the Prosperous tunnel neck the wall was behind the ship (north) while it tried to go south.
        hop = nav.open_spot_near(world.x, world.y, 1200)
        # Never back into a live Orbital Strike blast: a dodge stuck on a wall hopped to 1,250 from the marker and the
        # strike took 97% -> 42% (Vénar 20:08:55-58, death). Further out if needed to leave the blast.
        blasts = [(sx, sy, STRIKE_RADIUS + STRIKE_MARGIN) for sx, sy in getattr(world, "strikes", ())]
        if blasts:
            hop = (nav.open_spot_near(world.x, world.y, 1200, blasts)
                   or nav.open_spot_near(world.x, world.y, STRIKE_RADIUS + STRIKE_MARGIN + 600, blasts) or hop)
        if hop is not None:
            self.unstick = (hop, now + UNSTICK_S)
            self._debug(f"unstick hop to ({hop[0]:.0f}, {hop[1]:.0f})")
        return True

    def _move_toward(self, world, x, y, label, face=True):
        """Fly toward a world point. Returns False once the point is given up (no route, no progress for GIVE_UP_S
        seconds, or a squad blocked the way for SQUAD_WAIT_S), else True. self.squad_blocked tells whether this call
        held position for a squad instead of moving."""
        now = time.monotonic()
        self.squad_blocked = False
        self.enemy_blocked = False
        # Progress toward a goal only counts while the bot keeps flying to it: after a fight on the way the old
        # timer made the goal look hopeless and it was given up.
        if self.progress is not None and now - self.progress_touch > 3.0:
            self.progress = None
        self.progress_touch = now
        nav = self._nav(world)
        if nav is not None:
            point = nav.open_point(x, y)
            if point is not None:
                x, y = point
        goal = (round(x, -2), round(y, -2))
        distance_left = math.hypot(x - world.x, y - world.y)
        if self.progress is None or self.progress[0] != goal:
            self.progress = [goal, distance_left, now]
        elif distance_left < self.progress[1] - 300:
            self.progress[1:] = [distance_left, now]
        elif now - self.progress[2] > GIVE_UP_S:
            self._debug(f"no progress toward {label} ({x:.0f}, {y:.0f}) for {GIVE_UP_S} s; giving it up")
            self.progress = None
            self.route = None
            return False
        fleeing = label in ("safety", STRIKE_LABEL, STUN_LABEL)  # escapes pass enemies (a strike dodge is ~1,700 units)
        # Escapes (often with the afterburner) click further ahead and twice as often: with 2,200-unit clicks once a
        # second the boosted ship reached each point early and stopped, no faster than flying normally (user).
        if now - self.last_move < (FLEE_CLICK_S if fleeing else 1.0):
            return True
        start = (world.x, world.y)
        if nav is not None:
            stuck = self._check_stuck(world, nav)
            # With a squad in view the route is re-checked on every move, not every 5 s.
            replan = (self.route is None or stuck or now - self.route_time > 1.0
                      or math.hypot(self.route_goal[0] - x, self.route_goal[1] - y) > 800)
            if replan:
                # Around squads if possible, through their area only if it is the only way (tunnels). Stuck marks
                # are dropped if they close the way (they can be wrong); a target with no route is given up.
                # Stuck marks only in open ground: in a narrow passage "no progress" came from back-and-forth, and a
                # mark closed the passage and sent the next route out the other end (run 01:19, Mantis Hive).
                marks = [(sx, sy) for sx, sy, t in self.stuck_spots
                         if now - t < STUCK_MEMORY_S and nav.room(sx, sy) >= NARROW_ROOM]
                # Around the aggro range of every enemy that is not the target: flying past a pack pulled 3-5
                # Rocketeers at once (8 of 10 deaths since 19:00 were at the tunnel-neck pack).
                # Each enemy's own aggression range, without the safety margin used for firing spots: with the margin
                # nearly every route on crowded Mantis Hive counted as blocked (user: far too aggressive).
                zones = self._path_zones(world, exclude={self.target_id}, margin=False)
                route = None
                for attempt in ((marks, zones), ((), zones), (marks, ()), ((), ())):
                    route = nav.path(start, (x, y), *attempt)
                    if route is not None:
                        break
                # Keep going the way the ship is going: a re-plan that would turn it around while the current route
                # still leads to the same goal is ignored. Routes of nearly equal length out of both ends of a passage
                # took turns every second and the ship swung back and forth (run 01:19).
                old = self.route
                if (route is not None and old is not None and len(old) > 1 and len(route) > 1 and not stuck
                        and math.hypot(self.route_goal[0] - x, self.route_goal[1] - y) <= 800
                        and min(_segment_distance(start, a, b) for a, b in zip(old, old[1:])) < 1500):
                    # Only while the current route still has a real way to go: near its end the old route led to
                    # where the ship already was, every new route "turned it around", and it stood still until the
                    # target was given up (tunnel simulation).
                    rest = [start] + [pt for pt in old[1:]]
                    rest_length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(rest, rest[1:]))
                    ahead_old = next((pt for pt in old[1:] if math.hypot(pt[0] - start[0], pt[1] - start[1]) > 300),
                                     None)
                    if ahead_old is not None and rest_length >= ROUTE_FLIP_MIN:
                        ahead_new = route[1]
                        turn = abs((math.degrees(math.atan2(ahead_new[1] - start[1], ahead_new[0] - start[0])
                                                 - math.atan2(ahead_old[1] - start[1], ahead_old[0] - start[0]))
                                    + 180) % 360 - 180)
                        if turn > ROUTE_FLIP_DEG:
                            route = rest
                clearing = label == "range of the target" and self.target_id in self.path_blockers
                # Energy almost gone: the way to an energy field may pass enemies, no skill works without energy
                # (user); only squads still count (checked below).
                critical = label == "energy orbs" and self.energy_critical
                if route is not None and zones and not fleeing and label != GOLD_LABEL and not clearing \
                        and not critical:
                    # Only a way through enemies: take it if it pulls no more than the pull limit allows (they are
                    # fought on the way); giving every such target up left the bot standing still with 1,100
                    # destinations dropped in 2 s (run 21:42-21:43).
                    # Only the next ROUTE_NEAR units count: far enemies are judged again as the ship comes closer
                    # (routes are re-planned every second), and one at a time, as pulling two together at the tunnel
                    # neck was too much (pack simulation).
                    crossed = self._zones_crossed(route, zones, ROUTE_NEAR)
                    attackers = sum(1 for u in world.units if self._attacking(world, u))
                    # Blocked only when the way would pull more than the pull limit (attackers included); up to
                    # that the bot flies through and fights whatever joins (user: blocking was far too aggressive).
                    if crossed and crossed + attackers > self.eff_max_pull:
                        # Remember who is in the way: the farm loop fights them one by one (a player clears the way;
                        # on crowded Mantis Hive every patrol point was given up and the bot stood still).
                        fightable = 0
                        for u in self._enemy_units(world):
                            if u.unit_id != self.target_id and not self._attacking(world, u) and \
                                    self._zones_crossed(route, [(u.x, u.y, self._aggro_reach(u, False))],
                                                        ROUTE_NEAR):
                                entry = self._route_entry(route, u)
                                old = self.path_blockers.get(u.unit_id)
                                self.path_blockers[u.unit_id] = (now, min(entry, old[1]) if old else entry)
                                fightable += self._can_clear(u) and self._clear_allowed(world, u)
                        self.route = None
                        self.progress = None
                        # Per destination: a fight target by its id (it moves, and its firing spot with it; keyed by
                        # position the timer restarted every few seconds and never ran out), else the goal itself.
                        block_key = ("target", self.target_id) if label == "range of the target" else goal
                        since = self.blocked_since.setdefault(block_key, now)
                        # The limit only counts time without a fight: clearing the way can take long, and a fight
                        # must not use it up (else A -> B -> A again); blockers that cannot be fought end it.
                        blocked_for = now - max(since, self.last_fight_time)
                        # Not while new fights are not allowed (repair cooling down, squad near): the blockers are
                        # never fought then and the bot stood still "clearing the way" until Snipers came to it
                        # (Vénar 22:47:53-57, death 22:48:13). Then the destination is given up like any other.
                        if label in KEEP_WHEN_BLOCKED and fightable and blocked_for < BLOCKED_GIVE_UP_S and \
                                not self.no_new_fights:
                            # Keep the destination: the blockers are fought, then the bot carries on toward it. Giving
                            # it up made the seek patrol switch A -> B -> A after every fight on the way (user).
                            if now - self.last_blocked_note > 5:
                                self.last_blocked_note = now
                                self._debug(f"way to {label} ({x:.0f}, {y:.0f}) passes {crossed} enemies; clearing it")
                            self.enemy_blocked = True
                            self.status = "Clearing the way"
                            return True
                        if not fightable and attackers == 0 and crossed <= self.eff_max_pull:
                            # Nothing in the way can be cleared (unreachable for now, or on the ignore list): pass
                            # them if that pulls no more than the pull limit. Giving up made one unreachable enemy
                            # drop every destination at once (Mantis Hive simulation).
                            self._debug(f"way to {label} ({x:.0f}, {y:.0f}) passes {crossed} enemies that cannot be "
                                        f"cleared; going through")
                            self.route, self.route_goal, self.route_time = route, (x, y), now
                        else:
                            self._debug(f"way to {label} ({x:.0f}, {y:.0f}) passes {crossed} enemies; giving it up")
                            return False
                # Escapes (squad flees and strike dodges) never wait for a squad: a strike dodge held "waiting for the
                # squad to pass" 493 from the marker and took 76% -> 43% (13:47:00-05).
                if route is not None and not fleeing and not self._route_safe(route):
                    # The direct route would meet a squad that is in view (checked against where it is heading).
                    # Go around it if possible; else hold position and let it pass (up to SQUAD_WAIT_S), then
                    # carry on toward the same target. Old sightings do not block routes.
                    zones = zones + [(t[0], t[1], self.danger_radius + DANGER_ROUTE_MARGIN)
                                     for t in self._threats()]
                    around = nav.path(start, (x, y), marks, zones) or nav.path(start, (x, y), (), zones)
                    if around is not None and self._route_safe(around):
                        route = around
                    elif label == "range of the target":
                        # A fight target behind a squad: not worth waiting for. Waiting was keyed by the firing spot,
                        # which moves with the target, so the 30 s never ran out and the bot switched between the
                        # target and "waiting" several times a second for 5 minutes (Vénar 21:59-22:04).
                        self._debug(f"a squad is in the way to {label} ({x:.0f}, {y:.0f}); leaving that target")
                        self.route = None
                        self.progress = None
                        return False
                    else:
                        # Per goal, so waiting for one target does not use up the wait for another.
                        self.squad_waits = {g: t for g, t in self.squad_waits.items() if now - t < SQUAD_WAIT_S * 2}
                        since = self.squad_waits.setdefault(goal, now)
                        if since == now:
                            self._debug(f"waiting for the squad to pass before flying to {label} ({x:.0f}, {y:.0f})")
                        if now - since < SQUAD_WAIT_S:
                            self.status = "Waiting for the squad to pass"
                            self.squad_blocked = True
                            self.progress = None  # waiting is not being stuck
                            self.route = None
                            return True
                        self._debug(f"squad still in the way to {label} after {SQUAD_WAIT_S} s; giving it up")
                        self.squad_waits.pop(goal, None)
                        self.route = None
                        self.progress = None
                        return False
                self.squad_waits.pop(goal, None)
                if route is not None and not self.enemy_blocked:
                    self.blocked_since.pop(("target", self.target_id) if label == "range of the target" else goal,
                                            None)
                if route is None:
                    self._debug(f"no route to {label} ({x:.0f}, {y:.0f}); giving it up")
                    self.route = None
                    self.progress = None
                    return False
                self.route, self.route_goal, self.route_time = route, (x, y), now
            # Next waypoint: the first one not yet reached.
            while len(self.route) > 2 and math.hypot(self.route[1][0] - world.x, self.route[1][1] - world.y) < 500:
                self.route.pop(1)
            waypoint = self.route[1] if len(self.route) > 1 else (x, y)
            # The rest of the route from the ship: clicks go a hop ahead ALONG it, past corners, so the ship flows
            # through them instead of slowing to a stop at every waypoint (user: movement looked rigid).
            path = [start] + list(self.route[1:]) if len(self.route) > 1 else [start, (x, y)]
            if self.unstick is not None:
                # Backing off a wall first: fly the unstick hop, then the route again.
                hop, until = self.unstick
                if now < until and math.hypot(hop[0] - world.x, hop[1] - world.y) > 300:
                    waypoint = hop
                    path = [start, hop]
                else:
                    self.unstick = None
        else:
            waypoint = (x, y)
            path = [start, (x, y)]
        distance = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))
        if distance < 1:
            return True
        pixel = None
        camera = self._camera()
        if camera is None:
            return True
        self.input.refresh()
        first_hop = FLEE_HOP if fleeing else self.config.move_hop
        hops = (first_hop, first_hop / 2, first_hop / 4, self.config.move_hop / 4)
        straight = [start, waypoint]
        straight_length = math.hypot(waypoint[0] - world.x, waypoint[1] - world.y)
        # Along the route first; straight at the next waypoint (the old way) if no point along it can be clicked.
        candidates = [(path, min(distance, h)) for h in hops]
        if len(path) > 2:
            candidates += [(straight, min(straight_length, h)) for h in hops]
        for line, hop in candidates:
            tx, ty = _point_along(line, hop)
            # Cutting a corner must not cross terrain (the route keeps its wall margin; the straight click may not).
            if nav is not None and line is path and len(path) > 2 and not nav.line_clear(start, (tx, ty)):
                continue
            ground = nav.ground_height(tx, ty) if nav is not None else None
            height = ground if ground is not None else world.ship_height
            pixel = self._project_on(camera, tx, ty, height)
            # A click with terrain between camera and ground point lands on that terrain instead.
            if pixel is not None and nav is not None and height is not None \
                    and not nav.sight_clear(camera.eye, (tx, height, ty)):
                pixel = None
            if pixel is not None:
                break
        if pixel is None:
            # No clickable point for a while with the camera already facing the way: the ship stood still for
            # 3 min at (878, -17527) "moving into range" of a patrolling Mantis Signor (2026-11-12 14:55); its
            # firing spot moved, so the no-progress timer restarted and never gave up. After NO_CLICK_RELAX_S click
            # without the terrain-sight test (a click on terrain in between still heads that way); after
            # NO_CLICK_GIVE_UP_S give the destination up.
            if self.no_click_since is None:
                self.no_click_since = now
            waited = now - self.no_click_since
            if waited > NO_CLICK_RELAX_S:
                for line, hop in candidates:
                    tx, ty = _point_along(line, hop)
                    ground = nav.ground_height(tx, ty) if nav is not None else None
                    pixel = self._project_on(camera, tx, ty, ground if ground is not None else world.ship_height)
                    if pixel is not None:
                        break
            if pixel is None and waited > NO_CLICK_GIVE_UP_S:
                self._debug(f"no clickable point toward {label} for {waited:.0f} s; giving it up")
                self.no_click_since = None
                self.route = None
                self.progress = None
                return False
        if pixel is None and self._zoom_out_quick(camera, "no clickable point"):
            return True
        if pixel is None and camera is not None:
            # Tilt only with the way already in front of the camera (behind it, turning is what helps).
            ahead = math.degrees(math.atan2(waypoint[1] - world.y, waypoint[0] - world.x))
            if abs((ahead - self._camera_yaw(camera) + 180) % 360 - 180) < 45 \
                    and self._tilt_for_view(camera, f"no clickable point toward {label}"):
                return True
        if pixel is None:
            # Not on screen or hidden by terrain: turn the camera toward where the ship is going, click next tick.
            # face=False: the camera belongs to something else (shooting); skip this click instead.
            if face and not self._face(world, waypoint[0], waypoint[1], f"looking toward {label}", tolerance=15.0):
                self._warn_once(f"move-{label}", f"Cannot find a clickable point toward {label}")
            return True
        self.no_click_since = None
        self._debug(f"move toward {label}: waypoint ({waypoint[0]:.0f}, {waypoint[1]:.0f}) click ground "
                    f"({tx:.0f}, {ty:.0f}) pixel ({pixel[0]:.0f}, {pixel[1]:.0f})")
        self.input.click(pixel[0], pixel[1], "left")
        self.last_move = now
        return True

    def _select(self, world, unit, button="left", move_if_hidden=True, face=True):
        """Click a unit to select it. Left-click selects and flies to it (pickups); right-click only selects
        (enemies: fight from range). face: turn the camera toward it when it is off screen. Returns True if a click
        was sent."""
        label = assets.display_name(unit.desc or unit.name)
        # At most one select click per unit per second: right-clicks 3-4 times a second at enemies hugging the ship
        # went with the ship standing still for 8 s while fleeing (run 21:00:54-21:01:02).
        now = time.monotonic()
        key = getattr(unit, "unit_id", None) or (round(unit.x), round(unit.y))
        if now - self.select_times.get(key, -math.inf) < SELECT_EVERY_S:
            return False
        self.select_times = {k: t for k, t in self.select_times.items() if now - t < 10}
        self.select_times[key] = now
        pixel = self._project(unit.x, unit.y, unit.height)
        if pixel is not None:
            self._debug(f"select ({button}) {label} at distance {unit.distance:.0f} via 3D pixel "
                        f"({pixel[0]:.0f}, {pixel[1]:.0f})")
            self.input.click(pixel[0], pixel[1], button)
            return True
        # Not on screen: look at it first (the bot used to fly closer until it happened to come into view).
        if face and unit.distance < CAMERA_FACE_RANGE and self._face(world, unit.x, unit.y, f"looking at {label}"):
            return False
        if unit.marker and unit.distance < 4600:
            mx, my = self._minimap_pixel(world, unit)
            self._debug(f"select ({button}) {label} at distance {unit.distance:.0f} via minimap ({mx:.0f}, {my:.0f})")
            self.input.click(mx, my, button)
            return True
        if move_if_hidden:
            self._move_toward(world, unit.x, unit.y, "target")
        return False
