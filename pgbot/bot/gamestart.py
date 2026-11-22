"""Start the game and log in (docs/ROADMAP.md 8), so the bot can run when the game is not open or has crashed.

Steps, each checked on screen or in memory before the next:
1. Run Launcher.exe from the game folder (the official launcher; it updates files if needed, then starts the client).
2. Put its windows on virtual desktop 2: moved there as they appear (Windows' internal desktop interface, the user's
   view never changes); if that interface does not work on this Windows build, the user's view is switched to
   desktop 2 for the start (Win+Ctrl+Right) and back once the game window is open. One desktop only: the game opens
   where the user is.
3. Login screen: click Login (the game remembers the account: "Remember me"; nothing is typed, no password is
   stored by the bot). The first menu (Login / Register / Exit) has a Login button too.
4. Select Pilot: click the pilot in the row set in Settings (1 = top), after checking that row holds a pilot
   (its green "Server Online" line), so "Create new pilot" is never clicked.
5. Wait until memory shows the ship in the world.
Clicks are posted to the game window (background input), like all of the bot's input.
"""
import pathlib
import subprocess
import time

import cv2
import numpy as np

from ..game import location
from ..input import vdesktop_move
from ..input.background import BackgroundInput
from ..input.capture import capture
from ..input.window import game_window, windows_of

UI_DIR = pathlib.Path(__file__).resolve().parent.parent / "input" / "ui"
LOG_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "logs"
TARGET_DESKTOP = 1           # desktop 2
LAUNCH_TIMEOUT_S = 900       # the launcher may download an update first
LOGIN_TIMEOUT_S = 180        # from the game window to the ship in the world
CLICK_GAP_S = 4.0            # after a click, the screen gets this long to change before clicking again
MAX_CLICKS = 6               # per screen
LIST_STILL_DIFF = 2.0        # the pilot list counts as drawn when two captures differ by less than this (mean)
PILOT_RANK_TOLERANCE = 5     # a logged-in rank this far from the last run's is another pilot
TOP_ROW = (-45, 34)          # first pilot's name relative to the centre of the "Select Pilot" title
ROW_STEP = 63                # pilot rows are this far apart


class StartFailed(Exception):
    pass


