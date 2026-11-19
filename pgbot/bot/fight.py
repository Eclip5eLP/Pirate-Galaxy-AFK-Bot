"""Fighting: targets, firing, pulls, aggro zones, attackers.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game import assets
from ..data import myzen
from ..learn.learner import etype_of
from .constants import *  # noqa: F401,F403
from .util import _point_along, _segment_distance


class FightMixin:
    def _seek_match(self, unit):
        if self.seek is None:
            return False
        name = assets.display_name(unit.desc or unit.name)
        return myzen.enemy_matches(name, unit.level, self.seek["names"], self.seek["levels"],
                                   exact=self.seek.get("exact", True))

    def _wanted(self, unit, attacking):
        """Should this enemy be fought in the current mode?"""
        cfg = self.config
        dangerous = unit.unit_id in self.danger_ids or self._in_danger(unit.x, unit.y, DANGER_TARGET_MARGIN) \
            or (not attacking and self.route_check(unit.x, unit.y))
        ignores_danger = cfg.seek_ignores_danger and self.caution != "lower"
        if dangerous and not (ignores_danger and self._seek_match(unit)):
            return False  # dangerous groups are avoided, even when they attack (the bot flees instead)
        if attacking:
            return True
        if self._listed_avoid(unit):
            return False  # on the danger list: no fight is started on it (it is fought when it attacks)
        if self.caution == "lower" and any(w in (unit.desc or unit.name or "").lower() for w in LOWER_SKIP_NAMES):
            return False
        if self.seek is not None and self._seek_match(unit):
            return True
        if self._name_in_list(unit, cfg.ignore_names):
            return False
        aggressive = self._aggressive(unit)
        # Aggressive enemies near enough to come for the ship are fought in every mode; seek modes reach a bit
        # further, so the area around the sought enemies gets cleared (user, Mantis Hive 2026-11-11).
        reach = SEEK_AGGRESSIVE_RANGE if self.seek is not None else max(unit.aggression_range or 0, 4000) + 1500
        close_aggressive = aggressive and unit.distance <= reach
        if cfg.mode == "energy":
            return close_aggressive
        if self.seek is not None:
            return self._seek_match(unit) or close_aggressive
        if not cfg.attack_neutral and not aggressive:
            return False
        level = unit.level or 0
        if cfg.min_enemy_level and level < cfg.min_enemy_level:
            return False
        if cfg.max_enemy_level and level > cfg.max_enemy_level:
            return False
        return True

    def _resurrector_target(self, world, attackers, blaster):
        """The nearest Resurrector that attacks the ship and can be shot now (in range, clickable, clear line of
        fire), or None."""
        weapon_range = blaster.range if blaster is not None and blaster.range else 3000.0
        nav = self._nav(world)
        now = time.monotonic()
        found = [u for u in attackers
                 if "resurrector" in (assets.display_name(u.desc or u.name) or "").lower()
                 and SELECT_MIN_DIST <= u.distance <= weapon_range * 0.95
                 and self.ignored.get(u.unit_id, 0) <= now
                 and (nav is None or nav.fire_line_clear((world.x, world.y), (u.x, u.y)))]
        return min(found, key=lambda u: u.distance) if found else None

    def _fight(self, world, enemies, blaster, attacks, buffs):
        """attacks and buffs are empty when energy is below the skills threshold; the blaster always fires."""
        # An enemy counts as attacking for a few seconds after it last aimed at the ship: Mantis switch their target
        # for a moment, and the bot then swapped targets between an Interceptor, a Pyro and a Custodian Commander
        # every second, firing little and standing still (2026-11-12 10:55:50-10:56:03).
        tick_now = time.monotonic()
        for u in enemies:
            if self._attacking(world, u):
                self.attacked_at[u.unit_id] = tick_now
        self.attacked_at = {k: v for k, v in self.attacked_at.items() if tick_now - v < 30}

        def attacking(u):
            return tick_now - self.attacked_at.get(u.unit_id, -math.inf) < ATTACK_MEMORY_S

        attackers = [u for u in enemies if attacking(u)]
        current = next((u for u in enemies if u.unit_id == self.target_id), None)
        if current is not None and attackers and not attacking(current):
            # Enemies shooting at the ship come first, also when another target was already chosen: the bot kept
            # trying to reach a Mantis Signor behind 4-9 attackers, fired twice in 42 s and died (2026-11-12 10:25).
            current = None
        if current is not None and current.distance < SELECT_MIN_DIST and any(
                u.distance >= SELECT_MIN_DIST for u in attackers):
            current = None  # under the ship: a click there hits the own ship, which cannot be selected (see below)
        # Resurrectors first, softly (user, 2026-11-19): one revives the others as they die. An L43 ship that should
        # win 10 against 1 on Technatoria killed nothing in 73 s against a Death Squad and died. Only while it attacks
        # and is in range with a clear line of fire; Resurrectors like to flee, and chasing one out of range would
        # leave the others shooting. Otherwise the usual choice.
        resurrector = self._resurrector_target(world, attackers, blaster)
        if resurrector is not None and (current is None or current.unit_id != resurrector.unit_id):
            self._debug(f"targeting the Resurrector first ({resurrector.distance:.0f} away)")
            current = resurrector
            self.target_id = current.unit_id
            self.target_failures = 0
        elif resurrector is None and current is not None and len(attackers) > 1 \
                and "resurrector" in (assets.display_name(current.desc or current.name) or "").lower():
            current = None  # it fled out of range or behind a rock: the usual choice instead of chasing it
        if current is None:
            if attackers:
                # Not one under the ship, nor one just set aside as unselectable: a Mantis Marauder 96 away was
                # clicked every second for over a minute (each click hit the own ship, which the player cannot
                # select, so nothing was selected; user), the blaster never
                # fired, 10 attackers on the ship (Molikar 01:03; user: only the sniper shot, no blaster). Attackers
                # bypass the ignore list elsewhere, so the same one came back at once.
                now_ = time.monotonic()
                clickable = [u for u in attackers if u.distance >= SELECT_MIN_DIST
                             and self.ignored.get(u.unit_id, 0) <= now_]
                current = min(clickable or attackers, key=lambda u: u.distance)  # includes Harvester waves
                if not clickable and current.distance < SELECT_MIN_DIST:
                    # All of them on top of the ship: back off a little so one can be clicked.
                    dx, dy = world.x - current.x, world.y - current.y
                    length = math.hypot(dx, dy) or 1.0
                    nav = self._nav(world)
                    spot = (world.x + dx / length * SELECT_BACK_OFF, world.y + dy / length * SELECT_BACK_OFF)
                    spot = nav.open_point(*spot) if nav is not None else spot
                    if spot is not None:
                        self._move_toward(world, *spot, "room to aim", face=False)
            elif self.config.pull_carefully:
                current = self._pick_carefully(world, enemies)
            else:
                current = min(enemies, key=lambda u: u.distance)
            self.target_id = current.unit_id
            self.target_failures = 0
        selected = world.selected_target
        if selected is not None and selected.unit_id != current.unit_id and current is not resurrector:
            # In a crowd the right-click often lands on a neighbour ("Could not select Mantis Interceptor" three times
            # in 15 s, 10:48). If the game already has another enemy of this fight selected and it is in range, shoot
            # that one instead of fighting the selection (attackers first, then any enemy of the fight).
            weapon_range_now = blaster.range if blaster is not None and blaster.range else 3000.0
            chosen = next((u for u in enemies if u.unit_id == selected.unit_id), None)
            if chosen is not None and chosen.distance <= weapon_range_now * 0.95 and (
                    attacking(chosen) or not attacking(current)):
                current = chosen
                self.target_id = chosen.unit_id
                self.target_failures = 0
        label = assets.display_name(current.desc or current.name)
        nav = self._nav(world)
        # Fight from mid to max range like a player (user, 2026-11-11): enemies then come to the ship and leave their
        # group. Selecting is a right-click (select only); a left-click flew the ship right into the group.
        weapon_range = (blaster.range if blaster is not None and blaster.range else 3000.0)
        standoff = weapon_range * STANDOFF_SHARE
        line_clear = nav is None or nav.fire_line_clear((world.x, world.y), (current.x, current.y))
        others = self._path_zones(world, exclude={current.unit_id})
        inside_other = any(math.hypot(world.x - zx, world.y - zy) < zr - AGGRO_MARGIN for zx, zy, zr in others)
        in_range = current.distance <= weapon_range * 0.95 and line_clear
        if not in_range:
            # Out of range, a wall in the line of fire, or inside another enemy's aggro range: move to a firing spot
            # at standoff distance with a clear line, outside every other enemy's aggro range; never to the target.
            spot = self._steady_spot(world, current, standoff, others, nav)
            far_wait = self.far_wait
            if current.unit_id not in far_wait:  # while waiting for it the status stays "Waiting for ..."
                self.status = f"Moving into range of {label}"
            if time.monotonic() - self.last_range_note > 2:
                self.last_range_note = time.monotonic()
                self._debug(f"{label} at ({current.x:.0f}, {current.y:.0f}) {current.distance:.0f} away, line of fire "
                            f"{'clear' if line_clear else 'blocked'}; firing spot ({spot[0]:.0f}, {spot[1]:.0f})")
            # Select only when nearly in range: far away the clicks missed the moving target up to 4 times a second.
            if (selected is None or selected.unit_id != current.unit_id) and current.distance < weapon_range * 1.5:
                self._select(world, current, button="right", move_if_hidden=False)
            waiting_status = f"Waiting for {label} to come closer" if current.unit_id in far_wait else None
            if waiting_status is not None:
                # The move below says "Clearing the way" when blocked; while waiting the status must not flip
                # between that and "Waiting for ..." every tick (Gazica 19:37-19:38, several times a second).
                self._status_hold = waiting_status
            try:
                moved = self._move_toward(world, *spot, "range of the target")
            finally:
                self._status_hold = None
            if attacking(current) and (not moved or self.enemy_blocked):
                # It shoots at the ship from afar and the only way to it runs through other enemies: hold here and
                # let it come. Gazica 18:05: a Resurrector 19,000 away; the bot switched between chasing it and
                # clearing the way several times a second and did not move for 18 s.
                first, first_distance = far_wait.setdefault(current.unit_id, (tick_now, current.distance))
                # A turret or an enemy keeping its distance does not come: stop waiting once it has not come
                # closer for FAR_ATTACKER_APPROACH_S (18:14: a Stun Turret was waited for while it fired).
                not_coming = tick_now - first > FAR_ATTACKER_APPROACH_S and current.distance > first_distance - 300
                if tick_now - first > FAR_ATTACKER_WAIT_S or not_coming:
                    self._debug(f"{label} stays out of reach{' and does not come closer' if not_coming else ''}; "
                                f"leaving it until it comes within range")
                    self.far_given_up[current.unit_id] = tick_now + FAR_ATTACKER_IGNORE_S
                    far_wait.pop(current.unit_id, None)
                    self.target_id = None
                else:
                    if tick_now - first < 0.5:
                        self._debug(f"{label} attacks from {current.distance:.0f} away and the way to it is blocked; "
                                    f"waiting for it to come closer")
                    self.status = f"Waiting for {label} to come closer"
                return
            if not moved:
                self._debug(f"{label} cannot be reached; ignoring it for 60 s")
                self.ignored[current.unit_id] = time.monotonic() + 60
                self.target_id = None
            elif inside_other and line_clear and selected is not None and selected.unit_id == current.unit_id:
                self._fire(world, current, blaster, attacks)  # keep shooting while stepping out of the others' range
            return
        # In range: shoot. Inside another enemy's aggro range the ship also steps out, but that move never turns
        # the camera: turning it toward the firing spot and back to the target every second meant no target was
        # ever selected and the blaster fired twice in a minute with 9-12 attackers (2026-11-12 10:37, death).
        # Only while none of the others attacks yet: once they shoot anyway, stepping out only cost shots. With
        # several Assassins about (Vénar 21:43-21:44) the bot stepped every second, its ground clicks kept dropping
        # the selection, and it re-selected instead of shooting while 2-4 enemies hit it (user: "it just looked at
        # them").
        others_attacking = any((attacking(u) or self._attacking(world, u)) for u in self._enemy_units(world)
                               if u.unit_id != current.unit_id)
        if inside_other and not others_attacking:
            spot = self._steady_spot(world, current, standoff, others, nav)
            self._move_toward(world, *spot, "standoff distance", face=False)
        self.status = f"Fighting {label}"
        self.last_fight_time = time.monotonic()
        if selected is None or selected.unit_id != current.unit_id:
            if not self._select(world, current, button="right"):
                return  # not clickable yet (off screen and off the minimap)
            time.sleep(0.4)
            after = self._read()
            if after and after.selected_target and after.selected_target.unit_id == current.unit_id:
                self.target_failures = 0
            else:
                self.target_failures += 1
                hull_now = world.hitpoints or 0
                if self.target_failures == 1:
                    self.select_fail_hull = hull_now
                if self.target_failures >= 6 and attacking(current) and \
                        hull_now >= self.select_fail_hull - 1:
                    # Counted as attacking, never selectable, and the hull did not drop meanwhile: a ghost entry
                    # (two identical Assassins at 831 seen in memory; 30 s stall at 100% hull, Vénar 15:51:23-51).
                    self._debug(f"{label} attacks but cannot be selected and does no damage; ignoring it for "
                                f"{GHOST_IGNORE_S} s")
                    self.ghosts[current.unit_id] = time.monotonic() + GHOST_IGNORE_S
                    self.target_id = None
                    self.target_failures = 0
                    return
                if self.target_failures % 3 == 0 and attacking(current):
                    # An attacker is never skipped, so the same right-click on the same pixel repeated for 20 s
                    # while it shot the ship (Vénar 2026-11-15 12:32:16-35). Step sideways: the view and the pixel
                    # change, and the next click gets a new chance.
                    dx, dy = current.x - world.x, current.y - world.y
                    length = math.hypot(dx, dy) or 1.0
                    side = 1 if (self.target_failures // 3) % 2 else -1
                    sx, sy = world.x - dy / length * SELECT_STEP * side, world.y + dx / length * SELECT_STEP * side
                    point = nav.open_point(sx, sy) if nav is not None else (sx, sy)
                    if point is not None:
                        self._debug(f"{label} could not be selected {self.target_failures} times; stepping aside to "
                                    f"({point[0]:.0f}, {point[1]:.0f})")
                        self._move_toward(world, *point, "a clear view of the target")
                if self.target_failures >= 6:
                    self._log("info", f"Could not select {assets.display_name(current.desc)}; skipping it")
                    self.ignored[current.unit_id] = time.monotonic() + 30
                    self.target_id = None
            return
        energy = world.energy or 0
        self._fire(world, current, blaster, attacks, buffs)

    def _defense_needed(self, world, attackers):
        """Defensive skills are kept for when they matter (user: players do not use them at random): hull getting low,
        many attackers at once, or the hull falling fast. Thresholds by caution profile."""
        hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else 1.0
        history = self.hull_history
        span = history[-1][0] - history[0][0] if len(history) > 1 else 0.0
        rate = (history[0][1] - hull) / span if span >= 1.0 else 0.0
        return (hull < DEFENSE_HULL[self.caution] or attackers >= DEFENSE_ATTACKERS[self.caution]
                or hull - rate * RETREAT_LOOKAHEAD_S < 0.5)

    def _smart_heal(self, world, hull):
        """Repair out of combat although the hull is above the repair line: at an energy field while collecting
        there, or (lower / same level) before the next fight, so fights start near full with repair cooling down as
        little as possible. Never while anything attacks or an enemy's aggro range holds the ship."""
        if any(self._attacking(world, u) for u in world.units):
            return False
        if any(u.distance < self._aggro_reach(u, margin=False) for u in self._enemy_units(world)):
            return False
        if self.config.mode == "energy" and hull < 0.995:
            return True  # Collect Energy is a full refill of the ship: hull to 100% too (user, 2026-11-12)
        if self.at_field and hull < FIELD_HEAL_HULL:
            return True
        return hull < PRE_FIGHT_HEAL.get(self.caution, 0.0)

    def _defend(self, world):
        """Defensive skills when needed, outside a fight (gold blueprint runs, retreats): they target the own ship,
        so no selection is needed."""
        if self.repair_wanted or time.monotonic() < max(self.gcd_until, self.cast_until):
            return False
        attackers = sum(1 for u in world.units if self._attacking(world, u))
        if not attackers or not self._defense_needed(world, attackers):
            return False
        energy = world.energy or 0
        ready = [b for b in self.defensive if b.ready and not self._running(b) and energy >= (b.energy_cost or 0)
                 and time.monotonic() >= self.skill_paused.get(b.hotkey, 0.0)]
        return bool(ready) and self._skill(ready[0])

    def _fire(self, world, target, blaster, attacks, buffs=()):
        """All skills in a fight, one per global cooldown, in this order: defensive skills (when needed), Magnet Trap
        (crowds), attack skills, other buffs. The blaster fills the time between them, but holds while a skill is
        waiting for the global cooldown: each blaster shot starts it again (its cooldown, 1.65 s, is longer than the
        1.5 s global cooldown, so a skill only gets through when a shot is skipped). Returns True if a key was
        pressed."""
        now = time.monotonic()
        energy = world.energy or 0
        attackers = sum(1 for u in world.units if self._attacking(world, u))
        wanted = []
        if not self.repair_wanted:
            needed = self._defense_needed(world, attackers)
            hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else 1.0
            foe_types = [etype_of(u) for u in self._enemy_units(world) if self._attacking(world, u)]
            for b in self.defensive:
                if not b.ready or self._running(b):
                    continue
                learned = self._learn(lambda l: l.defense_use(b.desc or b.name or "?", foe_types, hull,
                                                              l._duration(b)), area="skills")
                if learned if learned is not None else needed:
                    wanted.append(b)
            trap = self._use_trap(world, target)
            if trap is not None:
                wanted.append(trap)
            ordered = list(attacks)
            if self._learner("skills") is not None:
                scores = {id(s): self._learn(lambda l: l.skill_score(s.desc or s.name or "?")) for s in attacks}
                # Untried skills first (they need samples), then by measured effect per energy.
                ordered.sort(key=lambda s: (scores[id(s)] is not None, -(scores[id(s)] or 0.0)))
            wanted += [skill for skill in ordered if skill.ready and not self._running(skill)
                       and target.distance <= (skill.range or 3000) and self._deploy_ok(world, skill, target) is not False]
            group = [etype_of(u) for u in self._enemy_units(world) if self._attacking(world, u)
                     or u.unit_id == target.unit_id]
            for skill in buffs:
                if not skill.ready or self._running(skill):
                    continue
                deploy = self._deploy_ok(world, skill, target)
                if deploy is False:
                    continue
                if deploy is None and self._learn(lambda l: l.buff_use(skill.desc or skill.name or "?", group,
                                                                       l._duration(skill)), area="skills") is False:
                    continue
                wanted.append(skill)
        wanted = [skill for skill in wanted if energy >= (skill.energy_cost or 0)
                  and now >= self.skill_paused.get(skill.hotkey, 0.0)]
        if wanted:
            if now < self.gcd_until or now < self.cast_until:
                self._fire_note(f"holding for {wanted[0].desc or wanted[0].hotkey} (cooldown)")
                return False  # hold the blaster until the skill can go
            skill = wanted[0]
            if not self._skill(skill):
                self._fire_note(f"{skill.desc or skill.hotkey} wanted but not pressed (repair cast "
                                f"{self.repair_cast}, active {skill.is_active}); blaster held")
                return False
            if True:  # pressed
                self.last_skill_press = now
                self._learn(lambda l: l.pressed(skill))
                deploys = self._learn(lambda l: l.is_deploy(skill.desc or skill.name or "?"))
                if skill in attacks:
                    self.last_press = now
                    if not deploys:
                        self._learn(lambda l: l.skill_pressed(skill, target))
                elif deploys or skill is self.trap:
                    pass  # measured from the unit it puts down
                elif skill in self.defensive:
                    self._learn(lambda l: l.effect_started("defense", skill))
                elif skill in buffs:
                    self._learn(lambda l: l.effect_started("buff", skill))
                if skill is self.trap:
                    self._debug("magnet trap used")
                # Only an attack skill that keeps firing holds the blaster for its active time. The Magnet Trap and
                # skills that put something down run on their own: holding for their active time left the ship idle
                # for 26 s after a trap with an Assassin on it (Vénar 2026-11-15 12:18:02-12:18:28).
                firing = getattr(skill, "on_enemy", False) and skill in attacks and not deploys
                hold = (getattr(skill, "active_time", 0.0) or 1.0) if firing else (skill.warmup or 0.0) + CAST_MARGIN_S
                self.weapon_hold = now + hold + WEAPON_GAP_S
                return True
            return False
        if blaster is not None and blaster.ready and target.distance <= (blaster.range or 3000) \
                and now >= self.weapon_hold and now >= self.cast_until:
            pressed = self._skill(blaster)
            if pressed:
                self.last_press = now
                self._learn(lambda l: l.skill_pressed(blaster, target))
            return pressed
        self._fire_note(f"blaster not fired: ready {getattr(blaster, 'ready', None)}, distance {target.distance:.0f} "
                        f"/ range {getattr(blaster, 'range', None)}, hold {self.weapon_hold - now:.1f} s, cast "
                        f"{self.cast_until - now:.1f} s")
        return False

    def _fire_note(self, text):
        """Why no key was pressed (2026-11-19 Technatoria: 3 min in a fight without a shot and nothing logged), at
        most every 3 s."""
        now = time.monotonic()
        if now - getattr(self, "last_fire_note", 0.0) > 3.0:
            self.last_fire_note = now
            self._debug(f"fire: {text}")

    def _steady_spot(self, world, target, standoff, others, nav):
        """The firing spot for this target, kept while the target stays within FIRE_SPOT_KEEP of where it was when the
        spot was chosen and the spot still has a clear line and no other enemy's aggro range over it. Recomputing it
        whenever the target moved sent a stream of small correction clicks (107 of 131 move clicks, run 23:01)."""
        kept = self.fire_spot
        if kept is not None and kept[0] == target.unit_id:
            _, spot, (ox, oy) = kept
            still_good = (math.hypot(target.x - ox, target.y - oy) < FIRE_SPOT_KEEP
                          and math.hypot(spot[0] - target.x, spot[1] - target.y) <= self.weapon_range * 0.95
                          and (nav is None or nav.fire_line_clear(spot, (target.x, target.y)))
                          and not any(math.hypot(spot[0] - zx, spot[1] - zy) < zr for zx, zy, zr in others))
            if still_good:
                return spot
        spot = self._firing_spot(world, target, standoff)
        if spot is None:
            dx, dy = world.x - target.x, world.y - target.y
            length = math.hypot(dx, dy) or 1.0
            spot = (target.x + dx / length * standoff, target.y + dy / length * standoff)
        self.fire_spot = (target.unit_id, spot, (target.x, target.y))
        return spot

    def _pull(self, world, attackers, blaster, attacks):
        """Pull, as the player does (2026-11-16 recordings, user: "I pull enemies alone ... to a position where I won't
        get ganked by more enemies"): once one or two enemies attack while others stand within their aggro reach +
        PULL_MARGIN of the ship or the attackers, fly to a spot PULL_DISTANCES away that is clear of those others,
        shooting back on the way; the attackers follow (in the recordings they kept 800-1,850 behind and the fight then
        had no bystander within 4,000-7,000). One pull per attacker; it ends at the spot, after PULL_MAX_S, or when the
        attackers stop attacking. Returns True while pulling (this tick is used)."""
        now = time.monotonic()
        pull = self.pull
        if pull is not None:
            ids = {u.unit_id for u in attackers}
            followers = [u for u in attackers if u.unit_id in pull["ids"]]
            if followers:
                pull["seen"] = now
            if followers and min(u.distance for u in followers) > self.weapon_range * 1.2:
                self._debug("pull ended (the attackers do not follow)")
                self.pull = None
                return False
            reason = ("at the spot" if math.hypot(world.x - pull["spot"][0], world.y - pull["spot"][1]) < PULL_ARRIVE
                      else "time" if now - pull["start"] > PULL_MAX_S
                      else "no longer attacked" if now - pull["seen"] > 3.0 else None)
            if reason:
                self._debug(f"pull ended ({reason})")
                self.pull = None
                return False
            self.status = f"Pulling {pull['name']} to a clear spot"
            if not self._move_toward(world, *pull["spot"], "a clear spot") or self.squad_blocked:
                # A pull never waits for a squad: it switched between pulling and "waiting for the squad to pass"
                # every tick with the attackers on the ship (Vénar 03:48:51-54); fight here instead.
                self._debug("pull ended (way to the spot blocked)")
                self.pull = None
                return False
            self._shoot_back(world, attackers, blaster, attacks)
            return True
        if not attackers or len(attackers) >= BREAKAWAY_ATTACKERS:
            return False
        pulled = self.pulled
        for key in [k for k, t in pulled.items() if now - t > PULL_MEMORY_S]:
            del pulled[key]
        # Only attackers that come after the ship can be pulled: one that stays put and shoots (a Sniper) does not
        # follow, and "pulling" it meant flying away while it kept shooting (user, Vénar 13:18:42).
        # ...and only from within reach: a pull started on a Signor beyond 1.2x blaster range ended in the same second
        # as "not following" (Vénar 13:49:53).
        fresh = [u for u in attackers if u.unit_id not in pulled and self._closing(u) > PULL_CLOSING_MIN
                 and u.distance <= self.weapon_range * 1.2]
        if not fresh:
            return False
        ids = {u.unit_id for u in attackers}
        others = [u for u in self._enemy_units(world) if u.unit_id not in ids and not self._never_shoots(u)
                  and not self._is_stun_turret(u)]
        points = [(world.x, world.y)] + [(u.x, u.y) for u in attackers]
        # Only enemies that would actually join: aggressive ones (neutral enemies join only when attacked themselves)
        # with the ship or an attacker inside their aggro range (+ a small margin). Counting every enemy near the fight
        # made pulls look forced, with nothing around that could join (user, Vénar 13:5x).
        crowded = [u for u in others if self._aggressive(u) and any(
            math.hypot(u.x - px, u.y - py) < self._aggro_reach(u, False) + PULL_NEAR_MARGIN for px, py in points)]
        for u in fresh:
            pulled[u.unit_id] = now
        if not crowded:
            return False
        spot = self._pull_spot(world, attackers, others)
        if spot is None:
            self._debug("no clear spot to pull to; fighting here")
            return False
        name = assets.display_name(fresh[0].desc or fresh[0].name)
        self.pull = {"ids": ids, "spot": spot, "start": now, "seen": now, "name": name}
        near = ", ".join(f"{assets.display_name(u.desc or u.name)} {min(math.hypot(u.x - px, u.y - py) for px, py in points):.0f}"
                         for u in crowded[:4])
        self._debug(f"pulling {name} to ({spot[0]:.0f}, {spot[1]:.0f}): would be joined by {near}")
        return self._pull(world, attackers, blaster, attacks)

    def _pull_spot(self, world, attackers, others):
        """The best spot PULL_DISTANCES away from the ship: open ground, out of strike blasts and squad zones, every
        other enemy at least its aggro range + PULL_MARGIN away; prefers the side away from the attackers and short
        flights. None if there is none."""
        nav = self._nav(world)
        ax = sum(u.x for u in attackers) / len(attackers)
        ay = sum(u.y for u in attackers) / len(attackers)
        back = math.atan2(world.y - ay, world.x - ax)
        best, best_score = None, -math.inf
        for distance in PULL_DISTANCES:
            for step in range(16):
                angle = back + (step - 8) * math.pi / 8
                x, y = world.x + distance * math.cos(angle), world.y + distance * math.sin(angle)
                if nav is not None and (nav.is_blocked(x, y) or nav.room(x, y) < 300):
                    continue
                if self._in_danger(x, y) or any(math.hypot(x - sx, y - sy) < STRIKE_RADIUS + STRIKE_MARGIN
                                                for sx, sy in getattr(world, "strikes", ())):
                    continue
                clear = min((math.hypot(x - u.x, y - u.y) - self._aggro_reach(u, False) for u in others),
                            default=PULL_MARGIN * 4)
                if clear < PULL_MARGIN:
                    continue
                facing = math.cos(angle - back)  # 1 = straight away from the attackers
                score = min(clear, PULL_MARGIN * 3) + 1500 * facing - 0.3 * distance
                if score > best_score:
                    best, best_score = (x, y), score
        return best

    def _closing(self, unit):
        """How fast (u/s) this unit has been coming closer over the last ~2 s (negative: moving away); 0 if unknown.
        Distances are kept per unit each tick in _track_distances."""
        track = self.unit_tracks.get(unit.unit_id)
        if not track or len(track) < 2 or track[-1][0] - track[0][0] < 1.0:
            return 0.0
        return (track[0][1] - track[-1][1]) / (track[-1][0] - track[0][0])

    def _track_distances(self, world):
        now = time.monotonic()
        tracks = self.unit_tracks
        seen = set()
        for u in self._enemy_units(world):
            if u.unit_id is None:
                continue
            seen.add(u.unit_id)
            track = [e for e in tracks.get(u.unit_id, []) if now - e[0] < 2.5] + [(now, u.distance)]
            tracks[u.unit_id] = track
        for key in [k for k in tracks if k not in seen]:
            del tracks[key]

    @staticmethod
    def _hp_share(unit):
        hp, top = getattr(unit, "hitpoints", None), getattr(unit, "max_hitpoints", None)
        return hp / top if hp is not None and top else 1.0

    def _shoot_back(self, world, attackers, blaster, attacks):
        """While retreating: fire at the nearest attacker in range (selected with a right-click, which does not
        move the ship)."""
        in_range = [u for u in attackers if blaster is not None and u.distance <= (blaster.range or 3000)]
        # An enemy Orbital Strike goes off at once when its caster dies (user, 2026-11-16): no finishing a Signor while
        # a live marker is within reach of the ship.
        reach = STRIKE_RADIUS + STRIKE_MARGIN
        if any(math.hypot(world.x - sx, world.y - sy) < reach for sx, sy in getattr(world, "strikes", ())):
            in_range = [u for u in in_range if not ("signor" in (u.desc or u.name or "").lower()
                                                    and self._hp_share(u) < SIGNOR_FINISH_SHARE)]
        if not in_range:
            return
        # Like the player's running fights (2026-11-16 recordings): with few attackers, the weakest one, to finish it
        # while flying away (two of three Signors died during one retreat); with a crowd, the one with the most others
        # within ORBITAL_SPLASH of it, so an area skill fired at it (attack skills go first when ready) hits the group
        # before breaking away. Not one right on top of the ship (a click there lands on the own ship).
        def crowd(unit):
            return sum(1 for o in attackers if math.hypot(o.x - unit.x, o.y - unit.y) < ORBITAL_SPLASH)
        if len(attackers) >= BREAKAWAY_ATTACKERS:
            target = max(in_range, key=lambda u: (u.distance >= CLOSE_SELECT, crowd(u), -self._hp_share(u)))
        else:
            target = min(in_range, key=lambda u: (u.distance < CLOSE_SELECT, self._hp_share(u), u.distance))
        selected_id = world.selected_target.unit_id if world.selected_target is not None else None
        current = next((u for u in in_range if u.unit_id == selected_id), None)
        if current is not None and (self._hp_share(current) <= self._hp_share(target) + 0.1
                                    or time.monotonic() - self.last_skill_press < SELECT_KEEP_AFTER_PRESS_S):
            # No switching for a small difference, nor right after a press: a new right-click selection just after
            # the Orbital Strike key cancelled the strike (5 presses in 7 s that never took, Vénar 04:53:15-22).
            target = current
        selected = world.selected_target
        key = getattr(target, "unit_id", None) or (round(target.x), round(target.y))
        just_selected = time.monotonic() - self.select_times.get(key, -math.inf) < SELECT_TRUST_S
        if (selected is None or selected.unit_id != target.unit_id) and not just_selected:
            # No camera turn: the flight needs the camera looking ahead, and turning back to the attacker made the
            # two fight over the camera. Attackers in blaster range are on the minimap anyway.
            self._select(world, target, button="right", move_if_hidden=False, face=False)
            return
        # Fire also right after the selection click, before memory shows it: waiting for the read-back, the bot
        # re-selected the same Sniper every second and fired 3 times in 10 s (user, Vénar 13:18:42-52).
        self._fire(world, target, blaster, attacks)

    def _clear_allowed(self, world, unit, hull=None, attackers=()):
        """Clearing the way is a pull like any other: only a blocker whose fight stays within the pull limit (and the
        learned fight risk). Vénar 21:56:44: the bot cleared the way through 5 level-85 enemies with a pull limit of
        1, was stunned and died from 100% in 7 s."""
        if hull is None:
            hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else 1.0
        return self._pull_allowed(world, unit, hull, list(attackers))

    def _can_clear(self, unit):
        """An enemy in the way that the bot may fight to clear it: not a squad member, not ignored for now (could
        not be reached or selected), not on the ignore list."""
        return unit.unit_id not in self.danger_ids and unit.unit_id not in self.ignored \
            and not self._name_in_list(unit, self.config.ignore_names)

    def _aggro_reach(self, unit, margin=True):
        """How close the ship may come to an enemy it does not mean to pull (margin: plus AGGRO_MARGIN)."""
        return (unit.aggression_range or AGGRO_DEFAULT) + (AGGRO_MARGIN if margin else 0.0)

    def _route_entry(self, route, unit, step=200.0):
        """How far along the route the ship first comes within the unit's aggro reach (ROUTE_NEAR if not before)."""
        reach = self._aggro_reach(unit, False)
        travelled = 0.0
        while travelled < ROUTE_NEAR:
            x, y = _point_along(route, travelled)
            if math.hypot(x - unit.x, y - unit.y) < reach:
                return travelled
            travelled += step
        return ROUTE_NEAR

    @staticmethod
    def _zones_crossed(route, zones, limit=math.inf):
        """How many aggro zones the first `limit` units of a route pass through (zones holding its start do not
        count: the ship is already inside)."""
        near, left = [route[0]], limit
        for a, b in zip(route, route[1:]):
            leg = math.hypot(b[0] - a[0], b[1] - a[1])
            if leg >= left:
                near.append(_point_along([a, b], left))
                break
            near.append(b)
            left -= leg
        count = 0
        for zx, zy, zr in zones:
            if math.hypot(near[0][0] - zx, near[0][1] - zy) < zr:
                continue
            if any(_segment_distance((zx, zy), a, b) < zr for a, b in zip(near, near[1:])):
                count += 1
        return count

    def _aggro_zones(self, world, exclude=(), margin=True):
        """(x, y, reach) of every living enemy that would join if the ship came that close: all enemies except those
        in `exclude` and those already attacking the ship. Neutral enemies count (proximity aggro). margin: add
        AGGRO_MARGIN (firing spots, patrol points); routes use the bare aggression range."""
        return [(u.x, u.y, self._aggro_reach(u, margin)) for u in self._enemy_units(world)
                if u.unit_id not in exclude and not self._attacking(world, u)]

    def _path_zones(self, world, exclude=(), margin=True):
        """Aggro zones that routes, patrol points and firing spots keep out of. None in high level mode: the ship
        outlevels everything, so enemies on the way are just fought (user, 2026-11-17: the bot failed to find paths
        on Baumar because it routed around every enemy). Escapes and take-off clearance still use _aggro_zones."""
        if self.caution == "high":
            return []
        return self._aggro_zones(world, exclude, margin)

    def _firing_spot(self, world, target, standoff):
        """A spot at about `standoff` from the target with a clear line of fire, open ground, and outside every other
        enemy's aggro range; the nearest one to the ship. None if there is none."""
        nav = self._nav(world)
        others = self._path_zones(world, exclude={target.unit_id})
        dx, dy = world.x - target.x, world.y - target.y
        length = math.hypot(dx, dy) or 1.0
        candidates = [(target.x + dx / length * standoff, target.y + dy / length * standoff)]
        for ring in (standoff, standoff * 0.8):
            for step in range(16):
                angle = step * math.pi / 8
                candidates.append((target.x + math.cos(angle) * ring, target.y + math.sin(angle) * ring))
        best, best_distance = None, math.inf
        for x, y in candidates:
            distance = math.hypot(x - world.x, y - world.y)
            if distance >= best_distance or any(math.hypot(x - zx, y - zy) < zr for zx, zy, zr in others):
                continue
            if nav is not None and (nav.room(x, y) < 300 or not nav.fire_line_clear((x, y), (target.x, target.y))):
                continue
            best, best_distance = (x, y), distance
        return best

    def _pull_size(self, world, target):
        """How many enemies a fight on `target` brings: the target itself (attacked from range, its group does not
        join), plus every other enemy whose aggro range covers the firing spot. Enemies along the way do not count:
        moves go around them."""
        standoff = self.weapon_range * STANDOFF_SHARE
        spot = self._firing_spot(world, target, standoff)
        if spot is not None:
            return 1
        dx, dy = world.x - target.x, world.y - target.y
        length = math.hypot(dx, dy) or 1.0
        spot = (target.x + dx / length * standoff, target.y + dy / length * standoff)
        return 1 + sum(1 for zx, zy, zr in self._aggro_zones(world, exclude={target.unit_id})
                       if math.hypot(spot[0] - zx, spot[1] - zy) < zr)

    def _enemy_units(self, world):
        """Living enemy ships of hostile factions (players, wrecks and friendly factions left out). Enemies are
        `$alien.*` units (all 112 in the language files, turrets included); devices placed by players have item ids
        ($magnetarraylauncher..., $repairturret...) and are not enemies: the bot attacked its own Magnet Trap 14
        times and another player's repair turret (user, 2026-11-11). Anything else counts only while it attacks."""
        return [u for u in world.units
                if u.class_name.endswith("/rk") and not u.is_player and not u.exploded and (u.hitpoints or 0) > 0
                and u.faction not in self.config.friendly_factions
                and ((u.desc or u.name or "").startswith("$alien.") or self._enemy_marker(u)
                     or self._attacking(world, u))]

    @staticmethod
    def _enemy_marker(unit):
        """Map marker of an enemy: "agressive_alien" or "neutral_alien". Tau Ceti's enemies are not "$alien.*"
        ($assassin, $sniper, $repairdroid): the bot saw no enemy on Vénar and died standing still (2026-11-12).
        Player devices like the own Magnet Trap carry "friendly_alien" and stay out."""
        marker = unit.marker or ""
        return "alien" in marker and "friendly" not in marker

    @staticmethod
    def _aggressive(unit):
        """Attacks on sight (marker 'agressive', the game's spelling); neutral enemies only join when attacked."""
        return bool(unit.marker and "agressive" in unit.marker) or bool(unit.hostile)

    def _fight_back(self, world, attackers):
        """Only for the special cases (repair cooling down at lower level, hurt with repair down, squad near);
        everywhere else every aggressive attacker is fought back as usual. Attackers to shoot then: all of them while
        the ship is taking damage (hull dropped in
        the last HIT_RECENT_S), none while it is not (user, 2026-11-15: fight back against enemies that deal damage,
        decided from what happens, not from types or ranges). An enemy that targets the ship without damage is still
        closing in or does not attack at all (support units like Resurrectors); flying to it is a new fight: 18:56
        the bot healed, then flew 4,500 to a Signor with repair down and died."""
        hit = time.monotonic() - self.last_hit <= HIT_RECENT_S
        # A shield or protector soaks the hits, so the hull does not show them (user: take shields into account):
        # while one is active the ship counts as under fire. Shield points are not read from memory yet.
        if not hit and not any(getattr(b, "is_active", False) for b in self.defensive):
            return []
        # Under fire, an attacker out of blaster range is only flown to if that pulls nobody else: 20:44 a Sniper hit
        # the ship and the bot, repair and afterburner down, followed a Repogrub 3,500-5,500 away into 3 enemies.
        reach = self.weapon_range * 0.95
        return [u for u in attackers if u.distance <= reach or self._pull_size(world, u) <= 1]

    @staticmethod
    def _never_shoots(unit):
        """Enemy types that target the ship but cannot shoot (user, 2026-11-15: Repogrubs "CANT EVEN SHOOT"). Counted
        as attackers they were chased with repair down into other enemies (death 20:44) and inflated the attacker
        count; they are still fought as ordinary targets."""
        name = assets.display_name(unit.desc or unit.name or "").lower()
        return any(word in name for word in NON_SHOOTERS)

    def _attacking(self, world, unit):
        if self._never_shoots(unit):
            return False
        if world.own_id is not None and unit.target_id == world.own_id:
            return True
        # Assassins (Vénar, 2026-11-12) hit the ship with no target set: the bot saw 0 attackers, did nothing, and
        # the ship died. Enemies found while the hull falls with no visible attacker count too (_unseen_attackers).
        return unit.unit_id in self.implied_attackers

    def _unseen_attackers(self, world, damage_rate):
        """The hull is falling and no enemy has the ship as its target: the aggressive / hostile enemies within
        HOSTILE_ATTACK_RANGE are taken as the attackers for IMPLIED_ATTACK_S."""
        now = time.monotonic()
        self.implied_attackers = {k: v for k, v in self.implied_attackers.items() if v > now}
        if damage_rate <= 0.002:
            return
        if any(world.own_id is not None and u.target_id == world.own_id for u in world.units):
            return
        suspects = [u for u in self._enemy_units(world) if u.distance < HOSTILE_ATTACK_RANGE
                    and (u.hostile or self._aggressive(u))]
        for u in suspects:
            if u.unit_id not in self.implied_attackers:
                self._debug(f"hull falling with no attacker targeting the ship: taking "
                            f"{assets.display_name(u.desc or u.name)} {u.distance:.0f} away as an attacker")
            self.implied_attackers[u.unit_id] = now + IMPLIED_ATTACK_S
