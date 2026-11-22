"""Learner: watches the bot's play (one call per tick) and answers its questions (docs/LEARNING.md).

Observed, never decided from one event:
- fights as episodes with their situation (who attacked, how many, how the group grew, start / lowest hull, outcome);
- damage each enemy type does to this ship (each second's hull loss split over the attackers by current estimates);
- time to kill each type; helpers spawned around a type (Harvester waves); drops, cryonite and XP per kill;
- stuck spots vs successful passes per planet cell; where enemies are seen;
- drones (cost vs measured effect) and skills (target hull removed per energy).
Every estimate carries its sample count; decisions require enough samples and use a cautious margin.
"""
import math
import time

from ..game import assets
from .store import Store

SECOND = 1.0
EPISODE_END_S = 8.0          # no attacker this long ends a fight episode
KILL_VIEW = 7000.0           # an engaged enemy vanishing closer than this was killed (not just out of view)
DROP_LINK_S = 5.0            # a drop appearing this soon after a death, near it, came from that enemy
DROP_LINK_R = 900.0          # drops lie where the enemy died
DROP_TIE_R = 300.0           # deaths this much further than the nearest one are as likely: the drop is split
DROP_KEEP_S = 300.0          # drops stay on the ground about this long
LOOT_MATCH_S = 2.5           # a cryonite / energy rise belongs to pickups within this time, either way round
PICKUP_R = 1500.0
HELPER_WATCH_S = 30.0
HELPER_R = 3000.0
CAP_ATT_S = 900.0            # damage estimates: at most this many attacker-seconds of weight (older evidence fades)
CAP_KILLS = 40.0
CAP_FIGHTS = 60.0
CAP_DROP_KILLS = 400.0
MIN_ATT_S = 20.0             # damage estimate trusted from this many attacker-seconds
MIN_KILLS = 3.0              # time to kill trusted from this many kills
MIN_FIGHTS_HARMLESS = 5.0
RETREAT_END_HULL = 0.10  # a fight predicted to end below this (with the margin) is left
HISTORY_TRUSTED = 10  # fights of a situation from which its history decides the retreat over a short burst
LIVE_OVERRIDE = 2.0  # ...unless the damage now is more than this many times the attackers' learned rate
ROUTE_CELL_COST = 25.0       # route cost of a trouble cell at a stuck share of 1 (one cell step costs 1)
PLACE_CELL = 4000.0
GROUP_BUCKETS = ((1, "1"), (3, "2-3"), (6, "4-6"), (math.inf, "7+"))
BUCKET_ORDER = [label for _, label in GROUP_BUCKETS]


def etype_of(unit):
    return f"{assets.display_name(unit.desc or unit.name) or '?'}|{unit.level or 0}"


def type_name(etype):
    return etype.rsplit("|", 1)[0]


def bucket(n):
    return next(label for top, label in GROUP_BUCKETS if n <= top)


def _scale(entry, keys, cap_key, cap):
    """Keep the weight of an estimate bounded so new evidence keeps counting (older evidence fades)."""
    weight = entry.get(cap_key, 0.0)
    if weight > cap:
        factor = cap / weight
        for key in keys:
            if key in entry:
                entry[key] *= factor


