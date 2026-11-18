"""Keys, skills and hotkeys, clicks and UI checks (screenshots, tabs, labels).

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

import cv2

from ..game import assets
from ..input import window
from ..input.capture import capture
from .constants import *  # noqa: F401,F403
from .util import _load_state, _save_state, StopBot


class ActionsMixin:
    # Actions ------------------------------------------------------------------------------------------------

    def _key(self, key):
        self._debug(f"key {key}")
        self.input.key(key)

    def _skill(self, item):
        """Press a skill key. After a press the skill is in its warm-up and does not show as cooling down yet;
        pressing again then cancels it (2026-11-11: the Atomic Repair Droid, 0.5 s warm-up, was pressed six times in
        3 s and never repaired; the ship died with repair ready). So the same key waits for the warm-up plus 1 s
        (at most 80% of the cooldown), and a skill that is running is never pressed."""
        # The blaster reports isActive permanently (seen live 2026-11-11: status 0, isActive True while idle), so the
        # "running" rule would block it completely; it only applies to the other skills.
        desc = getattr(item, "desc", None) or ""
        if item.is_active and "blaster" not in desc:
            return False
        now = time.monotonic()
        if now < self.cast_until or self.repair_cast is not None:
            # Another skill is in its warm-up: pressing anything now cancels it (2026-11-11: the repair was pressed
            # seven times in a fight, each cancelled by the next blaster or rocket press, until the hull was at 4%).
            return False
        wait = max(0.5, (item.warmup or 0.0) + 1.0)
        if item.cooldown:
            wait = min(wait, 0.8 * item.cooldown)
        if now - self.last_skill.get(item.hotkey, 0.0) < wait:
            return False
        if now < self.skill_paused.get(item.hotkey, 0.0):
            return False
        # Global cooldown (read from memory each tick, and counted from our own last press until the next read): no
        # other item can be used during it. The blaster is exempt from the check.
        if "blaster" not in desc and now < self.gcd_until:
            return False
        self.last_skill[item.hotkey] = now
        self.last_any_skill = now
        self.gcd_until = max(self.gcd_until, now + GLOBAL_COOLDOWN_S / max(self.cooldown_factor, 0.1) + GCD_MARGIN_S)
        self._key(item.hotkey)
        if "blaster" not in desc:  # the blaster's flags are unreliable (isActive stuck), its presses are not judged
            self.skill_pending.setdefault(item.hotkey, now)
            # Which items were ready at the press: if another one goes off instead, the key belongs to that one.
            self.press_ready[item.hotkey] = {i.desc: i.ready for i in self.last_items}
        warmup = getattr(item, "warmup", None) or 0.0
        if warmup >= 0.2:
            self.cast_until = now + warmup + CAST_MARGIN_S
        if "repair" in desc:
            # The repair holds every other key until it reads as used (not ready / running): a lock of warm-up
            # + 0.3 s was not enough, and Thermo-blast or blaster presses 1-2 s later cancelled it seven times in a
            # row (run 20:55:56-20:56:05, dead with repair "ready").
            self.repair_cast = (item.hotkey, now)
            self.repair_tries = [t for t in self.repair_tries if now - t < REPAIR_TRY_WINDOW_S] + [now]
        return True

    def _check_presses(self, items):
        """Each tick: a skill pressed SKILL_CONFIRM_S ago that still reads ready did not take. After SKILL_FAILS such
        presses in a row it is paused for SKILL_PAUSE_S (2026-11-11: Orbital Strike was pressed every second for
        15 s and never left "ready")."""
        now = time.monotonic()
        self.last_items = list(items)
        by_key = {i.hotkey: i for i in items}
        for key, since in list(self.skill_pending.items()):
            item = by_key.get(key)
            if item is None:
                del self.skill_pending[key]
            elif not item.ready or item.is_active:
                del self.skill_pending[key]
                self.skill_fails[key] = 0
                self._learn_key(item, key)  # checked against the game: this key is this item's
            elif now - since > SKILL_CONFIRM_S:
                del self.skill_pending[key]
                # Did another item go off instead? Then the key is that item's (keys are fixed slots per ship and
                # the layout is not in memory; user: the bot must check, e.g. storm ships have the rocket on key 3).
                before = self.press_ready.pop(key, {})
                # Never the blaster: its flags are unreliable (isActive stuck), it "went off" after every press and
                # took keys 2, 6, 1 and 8 from the collector in 30 s (07:02:16-07:02:38, 2026-11-17).
                other = next((i for i in items if i is not item and before.get(i.desc) and (not i.ready or i.is_active)
                              and "blaster" not in (i.desc or "")
                              and now - self.last_skill.get(i.hotkey, -math.inf) > SKILL_CONFIRM_S + 1.0), None)
                # Only a press with no other key pressed until now counts (01:53:43: a key 5 press came in between and
                # the blaster's cooldown was taken for key 7's; every cooldown in a 13 min recording followed its own
                # key's press, nothing goes off by itself - user).
                if other is not None and any(k != key and since < t <= now for k, t in self.last_skill.items()):
                    other = None
                crossed = self.key_crossed
                if other is not None and crossed.get(key) != other.desc:
                    crossed[key] = other.desc  # seen once: learned only when the same key does it again
                    other = None
                if other is not None:
                    self._learn_key(other, key)
                    self._log("info", f"Key {key} is {assets.display_name(other.desc)}, not "
                                      f"{assets.display_name(item.desc)}; learned")
                    self._try_other_key(item, by_key)
                    continue
                self.skill_fails[key] = self.skill_fails.get(key, 0) + 1
                if self.skill_fails[key] >= SKILL_FAILS and self._try_other_key(item, by_key):
                    self.skill_fails[key] = 0
                    continue
                if self.skill_fails[key] >= SKILL_FAILS and "repair" not in (item.desc or ""):
                    self.skill_fails[key] = 0
                    self.skill_paused[key] = now + SKILL_PAUSE_S
                    self._warn_once(f"skill-fails-{key}", f"{assets.display_name(item.desc)} (key {key}) presses do "
                                                         f"not take; pausing it for {SKILL_PAUSE_S} s at a time")

    def _try_other_key(self, item, by_key):
        """An item whose presses never take may sit on another key than assumed (fixed slots per ship; a ship that mixes
        item classes has no known layout): try the keys no other item uses, one at a time, and keep the one that
        works for this ship (state.json "hotkeys"). True if another key is tried now."""
        ship = self.ship_type_now
        if not ship or not item.desc:
            return False
        tried = self.hotkey_tried.setdefault((ship, item.desc), {item.hotkey})
        free = [str(k) for k in range(1, 9) if str(k) not in by_key and str(k) not in tried]
        if not free:
            return False
        key = free[0]
        tried.add(key)
        saved = _load_state().get("hotkeys") or {}
        saved.setdefault(ship, {})[item.desc] = key
        _save_state({"hotkeys": saved})
        self._log("info", f"{assets.display_name(item.desc)}: key {item.hotkey} does not work, trying key {key}")
        return True

    def _learn_key(self, item, key):
        """Save a key checked against the game for this ship and item (state.json "hotkeys")."""
        ship = self.ship_type_now
        if not ship or not item.desc:
            return
        saved = _load_state().get("hotkeys") or {}
        if saved.get(ship, {}).get(item.desc) == key:
            return
        saved.setdefault(ship, {})[item.desc] = key
        _save_state({"hotkeys": saved})
        self._hotkey_cache = (None,)
        item.hotkey = key

    def _apply_hotkeys(self, world):
        """Keys learned per ship (checked against the game) override the first guess (state.py)."""
        self.ship_type_now = world.ship_type
        if not world.ship_type or not world.items:
            return
        now = time.monotonic()
        cache = self._hotkey_cache
        if not cache or len(cache) < 3 or cache[0] != world.ship_type or now - cache[1] > 10:
            learned = (_load_state().get("hotkeys") or {}).get(world.ship_type, {})
            self._hotkey_cache = cache = (world.ship_type, now, learned)
        for item in world.items:
            if item.desc in cache[2]:
                item.hotkey = cache[2][item.desc]

    def _update_repair_cast(self, repair):
        """Each tick: end the repair key lock once the repair reads as used, or after REPAIR_CAST_S."""
        now = time.monotonic()
        used = repair is not None and (not repair.ready or repair.is_active)
        if used:
            self.repair_tries = []
        if self.repair_cast is not None and (used or repair is None or now - self.repair_cast[1] > REPAIR_CAST_S):
            if not used and repair is not None:
                self._debug("repair press did not take")
            self.repair_cast = None

    def _click(self, spec, row=0):
        x, y = self._point(spec, row)
        self._debug(f"click UI {spec} row {row} at ({x:.0f}, {y:.0f})")
        # UI elements react to the cursor resting on them first (HUD buttons do); a move 0.02 s before a 0.05 s press
        # was often not taken: the first rescue attempt mostly failed, and at 17:07-17:08 three in a row (user: maybe
        # the click is too short). Rest on it, then a longer press.
        self.input.hover(x, y)
        time.sleep(UI_HOVER_S)
        self.input.click(x, y, "left", hold_s=UI_HOLD_S)

    def _snapshot(self, label):
        """Save a screenshot of the game window to logs/ (for UI steps that went wrong). Sends nothing to the game."""
        try:
            game = window.game_window(self.pid)
            image = capture(game.hwnd) if game else None
            if image is not None:
                LOG_DIR.mkdir(exist_ok=True)
                path = LOG_DIR / f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{label}.png"
                cv2.imwrite(str(path), image)
                self._debug(f"screenshot {path.name}")
        except Exception as error:
            self._debug(f"screenshot failed: {error}")

    def _fail(self, label, message):
        """A StopBot for a UI step that went wrong, with a screenshot saved to logs/ (raise the result)."""
        self._snapshot(label)
        return StopBot(f"{message}; screenshot in logs/")

    def _ui_shows(self, name):
        """True if the game window shows a known UI piece (UI_CHECKS: reference image and where to look). Read from a
        screenshot; sends nothing to the game. Used to check a panel before clicking into it (2026-11-12 12:52: the
        Maintenance click did not register, the Repair click then landed in the Configuration item list and opened the
        Item Shop, and the OK click selected a blaster there; a colour test could not tell the shop from the dialog)."""
        try:
            game = window.game_window(self.pid)
            image = capture(game.hwnd) if game else None
        except Exception as error:
            self._debug(f"screenshot failed: {error}")
            return False
        if image is None:
            return False
        reference = cv2.imread(str(UI_DIR / f"{name}.png"))
        h, w = image.shape[:2]
        x0, y0, x1, y1 = UI_CHECKS[name](w, h)
        area = image[max(0, y0):y1, max(0, x0):x1]
        if reference is None or area.shape[0] < reference.shape[0] or area.shape[1] < reference.shape[1]:
            return False
        score = float(cv2.matchTemplate(area, reference, cv2.TM_CCOEFF_NORMED).max())
        self._debug(f"UI check {name}: match {score:.2f}")
        return score >= UI_MATCH_MIN

    def _click_tab(self, name, fallback):
        """Click a hangar panel tab found by its label (tab_<name>.png / tab_<name>_active.png) in the tab bar: some
        hangars have a CONQUEST tab in front and every tab sits ~80 px further left (Ob 01:18: the fixed click hit
        CONFIGURATION). The fixed position if the label is not found."""
        spot = self._find_tab(name)
        if spot is not None:
            self.input.hover(*spot)
            time.sleep(UI_HOVER_S)
            self.input.click(spot[0], spot[1], "left", hold_s=UI_HOLD_S)
            return
        self._click(fallback)

    def _hangar_panel_open(self):
        """The hangar panel (Space) is open: its tab labels show, whichever tab is active (the Maintenance-active
        image did not match with CONFIGURATION active, Space then closed the open panel; Ob 01:22)."""
        return self._find_tab("maintenance") is not None or self._find_tab("hangar") is not None

    def _find_label(self, files, area):
        """(x, y) of the best match of any of these UI images (UI_DIR) inside area(w, h), or None."""
        try:
            game = window.game_window(self.pid)
            image = capture(game.hwnd) if game else None
        except Exception:
            return None
        if image is None:
            return None
        h, w = image.shape[:2]
        x0, y0, x1, y1 = area(w, h)
        region = image[y0:y1, x0:x1]
        best = None
        for file in files:
            ref = cv2.imread(str(UI_DIR / file))
            if ref is None or region.shape[0] < ref.shape[0] or region.shape[1] < ref.shape[1]:
                continue
            _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(region, ref, cv2.TM_CCOEFF_NORMED))
            if best is None or score > best[0]:
                best = (score, x0 + loc[0] + ref.shape[1] // 2, y0 + loc[1] + ref.shape[0] // 2)
        if best is not None and best[0] >= UI_MATCH_MIN:
            return best[1], best[2]
        return None

    def _click_at(self, spot):
        self.input.hover(*spot)
        time.sleep(UI_HOVER_S)
        self.input.click(spot[0], spot[1], "left", hold_s=UI_HOLD_S)

    def _load_color_patterns(self):
        """In a hangar: CONFIGURATION -> Color Pattern, so the client loads the color pattern item data (ids of
        "Ink", "Crossed Strings", ... are in memory nowhere else; user: identify them). Closes the panel again."""
        if not self._hangar_panel_open():
            self._key("SPACE")
            time.sleep(1.5)
        tab = self._find_label(["tab_configuration_active.png"], lambda w, h: (w - 700, 20, w - 120, 75))
        if tab is not None:
            self._click_at(tab)
            time.sleep(1.0)
        entry = self._find_label(["menu_color_pattern.png"], lambda w, h: (w - 720, 60, w - 250, 300))
        if entry is None:
            self._debug("color patterns: the Color Pattern entry was not found")
        else:
            self._click_at(entry)
            time.sleep(2.0)
        self._key("SPACE")
        time.sleep(0.8)

    def _find_tab(self, name):
        """(x, y) of a hangar panel tab's label in the tab bar, or None."""
        try:
            game = window.game_window(self.pid)
            image = capture(game.hwnd) if game else None
        except Exception:
            image = None
        if image is not None:
            h, w = image.shape[:2]
            x0, y0 = w - 700, 20
            area = image[y0:75, x0:w - 120]
            best = None
            for suffix in ("", "_active"):
                ref = cv2.imread(str(UI_DIR / f"tab_{name}{suffix}.png"))
                if ref is None or area.shape[0] < ref.shape[0] or area.shape[1] < ref.shape[1]:
                    continue
                result = cv2.matchTemplate(area, ref, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(result)
                if best is None or score > best[0]:
                    best = (score, x0 + loc[0] + ref.shape[1] // 2, y0 + loc[1] + ref.shape[0] // 2)
            if best is not None and best[0] >= UI_MATCH_MIN:
                self._debug(f"{name} tab found at ({best[1]}, {best[2]}), match {best[0]:.2f}")
                return best[1], best[2]
        return None

    def _warn_once(self, key, text):
        if key not in self.warned:
            self.warned.add(key)
            self._log("warning", text)