class GameStarter:
    def __init__(self, log, debug, stop_event, read_world, pilot_row=1):
        """log(level, text), debug(text); stop_event: the bot's stop flag; read_world(pid) -> World or None;
        pilot_row: which pilot of the Select Pilot list to log in with (1 = top)."""
        self.log, self.debug, self.stop, self.read_world = log, debug, stop_event, read_world
        self.pilot_row = max(1, int(pilot_row))
        self.templates = {name: cv2.imread(str(UI_DIR / f"{name}.png"))
                          for name in ("login_button", "select_pilot_title")}

    # Desktop handling ------------------------------------------------------------------------------------------

    def _desktop_mode(self):
        desktops, current = vdesktop_move.registry_desktops()
        if len(desktops) <= TARGET_DESKTOP:
            self.log("info", "Only one virtual desktop: the game opens on this one")
            return None, None
        try:
            internal = vdesktop_move.InternalDesktops()
            self.debug(f"virtual desktops: internal interface works ({internal.build}); windows are moved to "
                       f"desktop {TARGET_DESKTOP + 1}")
            return "move", internal
        except OSError as error:
            self.debug(f"virtual desktops: internal interface not usable ({error})")
        if current == desktops[TARGET_DESKTOP]:
            return "here", None  # the user is on desktop 2 already: the game opens there
        if current != desktops[0]:
            self.log("warning", "Not on desktop 1: the game opens on the current desktop")
            return None, None
        self.log("info", f"Switching to desktop {TARGET_DESKTOP + 1} while the game starts")
        vdesktop_move.switch_by_keys(+1)
        if not vdesktop_move.wait_current(desktops[TARGET_DESKTOP]):
            self.log("warning", "The desktop switch did not happen; the game opens on the current desktop")
            return None, None
        return "switch", desktops

    def _switch_back(self, desktops):
        vdesktop_move.switch_by_keys(-1)
        if not vdesktop_move.wait_current(desktops[0]):
            self.log("warning", "Could not switch back to desktop 1")

    # Steps -----------------------------------------------------------------------------------------------------

    def _find(self, image, name, threshold=0.85):
        tpl = self.templates.get(name)
        if image is None or tpl is None or image.shape[0] < tpl.shape[0] or image.shape[1] < tpl.shape[1]:
            return None
        _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(image, tpl, cv2.TM_CCOEFF_NORMED))
        if score < threshold:
            return None
        return loc[0] + tpl.shape[1] // 2, loc[1] + tpl.shape[0] // 2

    def _check_pilot(self, world):
        """The logged-in pilot is the one the bot farms with: its rank close to the last run's. Another pilot of the
        account (a rank 2 alt, 2026-11-20) stops the start instead of running the bot on it."""
        from .util import _load_state, _save_state
        rank = world.rank_raw
        if rank is None:
            return
        # Per row of the pilot list (Settings: Pilot to log in): another row is another pilot with its own rank (user,
        # 2026-11-20). A first login on a row only records its rank.
        ranks = _load_state().get("pilot_rank")
        if not isinstance(ranks, dict):
            ranks = {"1": ranks} if isinstance(ranks, int) else {}  # first version: one rank, saved with row 1
        row = str(self.pilot_row)
        last = ranks.get(row)
        if last is not None and abs(rank - last) > PILOT_RANK_TOLERANCE:
            raise StartFailed(f"logged in with another pilot (rank {rank}; the pilot in row {row} was rank {last} "
                              f"last time); check Settings: Pilot to log in")
        if last != rank:
            ranks[row] = rank
            _save_state({"pilot_rank": ranks})

    @staticmethod
    def _has_pilot(image, spot):
        """A pilot row has the green "Server Online" (or a coloured status) line under the name; the "Create new
        pilot" row and empty space below the list do not."""
        x, y = spot
        area = image[max(0, y + 18):y + 42, max(0, x - 10):x + 110]
        if area.size == 0:
            return False
        b, g, r = area[:, :, 0].astype(int), area[:, :, 1].astype(int), area[:, :, 2].astype(int)
        coloured = ((g > 150) & (g > r + 60) & (g > b + 60)) | ((r > 150) & (r > g + 60))  # online green / offline red
        return int(coloured.sum()) >= 15

    @staticmethod
    def _game_pid():
        """The client (not the launcher's own Java process): the one with the 'PirateGalaxy Version' window."""
        for pid in location.game_pids():
            if any(w.visible and w.title.startswith("PirateGalaxy") for w in windows_of(pid)):
                return pid
        return None

    def _sleep(self, seconds):
        if self.stop.wait(seconds):
            raise StartFailed("stopped")

    def _save_screen(self, image, what):
        try:
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / f"start-{what}-{time.strftime('%Y%m%d-%H%M%S')}.png"
            cv2.imwrite(str(path), image)
            return path
        except Exception:
            return None

    def start(self):
        """Starts the game and logs in. Returns the game's process id; raises StartFailed."""
        pid = self._game_pid()
        if pid:
            # Server mode keeps the game when only the bot restarts (tools/server/session.sh): logged in, the bot takes
            # it over; still at the login screen or the pilot list, it logs in from there.
            world = self.read_world(pid)
            if world is not None and world.zone in ("planet", "orbit", "hangar", "starmap") and                     (world.hitpoints or world.dead):
                self._check_pilot(world)
                return pid
            self.log("info", "The game is open but not logged in; logging in")
            return self._login(pid, None, set())
        launcher = location.launcher()
        if not launcher.exists():
            raise StartFailed(f"Launcher.exe not found in {location.folder()} (Settings: Game folder)")
        mode, data = self._desktop_mode()
        switched_back = mode != "switch"
        moved = set()
        try:
            self.log("info", "Starting the game")
            subprocess.Popen([str(launcher)], cwd=str(location.folder()))
            started = time.monotonic()
            pid = None
            while pid is None:
                if mode == "move":
                    self._move_new_windows(data, moved)
                pid = self._game_pid()
                if pid is None:
                    if time.monotonic() - started > LAUNCH_TIMEOUT_S:
                        raise StartFailed("the game window did not open (launcher update stuck?)")
                    self._sleep(0.2)
            if mode == "move":
                self._move_new_windows(data, moved)
            elif mode == "switch":
                self._sleep(1.0)  # let the window settle on this desktop before leaving it
                self._switch_back(data)
                switched_back = True
            self.debug(f"game window open after {time.monotonic() - started:.0f} s (pid {pid})")
            return self._login(pid, data if mode == "move" else None, moved)
        finally:
            if not switched_back:
                self._switch_back(data)

    def _move_new_windows(self, internal, moved):
        pids = set(location.game_pids()) | set(location.launcher_pids())
        for pid in pids:
            for w in windows_of(pid):
                if w.visible and w.hwnd not in moved:
                    try:
                        internal.move(w.hwnd, TARGET_DESKTOP)
                        moved.add(w.hwnd)
                        self.debug(f"moved window '{w.title}' to desktop {TARGET_DESKTOP + 1}")
                    except OSError:
                        pass  # not ready yet (no view): next round

    def _login(self, pid, internal, moved):
        inp = BackgroundInput(pid)
        started = time.monotonic()
        clicks = {"login": 0, "pilot": 0}
        list_area = None
        last_click = -1e9
        last_read = 0.0
        while True:
            if not self._game_pid():
                raise StartFailed("the game closed during login")
            if internal is not None:
                self._move_new_windows(internal, moved)
            if time.monotonic() - last_read > 2.0:
                last_read = time.monotonic()
                world = self.read_world(pid)
                # A shot-down ship reads hull 0 (2026-11-17 23:59: the login wait never ended after a restart while
                # dead); "dead" is read from the same ship properties, so it also means the ship is loaded.
                if world is not None and world.zone in ("planet", "orbit", "hangar", "starmap") and \
                        (world.hitpoints or world.dead):
                    self._check_pilot(world)
                    self.log("info", f"Game started and logged in ({time.monotonic() - started:.0f} s)")
                    return pid
            if time.monotonic() - started > LOGIN_TIMEOUT_S:
                win = game_window(pid)
                shot = self._save_screen(capture(win.hwnd), "stuck") if win else None
                raise StartFailed(f"login did not finish in {LOGIN_TIMEOUT_S} s"
                                  + (f" (screen saved: {shot.name})" if shot else ""))
            if time.monotonic() - last_click < CLICK_GAP_S:
                self._sleep(0.3)
                continue
            win = game_window(pid)
            image = capture(win.hwnd) if win else None
            title = self._find(image, "select_pilot_title")
            login = self._find(image, "login_button")
            if title is not None:
                # Only a list that has stopped changing: a click while it was still being drawn logged into
                # another pilot (rank 2 instead of 90, 2026-11-20 14:19; the rows never change order).
                area = image[title[1]:title[1] + 340, max(0, title[0] - 150):title[0] + 170].astype(np.int16)
                still = list_area is not None and list_area.shape == area.shape and \
                    float(np.abs(area - list_area).mean()) < LIST_STILL_DIFF
                list_area = area
                if not still:
                    self._sleep(0.7)
                    continue
                spot = (title[0] + TOP_ROW[0], title[1] + TOP_ROW[1] + ROW_STEP * (self.pilot_row - 1))
                if not self._has_pilot(image, spot):
                    shot = self._save_screen(image, "pilot")
                    raise StartFailed(f"no pilot in row {self.pilot_row} of the pilot list (Settings: Pilot to log in)"
                                      + (f" (screen saved: {shot.name})" if shot else ""))
                what = "pilot"
            elif login is not None:
                spot, what = login, "login"
            else:
                self._sleep(0.5)  # loading, or a screen without a known button yet
                continue
            clicks[what] += 1
            if clicks[what] > MAX_CLICKS:
                shot = self._save_screen(image, what)
                raise StartFailed(f"the {what} click does not take" + (f" (screen saved: {shot.name})" if shot else ""))
            self.debug(f"start: clicking {what} at {spot}")
            inp.click(*spot)  # marks the game active itself (once per window)
            last_click = time.monotonic()