class Learner:
    def __init__(self, store=None, debug=lambda text: None):
        self.store = store or Store()
        self.debug = debug
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.units = {}        # uid -> [etype, x, y, last_seen, hp share]
        self.items = {}        # uid -> [x, y, first_seen, source etype or None, template, name, rare]
        self.engaged = {}      # uid -> (first engaged time, etype)
        self.kills = []        # recent deaths near the ship (time, etype, x, y): own kills and others (skills,
                               # turrets, other players): every death may leave a drop
        self.prev_enemies = set()
        self.away = {}         # drop key -> (time it left view, item entry): drops out of view, not picked up
        self.loot_rises = []   # (time, "cryo" / "energy", amount) not matched to a pickup yet
        self.pickups = []      # (time, etype, template, cryonite before, energy before, xp before)
        self.watch = {}        # etype -> (until, x, y, seen uids at start, new uids)
        self.episode = None
        self.sec = None        # (start time, hull, {etype: attacker count})
        self.last_xp = None
        self.last_cell = None
        self.place_seen = {}   # uid -> cell key
        self.last_place = 0.0
        self.drone_state = {}  # drone desc -> (time, charge, active)
        self.caution = None    # farming level of the current tick (lower / same / high), from the bot
        self.drone_sec = None
        self.pending_skills = []
        self.effects = []      # running shields / protectors / buffs / deployed turrets and drones being measured
        self.presses = []      # (time, desc, energy) of recent skill presses: a friendly unit appearing next to the
                               # ship right after one means that skill deploys something (turret, drone, trap)
        self.friendly = set()  # friendly units (not players) already seen
        self.attacking_types = []
        self.config_checked = False
        self.hull = None
        self.prev = (None, None)
        self.xp_rises = []     # (time, XP gained) not matched to a kill yet  # cryonite, energy at the previous tick (a pickup's gain is counted from there)

    # Per tick ---------------------------------------------------------------------------------------------------

    def tick(self, world, ctx):
        """ctx: attacking (set of uids), target_id, firing (pressed in the last 2 s), retreating, caution, moving,
        at_field, blaster_template, rank_raw, refilling. Called by the bot on planets only."""
        with self.store.lock:  # the Learned tab copies the data under the same lock
            self._tick(world, ctx)

    def _tick(self, world, ctx):
        now = time.monotonic()
        store = self.store
        if not world.ship_type:
            return
        store.use_ship(world.ship_type)
        self.caution = ctx.get("caution") or "same"
        if not self.config_checked:
            self._check_config(world, ctx)
        if world.hitpoints is None or not world.max_hitpoints:
            return
        hull = world.hitpoints / world.max_hitpoints
        self.hull = hull
        enemies = {}
        for u in world.units:
            if u.is_item or u.is_player or u.class_name.endswith("/tb"):
                continue
            if u.unit_id is None or not (u.marker and "alien" in u.marker and "friendly" not in u.marker
                                         or (u.desc or u.name or "").startswith("$alien.")):
                continue
            if u.exploded or (u.hitpoints or 0) <= 0:
                continue
            enemies[u.unit_id] = u
        self._kills(world, enemies, ctx, now)
        self._seen(world, enemies, now)
        self._damage(enemies, ctx, hull, now)
        self._episodes(world, enemies, ctx, hull, now)
        self._helpers(enemies, now)
        self._drops(world, now)
        self._planet_name(world)
        self._places(world, enemies, now)
        self._routes(world, ctx)
        self._drones(world, ctx, hull, now)
        self._skills(world, enemies, now)
        self.attacking_types = [etype_of(enemies[uid]) for uid in ctx.get("attacking", ()) if uid in enemies]
        self._deployed(world, now)
        self._effects(hull, now)
        self.prev = (world.cryonite, world.energy)
        store.save()

    def _check_config(self, world, ctx):
        """Ship configuration (armor, items, drones). A change keeps what was learned; only what depends on it is
        weakened: armor -> damage taken, weapon -> time to kill. New items simply start without data."""
        if not world.items:
            return
        self.config_checked = True
        ship = self.store.ship
        config = {"armor": world.armor_level, "items": sorted(i.template_id for i in world.items if i.template_id),
                  "weapon": ctx.get("blaster_template"), "drones": sorted(d.desc or "" for d in world.drones),
                  "level": world.ship_level}
        name = assets.display_name(world.ship_name) if world.ship_name else None
        old = ship.get("config") or {}
        if old and old != config:
            changes = []
            if old.get("armor") != config["armor"] or old.get("level") != config["level"]:
                for entry in ship["enemies"].values():
                    for key in ("dmg", "att_s"):
                        entry[key] = entry.get(key, 0.0) * 0.5
                changes.append("armor: damage estimates weakened")
            if old.get("weapon") != config["weapon"]:
                for entry in ship["enemies"].values():
                    for key in ("kill_s", "kills"):
                        entry[key] = entry.get(key, 0.0) * 0.5
                changes.append("weapon: kill times weakened")
            new_items = set(config["items"]) - set(old.get("items", []))
            if new_items:
                changes.append(f"{len(new_items)} new item(s) learned from scratch")
            self.debug("learning: ship configuration changed (" + ("; ".join(changes) or "drones") + ")")
        ship["config"] = config
        ship["name"] = name or ship.get("name")
        self.store.mark("ship")

    def _seen(self, world, enemies, now):
        for uid, u in enemies.items():
            share = (u.hitpoints / u.max_hitpoints) if u.max_hitpoints else None
            self.units[uid] = [etype_of(u), u.x, u.y, now, share]
        self.units = {k: v for k, v in self.units.items() if now - v[3] < 60}

    def _kills(self, world, enemies, ctx, now):
        target = ctx.get("target_id")
        if target in enemies and ctx.get("firing"):
            # Kill time counts only the seconds spent firing at it: from first shot to death it included retreats
            # and strike dodges, so Signors read 31 s to kill (4 kills in 17 fights), every lone Signor was
            # "predicted lost" at full hull and the bot fled instead of finishing it (user, 2026-11-15 22:09).
            entry = self.engaged.setdefault(target, [now, etype_of(enemies[target]), 0.0, now])
            entry[2] += min(now - entry[3], 1.5)
            entry[3] = now
        # Enemies that died without being the bot's target (stacked enemies hit by a skill, a turret, another
        # player): no kill time, but a possible source of the drops around them.
        exploded = {u.unit_id for u in world.units if u.exploded}
        for uid in self.prev_enemies - set(enemies) - set(self.engaged):
            last = self.units.get(uid)
            if last is None or world.x is None or now - last[3] > 1.5:
                continue
            if math.hypot(last[1] - world.x, last[2] - world.y) > KILL_VIEW:
                continue
            if uid in exploded or (last[4] is not None and last[4] < 0.35):
                self._death(last[0], last[1], last[2], now)
        self.prev_enemies = set(enemies)
        for uid, (start, etype, active, _) in list(self.engaged.items()):
            if uid in enemies:
                continue
            del self.engaged[uid]
            last = self.units.get(uid)
            if last is None or world.x is None:
                continue
            distance = math.hypot(last[1] - world.x, last[2] - world.y)
            if distance > KILL_VIEW or now - last[3] > 2.0:
                continue  # flew out of view: not a kill
            ttk = active
            if ttk <= 0.3:
                continue
            ship = self.store.ship["enemies"].setdefault(etype, {})
            ship["kills"] = ship.get("kills", 0.0) + 1
            ship["kill_s"] = ship.get("kill_s", 0.0) + ttk
            _scale(ship, ("kills", "kill_s"), "kills", CAP_KILLS)
            self._death(etype, last[1], last[2], now)
            self._buff_kill(etype, ttk, now)
            self.store.mark("ship")
            self.store.mark("shared")
        self.kills = [k for k in self.kills if now - k[0] < 6]
        # XP comes with the kill, sometimes a moment before the bot sees the enemy gone: a rise is split over the
        # kills within 3 s of it, either way round.
        if world.xp is not None:
            if self.last_xp is not None and world.xp > self.last_xp:
                self.xp_rises.append((now, world.xp - self.last_xp))
            self.last_xp = world.xp
        for rise in list(self.xp_rises):
            t, amount = rise
            near = [k for k in self.kills if abs(k[0] - t) < 3]
            if near and now - t >= 0.5:
                for _, etype, _, _ in near:
                    drops = self.store.shared["drops"].setdefault(etype, {})
                    drops["xp"] = drops.get("xp", 0.0) + amount / len(near)
                self.xp_rises.remove(rise)
            elif now - t > 3.5:
                self.xp_rises.remove(rise)

    def _death(self, etype, x, y, now):
        drops = self.store.shared["drops"].setdefault(etype, {})
        drops["kills"] = drops.get("kills", 0.0) + 1
        if drops["kills"] > CAP_DROP_KILLS:
            factor = CAP_DROP_KILLS / drops["kills"]
            for key in ("kills", "cryo", "xp", "drops", "bp", "gold"):
                if key in drops:
                    drops[key] *= factor
            for name in drops.get("names", {}):
                drops["names"][name] *= factor
        self.kills.append((now, etype, x, y))
        self.store.mark("shared")

    def _damage(self, enemies, ctx, hull, now):
        """Each second's hull loss split over the attackers in proportion to their current estimates (equal at
        first); repeated over many seconds and mixes this separates strong from weak attackers."""
        attackers = {}
        for uid in ctx.get("attacking", ()):
            if uid in enemies:
                etype = etype_of(enemies[uid])
                attackers[etype] = attackers.get(etype, 0) + 1
        if self.sec is None or now - self.sec[0] < SECOND:
            if self.sec is None:
                self.sec = (now, hull, attackers)
            return
        start, hull0, counted = self.sec
        self.sec = (now, hull, attackers)
        span = now - start
        loss = hull0 - hull
        if loss < -0.05:  # a repair landed: record how much one heals
            repair = self.store.ship["repair"]
            repair["n"] = min(repair["n"] + 1, 30)
            repair["heal"] += (-loss - repair["heal"]) / repair["n"]
            return
        if loss < 0 or not counted or span > 3:
            return
        if any(e["kind"] in ("defense", "deploy") and e["start"] < now for e in self.effects):
            return  # a shield, turret or trap was up: this second says nothing about how hard the enemies hit
        ship = self.store.ship["enemies"]
        weights = {t: n * max(self._dps(t) or 0.0, 0.002) for t, n in counted.items()}
        total = sum(weights.values())
        for etype, n in counted.items():
            entry = ship.setdefault(etype, {})
            entry["dmg"] = entry.get("dmg", 0.0) + loss * weights[etype] / total
            entry["att_s"] = entry.get("att_s", 0.0) + n * span
            _scale(entry, ("dmg", "att_s"), "att_s", CAP_ATT_S)
        self.store.mark("ship")

    def _episodes(self, world, enemies, ctx, hull, now):
        attacking = [uid for uid in ctx.get("attacking", ()) if uid in enemies]
        ep = self.episode
        if attacking:
            if ep is None:
                ep = self.episode = {"start": now, "hull": hull, "min": hull, "types": {}, "max": 0, "last": now,
                                     "retreated": False, "caution": ctx.get("caution") or "same"}
                self.debug(f"learning: fight started at {hull * 100:.0f}% hull")
            ep["last"] = now
            ep["min"] = min(ep["min"], hull)
            ep["max"] = max(ep["max"], len(attacking))
            for uid in attacking:
                ep["types"].setdefault(uid, etype_of(enemies[uid]))
                # A new fighter: watch for helpers it spawns (Harvester waves).
                etype = ep["types"][uid]
                if etype not in self.watch:
                    u = enemies[uid]
                    self.watch[etype] = [now + HELPER_WATCH_S, u.x, u.y, set(self.units), set()]
            ep["retreated"] |= bool(ctx.get("retreating"))
        elif ep is not None:
            ep["min"] = min(ep["min"], hull)
            if now - ep["last"] > EPISODE_END_S:
                self._end_episode(died=False)

    def died(self):
        """The ship was shot down: the fight episode ends as a death (one sample, like any other)."""
        if self.episode is not None:
            self.episode["min"] = 0.0
            self._end_episode(died=True)

    def _end_episode(self, died):
        ep, self.episode = self.episode, None
        if not ep or not ep["types"]:
            return
        counts = {}
        for etype in ep["types"].values():
            counts[etype] = counts.get(etype, 0) + 1
        group = [t for t, n in counts.items() for _ in range(n)]
        predicted = self.predict(group, raw=True)
        actual = ep["hull"] - ep["min"] + (0.3 if died else 0.0)
        ship = self.store.ship
        for etype in counts:
            entry = ship["enemies"].setdefault(etype, {})
            entry["fights"] = entry.get("fights", 0.0) + 1
            if died:
                entry["deaths"] = entry.get("deaths", 0.0) + 1
            if ep["retreated"]:
                entry["retreats"] = entry.get("retreats", 0.0) + 1
            _scale(entry, ("fights", "deaths", "retreats"), "fights", CAP_FIGHTS)
        main = max(counts, key=lambda t: (counts[t], self._dps(t) or 0))
        key = f"{main}|{bucket(len(group))}|{ep['caution']}"
        sit = ship["situations"].setdefault(key, {"n": 0.0, "resid": 0.0, "deaths": 0.0, "loss": 0.0})
        sit["n"] += 1
        sit["loss"] += actual
        sit["deaths"] += 1 if died else 0
        if predicted is not None:
            sit["resid"] += actual - predicted[0]
            sit["pn"] = sit.get("pn", 0.0) + 1
        _scale(sit, ("n", "resid", "deaths", "loss", "pn"), "n", CAP_FIGHTS)
        self.store.mark("ship")
        self.debug(f"learning: fight ended ({'death' if died else 'retreat' if ep['retreated'] else 'won'}), "
                   f"{len(group)} attackers ({', '.join(sorted(counts))}), hull {ep['hull'] * 100:.0f}% -> "
                   f"{ep['min'] * 100:.0f}%, predicted loss "
                   f"{'-' if predicted is None else f'{predicted[0] * 100:.0f}%'}")

    def _helpers(self, enemies, now):
        for etype, w in list(self.watch.items()):
            until, x, y, before, new = w
            for uid, u in enemies.items():
                if uid not in before and math.hypot(u.x - x, u.y - y) < HELPER_R:
                    new.add((uid, etype_of(u)))
            if now > until:
                del self.watch[etype]
                entry = self.store.shared["behaviour"].setdefault(type_name(etype), {"fights": 0.0, "helpers": 0.0,
                                                                                     "types": {}})
                entry["fights"] += 1
                entry["helpers"] += len(new)
                for _, helper in new:
                    entry["types"][helper] = entry["types"].get(helper, 0) + 1
                _scale(entry, ("fights", "helpers"), "fights", CAP_FIGHTS)
                self.store.mark("shared")

    @staticmethod
    def _item_key(u):
        # Drops have no unit id in memory; they lie still, so their position names them.
        return u.unit_id if u.unit_id is not None else (round(u.x / 10), round(u.y / 10))

    def _drop_source(self, x, y, now):
        """{enemy type: share} for a drop that just appeared at (x, y): the enemy that died nearest to it in the last
        few seconds. Stacked enemies that died at nearly the same spot share it, so no drop is given to the wrong
        enemy for sure. Nothing near: not counted."""
        near = [(math.hypot(x - kx, y - ky), etype) for t, etype, kx, ky in self.kills
                if now - t < DROP_LINK_S and math.hypot(x - kx, y - ky) < DROP_LINK_R]
        if not near:
            return None
        best = min(d for d, _ in near)
        tied = [etype for d, etype in near if d <= best + DROP_TIE_R]
        return {etype: tied.count(etype) / len(tied) for etype in set(tied)}

    def _drops(self, world, now):
        for u in world.units:
            if not u.is_item or u.class_name.endswith("/tb") or math.isnan(u.x):
                continue
            key = self._item_key(u)
            if key in self.items:
                continue
            if key in self.away:
                # Back in view (the ship had moved off while fighting): it keeps the enemy it came from.
                self.items[key] = self.away.pop(key)[1]
                continue
            source = self._drop_source(u.x, u.y, now)
            rare = bool(u.rare or "_rare" in (u.mesh or ""))
            # Cryonite lies on the ground as "$crystal" (display name "Cryonite"), 2026-11-14.
            text = f"{u.desc or ''} {u.name or ''} {assets.display_name(u.desc or u.name) or ''}".lower()
            kind = "blueprint" if u.item_name else "cryonite" if ("cryonite" in text or "$crystal" in text) \
                else "other"
            self.items[key] = [u.x, u.y, now, source, kind, u.item_name]
            if source is None:
                continue
            name = (assets.display_name(u.item_name) or u.item_name) if u.item_name else None
            for etype, share in source.items():
                drops = self.store.shared["drops"].setdefault(etype, {})
                drops["drops"] = drops.get("drops", 0.0) + share
                if name:
                    drops["bp"] = drops.get("bp", 0.0) + share
                    if rare:
                        drops["gold"] = drops.get("gold", 0.0) + share
                    names = drops.setdefault("names", {})
                    names[name] = names.get(name, 0.0) + share
            if name:
                self.store.shared["blueprints"].setdefault(name, {})["template"] = u.item_template
                self.store.shared["blueprints"][name]["gold"] = rare
            self.store.mark("shared")
        present = {self._item_key(u) for u in world.units if u.is_item and not math.isnan(u.x)}
        for key, entry in list(self.items.items()):
            if key in present:
                continue
            x, y, _, source, kind, name = entry
            del self.items[key]
            if world.x is not None and math.hypot(x - world.x, y - world.y) < PICKUP_R:
                if source is not None:
                    self.pickups.append({"t": now, "source": source, "kind": kind, "name": name})
            else:
                self.away[key] = (now, entry)  # out of view, not picked up: remembered while drops last
        self.away = {k: v for k, v in self.away.items() if now - v[0] < DROP_KEEP_S}
        self._loot_gains(world, now)

    def _loot_gains(self, world, now):
        """Cryonite rises go to the cryonite drops picked up within LOOT_MATCH_S (either way round), split between
        them when the collector took several at once; energy jumps to blueprint pickups (completed blueprints)."""
        cryo0, energy0 = self.prev
        if cryo0 is not None and world.cryonite is not None and world.cryonite > cryo0:
            self.loot_rises.append((now, "cryo", world.cryonite - cryo0))
        if energy0 is not None and world.energy is not None and world.energy > energy0 + 50:
            self.loot_rises.append((now, "energy", world.energy - energy0))
        for rise in list(self.loot_rises):
            t, what, amount = rise
            kind = "cryonite" if what == "cryo" else "blueprint"
            takers = [p for p in self.pickups if p["kind"] == kind and abs(p["t"] - t) <= LOOT_MATCH_S]
            if takers and now - t >= 0.5:
                for p in takers:
                    self.pickups.remove(p)
                    if what == "cryo":
                        for etype, share in p["source"].items():
                            drops = self.store.shared["drops"].setdefault(etype, {})
                            drops["cryo"] = drops.get("cryo", 0.0) + amount / len(takers) * share
                    else:
                        bp = self.store.shared["blueprints"].setdefault(
                            assets.display_name(p["name"]) or p["name"], {})
                        bp["energy_n"] = bp.get("energy_n", 0) + 1
                        bp["energy"] = bp.get("energy", 0.0) + amount / len(takers)
                self.loot_rises.remove(rise)
                self.store.mark("shared")
            elif now - t > LOOT_MATCH_S + 0.5:
                self.loot_rises.remove(rise)
        self.pickups = [p for p in self.pickups if now - p["t"] <= LOOT_MATCH_S + 0.5]

    def _planet_name(self, world):
        if world.planet_map and world.current_planet:
            names = self.store.shared.setdefault("planet_names", {})
            name = assets.display_name(world.current_planet)
            if name and names.get(world.planet_map) != name:
                names[world.planet_map] = name
                self.store.mark("shared")

    def _places(self, world, enemies, now):
        if now - self.last_place < 5.0 or not world.planet_map:
            return
        self.last_place = now
        places = self.store.shared["places"].setdefault(world.planet_map, {})
        for uid, u in enemies.items():
            cell = f"{round(u.x / PLACE_CELL)},{round(u.y / PLACE_CELL)}"
            if self.place_seen.get(uid) == cell:
                continue
            self.place_seen[uid] = cell
            entry = places.setdefault(cell, {})
            etype = etype_of(u)
            entry[etype] = entry.get(etype, 0) + 1
        if len(self.place_seen) > 5000:
            self.place_seen.clear()
        self.store.mark("shared")

    def _routes(self, world, ctx):
        nav = ctx.get("nav")
        if nav is None or not ctx.get("moving") or world.x is None:
            self.last_cell = None
            return
        cell = nav.to_cell(world.x, world.y)
        if cell == self.last_cell:
            return
        self.last_cell = cell
        routes = self.store.shared["routes"].setdefault(world.planet_map, {})
        key = f"{cell[0]},{cell[1]}"
        if key in routes:  # passes only matter where the ship was stuck before
            routes[key]["passes"] = routes[key].get("passes", 0) + 1
            self.store.mark("shared")

    def stuck(self, planet_map, x, y, nav):
        """The bot was stuck (not next to a squad). One event is only one occasion; the cost needs repeats."""
        if nav is None:
            return
        cell = nav.to_cell(x, y)
        routes = self.store.shared["routes"].setdefault(planet_map, {})
        entry = routes.setdefault(f"{cell[0]},{cell[1]}", {"stuck": 0, "passes": 0, "occasions": []})
        entry["stuck"] += 1
        occasion = f"{self.run_id}/{int(time.time() // 900)}"  # same quarter hour of one run = one occasion
        if occasion not in entry["occasions"]:
            entry["occasions"] = (entry["occasions"] + [occasion])[-12:]
        self.store.mark("shared")

    def _drones(self, world, ctx, hull, now):
        if self.drone_sec is not None and now - self.drone_sec[0] < SECOND:
            return
        previous, self.drone_sec = self.drone_sec, (now, world.energy, world.cryonite, hull)
        if previous is None or not world.drones:
            return
        span = now - previous[0]
        if span > 3:
            return
        drones = self.store.ship["drones"]
        rank = ctx.get("rank_raw") or 0
        energy_gain = (world.energy or 0) - (previous[1] or 0)
        cryo_gain = (world.cryonite or 0) - (previous[2] or 0)
        loss = previous[3] - hull
        dps = sum(self._dps(etype_of(u)) or 0.0 for u in world.units if u.unit_id in ctx.get("attacking", ()))
        for d in world.drones:
            role = ctx.get("drone_roles", {}).get(d.index)
            if not role or not d.desc:
                continue
            entry = drones.setdefault(d.desc, {"role": role, "active_s": 0.0, "cost": 0.0,
                                               "with": [0.0, 0.0], "without": [0.0, 0.0]})
            last = self.drone_state.get(d.desc)
            self.drone_state[d.desc] = (now, d.charge, d.active)
            useful = d.active and d.charge > 0
            if useful:
                entry["active_s"] += span
                if last is not None and last[1] > d.charge:
                    full = max(1000, 250 * rank) * 0.55 * d.energy_factor
                    entry["cost"] += (last[1] - d.charge) * full
            side = entry["with"] if useful else entry["without"]
            sample = None
            if role == "energy" and ctx.get("at_field") and energy_gain >= 0:
                sample = energy_gain / span
            elif role == "cryonite" and cryo_gain > 0:
                sample = cryo_gain
            elif role == "combat" and dps > 0.002 and loss >= 0:
                sample = (loss / span) / dps
            if sample is not None:
                side[0] += 1
                side[1] += sample
                # Like with like: per farming level, and for a stat drone per number of attackers (it is switched on
                # in the harder fights, so a plain with/without average compares hard fights with easy ones).
                level = self.caution or "same"
                group = f"{level}|{bucket(len(ctx.get('attacking', ())))}" if role == "combat" else level
                cell = entry.setdefault("ctx", {}).setdefault(group, {"with": [0.0, 0.0], "without": [0.0, 0.0]})
                part = cell["with" if useful else "without"]
                part[0] += 1
                part[1] += sample
                for key in ("with", "without"):
                    if cell[key][0] > 300:
                        cell[key] = [cell[key][0] * 0.5, cell[key][1] * 0.5]
            for key in ("with", "without"):
                if entry[key][0] > 300:
                    entry[key] = [entry[key][0] * 0.5, entry[key][1] * 0.5]
        self.store.mark("ship")

    def skill_pressed(self, item, target):
        """An attack skill (or the blaster) was pressed at target: its effect is measured 2.5 s later."""
        if item is None or target is None or not target.max_hitpoints:
            return
        self.pending_skills.append((time.monotonic(), item.desc or item.name or "?", item.energy_cost or 0,
                                    target.unit_id, target.hitpoints / target.max_hitpoints))

    def _skills(self, world, enemies, now):
        for entry in list(self.pending_skills):
            t, desc, cost, uid, share0 = entry
            if now - t < 2.5:
                continue
            self.pending_skills.remove(entry)
            if uid in enemies and enemies[uid].max_hitpoints:
                drop = share0 - enemies[uid].hitpoints / enemies[uid].max_hitpoints
            elif uid in self.units:
                drop = share0  # it died
            else:
                continue
            skills = self.store.ship["skills"].setdefault(desc, {"n": 0.0, "drop": 0.0, "energy": cost})
            skills["n"] += 1
            skills["drop"] += max(0.0, drop)
            skills["energy"] = cost
            if skills["n"] > 200:
                skills["n"], skills["drop"] = skills["n"] / 2, skills["drop"] / 2
            level = skills.setdefault("by_level", {}).setdefault(self.caution or "same", [0.0, 0.0])
            level[0] += 1
            level[1] += max(0.0, drop)
            if level[0] > 200:
                level[0], level[1] = level[0] / 2, level[1] / 2
            self.store.mark("ship")

    # Estimates -------------------------------------------------------------------------------------------------

    def _dps(self, etype):
        entry = self.store.ship.get("enemies", {}).get(etype)
        if not entry or entry.get("att_s", 0.0) < 1.0:
            return None
        return entry["dmg"] / entry["att_s"]

    def _ttk(self, etype):
        entry = self.store.ship.get("enemies", {}).get(etype)
        if not entry or entry.get("kills", 0.0) < 0.5:
            return None
        return entry["kill_s"] / entry["kills"]

    def known(self, etype):
        entry = self.store.ship.get("enemies", {}).get(etype) or {}
        return entry.get("att_s", 0.0) >= MIN_ATT_S and entry.get("kills", 0.0) >= MIN_KILLS

    def helpers(self, etype):
        """Helpers a type is expected to bring, as a list of types (Harvester -> its Interceptors)."""
        entry = self.store.shared["behaviour"].get(type_name(etype))
        if not entry or entry["fights"] < 3:
            return []
        mean = entry["helpers"] / entry["fights"]
        if mean < 1.0 or not entry["types"]:
            return []
        helper = max(entry["types"], key=entry["types"].get)
        return [helper] * int(round(mean))

    def predict(self, group, raw=False):
        """(hull loss, confidence) for fighting this group of enemy types one after another (the most dangerous per
        time first), helpers included; None if any member is not known well enough. raw: without the situation
        correction and without helpers (used to learn the correction)."""
        if not group:
            return 0.0, math.inf
        # Helpers once per type: they are learned per fight with that type, not per member (six Custodian Commanders
        # were predicted with twelve Rocketeers).
        types = list(group) if raw else list(group) + [h for t in set(group) for h in self.helpers(t)]
        stats = []
        confidence = math.inf
        for etype in types:
            entry = self.store.ship.get("enemies", {}).get(etype) or {}
            if entry.get("att_s", 0.0) < MIN_ATT_S or entry.get("kills", 0.0) < MIN_KILLS:
                return None
            stats.append((self._dps(etype), self._ttk(etype)))
            confidence = min(confidence, entry["kills"])
        stats.sort(key=lambda s: -(s[0] / max(s[1], 0.5)))
        loss = 0.0
        alive = sum(d for d, _ in stats)
        for dps, ttk in stats:
            loss += alive * ttk
            alive -= dps
        return loss, confidence

    def situation(self, group, caution):
        if not group:
            return None
        counts = {}
        for t in group:
            counts[t] = counts.get(t, 0) + 1
        main = max(counts, key=lambda t: (counts[t], self._dps(t) or 0))
        return self.store.ship.get("situations", {}).get(f"{main}|{bucket(len(group))}|{caution}")

    def fight_ok(self, group, hull, safe_line, caution):
        """True: the fight is predicted safe; False: predicted to take the hull below safe_line (or this situation
        killed the ship repeatedly); None: not enough data (the settings decide)."""
        sit = self.situation(group, caution)
        if sit and sit["deaths"] >= 2 and sit["deaths"] / (sit["n"] + 1) >= 0.25:
            return False
        pred = self.predict(group)
        if pred is None:
            return None
        loss, confidence = pred
        if sit and sit.get("pn", 0) >= 2:
            n = sit["pn"]
            loss += (sit["resid"] / n) * (n / (n + 3))
        loss = max(0.0, loss) * (1 + 1 / math.sqrt(max(confidence, 1)))
        return hull - loss >= safe_line

    def _deaths_at(self, etype, caution, size):
        """(fights, deaths) learned for fights led by this type at this farming level with at most `size` enemies.
        Smaller groups count too (more enemies are never safer), larger ones do not (a crowd's deaths say nothing
        about the type alone)."""
        limit = BUCKET_ORDER.index(bucket(max(size, 1)))
        n = deaths = 0.0
        for key, sit in self.store.ship.get("situations", {}).items():
            main, group, level = key.rsplit("|", 2)
            if main == etype and level == caution and BUCKET_ORDER.index(group) <= limit:
                n += sit["n"]
                deaths += sit["deaths"]
        return n, deaths

    def verdict(self, etype, squad, caution):
        """For the danger list, learned per situation (user, 2026-11-14: the same enemy can be weak when the ship is
        higher, deadly at the same level and deadlier in groups): 'deadly' when this group, at this farming level, is
        predicted to nearly kill the ship or such fights killed it repeatedly; 'harmless' when fights like it were
        safe; None when not known (the settings decide)."""
        squad = list(squad) or [etype]
        n, deaths = self._deaths_at(etype, caution, len(squad))
        if deaths >= 2 and deaths / (n + 1) >= 0.25:
            return "deadly"
        ok = self.fight_ok(squad, 1.0, 0.3, caution)
        if ok is False:
            return "deadly"
        if ok and n >= MIN_FIGHTS_HARMLESS and deaths / n < 0.1:
            return "harmless"
        return None

    def danger_profile(self, etype):
        """{farming level: text} for the Learned tab: from which group size the type was deadly at that level, or
        harmless in the groups it was fought in."""
        result = {}
        levels = {key.rsplit("|", 1)[1] for key in self.store.ship.get("situations", {}) if key.startswith(etype + "|")}
        for level in levels:
            deadly_from = next((b for b, top in zip(BUCKET_ORDER, (1, 3, 6, 7))
                                if self.verdict(etype, [etype] * top, level) == "deadly"), None)
            if deadly_from:
                result[level] = ("deadly" if deadly_from == "1" else f"deadly from {deadly_from}", "error")
            elif self.verdict(etype, [etype], level) == "harmless":
                result[level] = ("harmless", "ok")
            else:
                result[level] = ("still learning", "muted")
        return result

    def fight_outlook(self, attackers, joiners=()):
        """Predicted hull loss for fighting on: attackers [(type, share of their hull left)] are killed one after
        another (most damage per remaining kill time first) while all still alive keep hitting; joiners (types about
        to join, e.g. enemies whose aggro range the ship is in) count as full-hull attackers, plus the helpers the
        types are known to bring. Returns (loss, confidence) or None if any of them is not known well enough."""
        types = [(t, max(0.05, min(1.0, share))) for t, share in attackers]
        types += [(t, 1.0) for t in joiners]
        types += [(h, 1.0) for t in {t for t, _ in types} for h in self.helpers(t)]
        stats, confidence = [], math.inf
        for etype, share in types:
            entry = self.store.ship.get("enemies", {}).get(etype) or {}
            if not self.known(etype):
                return None
            dps, ttk = self._dps(etype), self._ttk(etype)
            if dps is None or ttk is None:
                return None
            stats.append((dps, ttk * share))
            confidence = min(confidence, entry["kills"])
        stats.sort(key=lambda s: -(s[0] / max(s[1], 0.5)))
        loss, alive = 0.0, sum(d for d, _ in stats)
        for dps, left in stats:
            loss += alive * left
            alive -= dps
        return loss, confidence

    def remaining_s(self, attackers, joiners=()):
        """Seconds the rest of the fight takes (learned kill times, scaled by what each enemy has left); None if any
        is not known."""
        total = 0.0
        for etype, share in list(attackers) + [(t, 1.0) for t in joiners]:
            ttk = self._ttk(etype) if self.known(etype) else None
            if ttk is None:
                return None
            total += ttk * max(0.05, min(1.0, share))
        return total

    def retreat_advice(self, attackers, hull, line=None, joiners=(), caution=None, damage_rate=0.0, repair_heal=0.0):
        """True: retreat (fighting on is predicted to kill the ship, or nearly); False: fight on (predicted won with hull
        to spare, whatever the hull is now); None: not known (the settings decide). From the whole fight, not a hull
        line (user, 2026-11-14: a fixed line would flee from enemies still easily killed). attackers: [(type, share of
        hull left)] (plain types count as full hull); joiners: types about to join. Always retreats below 12%.
        First choice: how fights like this one went (same main type, group size and farming level): their average
        loss for what is left of the enemies, and their death rate. Vénar: 2-3 Assassins cost 17% on average over 35
        fights, while the per-enemy model said 120%. Else the per-enemy model with its learned error."""
        if hull < 0.12:
            return True
        attackers = [a if isinstance(a, tuple) else (a, 1.0) for a in attackers]
        if not attackers:
            return None
        shares = [share for _, share in attackers] + [1.0] * len(joiners)
        left = sum(max(0.05, min(1.0, x)) for x in shares) / len(shares)
        group = [t for t, _ in attackers] + list(joiners)
        sit = self.situation(group, caution) if caution else None
        # What is happening now counts as much as history: hull falling ~10%/s while the history said "won", the
        # bot fought on at 53% and died 4 s later (Vénar 02:21:47). The damage rate of the last seconds over the
        # time the rest of the fight takes.
        rest = self.remaining_s(attackers, joiners)
        # The incoming damage falls as the enemies die one after another: on average (n + 1) / 2n of it lasts the
        # whole rest. A ready repair adds its heal to the hull. Without both, a 1-2 s burst (85% -> 79%) at
        # 2 Assassins both below half, repair ready, was read as 79% lost and the bot fled (Vénar 21:12:08, user:
        # "they were almost dead"; history: 60 such fights, ~19% loss).
        count = len(attackers) + len(joiners)
        live = (damage_rate * rest * (count + 1) / (2 * count)
                if rest is not None and damage_rate > 0 and count else 0.0)
        # A ready repair: what it heals over its active time (from the item in memory, repair_heal, share of the hull;
        # the learned one-second "heal" was 5x too low: 15% for a repair that heals 80-100%, 2026-11-16).
        hull += max(0.0, repair_heal)
        # Rich history decides unless the damage now is clearly worse than these enemies usually do (more than
        # LIVE_OVERRIDE x their learned rate): the 02:21:47 case (~10%/s) still overrides it, a short burst does not.
        if live and sit and sit["n"] >= HISTORY_TRUSTED:
            rates = [self._dps(t) for t, _ in attackers]
            if all(r is not None for r in rates) and damage_rate <= LIVE_OVERRIDE * sum(rates):
                live = 0.0
        if live and hull - live < RETREAT_END_HULL:
            return True
        if sit and sit["n"] >= 3:
            if sit["deaths"] / (sit["n"] + 1) >= 0.25:
                return True
            expected = sit["loss"] / sit["n"] * left * (1 + 1 / math.sqrt(sit["n"]))
            return hull - max(expected, live) < RETREAT_END_HULL
        outlook = self.fight_outlook(attackers, joiners)
        if outlook is None:
            return None
        loss, confidence = outlook
        if sit and sit.get("pn", 0) >= 2:
            n = sit["pn"]
            loss = max(0.0, loss + (sit["resid"] / n) * (n / (n + 3)) * left)
        loss *= 1 + 1 / math.sqrt(max(confidence, 1))  # less sure: more room
        return hull - max(loss, live) < RETREAT_END_HULL

    def route_costs(self, planet_map):
        """{nav cell: extra cost} for spots stuck at on two or more occasions; the cost follows the stuck share."""
        costs = {}
        for key, entry in self.store.shared["routes"].get(planet_map, {}).items():
            if len(entry.get("occasions", [])) < 2:
                continue
            share = (entry["stuck"] + 0.5) / (entry["stuck"] + entry.get("passes", 0) + 2)
            cx, cy = (int(v) for v in key.split(","))
            costs[(cx, cy)] = ROUTE_CELL_COST * share
        return costs

    def places_of(self, planet_map, wanted, min_count=2):
        """[(x, y, sightings)] of the map cells where wanted enemy types ("Mantis Paralyzor|33") were seen, busiest
        first. Cells with fewer than min_count sightings are left out."""
        places = self.store.shared["places"].get(planet_map) or {}
        out = []
        for cell, types in places.items():
            count = sum(n for etype, n in types.items() if wanted(etype))
            if count >= min_count:
                cx, cy = (int(v) for v in cell.split(","))
                out.append((cx * PLACE_CELL, cy * PLACE_CELL, count))
        return sorted(out, key=lambda p: -p[2])

    def place_bonus(self, planet_map, x, y, wanted):
        """0..1: how often wanted enemies are seen around this point (busy areas)."""
        places = self.store.shared["places"].get(planet_map)
        if not places:
            return 0.0
        cell = places.get(f"{round(x / PLACE_CELL)},{round(y / PLACE_CELL)}", {})
        score = sum(n for etype, n in cell.items() if wanted(etype))
        top = max((sum(c.values()) for c in places.values()), default=0) or 1
        return min(1.0, score / top)

    def per_kill(self, etype):
        """(kills, cryonite per kill, XP per kill, blueprint drops per kill)."""
        d = self.store.shared["drops"].get(etype) or {}
        kills = d.get("kills", 0.0)
        if kills < 1:
            return kills, 0.0, 0.0, 0.0
        return kills, d.get("cryo", 0.0) / kills, d.get("xp", 0.0) / kills, d.get("bp", 0.0) / kills

    def value(self, etype):
        """0..1 relative worth of a kill (cryonite + XP) among the known types; None below 5 kills."""
        kills, cryo, xp, _ = self.per_kill(etype)
        if kills < 5:
            return None
        known = [self.per_kill(t) for t in self.store.shared["drops"]]
        known = [k for k in known if k[0] >= 5]
        top_cryo = max((k[1] for k in known), default=0) or 1
        top_xp = max((k[2] for k in known), default=0) or 1
        return (cryo / top_cryo + xp / top_xp) / 2

    def completed_rate(self, etype, completed):
        """Completed-blueprint drops per kill (completed(template) -> True/False/None), and the energy one gives."""
        d = self.store.shared["drops"].get(etype) or {}
        kills = d.get("kills", 0.0)
        if kills < 10:
            return None
        hits, energy = 0.0, []
        for name, count in d.get("names", {}).items():
            bp = self.store.shared["blueprints"].get(name, {})
            if completed(bp.get("template")):
                hits += count
                if bp.get("energy_n"):
                    energy.append(bp["energy"] / bp["energy_n"])
        return hits / kills, (sum(energy) / len(energy) if energy else None)

    def drone_compare(self, desc, caution=None):
        """(with, without, weight) at this farming level, like with like (combat: per number of attackers, weighted by
        the smaller side); None when not enough is known at this level."""
        entry = self.store.ship.get("drones", {}).get(desc)
        if not entry:
            return None
        level = caution or self.caution or "same"
        pairs = []
        for group, cell in (entry.get("ctx") or {}).items():
            if group.split("|")[0] != level:
                continue
            (n1, s1), (n0, s0) = cell["with"], cell["without"]
            if n1 >= 5 and n0 >= 5:
                pairs.append((min(n1, n0), s1 / n1, s0 / n0))
        weight = sum(w for w, _, _ in pairs)
        if weight < 30:
            return None
        return (sum(w * a for w, a, _ in pairs) / weight, sum(w * b for w, _, b in pairs) / weight, weight)

    def drone_ok(self, desc, caution=None):
        """False: no worthwhile effect at this farming level over enough like-for-like samples; True: it did; None: not
        known at this level (the settings decide). Per level: a drone may pay off against higher enemies and not
        against lower ones (user, 2026-11-14)."""
        entry = self.store.ship.get("drones", {}).get(desc)
        known = self.drone_compare(desc, caution)
        if not entry or known is None:
            return None
        with_, without, _ = known
        role = entry["role"]
        if role == "energy":
            gain = (with_ - without) * entry["active_s"]
            return gain >= entry["cost"] * 0.8
        if role == "cryonite":
            return with_ >= without * 1.03
        if role == "combat":
            return with_ <= without * 0.95
        return None

    # Squads per planet --------------------------------------------------------------------------------------------

    def squad_seen(self, planet, seen_types, moving_types):
        """Once a minute per type: was it in view, and did it fly in a moving group (a squad) that minute? A squad
        flies its patrol route together and regroups (user); the enemies of an area are mostly seen standing or
        fighting. Vénar: three Assassins moving together once must not make every Assassin a squad (2026-11-14)."""
        if not planet:
            return
        minute = int(time.time() // 60)
        table = self.store.shared.setdefault("squads", {}).setdefault(planet, {})
        for etype in set(seen_types) | set(moving_types):
            entry = table.setdefault(etype, {"seen": 0, "moving": 0, "seen_min": 0, "moving_min": 0})
            if etype in seen_types and entry["seen_min"] != minute:
                entry["seen_min"] = minute
                entry["seen"] += 1
            if etype in moving_types and entry["moving_min"] != minute:
                entry["moving_min"] = minute
                entry["moving"] += 1
            if entry["seen"] > 400:
                entry["seen"], entry["moving"] = entry["seen"] / 2, entry["moving"] / 2
        self.store.mark("shared")

    def is_squad_type(self, planet, etype):
        """Learned: this type flies as a squad on this planet (in a moving group in at least half the minutes it was
        seen, over at least 3 minutes)."""
        entry = (self.store.shared.get("squads", {}).get(planet) or {}).get(etype)
        return bool(entry and entry["moving"] >= 3 and entry["moving"] >= 0.5 * entry["seen"])

    def squad_ordinary(self, planet, etype):
        """Learned: this type is an ordinary enemy here (seen 10+ minutes, in a moving group under 20% of them)."""
        entry = (self.store.shared.get("squads", {}).get(planet) or {}).get(etype)
        return bool(entry and entry["seen"] >= 10 and entry["moving"] < 0.2 * entry["seen"])

    # Shields, protectors, traps, buffs, repair ------------------------------------------------------------------

    @staticmethod
    def _duration(item):
        return min(max(float(item.active_time or 0.0), 2.0), 60.0)

    def effect_started(self, kind, item, foes=None):
        """kind: 'defense' (shield, protector, Magnet Trap: measured as damage kept off the ship while it runs) or
        'buff' (measured as how much faster kills go while it runs). foes: enemy types it works against (default:
        the attackers of the last tick)."""
        now = time.monotonic()
        self.effects.append({"kind": kind, "desc": item.desc or item.name or "?", "start": now, "last": now,
                             "end": now + self._duration(item), "hull": self.hull, "expected": 0.0, "loss": 0.0,
                             "valid": True, "foes": list(foes) if foes is not None else None,
                             "energy": item.energy_cost or 0})

    def pressed(self, item):
        """Any skill press except blaster, collector and repair (used to find skills that deploy something)."""
        desc = item.desc or item.name or "?"
        entry = self.store.ship["skills"].setdefault(desc, {"n": 0.0, "drop": 0.0})
        entry["presses"] = entry.get("presses", 0.0) + 1
        self.presses.append((time.monotonic(), desc, item.energy_cost or 0))
        self.store.mark("ship")

    def _deployed(self, world, now):
        """A friendly unit (not a player) appearing near the ship within 2.5 s of a press came from that skill. Its
        effect is measured for as long as it exists."""
        self.presses = [p for p in self.presses if now - p[0] < 2.5]
        present = set()
        for u in world.units:
            if u.is_player or u.unit_id is None or not (u.marker and "friendly" in u.marker):
                continue
            present.add(u.unit_id)
            if u.unit_id in self.friendly:
                continue
            self.friendly.add(u.unit_id)
            if not self.presses or world.x is None or math.hypot(u.x - world.x, u.y - world.y) > 3500:
                continue
            _, desc, energy = self.presses.pop()
            entry = self.store.ship["skills"].setdefault(desc, {"n": 0.0, "drop": 0.0})
            entry["deploys"] = entry.get("deploys", 0.0) + 1
            entry["kind"] = "deploy"
            self.effects.append({"kind": "deploy", "desc": desc, "start": now, "last": now, "end": now + 90,
                                 "hull": self.hull, "expected": 0.0, "loss": 0.0, "valid": True, "foes": None,
                                 "energy": energy, "unit": u.unit_id})
            self.store.mark("ship")
        for e in self.effects:
            if e.get("unit") is not None and e["unit"] not in present and e["end"] > now:
                e["end"] = now  # the turret / drone is gone
        self.friendly &= present | {e.get("unit") for e in self.effects}

    def is_deploy(self, desc):
        entry = self.store.ship.get("skills", {}).get(desc) or {}
        return entry.get("deploys", 0) >= 2 and entry["deploys"] >= 0.5 * entry.get("presses", entry["deploys"])

    def _effects(self, hull, now):
        for e in list(self.effects):
            if e["kind"] in ("defense", "deploy"):
                dt = min(now, e["end"]) - e["last"]
                types = e["foes"] if e["foes"] is not None else self.attacking_types
                rates = [self._dps(t) for t in types]
                if dt > 0 and types:
                    if any(r is None for r in rates):
                        e["valid"] = False
                    else:
                        e["expected"] += sum(rates) * dt
                if e["hull"] is not None:
                    e["loss"] += max(0.0, e["hull"] - hull)
                    gain = hull - e["hull"]
                    if 0 < gain < 0.05:  # gradual healing (a repair lands as one big jump and is left out)
                        e["healed"] = e.get("healed", 0.0) + gain
                e["hull"] = hull
            e["last"] = now
            if now < e["end"]:
                continue
            self.effects.remove(e)
            entry = self.store.ship["skills"].setdefault(e["desc"], {"n": 0.0, "drop": 0.0})
            entry["kind"] = e["kind"]
            entry["energy"] = e["energy"]
            entry["uses"] = entry.get("uses", 0.0) + 1
            if e["kind"] == "deploy":
                entry["life"] = entry.get("life", 0.0) + (now - e["start"])
                entry["healed"] = entry.get("healed", 0.0) + e.get("healed", 0.0)
                entry["heal_uses"] = entry.get("heal_uses", 0.0) + 1
                _scale(entry, ("healed", "heal_uses"), "heal_uses", 80)
            if e["kind"] in ("defense", "deploy") and e["valid"]:
                entry["expected"] = entry.get("expected", 0.0) + e["expected"]
                entry["taken"] = entry.get("taken", 0.0) + e["loss"]
                entry["measured"] = entry.get("measured", 0.0) + 1
                if e["expected"] < 0.02:
                    entry["wasted"] = entry.get("wasted", 0.0) + 1
                _scale(entry, ("expected", "taken", "measured", "wasted"), "measured", 80)
            self.store.mark("ship")

    def _buff_kill(self, etype, ttk, now):
        """A kill while a buff runs: how fast it went against this type's usual kill time."""
        usual = self._ttk(etype)
        entry_e = self.store.ship.get("enemies", {}).get(etype) or {}
        if usual is None or entry_e.get("kills", 0) < MIN_KILLS:
            return
        for e in self.effects:
            if e["kind"] in ("buff", "deploy") and e["start"] <= now <= e["end"]:
                entry = self.store.ship["skills"].setdefault(e["desc"], {"n": 0.0, "drop": 0.0})
                entry["kind"] = e["kind"]
                entry["kills"] = entry.get("kills", 0.0) + 1
                entry["ratio"] = entry.get("ratio", 0.0) + min(ttk / usual, 3.0)
                _scale(entry, ("kills", "ratio"), "kills", 80)

    def defense_use(self, desc, foe_types, hull, duration):
        """Shield / protector / trap now? True: it would keep enough damage off the ship; False: not worth it now
        (too little coming in, or it has shown no effect); None: not known (the fixed rules decide). Below 25% hull
        it is always used."""
        if hull < 0.25:
            return True
        entry = self.store.ship.get("skills", {}).get(desc)
        if not entry or entry.get("measured", 0) < 5 or entry.get("expected", 0) < 0.1:
            return None
        share = max(0.0, min(1.0, 1 - entry["taken"] / entry["expected"]))  # damage it keeps off
        if entry["measured"] >= 10 and share < 0.05:
            return False
        rates = [self._dps(t) for t in foe_types]
        if not foe_types or any(r is None for r in rates):
            return None
        kept = share * sum(rates) * duration
        if kept >= 0.08:
            return True
        if kept < 0.03:
            return False
        return None

    def deploy_use(self, desc, foe_types, group_types, hull):
        """Turret / drone / trap skills (learned from what appears after the press). Each is judged by what it was
        seen to do, whichever applies: keeping damage off the ship (traps that slow or hold enemies, decoys),
        healing (healing turrets: used when that much hull is missing), or faster kills (attacking turrets and
        drones: used in fights long enough). True / False, or None when not known."""
        if not self.is_deploy(desc):
            return None
        entry = self.store.ship["skills"][desc]
        uses = entry.get("uses", 0.0)
        share = None
        if entry.get("measured", 0) >= 5 and entry.get("expected", 0) >= 0.1:
            share = max(0.0, min(1.0, 1 - entry["taken"] / entry["expected"]))
        gain = (1 - entry["ratio"] / entry["kills"]) if entry.get("kills", 0) >= 6 else None
        heal = entry["healed"] / entry["heal_uses"] if entry.get("heal_uses", 0) >= 3 else None
        if heal is not None and heal >= 0.05:
            if 1 - hull >= heal * 0.7:
                return True  # a healer, and that much hull is missing
            if (share is None or share < 0.05) and (gain is None or gain < 0.03):
                return False  # only heals, and the ship is nearly full
        if share is not None and gain is not None and share < 0.05 and gain < 0.03 and (heal or 0) < 0.05:
            return False
        times = [self._ttk(t) for t in group_types]
        if group_types and all(t is not None for t in times) and sum(times) < 6.0 and hull >= 0.4:
            return False  # over before it helps
        life = entry["life"] / uses if uses and entry.get("life") else 15.0
        rates = [self._dps(t) for t in foe_types]
        if share is not None and foe_types and all(r is not None for r in rates):
            if share * sum(rates) * life >= 0.06:
                return True
        if gain is not None and gain >= 0.05 and group_types and all(t is not None for t in times) \
                and sum(times) >= 8.0:
            return True
        return None

    def buff_use(self, desc, group_types, duration):
        """Buff now? False when it has shown no speed-up, or the fight is too short to use it; None if unknown."""
        entry = self.store.ship.get("skills", {}).get(desc)
        if not entry or entry.get("kills", 0) < 6:
            return None
        gain = 1 - entry["ratio"] / entry["kills"]
        if entry["kills"] >= 12 and gain < 0.03:
            return False
        times = [self._ttk(t) for t in group_types]
        if not group_types or any(t is None for t in times):
            return None
        return sum(times) >= 0.5 * duration

    def repair_hold(self, hull, attacker_types, repair_below, heal=None):
        """Hold the repair above the repair line when it would mostly be wasted (it heals more than is missing) and
        little damage is coming in. Never holds below the repair line. heal: what the repair heals over its active
        time (share of the hull, from the item); else the learned value."""
        repair = self.store.ship.get("repair") or {}
        if heal is None:
            if repair.get("n", 0) < 3:
                return False
            heal = repair["heal"]
        if hull <= repair_below:
            return False
        rates = [self._dps(t) for t in attacker_types]
        if any(r is None for r in rates):
            return False
        incoming = sum(rates) * 8.0
        return (1 - hull) < heal * 0.7 and hull - incoming > repair_below

    def skill_score(self, desc, caution=None):
        """Damage per energy at the current farming level; None until known there (what a skill does to lower-level
        enemies says little about higher ones)."""
        entry = self.store.ship.get("skills", {}).get(desc)
        if not entry:
            return None
        n, drop = (entry.get("by_level") or {}).get(caution or self.caution or "same", (0.0, 0.0))
        if n < 5:
            return None
        return (drop / n) / max(entry.get("energy") or 0, 50) * 1000
