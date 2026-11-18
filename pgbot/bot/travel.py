"""Death and travel: rescue, hangar, star gates, orbit, starmap.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""

import time

import cv2

from ..game import assets
from ..data import myzen
from .constants import *  # noqa: F401,F403
from .util import _save_state, _same_place, StopBot


class TravelMixin:
    # Death and travel ---------------------------------------------------------------------------------------

    def _rescue(self, world):
        """The rescue toast only reacts about 10 s after the ship was shot down (user, 2026-11-11)."""
        self.orbs.save()
        if self.dead_since is None:
            self.dead_since = time.monotonic()
        wait = RESCUE_UNLOCK_S - (time.monotonic() - self.dead_since)
        if wait > 0:
            self.status = "Dead - waiting for the rescue popup"
            self._stop.wait(wait)
        for attempt in range(3):
            time.sleep(1.5)
            self._click(RESCUE_TOAST)
            time.sleep(1.5)
            self._click(RESCUE_STARBASE_ROW)
            if self._wait(lambda w: not w.dead or w.zone != "planet", 25, "rescue"):
                self.target_id = None
                self.dead_since = None
                return
            self._log("warning", f"Rescue attempt {attempt + 1} had no effect")
        raise StopBot("could not use the rescue popup")

    def _leave_hangar(self, world):
        # "Full" allows for rounding: the computed maximum (base x armor factor, 525,617) is a little above what a
        # fully repaired ship reads (525,606), so an exact comparison never matched.
        def full(w):
            return w.hitpoints is not None and w.max_hitpoints and w.hitpoints >= w.max_hitpoints * HULL_FULL_SHARE
        if not full(world):
            self.status = "Repairing in the hangar"
            repaired = False
            # Twice at most: at 10:08 the same clicks did nothing (panel probably not open yet), at 10:26 they worked
            # (screenshots: Maintenance tab, "Use 147 energy to repair your ship?", OK where the bot clicks).
            for attempt in range(2):
                # SPACE toggles the panel: after a stop with the panel still open, it closed it again (13:57:08).
                if not self._ui_shows("maintenance_tabs") and not self._hangar_panel_open():
                    self._key("SPACE")
                    time.sleep(2.0 if attempt else 1.5)
                self._click_tab("maintenance", HANGAR_MAINTENANCE_TAB)
                time.sleep(1.2)
                if not self._ui_shows("maintenance_tabs"):
                    self._click_tab("maintenance", HANGAR_MAINTENANCE_TAB)  # the first click sometimes does not register
                    time.sleep(1.2)
                self._snapshot(f"hangar-maintenance-{attempt + 1}")
                # Never click into a panel that is not the Maintenance tab: a click there opens the Item Shop.
                if not self._ui_shows("maintenance_tabs"):
                    raise StopBot("hangar: the Maintenance tab with the Repair button did not show; stopped without "
                                  "clicking further (screenshot in logs/)")
                self._click(HANGAR_REPAIR_BUTTON)
                time.sleep(1.5)
                self._snapshot(f"hangar-repair-dialog-{attempt + 1}")
                if not self._ui_shows("repair_buttons"):
                    if attempt == 0:
                        # Once more from a closed panel (13:49:27: Repair visible, the click opened no dialog).
                        self._debug("hangar: no repair dialog after the Repair click; closing the panel and trying again")
                        self._key("SPACE")
                        time.sleep(1.0)
                        continue
                    raise StopBot("hangar: the repair dialog did not show; stopped without clicking OK (screenshot in "
                                  "logs/)")
                self._click(REPAIR_CONFIRM_OK)
                repaired = bool(self._wait(full, 8, "repair"))
                if not repaired:
                    self._snapshot(f"hangar-after-ok-{attempt + 1}")
                self._key("SPACE")
                time.sleep(0.8)
                if repaired:
                    break
            if not repaired:
                # Never leave with a damaged ship (user: on the main account the bot left at 5% hull three times).
                now = self._read()
                hull = (now.hitpoints / now.max_hitpoints * 100) if now and now.hitpoints and now.max_hitpoints else 0
                raise StopBot(f"repair in the hangar did not work ({hull:.0f}% hull); screenshots in logs/")
        self.status = "Leaving the hangar"
        self._zone_key("F5", "orbit")

    @staticmethod
    def _system_of(zone):
        """Star system of a planet, gate or starbase zone name ("Draconis2", "DraconisGate", "GeminiStarbase")."""
        if not zone:
            return None
        system = myzen.planet_system(assets.display_name(zone))
        if system:
            return system
        name = myzen.normalize(zone)
        for system, _, _ in sorted(myzen.SYSTEM_LEVELS, key=lambda t: -len(t[0])):
            if name.startswith(myzen.normalize(system)):
                return system
        return None

    def _target_system(self):
        # Places without site data (the Gemini Starbase, a fallback hangar) by their name ("Gemini Starbase").
        name = self._home_name()
        system = myzen.planet_system(name)
        if system is None and self.destination:
            system = self._system_of(name)  # only for a chosen destination (a start at a gate keeps its old ways)
        return system

    @staticmethod
    def _location(world):
        """Where the ship is: the orbit's own name in an orbit (the current planet lags after a gate jump), else the
        current planet."""
        if world.zone == "orbit" and world.orbit_place:
            return world.orbit_place
        return world.current_planet

    def _other_system(self, world):
        """The target planet is in another star system than the ship (gate travel needed)."""
        here, there = self._system_of(self._location(world)), self._target_system()
        return bool(here and there and here != there)

    def _is_gate_name(self, name, system, starbase_ok=None):
        """A zone that is this system's gate: "<System>Gate", or its starbase where the starbase is the gate
        (Gemini; learned when a starmap has no Gate entry). starbase_ok overrides that knowledge."""
        n = myzen.normalize(name or "")
        if not n.startswith(myzen.normalize(system)):
            return False
        if "gate" in n:
            return True
        if starbase_ok is None:
            starbase_ok = system in GATE_AT_STARBASE or system in self.gate_starbases
        return starbase_ok and "starbase" in n

    def _gate_jump(self, world):
        """At a gate orbit: Space opens the STARGATE tab (checked by image), the target system's row is found by its
        name image and clicked, then Jump; arrival is checked in memory (the current planet's system)."""
        target = self._target_system()
        self.status = f"Jumping to {target}"
        reference = cv2.imread(str(UI_DIR / f"gate_row_{target.lower().replace(' ', '_')}.png"))
        if reference is None:
            raise StopBot(f"no reference image for {target} in the star gate list (pgbot/input/ui)")
        self._key("SPACE")
        time.sleep(1.2)
        if not self._ui_shows("gate_tab"):
            raise self._fail("gate-no-panel", "Space at the gate did not open the STARGATE list")
        image = self._screen()
        if image is None:
            raise StopBot("no screenshot of the star gate list")
        h, w = image.shape[:2]
        x0, y0, x1, y1 = GATE_LIST_AREA(w, h)
        result = cv2.matchTemplate(image[y0:y1, x0:x1], reference, cv2.TM_CCOEFF_NORMED)
        _, score, _, (lx, ly) = cv2.minMaxLoc(result)
        self._debug(f"gate list: {target} row match {score:.2f}")
        if score < UI_MATCH_MIN:
            raise self._fail("gate-no-row", f"{target} is not in the star gate list (locked?)")
        self.input.click(x0 + lx + reference.shape[1] // 2, y0 + ly + reference.shape[0] // 2, "left")
        time.sleep(0.8)
        if not self._ui_shows("gate_jump"):
            raise self._fail("gate-no-jump", "no Jump button in the star gate list")
        self._click(GATE_JUMP)
        def there(w):
            if w.zone != "orbit":
                return False
            if w.skybox:  # the skybox names the system (orbit names differ: "$antaresgate.orbit.msg", "$draconisgate")
                return myzen.normalize(w.skybox) == myzen.normalize(target)
            return self._system_of(self._location(w)) == target
        arrived = self._wait(there, GATE_JUMP_TIMEOUT_S, "gate jump")
        if arrived is None:
            raise self._fail("gate-no-arrival", f"the jump to {target} did not arrive")
        self._log("info", f"Jumped to {target} ({self._location(arrived)})")
        self.zone_since = time.monotonic()

    def _from_orbit(self, world):
        if self.config.mode == "fly" and self.destination is None:
            self._setup_fly()  # started off a planet with Fly to Planet set
        if self._other_system(world):
            # The destination is in another star system: through this system's gate (user, 2026-11-12).
            here = self._system_of(self._location(world))
            if self._is_gate_name(self._location(world), here):
                self._gate_jump(world)
            else:
                self.status = f"Opening the starmap (to the {here} gate)"
                self._zone_key("F8", "starmap")
            return
        # At a starbase the player's current planet still names the planet left before, and after a gate jump the
        # old gate: land only where the orbit is a planet's (F6 at the Antares gate did nothing three times).
        here = self._location(world)
        at_planet = not (world.at_starbase or "gate" in (here or "").lower() or "starbase" in (here or "").lower())
        if self.config.mode == "fly" and self._is_home(world.current_planet, orbit=True) and at_planet:
            self.status = f"Arrived in orbit of {self._home_name()}"
            self._log("info", f"Arrived in orbit of {self._home_name()}; stopping (Fly to Planet)")
            self.stop()
            return
        target_here = self.bp_hangar_target and _same_place(self._location(world), self.bp_hangar_target)
        if self.bp_hangar_wanted and (at_planet or target_here):
            target = self.bp_hangar_target
            if not target or target_here or _same_place(world.current_planet, target):
                # Always end the step here: after F7 the ship is in the hangar (the old world still says orbit; it
                # went on to "fly to Ob" and pressed F8 in the hangar, paused at Ob 01:17), or a next hangar was
                # chosen; the next tick decides from the new state.
                self._enter_hangar(world)
                return
            if self.bp_hangar_target:
                self.status = f"Flying to {self.bp_hangar_target} for its hangar"
                self._zone_key("F8", "starmap")
                return
        if self._is_home(world.current_planet, orbit=True) and at_planet:
            why = self._landing_refused(world)
            if why:
                # Never land somewhere by mistake (user, 2026-11-17: "if it fails it should stay safe in orbit instead
                # of randomly landing and possibly dying"): stop here in orbit instead.
                self._log("warning", f"Not landing on {self._home_name()}: {why}; stopping in orbit")
                self.status = "Stopped in orbit"
                self.stop()
                return
            self.status = f"Landing on {self._home_name()}"
            self._land()
        else:
            self.status = "Opening the starmap"
            self._zone_key("F8", "starmap")

    def _landing_refused(self, world):
        """Why landing here would be a mistake, or None. BP Completion lands only on the planned planet (a hangar
        trip's destination stayed set once and the bot tried to land on Ob, L34-43, 2026-11-17 19:07)."""
        if self.energy_trip is not None and ENERGY_PLANET.lower() in self._home_name().lower():
            return None  # the energy refill trip lands on its own planet
        if self.config.mode == "bp_completion":
            planet = (self.bp or {}).get("planet")
            if not planet:
                return "no blueprint plan yet"
            if myzen.normalize(self._home_name()) != myzen.normalize(planet):
                return f"the plan is {planet}"
        return None

    def _zone_key(self, key, zone):
        for attempt in range(3):
            self._key(key)
            if self._wait(lambda w: w.zone in (zone, "loading"), 10, zone):
                self._wait(lambda w: w.zone == zone, self.config.zone_timeout_s, zone)
                self.zone_since = time.monotonic()
                return
        raise self._fail(f"{key}-no-{zone}", f"{key} did not lead to {zone}")

    def _starmap(self, world):
        if self.config.mode == "fly" and self.destination is None:
            self._setup_fly()
        gate_system = self._system_of(world.current_planet) if self._other_system(world) else None
        if gate_system:
            # Another system: fly to this system's gate ("<System>Gate"; a "<System>Starbase" entry only if no
            # Gate entry is listed, as in Gemini).
            self.status = f"Choosing the {gate_system} gate"
            if self._starmap_pick(lambda name: self._is_gate_name(name, gate_system, starbase_ok=False)):
                return
            if self.starmap_seen >= 8:
                # Only 8 entries could be read (the list did not scroll): the gate may be further down. Taking the
                # starbase for the gate sent the ship to Sayaron (Sol starbase) instead of the Sol gate (19:50).
                raise StopBot(f"no {gate_system} gate among the first 8 destinations (the list did not scroll); "
                              f"screenshot in logs/")
            if self._starmap_pick(lambda name: self._is_gate_name(name, gate_system, starbase_ok=True)):
                self.gate_starbases.add(gate_system)  # no Gate entry: the starbase is the gate here
                _save_state({"gate_starbases": sorted(self.gate_starbases)})
                return
            raise StopBot(f"no star gate of {gate_system} found in the destination list")
        self.status = "Choosing the destination"
        target = self.bp_hangar_target
        if self.bp_hangar_wanted and target:
            if self._starmap_pick(lambda name: self._is_home(name, orbit=True)):
                return  # travelling (picking again here opened the map mid-travel: "Ob not found", 2026-11-17 01:40)
            # A hangar candidate this pilot cannot travel to (the game knows a "Tau Ceti Starbase", the Tau Ceti
            # starmap has none): learned as no hangar, the next candidate is chosen.
            self._log("info", f"{target} is not in the destination list; trying the next hangar")
            zone = next((z for z in self._zone_ids()
                         if _same_place(z, target)), None)
            self._learn_hangar(zone or target, False)
            self.bp_hangar_target = None
            self._choose_hangar(self._location(world) or world.current_planet,
                                self._system_of(self._location(world) or world.current_planet), None)
            return
        if not self._starmap_pick(lambda name: self._is_home(name, orbit=True)):
            raise StopBot(f"{self._home_name()} not found in the destination list")

    def _starmap_pick(self, wanted):
        """Open the starmap list and travel to the first entry whose name `wanted` accepts. False if none. The list
        shows 8 rows; longer lists (Sol: planets, moons, starbase, gate, sectors) are scrolled by dragging the
        scrollbar until a page brings no new entry (user, 2026-11-16: the bot only ever checked the first 8)."""
        for opening in range(2):
            self._key("SPACE")
            time.sleep(1.5)
            seen = set()
            for page in range(STARMAP_MAX_PAGES):
                new = 0
                for row in range(8):
                    if self._stop.is_set():
                        raise StopBot("stopped")
                    self._click(STARMAP_ROW, row)
                    time.sleep(0.8)
                    current = self._read()
                    selected = current.selected_target if current else None
                    if selected is None:
                        break  # list not open: press Space again
                    if selected.name not in seen:
                        seen.add(selected.name)
                        new += 1
                    if wanted(selected.name):
                        self.travel_to = selected.name
                        self._click(STARMAP_TRAVEL)
                        if self._wait(lambda w: w.travelling or w.zone != "starmap", 10, "travel"):
                            return True
                        raise StopBot("the Travel button had no effect")
                if not new or not seen:
                    break  # end of the list (or it is not open)
                # Next page: drag the list's scrollbar thumb down (user; the wheel does not scroll it). The bar
                # runs at the list's right edge from y ~80 to ~410 (screenshot 19:54, Sol: 9+ destinations).
                x, y = self._point(STARMAP_SCROLLBAR)
                y0 = y + page * STARMAP_SCROLL_STEP
                self.input.drag(x, y0, x, y0 + STARMAP_SCROLL_STEP, "left")
                time.sleep(0.6)
            if seen:
                self._debug(f"starmap entries: {', '.join(sorted(seen))}")
                self.starmap_seen = len(seen)
                return False
        return False
