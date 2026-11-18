"""Project world positions to screen pixels using the camera state read from memory.

3D axes: x = world x, y = height, z = world y (confirmed 2026-11-11: the look-at point equals the ship's x, y).
The camera controller is component `client/screens/for` field `sA` (class `dx`, subclass of `x`):
`position` is the eye, `fa` the look-at point.
"""
import math
from dataclasses import dataclass

from ..mem import java
from ..mem.jvm import JVM

SCREENS_COMPONENT = "com/splitscreen/client/screens/for"
DEFAULT_FOV_Y_DEG = 75.0  # the controller's field eV, passed to Camera.setFOVy


@dataclass
class Camera:
    eye: tuple
    target: tuple
    fov_y_deg: float = DEFAULT_FOV_Y_DEG
    offset: tuple = (0.0, 0.0)

    def project(self, world_x, world_y, height, width_px, height_px, fov_y_deg=None):
        """Return (x, y) client pixels for a world point, or None if it is behind the camera.

        Matches Ogre's Frustum with focal length 1: ndc = (camera-space ratio - frustum offset) / tan(fov / 2).
        """
        fov_y_deg = self.fov_y_deg if fov_y_deg is None else fov_y_deg
        ex, ey, ez = self.eye
        fx, fy, fz = (self.target[0] - ex, self.target[1] - ey, self.target[2] - ez)
        length = math.sqrt(fx * fx + fy * fy + fz * fz)
        fx, fy, fz = fx / length, fy / length, fz / length
        # right = forward x up(0, 1, 0)
        rx, ry, rz = -fz, 0.0, fx
        rl = math.hypot(rx, rz)
        rx, rz = rx / rl, rz / rl
        # camera up = right x forward
        ux, uy, uz = ry * fz - rz * fy, rz * fx - rx * fz, rx * fy - ry * fx
        dx, dy, dz = world_x - ex, height - ey, world_y - ez
        cx = dx * rx + dy * ry + dz * rz
        cy = dx * ux + dy * uy + dz * uz
        cz = dx * fx + dy * fy + dz * fz
        if cz <= 1.0:
            return None
        tan_half = math.tan(math.radians(fov_y_deg) / 2)
        aspect = width_px / height_px
        ndc_x = (cx / cz - self.offset[0]) / (tan_half * aspect)
        ndc_y = (cy / cz - self.offset[1]) / tan_half
        return (ndc_x + 1) / 2 * width_px, (1 - ndc_y) / 2 * height_px


def _point(jvm, oop):
    return jvm.get(oop, "bx"), jvm.get(oop, "by"), jvm.get(oop, "UQ")


def read_camera(jvm: JVM, components):
    """Read eye and look-at from the camera controller. Call inside jvm.consistent()."""
    screens = components.get(SCREENS_COMPONENT)
    if not screens:
        return None
    controller = jvm.get(screens, "sA")
    if not controller:
        return None
    eye = jvm.get(controller, "position")
    target = jvm.get(controller, "fa")
    if not eye or not target:
        return None
    fov = jvm.get(controller, "eV")  # smoothed value object; field SB holds degrees (75)
    offset = jvm.get(controller, "eU")  # Ogre frustum offset
    return Camera(
        _point(jvm, eye),
        _point(jvm, target),
        jvm.get(fov, "SB") if fov else DEFAULT_FOV_Y_DEG,
        (jvm.get(offset, "bx"), jvm.get(offset, "by")) if offset else (0.0, 0.0),
    )


def hovered_object(jvm: JVM, components):
    """The object under the mouse cursor according to the minimap service (`ans.aLH`), or 0."""
    minimap = components.get("com/splitscreen/ans")
    return jvm.get(minimap, "aLH") if minimap else 0


def registry_components(jvm: JVM):
    """{class name: object} for the main registry and its parents."""
    registry = jvm.get_static("com/splitscreen/atp", "cL")
    result = {}
    current = registry
    while current:
        for oop in java.tint_object_map(jvm, jvm.get(current, "components")).values():
            if oop:
                result.setdefault(jvm.class_name(oop), oop)
        current = jvm.get(current, "parent")
    return result
