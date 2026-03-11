"""
SVG Path Parser — extracts points from SVG path 'd' attribute
and computes accurate bounding boxes.

Handles all standard SVG path commands:
M/m, L/l, H/h, V/v, C/c, S/s, Q/q, T/t, A/a, Z/z
"""
import re
from dataclasses import dataclass
from typing import Optional

_TOKEN_RE = re.compile(
    r'[MmLlHhVvCcSsQqTtAaZz]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?'
)


@dataclass
class BBox:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def cx(self) -> float:
        return (self.min_x + self.max_x) / 2

    @property
    def cy(self) -> float:
        return (self.min_y + self.max_y) / 2

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def min_dim(self) -> float:
        return min(self.width, self.height)

    @property
    def max_dim(self) -> float:
        return max(self.width, self.height)

    @property
    def area(self) -> float:
        return self.width * self.height


def _tokenize(d: str) -> list[str]:
    return _TOKEN_RE.findall(d)


def _consume_floats(tokens: list[str], idx: int, count: int) -> tuple[list[float], int]:
    """Consume `count` float tokens starting at `idx`."""
    vals = []
    for i in range(count):
        vals.append(float(tokens[idx + i]))
    return vals, idx + count


def parse_path_points(d: str) -> list[tuple[float, float]]:
    """
    Parse an SVG path 'd' attribute and return all significant
    (x, y) points including control points (for bbox purposes).
    """
    tokens = _tokenize(d)
    points: list[tuple[float, float]] = []
    cx, cy = 0.0, 0.0
    sx, sy = 0.0, 0.0  # subpath start
    i = 0
    cmd: Optional[str] = None

    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in ('Z', 'z'):
                cx, cy = sx, sy
                continue
        elif cmd is None:
            i += 1
            continue

        try:
            if cmd == 'M':
                vals, i = _consume_floats(tokens, i, 2)
                cx, cy = vals[0], vals[1]
                sx, sy = cx, cy
                points.append((cx, cy))
                cmd = 'L'
            elif cmd == 'm':
                vals, i = _consume_floats(tokens, i, 2)
                cx += vals[0]
                cy += vals[1]
                sx, sy = cx, cy
                points.append((cx, cy))
                cmd = 'l'
            elif cmd == 'L':
                vals, i = _consume_floats(tokens, i, 2)
                cx, cy = vals[0], vals[1]
                points.append((cx, cy))
            elif cmd == 'l':
                vals, i = _consume_floats(tokens, i, 2)
                cx += vals[0]
                cy += vals[1]
                points.append((cx, cy))
            elif cmd == 'H':
                vals, i = _consume_floats(tokens, i, 1)
                cx = vals[0]
                points.append((cx, cy))
            elif cmd == 'h':
                vals, i = _consume_floats(tokens, i, 1)
                cx += vals[0]
                points.append((cx, cy))
            elif cmd == 'V':
                vals, i = _consume_floats(tokens, i, 1)
                cy = vals[0]
                points.append((cx, cy))
            elif cmd == 'v':
                vals, i = _consume_floats(tokens, i, 1)
                cy += vals[0]
                points.append((cx, cy))
            elif cmd == 'C':
                for _ in range(3):
                    vals, i = _consume_floats(tokens, i, 2)
                    points.append((vals[0], vals[1]))
                cx, cy = points[-1]
            elif cmd == 'c':
                for j in range(3):
                    vals, i = _consume_floats(tokens, i, 2)
                    px = cx + vals[0]
                    py = cy + vals[1]
                    points.append((px, py))
                    if j == 2:
                        cx, cy = px, py
            elif cmd == 'S':
                for _ in range(2):
                    vals, i = _consume_floats(tokens, i, 2)
                    points.append((vals[0], vals[1]))
                cx, cy = points[-1]
            elif cmd == 's':
                for j in range(2):
                    vals, i = _consume_floats(tokens, i, 2)
                    px = cx + vals[0]
                    py = cy + vals[1]
                    points.append((px, py))
                    if j == 1:
                        cx, cy = px, py
            elif cmd == 'Q':
                for _ in range(2):
                    vals, i = _consume_floats(tokens, i, 2)
                    points.append((vals[0], vals[1]))
                cx, cy = points[-1]
            elif cmd == 'q':
                for j in range(2):
                    vals, i = _consume_floats(tokens, i, 2)
                    px = cx + vals[0]
                    py = cy + vals[1]
                    points.append((px, py))
                    if j == 1:
                        cx, cy = px, py
            elif cmd == 'T':
                vals, i = _consume_floats(tokens, i, 2)
                cx, cy = vals[0], vals[1]
                points.append((cx, cy))
            elif cmd == 't':
                vals, i = _consume_floats(tokens, i, 2)
                cx += vals[0]
                cy += vals[1]
                points.append((cx, cy))
            elif cmd == 'A':
                vals, i = _consume_floats(tokens, i, 7)
                cx, cy = vals[5], vals[6]
                points.append((cx, cy))
            elif cmd == 'a':
                vals, i = _consume_floats(tokens, i, 7)
                cx += vals[5]
                cy += vals[6]
                points.append((cx, cy))
            else:
                i += 1
        except (IndexError, ValueError):
            i += 1

    return points


def compute_bbox(d: str) -> Optional[BBox]:
    """Compute bounding box from an SVG path 'd' attribute."""
    points = parse_path_points(d)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return BBox(
        min_x=min(xs), min_y=min(ys),
        max_x=max(xs), max_y=max(ys),
    )
