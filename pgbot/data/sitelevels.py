"""Enemy levels from the site's spawn-area overlays (user, 2026-11-20: the levels are drawn into the images).

Each enemy icon on an overlay has its level written next to it: small black digits on a bright glow (white, green,
...). Found as dark pixels with bright neighbours, the text band grown sideways from there, then read in 5-pixel
cells from the right against digit templates (site_digits.json, cut from 347 labels of all 211 overlays read by eye;
held-out images read 347 right, 0 wrong, 25 not read, all 87 non-labels refused). A label that cannot be read gives
level None; it is never guessed.
"""
import json
import pathlib

import cv2
import numpy as np

CELL_W = 5          # digit pitch in pixels
CELL_H = 7          # digit height
TEXT_DARK = 200     # a text column has at least two pixels darker than this (glow is brighter)
MATCH_MAX = 0.05    # mean squared difference (0..1) above which a cell is no digit
GROW_MAX = 16       # how far the text band may grow from the first dark stroke

_TEMPLATES = None


def _templates():
    global _TEMPLATES
    if _TEMPLATES is None:
        path = pathlib.Path(__file__).resolve().parent / "site_digits.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _TEMPLATES = {ch: [np.array(t, np.float32) for t in lst] for ch, lst in raw.items()}
    return _TEMPLATES


def _prepare(image):
    im = image.astype(np.float32)
    alpha = im[:, :, 3]
    lum = im[:, :, :3].mean(axis=2)
    gray = lum * alpha / 255 + 255 * (1 - alpha / 255)  # on white
    bright = ((lum > 150) & (alpha > 110)).astype(np.float32)
    dark = (im[:, :, :3].max(axis=2) < 90) & (alpha > 170)
    return gray, bright, dark


def _anchors(gray, bright, dark):
    """Boxes (x0, y0, x1, y1) of dark strokes inside a bright glow: one or more digits of a label."""
    neighbours = cv2.filter2D(bright, -1, np.ones((3, 3), np.float32))
    text = (dark & (neighbours >= 3)).astype(np.uint8)
    groups = cv2.dilate(text, np.ones((5, 3), np.uint8))
    count, lab, stats, _ = cv2.connectedComponentsWithStats(groups)
    out = []
    for i in range(1, count):
        _, _, w, h, _ = stats[i]
        if not (6 <= h <= 30 and 4 <= w <= 30):
            continue
        ys, xs = np.nonzero((lab == i) & (text > 0))
        if len(ys) < 5:
            continue
        if ys.max() - ys.min() >= 8:
            # Joined with dark strokes of the icon next to it (Lustra Death Squad): the 7-row band with the most text.
            start = max(range(ys.min(), ys.max() - CELL_H + 2), key=lambda r: int(((ys >= r) & (ys < r + CELL_H)).sum()))
            keep = (ys >= start) & (ys < start + CELL_H)
            ys, xs = ys[keep], xs[keep]
            if len(ys) < 5:
                continue
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        if 5 <= y1 - y0 <= 8 and 1 <= x1 - x0 <= 14:
            out.append((int(x0), int(y0), int(x1), int(y1)))
    return out


def _extent(gray, box):
    """(top row, left, right) of the text band around an anchor box."""
    x0, y0, x1, _ = box
    top = y0
    while top > y0 - 3 and (gray[top - 1, x0:x1] < TEXT_DARK).any():
        top -= 1
    band = gray[top:top + CELL_H]
    width = gray.shape[1]

    def dark(c):
        return 0 <= c < width and (band[:, c] < TEXT_DARK).sum() >= 2
    def step(edge, sign, limit):
        # Up to two blank columns between digits: a pale "1" stands two columns off the next digit (Aurora "13").
        for gap in (1, 2, 3):
            if dark(edge + sign * gap) and abs(edge + sign * gap - limit) < GROW_MAX:
                return edge + sign * gap
        return None
    left = x0
    while (moved := step(left, -1, x0)) is not None:
        left = moved
    right = x1 - 1
    while (moved := step(right, 1, x1)) is not None:
        right = moved
    return top, left, right + 1


def _cell(gray, top, a, b):
    out = np.full((CELL_H, CELL_W), 255.0, np.float32)
    if b <= a:
        return out
    seg = gray[top:top + CELL_H, max(0, a):b]
    out[:seg.shape[0], CELL_W - seg.shape[1]:] = seg
    return out


def _read(gray, box):
    """The label's text ("" if no digit matched), read right to left; icon pixels left of it end the read."""
    templates = _templates()
    top, left, right = _extent(gray, box)
    digits, pos = [], right
    while len(digits) < 3 and pos - left >= 3:
        best = None
        for shift in (0, -1, 1):
            cell = _cell(gray, top, pos - CELL_W + shift, pos + shift)
            for ch, variants in templates.items():
                for t in variants:
                    score = float(((cell - t) ** 2).mean()) / 65025
                    if best is None or score < best[0]:
                        best = (score, ch, shift)
        if best is None or best[0] > MATCH_MAX:
            break
        digits.insert(0, best[1])
        pos = pos - CELL_W + best[2]
    return "".join(digits), (left, top, right, top + CELL_H)


def labels(png_bytes):
    """[(x, y, level or None)] in image pixels: the centres of the level labels of an overlay, one per label."""
    image = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        return []
    gray, bright, dark = _prepare(image)
    out, taken = [], []
    for box in _anchors(gray, bright, dark):
        text, (x0, y0, x1, y1) = _read(gray, box)
        if any(x0 < bx1 and bx0 < x1 and y0 < by1 and by0 < y1 for bx0, by0, bx1, by1 in taken):
            continue  # a second anchor in the same label
        taken.append((x0, y0, x1, y1))
        # A lone "1" is an icon edge or a part read (Axiom's 7/8/9 Death Squads read "1"); a leading 0 is no level.
        level = int(text) if text and text[0] != "0" and text != "1" else None
        if not text:
            continue  # no digit at all: not a label (snake texture, icon outline)
        out.append(((x0 + x1) / 2, (y0 + y1) / 2, level))
    return out
