"""Learning while playing (docs/LEARNING.md): advice used by fights and repairs.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time
import traceback

from ..learn.learner import Learner, etype_of, type_name
from .constants import *  # noqa: F401,F403


class LearningMixin:
    # Learning (docs/LEARNING.md) ------------------------------------------------------------------------------

    def _learner(self, area=None, entry=None):
        """The learner if learning is on (and the area / entry switch, if given), else None."""
        if not self.config.learning or self.learn_failed:
            return None
        if self.learner is None:
            try:
                self.learner = Learner(debug=self._debug)
            except Exception as error:
                self.learn_failed = True
                self._log("warning", f"Learning switched off for this run: {error}")
                return None
        if area is not None and not self.learner.store.on(area, entry):
            return None
        return self.learner

    def _learn(self, fn, area=None, entry=None):
        """Runs fn(learner); a failure switches learning off for this run instead of stopping the bot."""
        learner = self._learner(area, entry)
        if learner is None:
            return None
        try:
            return fn(learner)
        except Exception as error:
            self.learn_failed = True
            self._log("warning", f"Learning switched off for this run after an error: {type(error).__name__}: "
                                 f"{error}")
            self._debug(traceback.format_exc())
            return None

    def _learn_note(self, key, text, every=120):
        now = time.monotonic()
        if now - self.learn_notes.get(key, -math.inf) > every:
            self.learn_notes[key] = now
            self._debug(text)

    def _deploy_ok(self, world, skill, target):
        """Learned use of a skill that puts down a turret, drone or trap (any skill, recognised by what appears after
        the press): True / False, or None when not known (the normal rules decide)."""
        hull = world.hitpoints / world.max_hitpoints if world.hitpoints is not None and world.max_hitpoints else 1.0
        foes = [u for u in self._enemy_units(world) if math.hypot(u.x - world.x, u.y - world.y) < 4000
                and (self._attacking(world, u) or self._aggressive(u))]
        group = [etype_of(u) for u in self._enemy_units(world) if self._attacking(world, u)
                 or (target is not None and u.unit_id == target.unit_id)]
        return self._learn(lambda l: l.deploy_use(skill.desc or skill.name or "?", [etype_of(u) for u in foes],
                                                  group, hull), area="skills")

    def _repair_heal(self, world, repair):
        """What a repair heals over its active time, as a share of the hull (capped at 1), from the item's heal rate in
        memory; None if not known (the learned value is used then). A regeneration: 8.45%/s for 16 s on this ship."""
        rate = getattr(repair, "heal_rate", None)
        if not rate or not world.max_hitpoints:
            return None
        return min(1.0, rate * (repair.active_time or 0.0) / world.max_hitpoints)

    def _repair_held(self, world, hull):
        """Learned: a repair that would mostly be wasted (heals more than is missing, little damage coming in)."""
        types = [etype_of(u) for u in self._enemy_units(world) if self._attacking(world, u)]
        if not types:
            # Out of combat a heal before the next fight is never wasted (it recharges in about a minute): with the
            # repair's real heal (77% on the upgraded hull) the hold kept it back at 56%, the bot started a Signor fight
            # at 56% and died (Vénar 12:53:30-12:55:11).
            return False
        repair = next((i for i in world.items if i.desc and "repair" in i.desc), None)
        heal = self._repair_heal(world, repair) if repair is not None else None
        held = self._learn(lambda l: l.repair_hold(hull, types, self.config.repair_below, heal), area="skills")
        if held:
            self._learn_note("repair-hold", f"learning: holding the repair at {hull * 100:.0f}% hull (it would mostly "
                                            f"be wasted)", every=60)
        return bool(held)

    def _learn_context(self, world, blaster, now):
        nav = self._nav(world)
        return {"attacking": {u.unit_id for u in self._enemy_units(world) if self._attacking(world, u)},
                "target_id": self.target_id, "firing": now - self.last_press < 2.0,
                "retreating": self.retreating, "caution": self.caution,
                "moving": now - self.last_move < 2.5, "at_field": self.at_field,
                "nav": nav, "rank_raw": world.rank_raw,
                "blaster_template": getattr(blaster, "template_id", None),
                "drone_roles": {d.index: self._drone_role(d) for d in world.drones}}

    def _pull_group(self, world, target):
        """The enemies a fight on target brings (as for _pull_size), as units."""
        standoff = self.weapon_range * STANDOFF_SHARE
        if self._firing_spot(world, target, standoff) is not None:
            return [target]
        dx, dy = world.x - target.x, world.y - target.y
        length = math.hypot(dx, dy) or 1.0
        spot = (target.x + dx / length * standoff, target.y + dy / length * standoff)
        return [target] + [u for u in self._enemy_units(world) if u.unit_id != target.unit_id
                           and not self._attacking(world, u)
                           and math.hypot(spot[0] - u.x, spot[1] - u.y) < self._aggro_reach(u)]

    def _pull_allowed(self, world, unit, hull, attackers):
        """May a fight on this (not yet attacking) enemy start? The pull size setting, unless learning knows
        better: a fight predicted to take the hull below the retreat line (+ margin) is not started (Fight risk),
        and a predicted-safe one may pull up to LEARN_PULL_EXTRA more than the setting (Pull size)."""
        size = self._pull_size(world, unit)
        by_setting = size <= self.eff_max_pull
        risk = self._learner("fight_risk", type_name(etype_of(unit)))
        pulls = self._learner("pulls")
        if risk is None and pulls is None:
            return by_setting
        if not (risk or pulls).known(etype_of(unit)):
            return by_setting
        group = [etype_of(u) for u in self._pull_group(world, unit)] + [etype_of(u) for u in attackers]
        ok = self._learn(lambda l: l.fight_ok(group, hull, self.eff_retreat_below + LEARN_SAFE_MARGIN, self.caution))
        if ok is False and risk is not None:
            self._learn_note(f"risk-{etype_of(unit)}-{len(group)}",
                             f"learning: not starting a fight on {etype_of(unit)} ({len(group)} enemies predicted, "
                             f"hull {hull * 100:.0f}%): predicted too costly", every=30)
            return False
        if ok is True and pulls is not None:
            return size <= self.config.max_pull + LEARN_PULL_EXTRA
        return by_setting
