"""Danger: caution profile, Death Squads and their tracking, squad routes.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game import assets
from ..data import myzen
from .squadtrack import SiteRoutes, SquadTracks, point_along
from ..learn.learner import etype_of, type_name
from .constants import *  # noqa: F401,F403
from .util import _closest_approach, _point_along, _segment_distance, _save_state


class DangerMixin:
    # Danger -------------------------------------------------------------------------------------------------

    @property
    def danger_radius(self):
        """Squad keep-away distance: the setting, farther when the afterburner cannot outrun a squad."""
        radius = self.config.danger_radius
        if self._ab_weak():
            radius *= AB_WEAK_KEEP
        return radius

    def _squad_fight_eta(self):
        return AB_WEAK_ETA_S if self._ab_weak() else SQUAD_FIGHT_ETA_S

    def _ab_weak(self):
        gain = self.ab_gain
        return gain is not None and gain < AB_WEAK_GAIN

    def _track_afterburner(self, world):
        """Measure the afterburner's real gain: top speed with it on over the top speed with it off (90th
        percentiles). Saved with the hull it was measured on; a new hull measures again."""
        if world.zone != "planet" or world.x is None or not world.max_hitpoints or not world.items:
            self.speed_track = []
            return
        afterburner = next((i for i in world.items if i.desc and "afterburner" in i.desc), None)
        setup = [round(world.max_hitpoints), afterburner.desc if afterburner else None]
        if self.ab_gain_hull is not None and list(self.ab_gain_hull) != setup:
            # Another hull or afterburner item: the old gain no longer applies; measure it again.
            if self.ab_gain is not None:
                self._log("info", "Ship or afterburner changed; measuring the afterburner again")
            self.ab_gain = self.ab_gain_hull = None
            self.speed_normal, self.speed_boosted = [], []
        now = time.monotonic()
        active = bool(afterburner and afterburner.is_active)
        self.speed_track = [s for s in self.speed_track if now - s[0] < 1.0] + [(now, world.x, world.y, active)]
        first = self.speed_track[0]
        if now - first[0] < 0.5 or not all(s[3] == active for s in self.speed_track):
            return
        speed = math.hypot(world.x - first[1], world.y - first[2]) / (now - first[0])
        if speed > 4000 or speed < 300:
            return  # a jump (rescue, landing) or standing / turning
        samples = self.speed_boosted if active else self.speed_normal
        samples.append(speed)
        del samples[:-400]
        if len(self.speed_boosted) < 20 or len(self.speed_normal) < 60:
            return
        # Top speeds both ways (90th percentile): with the afterburner on the ship also turns, brakes at walls and
        # gets stuck, so its median read x0.49 on Earth in Unrest (549 vs 1,129 u/s, 2026-11-16 18:58).
        boosted = sorted(self.speed_boosted)[int(len(self.speed_boosted) * 0.9)]
        normal = sorted(self.speed_normal)[int(len(self.speed_normal) * 0.9)]
        gain = boosted / normal if normal else None
        if gain is None or not AB_GAIN_PLAUSIBLE[0] <= gain <= AB_GAIN_PLAUSIBLE[1]:
            # An afterburner never slows the ship: a reading below 1 is a bad sample mix (turns, walls, stuck), not
            # the item (user: the bot must notice wrong readings itself). Start the samples over.
            self._debug(f"afterburner reading x{gain or 0:.2f} is impossible; measuring again")
            self.speed_normal, self.speed_boosted = [], []
            return
        if self.ab_gain is None or abs(gain - self.ab_gain) > 0.1:
            weak = gain < AB_WEAK_GAIN
            self._log("info", f"Afterburner: {boosted:.0f} u/s vs {normal:.0f} normal (x{gain:.2f})"
                              + (f"; too weak to outrun squads, keeping {AB_WEAK_KEEP:.1f}x farther from them"
                                 if weak else ""))
            _save_state({"afterburner_gain": {"gain": round(gain, 3), "hull": list(setup)}})
        self.ab_gain, self.ab_gain_hull = gain, setup

    def _update_caution(self, world):
        """Farming mode from the level gap between the ship and the enemies seen near it in the last minute (median):
        lower (gap < LOWER_GAP), same, or high (gap >= HIGH_GAP), and the fight limits that go with it (user,
        2026-11-12). Lower: one enemy at a time, start fights only near full hull, retreat early, dangers avoided
        even when seeking, no Harvesters. Same: the settings (dangers avoided, smart pulls). High: pulls one more than
        the setting and retreats late (at 82 vs ~71 the bot fled at 50% hull; the level-26 Gemini ship died pulling
        two level-26 Rocketeers)."""
        cfg = self.config
        now = time.monotonic()
        self.level_samples = [s for s in self.level_samples if now - s[0] < LEVEL_MEMORY_S]
        self.level_samples += [(now, u.level) for u in self._enemy_units(world) if u.level and u.distance < 10000]
        levels = sorted(level for _, level in self.level_samples)
        caution = self.caution
        forced = (cfg.farming_mode or "auto").strip().lower()
        if forced in MODE_NAMES:
            # Set by the user (setting "Farming mode"; 2026-11-16: test lower level mode on Earth in Unrest).
            if caution != forced or not self._forced_logged:
                self._forced_logged = True
                self._log("info", f"{MODE_NAMES[forced]} (set in the settings)")
            caution = forced
        elif levels and world.ship_level:
            gap = world.ship_level - levels[len(levels) // 2]
            caution = "lower" if gap < LOWER_GAP else ("high" if gap >= HIGH_GAP else "same")
            if caution != self.caution:
                self._log("info", f"{MODE_NAMES[caution]}: ship level {world.ship_level}, enemies around level "
                                  f"{levels[len(levels) // 2]}")
                # Which enemies made it (user: "what was that level 82 enemy on Terasa?", 2026-11-12).
                near = sorted({(assets.display_name(u.desc or u.name), u.level) for u in self._enemy_units(world)
                               if u.level and u.distance < 10000}, key=lambda t: -t[1])
                self._debug("enemies in view: " + ", ".join(f"{n} L{lvl}" for n, lvl in near[:12]))
        self.caution = caution
        if caution == "lower":
            self.eff_max_pull = 1
            self.eff_retreat_below = max(cfg.retreat_below, 0.35)
            self.eff_engage_min = max(cfg.engage_min_hull, 0.7)
            self.eff_outnumbered = (3, 0.5)
        elif caution == "high":
            self.eff_max_pull = cfg.max_pull + 1
            self.eff_retreat_below = cfg.retreat_below * 0.6
            self.eff_engage_min = cfg.engage_min_hull * 0.6
            self.eff_outnumbered = (5, 0.4)
        else:
            self.eff_max_pull = cfg.max_pull
            self.eff_retreat_below = cfg.retreat_below
            self.eff_engage_min = max(cfg.engage_min_hull, 0.6)  # see PRE_FIGHT_HEAL
            self.eff_outnumbered = (3, OUTNUMBERED_RETREAT_HULL)

    def _ship_tier(self, world):
        """Star system matching the ship's level with armor upgrades included (GameReader: "armorshiplevel<armor>").
        Armor multiplies the hull (Raven Controller: base level 42, armor 5 -> level 82 with 81x the hitpoints), so
        the upgraded level is what the ship can take on."""
        return myzen.tier((world.ship_level or 1) - 1)

    def _outclassed(self, world, level, displayed=True):
        """True if the ship is from a system far enough ahead of this enemy level that dangers do not apply.
        Unit levels are read as displayed (raw + 1); system ranges use raw levels."""
        if level is not None and displayed:
            level -= 1
        # Enemies belong to the system they live in, not to the system their level number falls in: Molikar's R40-44
        # are Mizar enemies, but 43-44 read as Sol and made a level-60 (advanced Sol) ship "not ahead" (user,
        # 2026-11-17). The planet's system wins; the level is the fallback when the planet is not known.
        # Rules (user, 2026-11-17): in Sol (two stages in one system, advanced Sol is just Earth, no clear level line)
        # the normal level-ahead rule (gap >= HIGH_GAP); elsewhere the ship's system must be ahead AND its level above
        # the enemy's (a Mizar ship with unupgraded armor may be little above Gemini levels). The ship's level
        # includes armor: mixed class ships (base ~43) scale to end game with it.
        ship_level = (world.ship_level or 1) - 1
        system = self._system_of(world.current_planet)
        if system == "Sol":
            return level is not None and ship_level - level >= HIGH_GAP
        index = next((i for i, (name, _, _) in enumerate(myzen.SYSTEM_LEVELS) if name == system), None)
        ship, enemy = self._ship_tier(world), index if index is not None else myzen.tier(level)
        return (ship is not None and enemy is not None and ship - enemy >= self.config.danger_tier_gap
                and (level is None or ship_level > level))

    def _listed_avoid(self, unit):
        """On the danger list (Settings): no fight is started on it, unless learning found it harmless for this ship
        (Fight risk switch). The danger list no longer makes squads (user, 2026-11-15)."""
        if not self._is_danger_name(unit):
            return False
        etype = etype_of(unit)
        learner = self._learner("fight_risk", type_name(etype))
        if learner is None:
            return True
        now = time.monotonic()
        cached = self.learn_verdicts.get(etype)
        if cached is None or now - cached[0] > 10:
            cached = self.learn_verdicts[etype] = (now, self._learn(lambda l: l.verdict(etype, [etype], self.caution)))
        return cached[1] != "harmless"

    def _is_danger_name(self, unit):
        return self._name_in_list(unit, self.config.danger_names)

    def _name_in_list(self, unit, names):
        """A listed name matches when all its words are in the enemy's name
        ("Custodian Commander" matches "Mantis Custodian Commander", "Harvester" matches every Harvester)."""
        name = set(myzen._words(assets.display_name(unit.desc or unit.name)))
        for entry in names.split(","):
            words = myzen._words(entry)
            if words and all(word in name for word in words):
                return True
        return False

    def _death_squads(self, world, enemies, routes):
        """Ids of the units of Death Squads in view: a unit named Custodian (the leader) with a Resurrector and at
        least DS_MIN_SIZE units in all within DS_RADIUS of it; on a planet with site patrol routes, the leader on one.
        Not when the ship is far enough ahead of their level (danger tier gap)."""
        found = set()
        for leader in enemies:
            name = assets.display_name(leader.desc or leader.name) or ""
            if "custodian" not in name.lower() or self._outclassed(world, leader.level):
                continue
            group = [v for v in enemies if math.hypot(v.x - leader.x, v.y - leader.y) < DS_RADIUS]
            if len(group) < DS_MIN_SIZE or not any(
                    "resurrector" in (assets.display_name(v.desc or v.name) or "").lower() for v in group):
                continue
            if routes is not None and routes.grid and routes._nearest(leader.x, leader.y) is None:
                continue
            found |= {v.unit_id for v in group}
        return found

    def _moving_squads(self, world, enemies, visible, now):
        """Ids of enemies that fly as a squad: SQUAD_MOVE_GROUP or more within SQUAD_MOVE_R of each other, all moving
        at SQUAD_MOVE_MIN+ the same way over SQUAD_MOVE_WINDOW_S, none attacking, the group away from the ship."""
        tracks = self.enemy_tracks
        for uid in list(tracks):
            if uid not in visible:
                del tracks[uid]
        movers = {}
        for uid, u in visible.items():
            track = [e for e in tracks.get(uid, []) if now - e[0] <= SQUAD_MOVE_WINDOW_S] + [(now, u.x, u.y)]
            tracks[uid] = track
            span = track[-1][0] - track[0][0]
            if span < SQUAD_MOVE_WINDOW_S * 0.8 or self._attacking(world, u) or self._outclassed(world, u.level):
                continue
            vx, vy = (track[-1][1] - track[0][1]) / span, (track[-1][2] - track[0][2]) / span
            if math.hypot(vx, vy) >= SQUAD_MOVE_MIN:
                movers[uid] = (u.x, u.y, vx, vy)
        found = set()
        for uid, (x, y, vx, vy) in movers.items():
            speed = math.hypot(vx, vy)
            mates = [o for o, (ox, oy, ovx, ovy) in movers.items()
                     if math.hypot(ox - x, oy - y) < SQUAD_MOVE_R
                     and (vx * ovx + vy * ovy) / (speed * (math.hypot(ovx, ovy) or 1.0)) >= SQUAD_MOVE_COS]
            if len(mates) < SQUAD_MOVE_GROUP:
                continue
            cx = sum(movers[o][0] for o in mates) / len(mates)
            cy = sum(movers[o][1] for o in mates) / len(mates)
            if math.hypot(cx - world.x, cy - world.y) < SQUAD_MOVE_SHIP:
                continue
            new = set(mates) - found
            if SQUAD_BY_MOVEMENT and new and not found and not (set(mates) & set(self.danger_members)):
                names = sorted({assets.display_name(visible[o].desc or visible[o].name) for o in mates})
                self._log("warning", f"Squad recognised by its movement ({len(mates)}: {', '.join(names)}) at "
                                     f"({cx:.0f}, {cy:.0f}); keeping away")
            found |= set(mates)
        return found

    def _update_dangers(self, world):
        """Track Death Squads (see _death_squads). Membership is renewed while the squad is seen as one, and ends
        DANGER_MEMORY_S later; positions follow the members while they are visible."""
        cfg = self.config
        now = time.monotonic()
        # High level farming dodges nothing (user, 2026-11-17: the ship outlevels them; Molikar R40-44 at level 60
        # still "kept away" from squads because the level 43-44 enemies are Sol, not a system behind).
        if not cfg.avoid_dangers or self.caution == "high":
            self.dangers, self.danger_ids, self.danger_members = [], set(), {}
            return
        enemies = self._enemy_units(world)
        visible = {u.unit_id: u for u in enemies}
        members = {}
        for uid, (x, y, seen, confirmed) in self.danger_members.items():
            if uid in visible:
                x, y, seen = visible[uid].x, visible[uid].y, now
            if now - confirmed < DANGER_MEMORY_S and (uid in visible or now - seen < DANGER_POSITION_S):
                members[uid] = [x, y, seen, confirmed]
        # Squads by what is learned and seen (user: Vénar's enemies are not Mantis; known data + learning tell what a
        # squad is): groups flying together, and types learned to fly as squads on this planet even while they
        # stand or regroup. A moving group of types learned to be ordinary (seen far more often standing) is not one.
        planet = assets.display_name(world.current_planet) or world.planet_map
        learner = self._learner("squads")
        moving = self._moving_squads(world, enemies, visible, now)
        # Known data: on a planet with site patrol routes, squads fly on them; a group moving elsewhere is not one
        # (Vénar Assassins roam in groups anywhere and were learned as "squad types", 23:04).
        cache = self.site_routes
        routes = cache.get(planet)
        if routes is None and planet:
            routes = cache[planet] = SiteRoutes(myzen.death_squad_cells(planet))
        if routes is not None and routes.grid:
            moving = {i for i in moving if routes._nearest(visible[i].x, visible[i].y) is not None}
        if learner is not None and SQUAD_BY_MOVEMENT:
            self._learn(lambda l: l.squad_seen(planet, {etype_of(u) for u in enemies},
                                               {etype_of(visible[i]) for i in moving}), area="squads")
            ordinary = {i for i in moving if self._learn(lambda l: l.squad_ordinary(planet, etype_of(visible[i])),
                                                         area="squads")}
            moving -= ordinary
            typed = [u for u in enemies if not self._outclassed(world, u.level)
                     and self._learn(lambda l, u=u: l.is_squad_type(planet, etype_of(u)), area="squads")]
            for u in typed:
                if any(v is not u and math.hypot(v.x - u.x, v.y - u.y) < SQUAD_RADIUS for v in typed):
                    moving.add(u.unit_id)
        if not SQUAD_BY_MOVEMENT:
            moving = set()
        # Death Squads by their make-up (user, 2026-11-15): a leader (Custodian), a Resurrector and a few more units
        # together, on a patrol route. A Custodian or Resurrector alone is no squad, and the danger list is not
        # what a squad is (a Resurrector in an ordinary Vénar group made a "squad", 02:02:10).
        moving |= self._death_squads(world, enemies, routes)
        for uid in moving:
            # Only the enemies flying (or learned to fly) with it: adding everyone within SQUAD_RADIUS made 26
            # "members" of the ordinary enemies along the Vénar squad route and nothing was fought for 5 minutes
            # (23:08-23:13).
            u = visible[uid]
            members[uid] = [u.x, u.y, now, now]
        self.danger_members = members
        self.danger_ids = set(members)
        self.squad_planet = (world.planet_map, assets.display_name(world.current_planet))
        try:
            self.squad_tracks.record(world.planet_map, self._threats(max_age=2))
        except Exception as error:  # recording must never stop the bot
            self._debug(f"squad track not recorded: {error}")
        if any(seen == now for _, _, seen, _ in members.values()):
            self.last_squad_seen = now
        # Each member's own recent positions, for its own heading. One shared squad centre mixed two squads into
        # nonsense headings (2026-11-11 18:56: "1,500-2,200 u/s"), so an approaching squad was missed.
        self._track_members(members, now, visible)

    def _track_members(self, members, now, visible):
        """Movement tracks and velocities of the squad members, and the danger zones around them."""
        tracks = {}
        for uid, (x, y, seen, _) in members.items():
            track = [e for e in self.member_tracks.get(uid, []) if now - e[0] < 3]
            if seen == now:
                track.append((now, x, y))
            tracks[uid] = track
        self.member_tracks = tracks
        # Last heading of each member, kept while it is out of view (route checks predict it from there).
        last = self.member_last_velocity
        for uid, track in tracks.items():
            if len(track) >= 2 and track[-1][0] - track[0][0] >= 0.5:  # measured (also standing still)
                last[uid] = self._member_velocity(uid)
        self.member_last_velocity = {uid: v for uid, v in last.items() if uid in members}
        zones = []
        for x, y, seen, _ in members.values():
            for zone in zones:
                if math.hypot(zone[0] - x, zone[1] - y) < 4000:
                    zone[3] += 1
                    zone[0] += (x - zone[0]) / zone[3]
                    zone[1] += (y - zone[1]) / zone[3]
                    zone[2] = max(zone[2], seen)
                    break
            else:
                zones.append([x, y, seen, 1])
        self.dangers = [z[:3] for z in zones]
        for x, y, _ in self.dangers:
            if not any(math.hypot(x - lx, y - ly) < 10000 and now - t < DANGER_MEMORY_S
                       for lx, ly, t in self.danger_logged):
                self._log("warning", f"Dangerous squad near ({x:.0f}, {y:.0f}); keeping away")
                # Who made it a squad, to check false ones (user: "there was no squad there", Vénar 02:02:10).
                near = [visible[uid] for uid in members if uid in visible
                        and math.hypot(visible[uid].x - x, visible[uid].y - y) < 6000]
                listed = sorted({assets.display_name(u.desc or u.name) for u in near if self._is_danger_name(u)})
                others = sorted({f"{assets.display_name(u.desc or u.name)} L{u.level}" for u in near})
                speeds = [math.hypot(*self._member_velocity(u.unit_id)) for u in near]
                self._debug(f"squad members: {len(near)} ({', '.join(others)}); listed: {', '.join(listed) or '-'}; "
                            f"speed {max(speeds, default=0):.0f} u/s")
            self.danger_logged = [e for e in self.danger_logged if now - e[2] < DANGER_MEMORY_S] + [(x, y, now)]

    def _nearest_member(self, world, max_age=10):
        now = time.monotonic()
        return min((math.hypot(world.x - x, world.y - y) for x, y, seen, _ in self.danger_members.values()
                    if now - seen < max_age), default=math.inf)

    def _danger_close(self, world):
        """True while the ship should evade. Evading starts when a squad member seen in the last 10 s is inside the
        keep-away distance and coming closer, or is closer than FLEE_SHARE of it in any case (leaving only at weapon
        range is too late: the Custodian Commander stuns). It lasts until no member has been within EVADE_EXIT of the
        keep-away distance for 3 s, and at least EVADE_MIN_S, so the ship does not switch back and forth."""
        now = time.monotonic()
        radius = self.danger_radius
        start = False
        nearest = math.inf
        for uid, (x, y, seen, _) in self.danger_members.items():
            if now - seen >= 10:
                continue
            distance = math.hypot(world.x - x, world.y - y)
            nearest = min(nearest, distance)
            # Closing speed from the squad's own movement toward the ship only. The change in distance also counts
            # the ship's movement, and flying toward a squad then looked like the squad charging (2026-11-11 run:
            # the bot never left the landing zone).
            vx, vy = self._member_velocity(uid)
            closing = (vx * (world.x - x) + vy * (world.y - y)) / (distance or 1.0)
            # Evade when the member would be inside the keep-away distance within EVADE_LOOKAHEAD_S at its current
            # closing speed (2026-11-11: a squad closing at ~700 u/s from 15,000 was only noticed at 5,000).
            # Only a squad heading at the ship counts (at least half its speed closes in); one patrolling past does
            # not. Capped at EVADE_WARNING_SHARE of the keep-away distance: squads only aggro within about 5,000
            # (user), and a wider trigger kept the bot from leaving the landing zone.
            speed = math.hypot(vx, vy)
            # A squad drifting closer at 150-250 u/s set this off 5,500 away (user: "miles away"); only a squad really
            # coming at the ship counts. Slow drift is handled by the FLEE_SHARE distance.
            heading_at_ship = closing >= EVADE_MIN_CLOSING and closing >= 0.5 * speed
            warning = radius + closing * EVADE_LOOKAHEAD_S if heading_at_ship else 0.0
            if distance < radius * FLEE_SHARE or distance < min(warning, radius * EVADE_WARNING_SHARE):
                start = True
        if start and self.evade_since is None:
            self.evade_since = now
            self.flee_point = None  # the patrol target is kept: after stepping aside the bot carries on toward it
            self._log("warning", f"Squad approaching ({nearest:.0f} away); moving away")
        if self.evade_since is not None:
            if nearest < radius * EVADE_EXIT:
                self.evade_clear = None
            elif self.evade_clear is None:
                self.evade_clear = now
            done = now - self.evade_since > EVADE_MIN_S and self.evade_clear is not None and now - self.evade_clear > 3
            if done:
                self._debug(f"evading done after {now - self.evade_since:.0f} s")
                self.evade_since, self.evade_clear, self.flee_point = None, None, None
        return self.evade_since is not None

    def _stale_threats(self, min_age=10, max_age=DANGER_POSITION_S):
        """(x, y, vx, vy, age) of squad members out of view for min_age..max_age s: last position and heading."""
        now = time.monotonic()
        last = self.member_last_velocity
        # Only members whose movement was measured before they left view (standing still included): a squad whose
        # heading is unknown is not assumed to have stayed (Gemini 18:43: it had left through a tunnel).
        return [(x, y, *last[uid], now - seen) for uid, (x, y, seen, _) in self.danger_members.items()
                if min_age <= now - seen < max_age and uid in last]

    def _threats(self, max_age=10):
        """(x, y, vx, vy) of squad members seen in the last few seconds, each with its own velocity."""
        now = time.monotonic()
        return [(x, y, *self._member_velocity(uid)) for uid, (x, y, seen, _) in self.danger_members.items()
                if now - seen < max_age]

    def _squad_future(self, squad, spread, horizon):
        """Predictor t -> [possible squad centres] for one squad (x, y, vx, vy), from the site's patrol routes and the
        learned tracks; None if neither knows this squad's way."""
        planet_map, planet = self.squad_planet
        tracks = self.squad_tracks
        if math.hypot(squad[2], squad[3]) < 100:
            # Standing (regrouping): it stays where it is; once it moves again, its heading gives the prediction.
            # Assumed to set off along its route both ways at 400 u/s, a standing squad 20,000-30,000 away blocked
            # nearly every destination, and the bot flew back and forth with no enemy near (Vénar 23:00-23:04).
            return lambda t, x=squad[0], y=squad[1]: [(x, y)]
        sources = []
        if planet:
            cache = self.site_routes
            routes = cache.get(planet)
            if routes is None:
                routes = cache[planet] = SiteRoutes(myzen.death_squad_cells(planet))
            speed = math.hypot(squad[2], squad[3])
            speed = speed if speed > 100 else SQUAD_SPEED_DEFAULT
            ways = routes.paths(squad, speed * horizon + routes.STEP)
            if ways:
                sources.append(lambda t, ways=ways, speed=speed: [point_along(w, speed * t) for w in ways])
        matches = tracks.matches(planet_map, squad, horizon) if planet_map and tracks is not None else []
        if matches:
            sources.append(lambda t, m=matches: [SquadTracks.at(x, t) for x in m])
        if not sources:
            return None
        return lambda t: [pt for source in sources for pt in source(t)]

    def _route_safe(self, route, full=False):
        """True if flying the route keeps clear of every squad in view.
        A squad whose way is known (site patrol route or learned track): the whole crossing (up to SQUAD_PREDICT_S)
        must keep the keep-away distance from where it will be (user: wait until it passes, 5,000-7,000 is fine).
        Otherwise as before: 80% of the keep-away distance for the next seconds, with the squad moving along its
        heading and staying put for the first seconds. full: also judge an unknown squad over the whole way, with it
        staying where it is (used to pick fight targets)."""
        threats = self._threats()
        stale = self._stale_threats()
        if not threats and not stale:
            return True
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(route, route[1:]))
        flight = length / SHIP_SPEED
        # A squad out of view for 10-30 s: predicted from where and when it was last seen, if its way is known (or
        # it was standing). The bot turned back toward a squad 15 s after losing sight of it (Vénar 02:02:55).
        for squad in SquadTracks.groups([t[:4] for t in stale]):
            age = max(t[4] for t in stale if math.hypot(t[0] - squad[0], t[1] - squad[1]) < 5000)
            horizon = min(flight + 2, SQUAD_PREDICT_S)
            future = self._squad_future(squad, 0.0, horizon + age)
            if future is None:
                continue
            need = self.danger_radius * SQUAD_PASS_SHARE
            t = 0.0
            while t <= horizon:
                ship = _point_along(route, SHIP_SPEED * t)
                if any(math.hypot(ship[0] - x, ship[1] - y) < need for x, y in future(t + age)):
                    return False
                t += 1.0
        if not threats:
            return True
        unknown = []
        for squad in SquadTracks.groups(threats):
            members = [t for t in threats if math.hypot(t[0] - squad[0], t[1] - squad[1]) < 5000]
            spread = max((math.hypot(t[0] - squad[0], t[1] - squad[1]) for t in members), default=0.0)
            horizon = min(flight + 2, SQUAD_PREDICT_S)
            future = self._squad_future(squad, spread, horizon)
            if future is None:
                unknown.extend(members)
                continue
            # Members within SQUAD_RADIUS include enemies standing by: their spread is capped (the bot waited with the
            # squad 11,000-14,000 away, user: 5,000-7,000 from a squad is fine).
            need = self.danger_radius * SQUAD_PASS_SHARE + min(spread, SQUAD_SPREAD_MAX)
            # Already inside the keep-away distance: a route is fine as long as it never brings the ship closer than
            # it is now. Every route out failed the check, and the bot stood "waiting for the squad to pass" for a
            # minute with a resting squad 6,700 away (user: "there is no squad", Vénar 14:25-14:26).
            here = min((math.hypot(route[0][0] - x, route[0][1] - y) for x, y in future(0.0)), default=math.inf)
            need = min(need, here - ROUTE_START_SLACK)
            t = 0.0
            while t <= horizon:
                ship = _point_along(route, SHIP_SPEED * t)
                if any(math.hypot(ship[0] - x, ship[1] - y) < need for x, y in future(t)):
                    return False
                t += 1.0
            # The next seconds also with the members where they are (the prediction follows the centre).
            near, _ = _closest_approach(route, members, (0.0, 0.0), horizon=min(horizon, 2.0))
            now_near = min(math.hypot(route[0][0] - m[0], route[0][1] - m[1]) for m in members)
            if near < min(self.danger_radius * 0.8, now_near - ROUTE_START_SLACK):
                return False
        if not unknown:
            return True
        horizon = min(ROUTE_AHEAD_S, flight + 2)
        # Squads follow curved patrol routes, so a straight-line forecast alone is unreliable: the route must stay
        # clear both of the squad moving on along its heading and of the squad staying where it is.
        moving, _ = _closest_approach(route, unknown, horizon=horizon)
        # "Staying put" only for the first seconds: a squad patrolling beside the landing-pocket exit blocked every
        # route out for good when assumed to stand still for the whole flight (2026-11-11).
        staying, _ = _closest_approach(route, unknown, (0.0, 0.0), horizon=min(horizon, ROUTE_STATIC_S))
        if full:
            staying = min(staying, min(_segment_distance((t[0], t[1]), route[0], route[-1]) for t in unknown))
        here = min(math.hypot(route[0][0] - t[0], route[0][1] - t[1]) for t in unknown)
        return min(moving, staying) >= min(self.danger_radius * 0.8, here - ROUTE_START_SLACK)

    def _squad_eta(self, world):
        """Seconds until the first squad member in view would reach the keep-away distance at its current closing
        speed toward the ship (inf if none is closing)."""
        eta = math.inf
        known = {uid for uid, track in self.member_tracks.items()
                 if len(track) >= 2 and track[-1][0] - track[0][0] >= 0.5}
        now = time.monotonic()
        for uid, (x, y, seen, _) in self.danger_members.items():
            if now - seen >= 10:
                continue
            vx, vy = self._member_velocity(uid)
            distance = math.hypot(world.x - x, world.y - y)
            if distance <= self.danger_radius:
                return 0.0
            if uid in known:
                closing = (vx * (world.x - x) + vy * (world.y - y)) / (distance or 1.0)
            elif now - seen < 1.0:
                closing = 900.0  # just came into view, heading not known yet: assume it comes straight at the ship
            else:
                continue  # out of view and heading unknown: no estimate (old positions blocked fights for nothing)
            if closing > 150:
                eta = min(eta, (distance - self.danger_radius) / closing)
        return eta

    def _member_velocity(self, uid):
        """A squad member's own velocity (units/s) over its last ~3 s in view; 0 if unknown."""
        track = self.member_tracks.get(uid, [])
        if len(track) < 2 or track[-1][0] - track[0][0] < 0.5:
            return 0.0, 0.0
        (t0, x0, y0), (t1, x1, y1) = track[0], track[-1]
        return (x1 - x0) / (t1 - t0), (y1 - y0) / (t1 - t0)

    def _in_danger(self, x, y, extra=0.0):
        """Within the keep-away distance of a dangerous squad, plus extra units."""
        reach = self.danger_radius + extra
        return any(math.hypot(x - d[0], y - d[1]) < reach for d in self.dangers)
