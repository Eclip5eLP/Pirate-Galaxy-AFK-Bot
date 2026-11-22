"""Camera: reading it, zoom, turning and tilting, world-to-screen projection.

A mixin of Bot (core.py): its methods use the bot's state and each other's methods."""
import math
import time

from ..game.camera import read_camera, registry_components
from .constants import *  # noqa: F401,F403
from .util import _load_state, _save_state


class CameraMixin:
    def _camera_distance(self):
        camera = self._camera()
        if camera is None:
            return None
        return math.dist(camera.eye, camera.target)

    def _stable_camera_distance(self, timeout=5.0):
        """Camera distance once it stops changing (after landing the camera still moves in for a few seconds; a
        wheel notch measured during that looked like zooming in, 2026-11-12 Terasa: 1,445 -> 551)."""
        last = self._camera_distance()
        deadline = time.monotonic() + timeout
        while last is not None and time.monotonic() < deadline:
            time.sleep(0.3)
            now = self._camera_distance()
            if now is None:
                return last
            if abs(now - last) <= max(2.0, last * 0.01):
                return now
            last = now
        return last

    def _zoom_out(self):
        """Zoom the camera out by ZOOM_NOTCHES wheel notches. The wheel direction that zooms out is learned once
        (one notch, camera distance compared once it is stable before and after) and kept in state.json."""
        try:
            before = self._stable_camera_distance()
            if before is None:
                return
            if before >= ZOOM_FAR:
                self._debug(f"camera already zoomed out ({before:.0f})")
                return
            direction = self.zoom_direction
            done = 0
            if direction is None:
                self.input.wheel(-1)
                after = self._stable_camera_distance(3.0) or before
                if after > before * 1.02:
                    direction, done = -1, 1
                elif after < before * 0.98:
                    direction = 1
                    self.input.wheel(1)  # undo the notch that zoomed in
                else:
                    self._debug(f"zoom: one wheel notch changed nothing (camera distance {before:.0f})")
                    return
                self.zoom_direction = direction
                _save_state({"zoom_direction": direction})
            self.input.wheel(direction * (ZOOM_NOTCHES - done))
            final = self._stable_camera_distance(3.0) or before
            for attempt in range(ZOOM_RETRIES):
                # The notches did not take (Earth in Unrest 2026-11-16 18:39: 695 -> 695, the bot then sat zoomed in
                # deep in a canyon, every click on terrain, and was shot): wait and turn the wheel again.
                if final > before * 1.05:
                    break
                time.sleep(1.0)
                self.input.wheel(direction * ZOOM_NOTCHES)
                final = self._stable_camera_distance(3.0) or final
            else:
                if final <= before * 1.05:
                    self._log("warning", f"Could not zoom out (camera distance {before:.0f} -> "
                                         f"{final:.0f})")
            self._debug(f"zoomed out: camera distance {before:.0f} -> {final:.0f}")
            if final < before * 0.98:
                self._log("warning", "Zooming out made the camera come closer; the wheel direction is learned again "
                                     "next landing")
                self.zoom_direction = None
                _save_state({"zoom_direction": None})
        except Exception as error:  # never let the zoom stop the bot
            self._debug(f"zoom failed: {error}")

    def _camera(self):
        return self.jvm.consistent(lambda: read_camera(self.jvm, registry_components(self.jvm)))

    # Screen geometry ----------------------------------------------------------------------------------------

    def _point(self, spec, row=0):
        self.input.refresh()
        game = self.input.game
        horizontal, dx, vertical, dy = spec
        x = {"centre": game.width / 2, "right": game.width, "left": 0}[horizontal] + dx
        y = {"bottom": game.height, "top": 0, "middle": game.height / 2}[vertical] + dy + row * STARMAP_ROW_STEP
        return x, y

    def _clickable(self, x, y):
        game = self.input.game
        if not (40 < x < game.width - 40 and SAFE_MARGIN_TOP < y < game.height - SAFE_MARGIN_BOTTOM):
            return False
        chat_w, chat_h = CHAT_AREA
        return not (x < chat_w and y > game.height - chat_h)

    def _project(self, x, y, height):
        camera = self._camera()
        if camera is None:
            return None
        self.input.refresh()
        return self._project_on(camera, x, y, height)

    # Camera -------------------------------------------------------------------------------------------------

    @staticmethod
    def _camera_yaw(camera):
        """Direction the camera looks at on the ground plane, degrees (atan2 of world y, world x)."""
        return math.degrees(math.atan2(camera.target[2] - camera.eye[2], camera.target[0] - camera.eye[0]))

    def _turn_camera(self, degrees, reason):
        """Turn the camera around the ship by a right-button drag in the background (0.458 deg per pixel to the
        right, measured 2026-11-11 with tools/camera_drag_test.py). Right-click only selects, so a drag on empty
        ground never moves the ship."""
        pixels = max(-CAMERA_MAX_DRAG_PX, min(CAMERA_MAX_DRAG_PX, degrees / CAMERA_DEG_PER_PX))
        if abs(pixels) < 4:
            return
        self.input.refresh()
        game = self.input.game
        cx, cy = game.width / 2, game.height * 0.42
        self._debug(f"camera turn {degrees:+.0f} deg ({pixels:+.0f} px): {reason}")
        self.input.drag(cx, cy, cx + pixels, cy, "right")
        self.last_turn = time.monotonic()
        time.sleep(0.25)  # let the camera settle before the next projection

    @staticmethod
    def _camera_pitch(camera):
        """Degrees the camera looks down at the ship (0 = level, 90 = straight down)."""
        (ex, ey, ez), (tx, ty, tz) = camera.eye, camera.target
        return math.degrees(math.atan2(ey - ty, math.hypot(ex - tx, ez - tz)))

    @staticmethod
    def _eye_at_pitch(camera, pitch):
        """Where the camera eye would be at this pitch (same distance and direction)."""
        (ex, ey, ez), (tx, ty, tz) = camera.eye, camera.target
        distance, flat = math.dist(camera.eye, camera.target), math.hypot(ex - tx, ez - tz) or 1.0
        r = math.radians(pitch)
        horizontal = distance * math.cos(r)
        return (tx + (ex - tx) / flat * horizontal, ty + distance * math.sin(r), tz + (ez - tz) / flat * horizontal)

    def _tilt_camera(self, camera, degrees, reason):
        """Tilt the camera by a vertical right-button drag; the rate is measured on each tilt and kept. True if done."""
        now = time.monotonic()
        if camera is None or now - self.last_tilt < CAMERA_TILT_EVERY_S or now - self.last_turn < 0.8:
            return False
        rate = self._pitch_rate
        if rate is None:
            rate = self._pitch_rate = _load_state().get("camera_pitch_per_px") or CAMERA_PITCH_PER_PX
        pixels = max(-200.0, min(200.0, degrees / rate))
        if abs(pixels) < 4:
            return False
        before = self._camera_pitch(camera)
        self.input.refresh()
        game = self.input.game
        cx, cy = game.width / 2, game.height * 0.42
        self._debug(f"camera tilt {degrees:+.0f} deg ({pixels:+.0f} px, now {before:.0f} deg): {reason}")
        self.input.drag(cx, cy, cx, cy + pixels, "right")
        self.last_tilt = self.last_turn = now
        time.sleep(0.25)
        after = self._camera()
        if after is not None:
            moved = self._camera_pitch(after) - before
            if abs(moved) >= 2.0:
                measured = moved / pixels
                if measured * rate < 0 or abs(measured - rate) > 0.02:
                    self._debug(f"camera tilt: {measured:.3f} deg/px measured ({moved:+.1f} deg for {pixels:+.0f} px)")
                self._pitch_rate = measured if measured * rate < 0 else (rate * 3 + measured) / 4
                _save_state({"camera_pitch_per_px": round(self._pitch_rate, 4)})
        return True

    def _tilt_for_view(self, camera, reason):
        """Tilt steeper (up to CAMERA_PITCH_MAX) so the camera looks down into a canyon. True if it tilted."""
        if camera is None:
            return False
        pitch = self._camera_pitch(camera)
        if self.pitch_home is None:
            self.pitch_home = pitch
        if pitch >= CAMERA_PITCH_MAX - 2:
            return False
        return self._tilt_camera(camera, min(CAMERA_PITCH_STEP, CAMERA_PITCH_MAX - pitch), reason)

    def _face(self, world, x, y, reason, tolerance=25.0):
        """Turn the camera to look from the ship toward a world point. Returns True if it turned."""
        if time.monotonic() - self.last_turn < 0.8:
            return False
        camera = self._camera()
        if camera is None:
            return False
        desired = math.degrees(math.atan2(y - world.y, x - world.x))
        delta = (desired - self._camera_yaw(camera) + 180) % 360 - 180
        if abs(delta) <= tolerance:
            return False
        # At most CAMERA_STEP_DEG per turn, continued on the next ticks: 90-180 degree snaps looked rigid (user).
        self._turn_camera(max(-CAMERA_STEP_DEG, min(CAMERA_STEP_DEG, delta)), reason)
        return True

    def _check_camera(self, world):
        """About every 2 s: if the camera eye sits inside terrain or terrain hides the ship from it, turn the camera
        the shortest way to a spot where it is clear (user, 2026-11-11: the camera inside a wall made every click
        land on the wall)."""
        now = time.monotonic()
        nav = self._nav(world)
        if nav is None or now - self.last_camera_check < 2.0 or now - self.last_turn < 0.8:
            return
        self.last_camera_check = now
        camera = self._camera()
        if camera is None:
            return
        # Keep the camera zoomed out (user, 2026-11-16): zoomed in it ends up in terrain and finds no clickable ground.
        # The zoom right after landing is often ignored (the landing camera still moves: 695 -> 695 four times on
        # Earth in Unrest), the same notches a few seconds later work (695 -> 1,232 -> 1,769).
        if self._zoom_out_quick(camera, "keeping the camera out", every=ZOOM_KEEP_EVERY_S):
            return
        (ex, ey, ez), (tx, ty, tz) = camera.eye, camera.target

        def clear(eye):
            # No heightmap for some planets (Technatoria, 2026-11-19 13:31: a TypeError every tick paused the bot):
            # nothing to test the camera against, so the view counts as clear.
            ground = nav.ground_height(eye[0], eye[2])
            return (ground is None or ground < eye[1] - 150) and nav.sight_clear(eye, (tx, ty, tz))
        if clear((ex, ey, ez)):
            # In the open for a while: tilt back toward the angle it had (a steep view sees less ahead).
            self.camera_clear_since = self.camera_clear_since or now
            pitch = self._camera_pitch(camera)
            if self.pitch_home is not None and pitch > self.pitch_home + 5 \
                    and now - self.camera_clear_since > CAMERA_PITCH_RELAX_S and now - self.last_tilt > CAMERA_PITCH_RELAX_S:
                back = max(-CAMERA_PITCH_STEP, self.pitch_home - pitch)
                if clear(self._eye_at_pitch(camera, pitch + back)):
                    self._tilt_camera(camera, back, "clear view again, tilting back")
            return
        self.camera_clear_since = None
        if self._zoom_out_quick(camera, "camera in terrain"):
            return
        # Steeper first: looking down into a canyon keeps the turn (user, 2026-11-17).
        pitch = self._camera_pitch(camera)
        if self.pitch_home is None:
            self.pitch_home = pitch
        for steeper in (CAMERA_PITCH_STEP, 2 * CAMERA_PITCH_STEP, 3 * CAMERA_PITCH_STEP):
            if pitch + steeper > CAMERA_PITCH_MAX:
                break
            if clear(self._eye_at_pitch(camera, pitch + steeper)):
                self._tilt_camera(camera, steeper, "camera inside or behind terrain")
                return
        ox, oz = ex - tx, ez - tz
        for delta in (30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180):
            a = math.radians(delta)
            eye = (tx + ox * math.cos(a) - oz * math.sin(a), ey, tz + ox * math.sin(a) + oz * math.cos(a))
            if clear(eye):
                self._turn_camera(delta, "camera inside or behind terrain")
                return
        self._debug("camera hidden by terrain in every direction")

    def _zoom_out_quick(self, camera, reason, every=None):
        """Zoomed in, the camera sits in terrain and finds no clickable ground much more often (user, 2026-11-16:
        zoom out not only after landing but whenever the camera gets stuck in terrain). Turns the wheel without
        waiting for the camera to settle (this runs mid-fight), at most every ZOOM_TERRAIN_EVERY_S. True if it did."""
        now = time.monotonic()
        if camera is None or self.zoom_direction is None or \
                now - self.last_terrain_zoom <= (ZOOM_TERRAIN_EVERY_S if every is None else every):
            return False
        distance = math.dist(camera.eye, camera.target)
        if distance >= ZOOM_FAR:
            return False
        last = self._last_zoom_distance
        self._last_zoom_distance = distance
        if now - self.zone_since > ZOOM_WARN_AFTER_S and last is not None and distance <= last + 50 \
                and not self._zoom_warned == self.zone_since:
            # Still zoomed in well after landing and the last wheel turn changed nothing: the wheel does not reach
            # the game (worth a look). A slow but working zoom (542 -> 3,379 in 26 s) is no reason to warn.
            self._zoom_warned = self.zone_since
            self._debug(f"Camera still zoomed in {ZOOM_WARN_AFTER_S} s after arriving (distance "
                                 f"{distance:.0f}); the wheel may not reach the game")
        self.last_terrain_zoom = now
        self._debug(f"{reason}: camera distance {distance:.0f}, zooming out")
        self.input.wheel(self.zoom_direction * ZOOM_NOTCHES)
        return True

    def _project_on(self, camera, x, y, height):
        """Client pixel for a world point with a given camera, if it is on the clickable part of the screen."""
        game = self.input.game
        pixel = camera.project(x, y, height if height is not None else camera.target[1], game.width, game.height)
        return pixel if pixel and self._clickable(*pixel) else None

    def _minimap_pixel(self, world, unit):
        left, top = self._point(MINIMAP)
        x = left + MINIMAP_SIZE / 2 + (unit.x - world.x) * MINIMAP_SIZE / 10000
        y = top + MINIMAP_SIZE / 2 + (unit.y - world.y) * MINIMAP_SIZE / 10000
        return x, y
