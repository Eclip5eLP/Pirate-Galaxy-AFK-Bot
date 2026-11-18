"""The farm tick on a planet: energy, items, leaving the planet, patrol.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game import assets
from .patrol import Patrol
from ..data import myzen
from ..learn.learner import etype_of
from .constants import *  # noqa: F401,F403
from .util import _is_orb, _same_place


class FarmMixin:
    # Energy -------------------------------------------------------------------------------------------------

    def _energy_share(self, world):
        """Energy as a share of the computed maximum (rank based, see GameReader; confirmed 6,250 at level 26).
        Fallback: if energy is ever higher than computed (drone or boost bonus), the highest value seen counts."""
        energy = world.energy or 0
        self.energy_seen = max(self.energy_seen, energy)
        return energy / (max(world.energy_cap or 0, self.energy_seen) or 1)

    # Planet: farming ----------------------------------------------------------------------------------------

    def _items(self, world):
        """Main weapon, collector, repair, other attacks, buffs. The Magnet Trap is kept apart (self.trap): it is only
        worth its energy against several enemies (user)."""
        def find(word):
            return next((i for i in world.items if i.desc and word in i.desc), None)
        # Main weapon: the blaster, else the fastest offensive enemy skill (other ships may carry another weapon).
        weapons = [i for i in world.items if i.on_enemy and i.offensive and i.cooldown]
        blaster = find("blaster") or (min(weapons, key=lambda i: i.cooldown) if weapons else None)
        collector = find("collector")
        repair = find("repair")
        self.trap = find("magnetarray")
        special = {id(i) for i in (blaster, collector, repair, self.trap) if i is not None}
        attacks = [i for i in world.items if i.on_enemy and i.offensive and id(i) not in special]
        buffs = [i for i in world.items if i.on_self and id(i) not in special and not (i.desc and "afterburner" in i.desc)]
        return blaster, collector, repair, attacks, buffs

    def _use_trap(self, world, target):
        """Magnet Trap: a small stun turret placed on the ship, or on the target when it is in range. High energy
        cost, so only when TRAP_MIN_ENEMIES or more enemies are close to where it lands (user, 2026-11-11). Returns
        the trap item if it should be used now (the skill scheduler presses it), else None."""
        trap = self.trap
        if trap is None or not self.offensive_ok or not trap.ready or (world.energy or 0) < (trap.energy_cost or 0):
            return None
        foes = [u for u in self._enemy_units(world) if self._attacking(world, u) or self._aggressive(u)]
        spots = [(world.x, world.y)]
        if target is not None and target.distance <= (trap.range or 0):
            spots.append((target.x, target.y))
        best = max(sum(1 for u in foes if math.hypot(u.x - x, u.y - y) < TRAP_RADIUS) for x, y in spots)
        near = [etype_of(u) for u in foes if math.hypot(u.x - world.x, u.y - world.y) < TRAP_RADIUS + 1500]
        hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else 1.0
        learned = self._deploy_ok(world, trap, target) if near else None
        if learned is not None:
            return trap if learned else None
        return trap if best >= TRAP_MIN_ENEMIES else None

    def _farm(self, world):
        cfg = self.config
        if self.patrol_centre is None and not self.leaving:  # just arrived on a new destination planet
            self.patrol_centre = (world.x, world.y)
        blaster, collector, repair, attacks, buffs = self._items(world)
        self.weapon_range = blaster.range if blaster is not None and blaster.range else 3000.0
        share = self._energy_share(world)
        tick_now = time.monotonic()
        self.cooldown_factor = world.cooldown_factor or 1.0
        self.gcd_until = max(self.gcd_until if tick_now - self.last_any_skill < 2.0 else 0.0,
                             tick_now + world.gcd_remaining)
        # Defensive skills (protector, shield) are used whenever they are needed, whatever the energy (user);
        # offensive ones (attacks, Magnet Trap, other buffs) only above "Skills only above energy".
        self.defensive = [b for b in buffs if any(w in (b.desc or "").lower() for w in DEFENSIVE_WORDS)]
        buffs = [b for b in buffs if b not in self.defensive]
        self.offensive_ok = cfg.use_skills and share >= cfg.skills_min_energy
        if not self.offensive_ok:
            attacks, buffs = [], []  # blaster, collector, repair and defensive skills are always used
        for unit in world.units:
            if _is_orb(unit):
                self.orbs.remember(world.current_planet, unit.x, unit.y)

        self._update_repair_cast(repair)
        self._start_blueprint_book(world)
        self._check_presses(world.items)
        now = time.monotonic()
        hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else 1.0
        # Damage rate over the last 3 s: three Rocketeers took 57% -> 9% in about 4 s (2026-11-11).
        if self.hull_history and hull < self.hull_history[-1][1] - 0.002:
            self.last_hit = now  # any drop between ticks, so repair healing does not hide the hits
        self.hull_history = [h for h in self.hull_history if now - h[0] <= 3.0] + [(now, hull)]
        span = now - self.hull_history[0][0]
        damage_rate = max(0.0, (self.hull_history[0][1] - hull) / span) if span >= 1.0 else 0.0
        self._unseen_attackers(world, damage_rate)
        if cfg.learning:
            # Built inside _learn too: nothing in the learning path may stop the farm tick.
            self._learn(lambda learner: learner.tick(world, self._learn_context(world, blaster, now)))
        # Repair below the line, or when the hull will be there within RETREAT_LOOKAHEAD_S at the current damage rate
        # (the run of 20:55 fell 56% -> 34% in 3 s before the first press).
        self._repair_tick(repair, hull, cfg, damage_rate, world, now)
        if cfg.use_drones and world.drones:
            self._manage_drones(world, hull)
        self.at_field = False  # set again by _refill this tick if the ship is collecting at a field

        self._energy_tick(share, cfg, world, now)

        self._check_camera(world)
        self._update_dangers(world)
        self._update_caution(world)
        self._track_afterburner(world)
        self.route_check = self._ds_route_check(world)
        self.ignored = {k: v for k, v in self.ignored.items() if v > now}
        home_x, home_y = self.patrol_centre or (world.x, world.y)  # no home while leaving a planet for another
        local = cfg.mode in ("patrol", "park")
        enemies = self._farm_enemies(world, now, cfg, local, home_x, home_y)

        evading = self._danger_close(world)
        self._track_distances(world)
        self._trace_tick(now, world, enemies, share, repair, evading)
        # An enemy Orbital Strike marker under or next to the ship: step out before it goes off (user, 2026-11-15:
        # Signors' strikes do massive delayed damage; 73% -> 16% in one second at 12:42:53).
        self._note_strikes(world)
        # A squad closing in comes first; its escape avoids strike blasts (see _best_escape). The dodge ran before
        # everything and held the ship beside a marker while a squad closed from 5,500 to 885 (user; Vénar 17:06:43-54,
        # death).
        if not evading and self._dodge_strike(world):
            return
        # A squad dropping in at its spawn point (user, 2026-11-15: squads respawn at a fixed spot, dropping from the
        # sky, and appear on the minimap out of nowhere; players get out of the way). Deaths 12:30 and 12:50 were at
        # one such spot, the squad seen only 610-1,090 away.
        spawn = self._watch_spawns(world)
        # One escape at a time: with a squad also in view, "dropping in" and "dangerous squad" picked spots in turn
        # (one toward the squad) and the ship went back and forth for 5 s (Vénar 14:32:10-15). The squad escape then
        # counts the drop point too.
        if spawn is not None and evading:
            self._flee(world, world.items, list(self._threats()) + [spawn])
            self._shoot_back(world, [u for u in enemies if self._attacking(world, u)], blaster, attacks)
            return
        if spawn is not None:
            self._flee(world, world.items, [spawn], "Avoiding a squad dropping in")
            self._shoot_back(world, [u for u in enemies if self._attacking(world, u)], blaster, attacks)
            return
        # Evading a squad: no fighting, looting or wandering, unless a seek target is being fought there on purpose.
        if evading:
            hunting_here = cfg.seek_ignores_danger and self.caution != "lower" and any(
                self._seek_match(u) for u in enemies)
            if not hunting_here:
                self._flee(world, world.items)
                # Shoot back while fleeing, as in retreats and strike dodges: 18 s of squad evading with 1-3
                # attackers on the ship, not one shot fired, 65% -> 0% (Vénar 23:59:53-00:00:14).
                self._shoot_back(world, [u for u in enemies if self._attacking(world, u)], blaster, attacks)
                return

        # Gold blueprints first, even with enemies attacking (user: they are the point of blueprint farming and very
        # rare). Only a squad (above) or a hull below the retreat line comes before them.
        gold = [u for u in world.units if self._gold_blueprint(u) and u.distance <= max(cfg.loot_radius, GOLD_LOOT_RADIUS)
                and (round(u.x), round(u.y)) not in self.ignored and not self._in_danger(u.x, u.y)]
        if not gold:
            self.gold_progress = None  # no gold run going on
        if gold and collector and hull >= self.eff_retreat_below:
            drop = min(gold, key=lambda u: u.distance)
            key = (round(drop.x), round(drop.y))
            if key not in self.gold_logged:
                self.gold_logged.add(key)
                self._log("info", f"Gold blueprint {assets.display_name(drop.desc or drop.name)} at "
                                  f"({drop.x:.0f}, {drop.y:.0f}); collecting it first")
            # Under attack the gold run must make progress: no closer for GOLD_STALL_S -> fight first, retry soon.
            if self.gold_progress is None or self.gold_progress[0] != key or drop.distance < self.gold_progress[1] - 150:
                self.gold_progress = (key, drop.distance, now)
            under_attack = any(self._attacking(world, u) for u in enemies)
            if under_attack and drop.distance > cfg.collect_range and now - self.gold_progress[2] > GOLD_STALL_S:
                self._log("warning", "Not getting closer to the gold blueprint under attack; fighting first")
                self.ignored[key] = now + GOLD_RETRY_S
                self.gold_progress = None
            else:
                self.target_id = None
                self._defend(world)
                self._collect(world, drop, collector, "Collecting a GOLD blueprint", gold=True)
                return

        # Low hull and repair not available: fly away from the attackers until repair is ready (2026-11-11 death).
        # "Not available" includes repeated presses that did not take: with repair stuck at "ready" the retreat
        # never started (run 20:55).
        repair_failing = len([t for t in self.repair_tries if now - t < REPAIR_TRY_WINDOW_S]) >= REPAIR_TRIES
        repair_waiting = repair is not None and (not repair.ready or repair_failing)
        attackers = [u for u in enemies if self._attacking(world, u)]
        # Retreat when the hull would pass the retreat line within RETREAT_LOOKAHEAD_S, or below
        # OUTNUMBERED_RETREAT_HULL with three or more attackers.
        out_count, out_hull = self.eff_outnumbered
        in_trouble = (hull < self.eff_retreat_below
                      or hull - damage_rate * RETREAT_LOOKAHEAD_S < self.eff_retreat_below
                      or (len(attackers) >= out_count and hull < out_hull))
        # Once started, the retreat holds while repair cools down until no attacker is within RETREAT_CLEAR of the
        # ship: the repair lifting the hull from 38% to 52% sent the bot back into five Rocketeers (2026-11-11).
        # Learned: the rest of the fight predicted from what each attacker has left, how hard it hits and how long it
        # takes, plus the enemies whose aggro range the ship is in (they are about to join) and the helpers the types
        # bring. Fight on when it is won with hull to spare, retreat when not; the fixed lines only where nothing is
        # known (user, 2026-11-14: decide from all that is known, not from a hull line).
        advice = None
        if attackers:
            fighting = [(etype_of(u), (u.hitpoints / u.max_hitpoints) if u.hitpoints and u.max_hitpoints else 1.0)
                        for u in attackers]
            attacker_ids = {u.unit_id for u in attackers}
            joining = [etype_of(u) for u in self._enemy_units(world) if u.unit_id not in attacker_ids
                       and u.distance < self._aggro_reach(u, False) and not self._never_shoots(u)]
            ready = repair is not None and repair.ready and not repair_failing
            heal = (self._repair_heal(world, repair) or 0.0) if ready else 0.0
            advice = self._learn(lambda l: l.retreat_advice(fighting, hull, joiners=joining, caution=self.caution,
                                                            damage_rate=damage_rate, repair_heal=heal),
                                 area="retreat")
        # A retreat that has started turns back into a fight only once "won" has held for ADVICE_STEADY_S: enemies
        # coming into and out of range flipped the call between won and lost every few seconds (Vénar 02:26:10-14).
        # Few attackers and hull to spare: a "lost" guess alone does not send the ship away, only damage that is really
        # coming in fast enough to reach the retreat line within FIGHT_ON_LOOK_S, or the hull lines themselves. With the
        # upgraded ship (hull +75%) the bot used the afterburner and ran from 2 enemies at 71% hull, and ran into more
        # (user: "just fighting and winning with ease", Vénar 13:50:24-13:51:32).
        # Any number of attackers: the upgraded ship retreated from 4 Assassins at 74% and from 3 at 93% losing ~2%/s,
        # spent the afterburner, dragged the fight into more enemies and then had nothing left for a squad (user: "how
        # is it dying at all with a ship that strong", Vénar 14:30:49-14:32:39). The damage coming in decides.
        if advice is True and hull >= FIGHT_ON_HULL \
                and hull - damage_rate * FIGHT_ON_LOOK_S > self.eff_retreat_below:
            advice = None
        if advice is False and len(attackers) >= LEARN_FIGHT_ON_MAX_ATTACKERS and hull < LEARN_FIGHT_ON_FLOOR:
            # Outnumbered and low: the hull rules decide, not a "won" guess. "Fighting on at 69% / 44% (3 attackers:
            # predicted won)" kept the ship among 3 Rocketeers until 39%, the retreat came too late (Vénar 21:50:50-21:51:18;
            # user: it did not run).
            advice = None
        if advice is False and self.retreating:
            if self.advice_won_since is None:
                self.advice_won_since = now
            if now - self.advice_won_since < ADVICE_STEADY_S:
                advice = True
        else:
            self.advice_won_since = None
        if advice is not None and advice != in_trouble:
            self._learn_note(f"retreat-{advice}", f"learning: {'retreating' if advice else 'fighting on'} at "
                                                  f"{hull * 100:.0f}% hull ({len(attackers)} attackers"
                                                  f"{f', {len(joining)} about to join' if joining else ''}: the "
                                                  f"fight is predicted {'lost' if advice else 'won'})", every=20)
            in_trouble = advice
        chasers = [u for u in attackers if u.distance < RETREAT_CLEAR]
        if advice is None and self.retreating and (repair is None or repair_waiting) and chasers and (
                hull < RETREAT_RESUME_HULL or len(chasers) >= out_count):
            # Also at a high hull while outnumbered: the repair's heal lifted the hull to 97% with 4-5 Assassins on the
            # ship and repair on cooldown; the bot stopped and fought, 97% -> dead in 12 s (Vénar 21:32:17-29).
            in_trouble = True  # ends once the hull is back up: at 100% the bot kept fleeing from 4 followers
        # A fight predicted lost is left also while repair is ready: at 86% hull with 1 attacker and 4 about to join,
        # "predicted lost" was logged but the bot stayed because repair was ready; all joined (7 attackers), then a
        # squad - dead (Vénar 16:57:27-16:58:01). Leaving before the joiners arrive is the getaway.
        lost = advice is True
        self.retreating = bool(in_trouble and (repair is None or repair_waiting or lost))
        if in_trouble and (repair is None or repair_waiting or lost):
            # Retreat holds until repair is ready: never attack meanwhile (2026-11-11: back to fighting at 12% hull).
            # Flee from aggressive enemies close by and attackers, and from squads in view with their headings (the
            # retreat used to ignore squads and fled toward two of them).
            # Keep moving away from attackers following the ship (within 8,000) and aggressive enemies close by;
            # standing still while five attackers came closer was no retreat.
            attacker_ids = {a.unit_id for a in attackers}
            close = [(u.x, u.y, 0.0, 0.0) for u in self._enemy_units(world)
                     if (u.unit_id in attacker_ids and u.distance < 8000) or (u.distance < 4000 and self._aggressive(u))]
            # Squads count only when near: one 8,000-12,000 away made every escape "unsafe" (keep 0), and squads in
            # view also re-picked the spot every second, so the retreat hopped between spots 1,000-2,000 away and
            # the ship stayed in one place for 30 s with 3-5 attackers on it (Vénar 13:30:51-13:31:27, death).
            reach = self.danger_radius * RETREAT_SQUAD_SHARE
            squads = [t for t in self._threats() if math.hypot(world.x - t[0], world.y - t[1]) < reach]
            # Learned squad spawn points count as threats too: a retreat from 3 Endurers ran onto one and a squad
            # dropped in on the ship at 3% hull (Earth in Unrest 19:00:32).
            spawns = [(sx, sy, 0.0, 0.0) for sx, sy in self._spawn_points(world)
                      if math.hypot(world.x - sx, world.y - sy) < RETREAT_SPAWN_REACH] if close or squads else []
            if close or squads:
                # The spot is re-picked every second only with a squad actually close: with one 9,000 away it flipped
                # sides every second, the ship zigzagged on the spot and died (Vénar 21:51:10-18; user: ran in circles).
                squad_close = any(math.hypot(world.x - t[0], world.y - t[1]) < self.danger_radius for t in squads)
                self._flee(world, world.items, close + squads + spawns,
                           f"Retreating to repair ({hull * 100:.0f}% hull)",
                           keep_s=1.0 if squad_close else RETREAT_KEEP_SPOT_S)
                # Keep shooting while flying away (user: "run and kill them at the same time"); fire keys and a
                # right-click selection do not stop the flight.
                self._shoot_back(world, attackers, blaster, attacks)
            else:
                self.status = f"Waiting for repair ({hull * 100:.0f}% hull)"
                self.target_id = None
            return
        # Against higher-level enemies no new fight while repair cools down, whatever the hull: Vénar 22:40:50 a fight
        # started at 100% hull with repair on cooldown, 4 Signors joined, dead in 19 s (fight back only).
        no_repair_fights = self.caution == "lower" and repair_waiting
        self.no_new_fights = False  # read by _move_toward: no clearing the way while new fights are not allowed
        enemies = self._limit_new_fights(hull, repair_waiting, no_repair_fights, world, attackers, enemies, now)

        refill_mode = self.refilling or cfg.mode == "energy"
        self.refill_mode = refill_mode
        self.energy_critical = share < CRITICAL_ENERGY
        enemies = self._refill_enemies(refill_mode, share, world, enemies)
        drops = self._drops_to_collect(cfg, collector, hull, world, local, home_x, home_y, now)
        if self.leaving:
            # Stop in orbit finishes up first: the loot near the ship (attackers are fought in _leave_planet).
            if self.stop_in_orbit and drops and not attackers:
                self.target_id = None
                self._collect(world, min(drops, key=lambda u: u.distance), collector, "Collecting loot before orbit")
                return
            self._leave_planet(world, attackers, blaster, attacks, buffs, now)
            return
        # Order: whatever attacks the ship, then loot (drops vanish after 5 min), then other enemies.
        if drops and not attackers:
            self.target_id = None
            self._collect(world, min(drops, key=lambda u: u.distance), collector, "Collecting loot")
            return
        self.collect = None
        # Do not start fights that pull more than max_pull enemies: neutral enemies near the target or along the
        # way join when the ship comes close (user, 2026-11-11: the Rocketeer pack in the Prosperous tunnel neck,
        # 3-4 attackers every run, deaths even with a working retreat). Enemies already attacking are fought back.
        # Checked every tick, also for the current target until it attacks: a lone Rocketeer patrolled back to its
        # pack and the bot followed it 6,000 units into three of them (run 20:55:37-50).
        if enemies:
            enemies = [u for u in enemies if self._attacking(world, u)
                       or self._pull_allowed(world, u, hull, attackers)]
        # Enemies in the way come before other fights (attackers still first): the sought Mantis Signor was given up
        # for 60 s every time others stood between, and the bot chased targets 24,000 away instead (run 22:51).
        self.path_blockers = {k: v for k, v in self.path_blockers.items() if now - v[0] < BLOCKER_MEMORY_S}
        blockers = [u for u in self._enemy_units(world) if u.unit_id in self.path_blockers and self._can_clear(u)
                    and self._clear_allowed(world, u, hull, attackers)]
        # The hull line only holds back while repair is cooling down, as for other fights: at 49% hull with repair
        # ready the bot neither cleared the way nor gave the blocked target up, and stood still (run 00:04:33).
        hurt = (hull < self.eff_engage_min and repair_waiting) or no_repair_fights
        if any(not self._is_stun_turret(u) for u in attackers) and self._leave_stun_zone(world):
            return  # other enemies on the ship inside a Stun Turret's reach: out of the stun first
        # Pull the attackers to a clear spot before fighting them (user, 2026-11-16 recordings).
        if self._pull(world, attackers, blaster, attacks):
            return
        if blockers and not hurt and not any(self._attacking(world, u) for u in enemies):
            # The one being cleared stays the target until it is gone; else the FIRST one the route runs into (nothing
            # stands in front of it). Picking "the one that can be pulled alone" chose blockers whose own way was
            # blocked, flipped between them several times a second and never moved (run 23:21:12-40).
            pick = next((u for u in blockers if u.unit_id == self.target_id), None)
            if pick is None:
                pick = min(blockers, key=lambda u: (self.path_blockers[u.unit_id][1], u.distance))
            if self.target_id != pick.unit_id:
                self._debug(f"clearing the way: {assets.display_name(pick.desc or pick.name)} at "
                            f"({pick.x:.0f}, {pick.y:.0f})")
                self.target_id = pick.unit_id
            self._fight(world, [pick], blaster, attacks, buffs)
            return
        if enemies:
            self.last_enemy_seen = now
            self.last_enemy_spot = min(((u.x, u.y) for u in enemies),
                                       key=lambda p: math.hypot(p[0] - world.x, p[1] - world.y))
            self._fight(world, enemies, blaster, attacks, buffs)
            return
        self.target_id = None
        if refill_mode:
            if cfg.mode == "energy":
                self._refill(world, collector, share, forever=True)
            elif share >= (0.995 if self.land_refill else min(cfg.refill_until, 0.995)) \
                    or self._refill(world, collector, share):
                # Lower / same level: energy full but the hull below the fight line and repair cooling down: stay at
                # the field until repair is back (then the smart heal uses it), at most FIELD_HEAL_WAIT_S.
                near = self.at_field or self._near_field(world)
                self.at_field = near
                heal_wait = (self.caution != "high" and near and repair is not None and not repair.ready
                             and hull < PRE_FIGHT_HEAL.get(self.caution, 0.0))
                if heal_wait:
                    self.field_heal_since = self.field_heal_since or now
                if heal_wait and now - self.field_heal_since < FIELD_HEAL_WAIT_S:
                    self.status = f"Healing at the energy field ({hull * 100:.0f}% hull)"
                else:
                    self.refilling = False
                    self.land_refill = False
                    self.orb_wait = None
                    self.field_heal_since = None
            return

        # Not toward enemies that are in view but were turned down (pull limit, ignore list, squad): the bot flew
        # into the Rocketeer pack it had just rejected as "4 enemies" (run 21:00:39-44) and died there.
        self._roam(world, local, cfg, now, home_x, home_y)

    def _limit_new_fights(self, hull, repair_waiting, no_repair_fights, world, attackers, enemies, now):
        """Hurt with the repair not ready (or lower level mode): no new fights, attackers only."""
        if (hull < self.eff_engage_min and repair_waiting) or no_repair_fights:
            # Hurt and repair cooling down: fight back, but pull nothing new.
            self.no_new_fights = True
            enemies = self._fight_back(world, attackers)
        elif self._squad_eta(world) < self._squad_fight_eta() or self._nearest_member(world, max_age=5) < \
                self.danger_radius * SQUAD_FIGHT_NEAR_SHARE:
            # A squad in view would reach the keep-away distance soon, or is near whichever way it heads: start no
            # new fight, only fight back (2026-11-11: an Avenger fight began with a squad 16,000 away closing at
            # 800 u/s; a Rocketeer fight began 6,700 from a squad that then turned toward the ship).
            if enemies != attackers and now - self.last_eta_note > 5:
                self.last_eta_note = now
                self._debug(f"squad near or arriving (eta {self._squad_eta(world):.0f} s); no new fights")
            self.no_new_fights = True
            enemies = self._fight_back(world, attackers)
        return enemies

    def _refill_enemies(self, refill_mode, share, world, enemies):
        """While refilling: only the enemies that attack the ship (or are about to) are fought."""
        if refill_mode:
            # While collecting energy only attackers and aggressive enemies close by are fought. Loot and clearing
            # the way still happen: refilling used to skip both, so drops lay next to the ship for minutes and the
            # bot stood still when enemies blocked the way to the energy field (run 00:53-00:55).
            # Hunting enemies for completed blueprints (energy on pickup) is for when no energy field is at hand, and
            # never on almost empty energy: right after landing next to a field at 6-9% the bot went after Assassins
            # (two blueprint-hunt fights 13:58:44-14:00:08) instead of collecting, and paused at 3% (627 energy).
            hunt = set() if (self.at_field or share < BP_HUNT_MIN_ENERGY
                             or self._field_near(world, BP_HUNT_FIELD_REACH)) else self._bp_hunters(world, enemies)
            # Almost empty: only what attacks is fought (aggressive enemies nearby pulled two fights at 6-9%).
            near_aggro = 5000 if share >= CRITICAL_ENERGY else 0
            enemies = [u for u in enemies if self._attacking(world, u)
                       or (self._aggressive(u) and u.distance < near_aggro) or u.unit_id in hunt]
        return enemies

    def _drops_to_collect(self, cfg, collector, hull, world, local, home_x, home_y, now):
        """Loot worth collecting now: in reach of the farm spot, safe to pick up, not ignored."""
        drops = []
        # No loot runs while hurt (2026-11-11: it flew to a drop at 4% hull).
        if cfg.collect_loot and collector and hull >= self.eff_engage_min:
            drops = [
                u for u in world.units
                if u.is_item and not _is_orb(u) and u.distance <= cfg.loot_radius
                and (not local or math.hypot(u.x - home_x, u.y - home_y) <= cfg.leash_radius + 2000)
                and (round(u.x), round(u.y)) not in self.ignored
                and not self._in_danger(u.x, u.y, DANGER_TARGET_MARGIN)
            ]
            drops = [u for u in drops if self._loot_safe(world, u, now)]
        return drops

    def _repair_tick(self, repair, hull, cfg, damage_rate, world, now):
        """Press the repair when the hull is (about to be) below the line; else hold everything for it."""
        if repair and repair.ready and not repair.is_active and (
                hull < cfg.repair_below or hull - damage_rate * RETREAT_LOOKAHEAD_S < cfg.repair_below
                or self._smart_heal(world, hull)) and not self._repair_held(world, hull):
            # Same one-at-a-time rule as the other skills: a repair pressed in the same second as a blaster shot did
            # not take, the next one 3 s later did, with 9 attackers (10:36:56). Hold the blaster and repair as soon
            # as its cycle is over.
            if now >= self.gcd_until and now >= self.cast_until:
                if self._skill(repair):
                    self.weapon_hold = now + (repair.warmup or 0.0) + CAST_MARGIN_S + WEAPON_GAP_S
                    self.repair_wanted = False
            else:
                # Repair first: hold the blaster and every other skill until the repair can go.
                self.repair_wanted = True
                self.weapon_hold = max(self.weapon_hold, now + 1.0)
        else:
            self.repair_wanted = False

    def _energy_tick(self, share, cfg, world, now):
        """Energy guardrails: very low energy, start refilling, the landing check, the energy trip back."""
        if share < cfg.stop_below and not self.just_landed:
            # Guardrail (user, 2026-11-15): with no energy the bot must never fly to a field that may lie behind
            # enemies - it cannot fight there and would die again and again. A field close by: collect it (fighting
            # back only). Else: to orbit and land again; the landing check below then needs energy in the landing
            # zone, or the bot stops in orbit.
            if self._field_near(world, LAND_FIELD_RANGE):
                if now - self._low_energy_note > 30:
                    self._low_energy_note = now
                    self._log("warning", f"Energy very low ({share * 100:.0f}%); collecting at the field close by")
            elif not self.reland and not self.stop_in_orbit:
                self._log("warning", f"Energy very low ({share * 100:.0f}%) and no energy field close by; going to "
                                     f"orbit to land again")
                self.reland = True
        if share < cfg.refill_below:
            self.refilling = True
        if self.just_landed:
            # Zooming out is kept up by the camera check (_check_camera, every 3 s while zoomed in): the wheel right at
            # landing is ignored while the landing camera still moves, so a zoom here only failed with a warning
            # (user, 2026-11-16: delay it instead). Here only while the wheel direction is still unknown (it is
            # learned once, with the camera settled first).
            if self.zoom_direction is None:
                self._zoom_out()
            # Refill on landing (user, 2026-11-12): to full, only if an energy field is near the landing spot.
            self.just_landed = False
            near_field = self._field_near(world, LAND_FIELD_RANGE)
            on_terasa = _same_place(world.current_planet, ENERGY_PLANET)
            if self.energy_trip is not None and on_terasa:
                if self._field_near(world, ENERGY_PLANET_FIELD_RANGE):
                    self._log("info", f"Landed on {ENERGY_PLANET} for energy; refilling to full ({share * 100:.0f}%)")
                    self.refilling = True
                    self.land_refill = True
                else:
                    self._log("warning", f"No energy field in {ENERGY_PLANET}'s landing zone; going to orbit to stop")
                    self.energy_trip = None
                    self.stop_in_orbit = True
            elif share < cfg.refill_below and not near_field and not self.stop_in_orbit:
                # Guardrail (user, 2026-11-15): landed low on energy (after a death, or to land again) and no energy
                # in the landing zone: do not look for a field elsewhere on this planet. Refill on Terasa (Antares)
                # and come back (user's fallback); if Terasa cannot be reached, go to orbit and stop.
                if self._energy_planet_ok(world):
                    self.energy_trip = {"destination": self.destination, "home_planet": self.home_planet}
                    self.destination = ENERGY_PLANET
                    self.patrol_centre = None
                    self._log("warning", f"Landed with {share * 100:.0f}% energy and no energy field in the landing "
                                         f"zone; flying to {ENERGY_PLANET} to refill, then back")
                else:
                    self._log("warning", f"Landed with {share * 100:.0f}% energy and no energy field in the landing "
                                         f"zone; going to orbit to stop")
                    self.stop_in_orbit = True
            elif cfg.refill_on_land and share < 0.995 and near_field:
                self._log("info", f"Landed next to an energy field; refilling to full first ({share * 100:.0f}%)")
                self.refilling = True
                self.land_refill = True

        if self.energy_trip is not None and not self.refilling and share >= ENERGY_TRIP_DONE and \
                _same_place(world.current_planet, ENERGY_PLANET):
            trip, self.energy_trip = self.energy_trip, None
            self.destination, self.home_planet, self.patrol_centre = trip["destination"], trip["home_planet"], None
            self._log("info", f"Refilled on {ENERGY_PLANET} ({share * 100:.0f}%); going back to {self._home_name()}")

    def _farm_enemies(self, world, now, cfg, local, home_x, home_y):
        """The enemies worth a fight this tick (in reach of the farm spot, not squads or spawn drops, ...)."""
        enemies = []
        squads_now = self._threats()
        stun_zones = self._stun_zones(world)
        # A squad in view is alive and cannot drop in; the normal squad handling covers it.
        # With a weak afterburner (upgraded hull, x1.2-1.4) no squad can be outrun even with it ready: near a spawn
        # point no new fights at all while the squad is not in view (3 deaths in an hour at the Vénar spawn points,
        # 21:00, 21:19, each a fresh squad dropping in 4,500-6,200 from a fight; user: the afterburner is too weak).
        spawn_risk = not squads_now and (not self._escape_ready(world) or self._ab_weak())
        self.ghosts = {k: v for k, v in self.ghosts.items() if v > now}
        for u in self._enemy_units(world):
            if u.unit_id in self.ghosts:
                continue
            attacking = self._attacking(world, u)
            # An enemy set aside (unreachable or not selectable for a while) still counts once it attacks: one that
            # was set aside chased the fleeing ship for 60 s and was never shot back or counted (tunnel simulation).
            far = self.far_given_up
            if far.get(u.unit_id, 0.0) > now and u.distance > self.weapon_range * 1.5:
                continue  # an attacker that stayed out of reach: only fought once it comes close
            if (u.unit_id in self.ignored and not attacking) or u.distance > cfg.engage_range:
                continue
            if local and not attacking and math.hypot(u.x - home_x, u.y - home_y) > cfg.leash_radius:
                continue
            if not attacking and spawn_risk and self._near_spawn(world, u.x, u.y):
                # Near a squad spawn point while the squad is not in view (it may drop in any moment): a new fight
                # there only with the escape ready - afterburner and repair (deaths 12:30, 12:50, 15:34, 15:58 at
                # one Venar spawn point, each time with one of them used up). Not a no-go area: routes, patrols and
                # fights with the escape ready go on there (user: an 8,000 keep-away blocks routes and easy enemies).
                continue
            if not attacking and not self._is_stun_turret(u) and any(
                    math.hypot(u.x - zx, u.y - zy) < zr for zx, zy, zr in stun_zones):
                # A Stun Turret alone is easy, but stunned with other enemies on the ship it is as bad as a Custodian
                # (user, 2026-11-15; death 14:30:00, 11 s stunned): no fight on other enemies inside a turret's reach.
                continue
            if not attacking and squads_now and not self._route_safe([(world.x, world.y), (u.x, u.y)], full=True):
                # The way to it passes a squad (where the squad will be, when its way is known): not a target now.
                # Chosen anyway, the bot flew toward the squad, gave the target up there, wandered back and took the
                # next one beyond it (Vénar 22:11-22:13, back and forth in the landing area for minutes).
                continue
            if self._wanted(u, attacking):
                enemies.append(u)
        return enemies

    def _trace_tick(self, now, world, enemies, share, repair, evading):
        """Once a second: one trace line with the ship's state and the attackers (debug log)."""
        if now - self.last_trace >= 1.0:
            # One line per second for log analysis: ship state always, the nearest squad member when one is known.
            self.last_trace = now
            hull_text = f"{world.hitpoints / world.max_hitpoints * 100:.0f}%" \
                if world.hitpoints is not None and world.max_hitpoints else "?"
            attacking = sum(1 for u in enemies if self._attacking(world, u))
            text = (f"trace ship ({world.x:.0f}, {world.y:.0f}) hull {hull_text} energy {share * 100:.0f}% "
                    f"attackers {attacking} repair {'ready' if repair and repair.ready else 'not ready'}")
            threats = self._threats()
            if threats:
                x, y, vx, vy = min(threats, key=lambda t: math.hypot(world.x - t[0], world.y - t[1]))
                distance = math.hypot(world.x - x, world.y - y)
                closing = (vx * (world.x - x) + vy * (world.y - y)) / (distance or 1.0)
                text += (f" | nearest member ({x:.0f}, {y:.0f}) {distance:.0f} away moving ({vx:.0f}, {vy:.0f})/s "
                         f"closing {closing:.0f}/s members in view {len(threats)} evading {evading}")
            self._debug(text)

    def _roam(self, world, local, cfg, now, home_x, home_y):
        """Nothing to fight or collect: fly to the last enemy sighting, patrol, return to the farm spot or wait."""
        if self.last_enemy_spot and any(math.hypot(u.x - self.last_enemy_spot[0], u.y - self.last_enemy_spot[1])
                                        < self._aggro_reach(u) + 2000 for u in self._enemy_units(world)):
            self.last_enemy_spot = None
        if self.last_enemy_spot and (self._in_danger(*self.last_enemy_spot, DANGER_TARGET_MARGIN)
                                     or math.hypot(self.last_enemy_spot[0] - world.x,
                                                   self.last_enemy_spot[1] - world.y) <= 1500):
            self.last_enemy_spot = None
        if self.last_enemy_spot and local:
            self.last_enemy_spot = None  # Park modes stay at the farm spot
        if self.last_enemy_spot:
            self.status = "Flying to where enemies were seen"
            if not self._move_toward(world, *self.last_enemy_spot, "the last enemy sighting"):
                self.last_enemy_spot = None
        elif cfg.mode in ("wander", "seek_enemy", "seek_blueprint", "bp_completion"):
            self._patrol(world, whole_map=True)
        elif cfg.mode == "patrol" and now - self.last_enemy_seen > cfg.patrol_after_s:
            self._patrol(world)
        elif math.hypot(world.x - home_x, world.y - home_y) > cfg.home_tolerance:
            self.status = "Returning to the farm spot"
            self._move_toward(world, home_x, home_y, "the farm spot")
        else:
            self.status = "Waiting for enemies"

    def _leave_planet(self, world, attackers, blaster, attacks, buffs, now):
        """Take off (F5) for another planet or Fly to Planet. The game refuses it in combat and only allows it a few
        seconds after the last fight, so: attackers are fought back (nothing new is pulled, no loot runs), then the
        ship moves clear of enemies that would join, and F5 is pressed once nothing has fought it for LEAVE_CALM_S.
        Repair, retreat and squad evasion stay as on any farm tick (they run before this)."""
        if any(self._attacking(world, u) for u in self._enemy_units(world)):
            self.leave_fight_at = now
        if attackers:
            self._fight(world, attackers, blaster, attacks, buffs)
            self.leave_fight_at = now
            return
        self.target_id = None
        battle = world.in_battle
        if battle:
            self.leave_fight_at = now
        calm_since = max(self.leave_fight_at, self.last_fight_time)
        if battle is False:
            # The game's own combat state (ship property "battle", read live 2026-11-14: off 7 s before a manual
            # take-off reached orbit): take off the moment it is off; no clearance move or calm timer needed.
            ready, retry_s = True, LEAVE_RETRY_KNOWN_S
        else:
            close = [(x, y) for x, y, reach in self._aggro_zones(world, margin=False)
                     if math.hypot(world.x - x, world.y - y) < reach + LEAVE_CLEAR_MARGIN]
            if close:
                self._flee(world, world.items, close, f"{self.leaving}: moving away from enemies")
                return
            # In combat by the game's flag, or the flag not readable: wait (by the flag, else LEAVE_CALM_S).
            ready, retry_s = battle is None and now - calm_since >= LEAVE_CALM_S, LEAVE_RETRY_S
        self.flee_point = None
        if not ready:
            self.status = f"{self.leaving}: waiting to be out of combat"
            return
        pressed = self.take_off_at
        # Again only when the last press cannot have worked: there was a fight since, or nothing happened for a while
        # (a second press during a take-off might cancel it).
        if pressed is None or calm_since > pressed or now - pressed > retry_s:
            if pressed is not None:
                self._debug(f"take-off did not start {now - pressed:.0f} s after F5; pressing it again")
            self.take_off_at = now
            self._key("F5")
        self.status = self.leaving

    def _patrol(self, world, whole_map=False):
        """Sweep the open ground reachable from the start spot until enemies show up (see patrol.py).
        whole_map: roam the planet (Wander). Seek modes visit the sought enemies' spawn areas from the site data
        when known, else roam too."""
        cfg = self.config
        now = time.monotonic()
        nav = self._nav(world)
        if nav is not None:
            areas = []
            if self.seek is not None:
                areas = myzen.enemy_areas(assets.display_name(world.current_planet), self.seek["names"])
            if areas:
                key = (world.planet_map, "areas", tuple(self.seek["names"]))
            else:
                key = (world.planet_map, "whole map" if whole_map else self.patrol_centre)
            if self.patrol is None or self.patrol_key != key:
                if areas:
                    self.patrol = Patrol.from_points(nav, areas, start=(world.x, world.y))
                    self._debug(f"patrol: {len(self.patrol)} spawn areas of {', '.join(self.seek['names'])}"
                                f" ({self.patrol.unreachable} not reachable from here left out)")
                    if not len(self.patrol) and self.patrol.unreachable:
                        self._warn_once(f"unreachable-{world.planet_map}",
                                        "The sought enemies' areas cannot be reached from this part of the planet")
                else:
                    centre = (world.x, world.y) if whole_map else self.patrol_centre
                    radius = assets.MAP_SPAN if whole_map else cfg.leash_radius
                    self.patrol = Patrol(nav, centre, radius, wander=whole_map)
                    if self._learner("patrol") is not None:
                        planet_map = world.planet_map
                        self.patrol.prefer = lambda x, y: (self._learn(
                            lambda l: l.place_bonus(planet_map, x, y, lambda et: True), area="patrol") or 0.0) \
                            * LEARN_PATROL_BONUS_S
                    self._debug(f"patrol area: {len(self.patrol)} blocks of open ground")
                self.patrol_key = key
            if len(self.patrol):
                self.patrol.update(world.x, world.y, now)
                avoid = self._avoid_point(world)
                # The current target is dropped only for a squad; an enemy wandering near it is fought on arrival.
                point = self.patrol.target(world.x, world.y, avoid, drop=self._avoid_point(world, squads_only=True))
                if point is None:
                    self.status = "Waiting: patrol area is dangerous"
                    return
                # A squad blocking the way to this point: try the nearest other points (a few per tick) instead of
                # standing still until it leaves (2026-11-11: 20+ s idle in the landing pocket). Blocked points are
                # only skipped for this tick, not marked as seen: marking them burned through the whole patrol while
                # a squad sat in a tunnel. If none is free, hold for the first one (up to SQUAD_WAIT_S).
                blocked, first_blocked, moving = [], None, False
                # Time budget per tick: 30-odd route checks across the map froze the bot for 10 s while a squad
                # charged from 7,500 to 2,000 unseen (no ticks, so its speed read 0), dead (Vénar 19:53:42-54:06).
                # The points found blocked are kept a few seconds, so the next ticks go on with the others.
                if now - self.patrol_blocked_at < PATROL_BLOCKED_KEEP_S:
                    blocked, first_blocked = list(self.patrol_blocked), self.patrol_first_blocked

                def skip(x, y):
                    return avoid(x, y) or any(abs(x - bx) < 1 and abs(y - by) < 1 for bx, by in blocked)
                if blocked and skip(*point):
                    point = self.patrol.target(world.x, world.y, skip, nearest=True)
                search_start = time.perf_counter()
                for attempt in range(PATROL_RETRIES):
                    if point is None:
                        break
                    if attempt and time.perf_counter() - search_start > PATROL_SEARCH_BUDGET_S:
                        break  # the rest next tick
                    if not self._move_toward(world, *point, "the next patrol point"):
                        self.patrol.give_up(now)  # unreachable, or blocked by a squad for too long
                    elif not self.squad_blocked:
                        moving = True
                        break
                    else:
                        blocked.append(point)
                        if first_blocked is None:
                            first_blocked = self.patrol.current
                    point = self.patrol.target(world.x, world.y, skip, nearest=True)
                if moving:
                    self.patrol_blocked_at = -1e9
                elif blocked:
                    if now - self.patrol_blocked_at >= PATROL_BLOCKED_KEEP_S:
                        self.patrol_blocked_at = now
                    self.patrol_blocked, self.patrol_first_blocked = blocked, first_blocked
                if moving and self.enemy_blocked:
                    self.status = "Clearing the way"
                elif moving:
                    self.status = "Searching" if self.seek is not None else (
                        "Wandering" if whole_map else "Patrolling for enemies")
                elif first_blocked is not None:
                    self.patrol.current = first_blocked
                    self.status = "Waiting for the squad to pass"
                return
        # No terrain map: fly a circle of points around the start spot.
        radius = cfg.leash_radius * cfg.patrol_radius_fraction
        angle = 2 * math.pi * self.patrol_index / cfg.patrol_points
        px = self.patrol_centre[0] + radius * math.cos(angle)
        py = self.patrol_centre[1] + radius * math.sin(angle)
        if math.hypot(px - world.x, py - world.y) < 1200:
            self.patrol_index = (self.patrol_index + 1) % cfg.patrol_points
            return
        self.status = "Patrolling for enemies"
        if not self._move_toward(world, px, py, "the next patrol point"):
            self.patrol_index = (self.patrol_index + 1) % cfg.patrol_points
