"""Drones: when they are needed, toggling and recharging them in the UI.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

import cv2

from ..game import assets
from ..input import window
from ..input.capture import capture
from .constants import *  # noqa: F401,F403


class DronesMixin:
    @staticmethod
    def _drone_role(drone):
        """energy (energy collection / cap), cryonite, blueprint, combat (stats), or None."""
        boosts = drone.boosts
        if "energyboost" in boosts or "energycapboost" in boosts:
            return "energy"
        if "cryoniteboost" in boosts:
            return "cryonite"
        if "blueprintboost" in boosts:
            return "blueprint"
        if any(b in boosts for b in COMBAT_BOOSTS):
            return "combat"
        return None

    def _drone_needed(self, world, role, hull):
        """Is this kind of drone's effect needed right now?"""
        attackers = sum(1 for u in world.units if self._attacking(world, u))
        if role == "energy":
            # While collecting energy at a field, or picking up a completed blueprint / energy (user). Energy above
            # the cap stays when the drone goes off (user).
            return self.at_field or time.monotonic() < self.energy_pickup_until
        if role == "cryonite":
            return time.monotonic() < self.cryonite_pickup_until
        if role == "blueprint":
            # Also while fighting a target that does not shoot back: drops are rolled on the kill, and the sought
            # Menders (Mizar Magnet Sticky Bomb) and Harvesters never attack (2026-11-20).
            return self.seek is not None and (attackers > 0 or self.target_id is not None)
        if role == "combat":
            return attackers > 0 and (attackers >= DEFENSE_ATTACKERS[self.caution] or hull < DEFENSE_HULL[self.caution])
        return False

    def _manage_drones(self, world, hull):
        """Each drone on only while its effect is needed (plus DRONE_HOLD_S), checked by the drone's state in
        memory; recharge only when safe. Every click is preceded by a screenshot check of the HUD drone panel (open,
        and where its icons are) and of stray Drone Setup windows; when something does not look as expected, drones
        pause for a while (DRONE_BACKOFF_S, growing) instead of clicking blindly."""
        now = time.monotonic()
        if now < self.drones_paused_until:
            return
        for drone in world.drones:  # a toggle that shows in memory ends the failure count
            if self.drone_toggle_want.get(drone.index) == drone.active:
                self.drone_toggle_want.pop(drone.index)
                self.drone_toggle_fails = 0
                self.drone_backoff = DRONE_BACKOFF_S
        for drone in world.drones:
            role = self._drone_role(drone)
            if role is None:
                continue
            if drone.desc and self._learn(lambda l: l.drone_ok(drone.desc), area="drones", entry=drone.desc) is False:
                self._learn_note(f"drone-{drone.desc}", f"learning: {assets.display_name(drone.desc)} has not been "
                                                        f"worth its recharge; not turning it on", every=600)
                if drone.active and now - self.drone_toggled_at.get(drone.index, -math.inf) >= DRONE_MIN_TOGGLE_S:
                    self._toggle_drone(world, drone, False)
                    return
                continue
            if drone.charge > 0.0 and self._drone_needed(world, role, hull):
                self.drone_wanted_at[drone.index] = now
            # Pickup drones (cryonite, energy) only for the moment of the pickup; the others keep the long hold: with
            # the short one the combat drone flickered on and off 19 times in 23 min (Vénar 13:28-13:51).
            pickup = role in ("cryonite", "energy")
            hold = DRONE_HOLD_S if pickup else DRONE_COMBAT_HOLD_S
            gap = DRONE_MIN_TOGGLE_S if pickup else DRONE_COMBAT_MIN_TOGGLE_S
            want = drone.charge > 0.0 and now - self.drone_wanted_at.get(drone.index, -math.inf) < hold
            if want == drone.active or now - self.drone_toggled_at.get(drone.index, -math.inf) < gap:
                continue
            self._toggle_drone(world, drone, want)
            return  # one click per tick; the next tick checks the result
        if self._recharge_safe(world):
            self._recharge_drones(world)

    def _move_cursor_away(self):
        """Hover the middle of the sky (top centre), away from the HUD, so no tooltip stays over it."""
        try:
            game = self.input.game
            self.input.hover(game.width // 2, game.height // 6)
        except Exception as error:
            self._debug(f"cursor move failed: {error}")

    def _pause_drones(self, reason):
        """Leave drones alone for a while (growing up to DRONE_BACKOFF_MAX_S), then try again. Right after a zone
        change the HUD may not be drawn yet (panel "not found" on landing, Vénar 21:13:55): a short quiet retry."""
        if time.monotonic() - self.zone_since < 20:
            self.drones_paused_until = time.monotonic() + 8
            self._debug(f"drones: {reason} right after the zone change; retrying in 8 s")
            return
        self._log("info", f"Drones: {reason}; trying again in {self.drone_backoff / 60:.0f} min")
        self.drones_paused_until = time.monotonic() + self.drone_backoff
        self.drone_backoff = min(self.drone_backoff * 2, DRONE_BACKOFF_MAX_S)
        self.drone_toggle_want.clear()
        self.drone_toggle_fails = 0

    def _screen(self):
        try:
            game = window.game_window(self.pid)
            return capture(game.hwnd) if game else None
        except Exception as error:
            self._debug(f"screenshot failed: {error}")
            return None

    def _drone_panel(self, image):
        """Find the HUD drone panel's arrow button (same picture open or closed; it sits next to the skill bar when
        the panel is closed, past the drone icons when open). Returns (open, arrow_x, arrow_y, columns) in game
        pixels, or None if the arrow is not found."""
        reference = cv2.imread(str(UI_DIR / "drone_panel_open.png"))
        if image is None or reference is None:
            return None
        h, w = image.shape[:2]
        x0, y0 = w // 2, h - 130
        area = image[y0:, x0:x0 + 450]
        result = cv2.matchTemplate(area, reference, cv2.TM_CCOEFF_NORMED)
        _, score, _, (lx, ly) = cv2.minMaxLoc(result)
        if score < UI_MATCH_MIN:
            return None
        offset = lx - DRONE_ARROW_CLOSED_DX  # 0 when closed; columns x 26 + 5 when open
        is_open = offset > DRONE_HUD_STEP // 2
        columns = max(1, round((offset - 5) / DRONE_HUD_STEP)) if is_open else 0
        return is_open, x0 + lx + reference.shape[1] // 2, y0 + ly + reference.shape[0] // 2, columns

    def _drone_slot_point(self, panel, index):
        """Centre of a drone's HUD icon, from the open panel's arrow: `columns` icons per row, 26 px apart, ending
        just left of the arrow (3440 x 1440: arrow centre (1951, 1391), slot 0 at (1900, 1368))."""
        _, ax, ay, columns = panel
        left = ax - DRONE_HUD_STEP * columns + 1
        return left + (index % columns) * DRONE_HUD_STEP, ay - 23 + (index // columns) * DRONE_HUD_STEP

    def _drone_ui_ready(self):
        """Screenshot checks before any drone click: no Drone Setup window open (a stray one is closed, checked),
        and the HUD panel found and open (opened once if closed, checked). Returns the panel or None."""
        image = self._screen()
        if image is not None and self._ui_shows("drone_setup_title"):
            self._debug("drone setup window open; closing it")
            self._click(DRONE_WINDOW_CLOSE)
            time.sleep(0.5)
            if self._ui_shows("drone_setup_title"):
                self._pause_drones("a Drone Setup window is open and did not close")
                return None
            image = self._screen()
        panel = self._drone_panel(image)
        if panel is None:
            # A tooltip from the last hover can cover the arrow (17:31:27): move the cursor away and look again.
            self._move_cursor_away()
            time.sleep(0.5)
            panel = self._drone_panel(self._screen())
        if panel is None:
            self._pause_drones("the drone panel was not found on the HUD")
            return None
        if not panel[0]:
            self._debug("drone panel closed; opening it")
            self.input.click(panel[1], panel[2], "left")
            time.sleep(0.5)
            panel = self._drone_panel(self._screen())
            if panel is None or not panel[0]:
                self._pause_drones("the drone panel did not open")
                return None
            time.sleep(0.7)  # a click 0.5 s after opening did not register (2026-11-12 16:21:25)
        return panel

    def _toggle_drone(self, world, drone, want):
        now = time.monotonic()
        if drone.index in self.drone_toggle_want:
            # The last click on this drone did not change it.
            self.drone_toggle_fails += 1
            if self.drone_toggle_fails >= 3:
                self._pause_drones("clicks on the drone icons had no effect")
                return
        panel = self._drone_ui_ready()
        if panel is None:
            return
        x, y = self._drone_slot_point(panel, drone.index)
        self._debug(f"drone {drone.index} ({drone.desc}) {'on' if want else 'off'}, charge {drone.charge:.0%}")
        # Hover first, then click, then check in memory and retry once at once: two of five plain right-clicks did
        # not register (2026-11-12 Terasa).
        for attempt in range(2):
            self.input.hover(x, y)
            time.sleep(0.15)
            self.input.click(x, y, "right")
            done = self._wait(lambda w: any(d.index == drone.index and d.active == want for d in w.drones), 1.5,
                              "drone toggle")
            if done is not None:
                break
            self._debug(f"drone {drone.index} click did not register; {'retrying' if attempt == 0 else 'giving up'}")
        self._move_cursor_away()  # no tooltip left over the HUD
        self.drone_toggled_at[drone.index] = now
        self.drone_toggle_want[drone.index] = want

    def _recharge_safe(self, world):
        """Recharging opens a window that hides part of the view: only with nothing attacking, no enemy within
        DRONE_RECHARGE_CLEAR or its aggro range + 1,500, not refilling, not in a gold blueprint run. (No enemy within
        8,000 almost never happened on Mantis Hive: drones ran down to 41% without a recharge, 2026-11-12.)"""
        # A refill only blocks recharging until energy is nearly full: in Collect Energy mode the refill flag stays
        # set, and with full energy at the field no drone was ever recharged (Vénar 21:16, user).
        filling = self.refilling and (world.energy or 0) < 0.98 * (world.energy_cap or 1)
        if filling or self.gold_progress is not None or any(self._attacking(world, u) for u in world.units):
            return False
        return not any(u.distance < max(DRONE_RECHARGE_CLEAR, self._aggro_reach(u, margin=False) + 1500)
                       for u in self._enemy_units(world))

    def _recharge_drones(self, world):
        """Recharge one drone below "Recharge drones below charge" when energy stays above "Recharge drones only
        above energy" afterwards (user: never when low on energy). A short checked sequence: Drone Setup window
        (left-click the icon, title checked), the drone's row, Charge (button checked, charge confirmed in memory),
        close (checked). Aborted, with the window closed, as soon as anything attacks."""
        cfg = self.config
        now = time.monotonic()
        cap = world.energy_cap or 1
        # Collect Energy refills the whole ship: every drone that is not full (user); else the "below" setting.
        below = 0.99 if cfg.mode == "energy" else cfg.drone_recharge_below
        due = [d for d in world.drones if d.charge < below and self._drone_role(d) is not None
               and now - self.drone_recharge_at.get(d.index, -math.inf) >= DRONE_RECHARGE_RETRY_S
               and (world.energy or 0) - d.recharge_cost(world.rank_raw) >= cfg.drone_recharge_min_energy * cap]
        if not due:
            return
        drone = min(due, key=lambda d: d.charge)
        self.drone_recharge_at[drone.index] = now
        panel = self._drone_ui_ready()
        if panel is None:
            return
        name = assets.display_name(drone.desc)
        cost = drone.recharge_cost(world.rank_raw)

        def safe():
            w = self._read()
            return w is not None and self._recharge_safe(w)

        opened = False
        try:
            x, y = self._drone_slot_point(panel, drone.index)
            self.input.click(x, y, "left")
            opened = True
            time.sleep(0.7)
            if not self._ui_shows("drone_setup_title"):
                self._pause_drones("the Drone Setup window did not open")
                return
            rx, ry = self._point(DRONE_WINDOW_ROW)
            self.input.click(rx, ry + drone.index * DRONE_WINDOW_ROW_STEP, "left")
            time.sleep(0.4)
            if not safe():
                self._debug("drone recharge aborted: enemies near")
                return
            if not self._ui_shows("drone_charge_button"):
                self._pause_drones("no Charge button in the Drone Setup window")
                return
            self._click(DRONE_CHARGE_BUTTON)
            after = self._wait(lambda w: any(d.index == drone.index and d.charge > drone.charge + 0.01
                                             for d in w.drones), 3, "drone recharge")
            if after is None:
                self._log("warning", f"Recharge of {name} did not show; trying again in "
                                     f"{DRONE_RECHARGE_RETRY_S / 60:.0f} min")
            else:
                self._log("info", f"Recharged drone {name} ({drone.charge:.0%} -> 100%, {cost:,} energy)")
        finally:
            if opened:
                time.sleep(0.2)
                if self._ui_shows("drone_setup_title"):
                    self._click(DRONE_WINDOW_CLOSE)
                    time.sleep(0.4)
                    if self._ui_shows("drone_setup_title"):
                        self._pause_drones("the Drone Setup window did not close")
