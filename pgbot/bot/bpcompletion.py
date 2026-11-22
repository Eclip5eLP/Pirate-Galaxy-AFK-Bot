"""BP Completion: planning, hangar visits to read the blueprint progress.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""

import time

from ..game import assets
from ..data import myzen
from . import bpplan
from .constants import *  # noqa: F401,F403
from .util import _load_state, _save_state, _same_place, StopBot


class BPCompletionMixin:
    # BP Completion -------------------------------------------------------------------------------------------

    def _setup_bp(self, world):
        """BP Completion: the star system's blueprints still missing are hunted, planet by planet (user,
        2026-11-16). Item data comes from the blueprint book's catalogue (and ids computed from item keys), the
        parts found from the player's blueprint container, which is only in memory in a hangar."""
        cfg = self.config
        system = cfg.bp_system.strip() or self._system_of(world.current_planet)
        known = {myzen.normalize(n): n for n, _, _ in myzen.SYSTEM_LEVELS}
        if not system or myzen.normalize(system) not in known:
            self.active_mode = None
            raise StopBot(f"BP Completion: unknown star system {cfg.bp_system!r}")
        if myzen.load() is None:
            self.active_mode = None
            raise StopBot("BP Completion needs the site data: Settings, Update game data")
        if self.blueprints is None:
            self.active_mode = None
            raise StopBot("BP Completion needs the blueprint reader (game memory)")
        system = known[myzen.normalize(system)]
        self.bp = {"system": system, "planned": None, "data_at": None, "planet": None, "unknown_logged": False}
        self.blueprints.request_catalog()
        # Always the current list first, from the nearest hangar (user, 2026-11-17): on a planet the normal routine
        # finishes and the ship takes off; in orbit F7, or the next orbit of the system with a hangar.
        # Except right after a restart (user, 2026-11-19: restarts wasted lots of time with hangar trips): progress
        # saved by this system's run less than BP_REUSE_S ago is used instead.
        saved = _load_state().get("bp_progress") or {}
        age = time.time() - (saved.get("at") or 0)
        self.bp_hangar_target = None
        # Only with the item data saved too (older saves lack it: the hangar is read once more).
        # Saves from before 2026-11-20 can hold addresses as part counts (torn container reads): not reused.
        polluted = any(not isinstance(v, int) or not 0 <= v <= MAX_SAVED_PARTS
                       for v in (saved.get("found") or {}).values())
        if polluted:
            self._log("info", "Saved blueprint progress holds wrong part counts; reading it in the hangar again")
        # Only the same pilot's progress (Settings: Pilot to log in; saves without a row are from row 1).
        same_pilot = int(saved.get("pilot_row") or 1) == int(getattr(self.config, "pilot_row", 1) or 1)
        if saved.get("system") == system and age < BP_REUSE_S and saved.get("found") and saved.get("templates")                 and not polluted and same_pilot:
            for key, value in saved["found"].items():
                template_id = int(key) if key.lstrip("-").isdigit() else key
                self.blueprints.found[template_id] = max(self.blueprints.found.get(template_id) or 0, value)
            self.bp["trusted"] = True
            # The item data the hangar loaded (color patterns from CONFIGURATION > Color Pattern, ships): without it
            # Bevel Shapes, Maze, Puzzle and the GC-X Fusion Engineer were "not identified" after every restart that
            # skipped the hangar (2026-11-19).
            self.bp["saved_templates"] = {int(k) if k.lstrip("-").isdigit() else k: v
                                          for k, v in (saved.get("templates") or {}).items()}
            self.bp_hangar_wanted = False
            self._log("info", f"Mode: BP Completion: {system} (progress from {age / 60:.0f} min ago; no hangar trip)")
            return
        self.bp_hangar_wanted = True
        self._log("info", f"Mode: BP Completion: {system} (reading the blueprint progress in the nearest hangar "
                          f"first)")

    def _bp_tick(self, world):
        """Re-plan when new progress was read (a hangar visit), every BP_REPLAN_S, or when nothing is planned."""
        bp, book = self.bp, self.blueprints
        if bp is None or book is None or self.bp_hangar_wanted:
            return
        if not book.catalog_done.is_set():
            self.status = "Reading the blueprint data"
            return
        now = time.monotonic()
        # Also at once after a pickup message: the last part of the Fusion Power Repair Target (4/4) left the page at
        # 3/4 and the plan on it for up to BP_REPLAN_S (user, 2026-11-19).
        data = (book.container_seen, getattr(book, "seen_changes", 0))
        if bp["planned"] is not None and bp["data_at"] == data and now - bp["planned"] < BP_REPLAN_S:
            return
        bp["planned"], bp["data_at"] = now, data
        for template_id, entry in (bp.get("saved_templates") or {}).items():
            book.templates.setdefault(template_id, entry)  # the client replaces its table on a reload: every plan
        resolved = bpplan.resolve(bp["system"], book.templates)
        book.track([i for _, i, _ in resolved if i is not None])
        found = book.found_now()
        _apply_pickup_messages(found, resolved, getattr(book, "seen", {}))
        plan = bpplan.plan(bp["system"], book.templates, found, (getattr(book, "seen", {}) or {}).get("__totals__"))
        if bp.get("trusted") or book.container_seen is not None:
            # Kept for a restart (see _setup_bp); only counts read this run or restored, never an empty book.
            bp["trusted"] = True
            kept = dict(bp.get("saved_templates") or {})
            kept.update({i: book.templates[i] for _, i, _ in resolved if i is not None and i in book.templates})
            bp["saved_templates"] = kept
            _save_state({"bp_progress": {"system": bp["system"], "at": time.time(),
                                         "pilot_row": int(getattr(self.config, "pilot_row", 1) or 1),
                                         "found": {str(k): v for k, v in found.items() if v},
                                         "templates": {str(k): v for k, v in kept.items()}}})
        if not bp.get("skipped_logged"):
            # Every site blueprint of the system that is not planned, with the reason, in the debug log; the run log
            # gets a count, and a warning for the unexpected reason (user, 2026-11-21: blueprints were dropped
            # silently more than once).
            bp["skipped_logged"] = True
            skipped = []
            bpplan.system_blueprints(bp["system"], skipped)
            for label, reason, _ in skipped:
                self._debug(f"BP Completion {bp['system']}: left out {label}: {reason}")
            odd = [label for label, _, warn in skipped if warn]
            if odd:
                self._log("warning", f"BP Completion {bp['system']}: {len(odd)} blueprint(s) left out, their site "
                                     f"location matches no planet: {', '.join(odd[:6])}{' ...' if len(odd) > 6 else ''}")
            if skipped:
                self._log("info", f"BP Completion {bp['system']}: {len(skipped)} site blueprint(s) not planned "
                                  f"(events, shop, Ancient, rare patterns{', unknown places' if odd else ''}; "
                                  f"list in the debug log)")
        if plan["unknown"] and not bp["unknown_logged"]:
            bp["unknown_logged"] = True
            more = " ..." if len(plan["unknown"]) > 8 else ""
            self._log("info", f"BP Completion: {len(plan['unknown'])} blueprint(s) not identified, skipped: "
                              f"{', '.join(plan['unknown'][:8])}{more}")
        planets = bpplan.by_planet(plan["missing"])
        bp["summary"] = {"done": plan["done"], "unknown": len(plan["unknown"]),
                         "missing": [[e["label"], e["progress"][0], e["progress"][1]] for e in plan["missing"]]}
        if not planets:
            # Done (user, 2026-11-17): to orbit and stop.
            self.seek = None
            bp["planet"] = None
            self._log("info", f"BP Completion: every blueprint of {bp['system']} that can be farmed is complete "
                              f"({plan['done']} done, {len(plan['unknown'])} not identified); stopping in orbit")
            self.set_stop_in_orbit(True)
            return
        ship = world.ship_level or 0

        def fit(item):
            planet, entries = item
            _, levels = bpplan.seek_targets(entries, planet)
            return (levels is not None and levels[1] > ship + BP_LEVEL_MARGIN, -len(entries))
        # Finish the current planet first (user, 2026-11-17): choosing by "most missing" each re-plan would fly back
        # and forth as the counts cross (Baumar 12 -> 4 left, Molikar 5: off to Molikar, then back). A planet whose
        # enemies became too high for the ship still gives way.
        current = bp.get("planet") or (_load_state().get("bp_planet") or {}).get(bp["system"])  # kept across restarts
        if current in planets and not fit((current, planets[current]))[0]:
            planet, entries = current, planets[current]
        else:
            planet, entries = min(planets.items(), key=fit)
        names, levels = bpplan.seek_targets(entries, planet)
        exact_known = {myzen.normalize(n) for n in assets.enemy_names()}
        wants = bpplan.seek_wants(entries, planet)
        self.seek = {"names": names, "levels": levels, "exact": all(myzen.normalize(n) in exact_known for n in names),
                     "spawners": myzen.spawners(names, exact_known), "wants": wants,
                     "name_levels": bpplan.name_levels(wants)}
        here = assets.display_name(world.current_planet)
        if bp["planet"] != planet:
            started = sum(1 for e in plan["missing"] if e["progress"][0] > 0)
            level_text = f", enemies R{levels[0]}-{levels[1]}" if levels else ""
            self._log("info", f"BP Completion {bp['system']}: {len(plan['missing'])} missing ({started} started), "
                              f"{plan['done']} done; {planet}: {len(entries)} of them{level_text}")
        if bp["planet"] != planet:
            _save_state({"bp_planet": {**(_load_state().get("bp_planet") or {}), bp["system"]: planet}})
        bp["planet"] = planet
        if myzen.normalize(planet) == myzen.normalize(here):
            self.destination = here
            self.home_planet = world.current_planet
        else:
            self.destination = planet
            self.patrol_centre = None

    def _hangar_orbits(self):
        """{"yes": [planet ids], "no": [planet ids]}: orbits known to have a hangar or not (learned, state.json)."""
        known = _load_state().get("hangars") or {}
        return {"yes": [p.lower() for p in known.get("yes", [])], "no": [p.lower() for p in known.get("no", [])]}

    def _learn_hangar(self, planet, has):
        if not planet:
            return
        planet = planet.lower()
        known = self._hangar_orbits()
        for side in ("yes", "no"):
            if planet in known[side]:
                known[side].remove(planet)
        known["yes" if has else "no"].append(planet)
        _save_state({"hangars": known})

    def _enter_hangar(self, world):
        """In a planet's orbit: F7 into its hangar. Without one there, the next orbit of this system that has one
        (known ones first, then untried ones); none left: go on with the progress known so far."""
        here = self._location(world)  # at a starbase the current planet still names the planet left before
        target = self.bp_hangar_target
        if target and not _same_place(here, target):
            return  # on the way there (travel below)
        self.status = "Entering the hangar"
        self._key(HANGAR_KEY)
        if self._wait(lambda w: w.zone in ("hangar", "loading"), HANGAR_KEY_WAIT_S, "hangar"):
            self._learn_hangar(here, True)
            self.bp_hangar_target = None  # reached
            self.zone_since = time.monotonic()
            return
        self._learn_hangar(here, False)
        self._choose_hangar(here, self._system_of(here), target)

    def _choose_hangar(self, here, system, target):
        """Next hangar to try: the system's starbase, orbits known to have one, untried orbits; then the fallbacks
        (Gemini Starbase, Kalabesh). Sets bp_hangar_target and the destination, or gives up."""
        known = self._hangar_orbits()
        at_fallback = target in HANGAR_FALLBACKS
        here_name = myzen.normalize(assets.display_name(here) or "")
        site = myzen.hangars(system) if system else []
        if site and not at_fallback:
            # The site lists the places with a hangar (user, 2026-11-17): only those, ones already visited first.
            places = [n for n in site if myzen.normalize(n) != here_name and n not in self.bp_hangars_tried]
            visited = {myzen.normalize(assets.display_name(z)) for z in known["yes"]}
            places.sort(key=lambda n: myzen.normalize(n) not in visited)
            if places:
                self.bp_hangars_tried.add(places[0])
                self._hangar_trip(places[0])
                self._log("info", f"No hangar at {assets.display_name(here)}; going to the {places[0]} hangar")
                return
        planets = [] if at_fallback or site else [p for p in self._system_planets(system)
                                                  if p not in known["no"] and p != (here or "").lower()]
        # The system's starbase first (starbases have a hangar: Mizar Starbase, ...; the site lists only planets),
        # then orbits known to have one, then untried ones.
        starbase = myzen.normalize(system or "") + "starbase"
        if not at_fallback and starbase in self._zone_ids() and starbase not in known["no"] and \
                starbase != (here or "").lower():
            planets.insert(0, starbase)
        planets.sort(key=lambda p: (p != starbase, p not in known["yes"]))
        if not planets:
            # Fallbacks every player has (user, 2026-11-17): the Gemini Starbase first (the Gemini gate is in its
            # orbit, so no further travel), then Kalabesh in Vega (the start planet).
            tried = self.bp_fallbacks_tried
            fallback = next((f for f in HANGAR_FALLBACKS if f not in tried
                             and not _same_place(here, f)), None)
            if fallback is None:
                self.bp_hangar_wanted = False
                self.bp_hangar_target = None
                self._log("warning", f"No hangar found (also not at {' / '.join(HANGAR_FALLBACKS)}); BP Completion "
                                     f"goes on with the progress known so far")
                return
            tried.add(fallback)
            self._hangar_trip(fallback)
            self._log("info", f"No hangar found in {system}; going to the {fallback} hangar")
            return
        self._hangar_trip(assets.display_name(planets[0]))
        self._log("info", f"No hangar at {assets.display_name(here)}; trying {self.bp_hangar_target}")

    def _hangar_trip(self, place):
        """Fly to `place` for its hangar (BP Completion progress read)."""
        self.bp_hangar_target = place
        self.destination = place
        self.patrol_centre = None  # a new place: no patrol centre yet

    def _system_planets(self, system):
        """Internal ids of the planets of a star system with site data ("TauCeti1", ...)."""
        out = []
        for key, entry in ((myzen.load() or {}).get("planets") or {}).items():
            if entry.get("system") == system and entry.get("label"):
                internal = next((z for z in self._zone_ids() if _same_place(z, entry["label"])), None)
                if internal and internal not in out:
                    out.append(internal)
        return out

    def _zone_ids(self):
        """Internal zone ids known from the language file ("taucet1" style keys with a name)."""
        assets.display_name("")
        ids = []
        for key in assets._LANG:
            if key.endswith(".name") and key.count(".") == 1:
                ids.append(key[:-len(".name")])
        return ids

    def _bp_hangar_read(self, world):
        """In a hangar during BP Completion: open the panel (the client then fetches the blueprint container from the
        server) and wait for the item catalogue (at most BP_CATALOG_WAIT_S). True once the hangar can be left; the
        progress itself is waited for in orbit (_bp_fresh_read, see BP_FRESH_WAIT_S). Done once per hangar visit."""
        book = self.blueprints
        now = time.monotonic()
        if book is None or self.bp_fresh_wait is not None:
            return True  # read already on this visit (it used to start over and request the catalogue again)
        if self.bp_hangar_since is None:
            self.bp_hangar_since = now
            self.bp_copies_before = set(book.copies)
            self._load_color_patterns()
            book.request_catalog()
            book.rescan()
        done = book.catalog_done.is_set()
        if not done and now - self.bp_hangar_since <= BP_CATALOG_WAIT_S:
            self.status = "Reading the blueprint data"
            return False
        if not done:
            self._log("warning", f"The item data was not read in {BP_CATALOG_WAIT_S} s in the hangar; going on")
        self.bp_hangar_since = None
        self.bp_hangar_wanted = False
        self.bp_hangar_target = None
        self.bp_fresh_wait = now
        self._learn_hangar(world.current_planet, True)
        if self.bp is not None:
            self.bp["planned"] = None  # plan now with what is known; again in orbit with the server's copy
        return True

    def _bp_fresh_read(self):
        """In orbit after a hangar visit: wait for a container copy that was not in memory when the hangar panel
        opened (the server's fresh copy), at most BP_FRESH_WAIT_S. True once the plan can be made."""
        book = self.blueprints
        now = time.monotonic()
        if book is None:
            self.bp_fresh_wait = None
            return True
        fresh = set(book.copies) - self.bp_copies_before
        waited = now - self.bp_fresh_wait
        if fresh and book.catalog_done.is_set():
            self._log("info", f"Blueprint progress read from the server's copy ({waited:.0f} s after the hangar)")
        elif waited > BP_FRESH_WAIT_S:
            self._log("info", f"No fresh blueprint progress {BP_FRESH_WAIT_S} s after the hangar; using the last "
                                 f"known counts and pickup messages")
        else:
            book.keep_scanning(4)
            self.status = "Waiting in orbit for the blueprint progress"
            return False
        self.bp_fresh_wait = None
        if self.bp is not None:
            self.bp["planned"] = None  # plan again with the fresh progress
        return True


def _apply_pickup_messages(found, resolved, seen):
    """Raise the parts counts by the game's pickup messages, matched to the plan's blueprints by name. BlueprintBook
    applies them only to templates the client has loaded; after a restart most counts come from the saved progress,
    so "Converted known blueprint 'Enhanced Fusion Speed Actuator'" (complete) left it at 3/4 and the bot farmed it on
    (user, 2026-11-19). Names are compared without spaces and punctuation ("Thermo-blast" in the message,
    "Thermoblast" on the site). A completed blueprint whose parts count is not known counts as done (999)."""
    by_name = {myzen.normalize(name): value for name, value in seen.items()}
    for entry, template_id, needed in resolved:
        if template_id is None:
            continue
        # Every name the game may use: the site adds "Spaceship" to ships ("STAM X-Shadow Sniper Spaceship"), the
        # game's message does not (2026-11-20).
        value = next((by_name[v] for v in bpplan._name_variants(entry) if v in by_name), None)
        if value == "known":
            found[template_id] = max(found.get(template_id, 0), needed or 999)
        elif isinstance(value, int):
            found[template_id] = max(found.get(template_id, 0), value)

