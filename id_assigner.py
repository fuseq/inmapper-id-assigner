"""
ID Assigner — Automated ID text placement for fair/exhibition SVG maps.

Reads a fair SVG containing unit layers (Stand/Food/Service/Exhibitor etc.),
computes each unit's geometric center, assigns IDs if missing, and creates
a "Writing" layer with properly positioned ID labels. Fully Inkscape-compatible.

Handles arbitrary transform chains (translate, rotate) on any layer.

Usage:
    python id_assigner.py input.svg -o output.svg --rotation -22
    python id_assigner.py input.svg -o output.svg --layers Exhibitor Food Service
    python id_assigner.py input.svg -o output.svg --auto-transform
"""
import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

from lxml import etree

from svg_path_parser import compute_bbox, parse_path_points, BBox

# ---------------------------------------------------------------------------
# Namespace constants
# ---------------------------------------------------------------------------
SVG_NS = 'http://www.w3.org/2000/svg'
INK_NS = 'http://www.inkscape.org/namespaces/inkscape'
SODI_NS = 'http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd'
XML_NS = 'http://www.w3.org/XML/1998/namespace'
XLINK_NS = 'http://www.w3.org/1999/xlink'

NSMAP = {
    None: SVG_NS,
    'inkscape': INK_NS,
    'sodipodi': SODI_NS,
    'xlink': XLINK_NS,
}

SVG = f'{{{SVG_NS}}}'
INK = f'{{{INK_NS}}}'
SODI = f'{{{SODI_NS}}}'
XML = f'{{{XML_NS}}}'

UNIT_ID_RE = re.compile(r'^ID\d+[A-Z]?$')

# ---------------------------------------------------------------------------
# Font size config (defaults; overridable via CLI)
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLD_SMALL = 172
DEFAULT_THRESHOLD_LARGE = 421

DEFAULT_FONT_SMALL = 12
DEFAULT_FONT_MEDIUM = 24
DEFAULT_FONT_LARGE = 36


# ---------------------------------------------------------------------------
# 2D Affine Transform Matrix
# ---------------------------------------------------------------------------
class AffineMatrix:
    """2D affine transform as a 3x3 matrix:
        [a  c  e]
        [b  d  f]
        [0  0  1]
    """

    def __init__(self, a=1.0, b=0.0, c=0.0, d=1.0, e=0.0, f=0.0):
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    @staticmethod
    def identity():
        return AffineMatrix()

    @staticmethod
    def translate(tx: float, ty: float):
        return AffineMatrix(e=tx, f=ty)

    @staticmethod
    def rotate(deg: float):
        rad = math.radians(deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        return AffineMatrix(a=cos_a, b=sin_a, c=-sin_a, d=cos_a)

    @staticmethod
    def from_svg_transform(transform_str: str) -> 'AffineMatrix':
        """Parse an SVG transform string into an AffineMatrix.
        Supports: translate, rotate, matrix, scale (chained)."""
        if not transform_str or not transform_str.strip():
            return AffineMatrix.identity()

        result = AffineMatrix.identity()
        for match in re.finditer(
            r'(translate|rotate|matrix|scale)\s*\(([^)]+)\)', transform_str
        ):
            func = match.group(1)
            args = [float(x) for x in re.findall(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', match.group(2))]

            if func == 'translate' and len(args) >= 1:
                tx = args[0]
                ty = args[1] if len(args) > 1 else 0.0
                result = result.multiply(AffineMatrix.translate(tx, ty))
            elif func == 'rotate' and len(args) >= 1:
                deg = args[0]
                if len(args) >= 3:
                    cx, cy = args[1], args[2]
                    result = result.multiply(AffineMatrix.translate(cx, cy))
                    result = result.multiply(AffineMatrix.rotate(deg))
                    result = result.multiply(AffineMatrix.translate(-cx, -cy))
                else:
                    result = result.multiply(AffineMatrix.rotate(deg))
            elif func == 'matrix' and len(args) >= 6:
                result = result.multiply(AffineMatrix(*args[:6]))
            elif func == 'scale' and len(args) >= 1:
                sx = args[0]
                sy = args[1] if len(args) > 1 else sx
                result = result.multiply(AffineMatrix(a=sx, d=sy))

        return result

    def multiply(self, other: 'AffineMatrix') -> 'AffineMatrix':
        """self * other (apply other first, then self)."""
        return AffineMatrix(
            a=self.a * other.a + self.c * other.b,
            b=self.b * other.a + self.d * other.b,
            c=self.a * other.c + self.c * other.d,
            d=self.b * other.c + self.d * other.d,
            e=self.a * other.e + self.c * other.f + self.e,
            f=self.b * other.e + self.d * other.f + self.f,
        )

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.a * x + self.c * y + self.e,
            self.b * x + self.d * y + self.f,
        )

    def inverse(self) -> 'AffineMatrix':
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-12:
            raise ValueError("Singular matrix, cannot invert.")
        inv_det = 1.0 / det
        return AffineMatrix(
            a=self.d * inv_det,
            b=-self.b * inv_det,
            c=-self.c * inv_det,
            d=self.a * inv_det,
            e=(self.c * self.f - self.d * self.e) * inv_det,
            f=(self.b * self.e - self.a * self.f) * inv_det,
        )

    def to_svg_transform(self) -> str:
        """Convert back to SVG transform string, using simplest form."""
        is_identity = (
            abs(self.a - 1) < 1e-9 and abs(self.b) < 1e-9
            and abs(self.c) < 1e-9 and abs(self.d - 1) < 1e-9
            and abs(self.e) < 1e-9 and abs(self.f) < 1e-9
        )
        if is_identity:
            return ''

        is_translate = (
            abs(self.a - 1) < 1e-9 and abs(self.b) < 1e-9
            and abs(self.c) < 1e-9 and abs(self.d - 1) < 1e-9
        )
        if is_translate:
            return f'translate({self.e},{self.f})'

        is_rotate = (
            abs(self.a - self.d) < 1e-9 and abs(self.b + self.c) < 1e-9
            and abs(self.a**2 + self.b**2 - 1) < 1e-9
            and abs(self.e) < 1e-9 and abs(self.f) < 1e-9
        )
        if is_rotate:
            deg = math.degrees(math.atan2(self.b, self.a))
            deg_int = int(round(deg))
            if abs(deg - deg_int) < 1e-6:
                return f'rotate({deg_int})'
            return f'rotate({deg})'

        return (f'matrix({self.a},{self.b},{self.c},'
                f'{self.d},{self.e},{self.f})')

    def __repr__(self):
        return f'AffineMatrix(a={self.a:.4f},b={self.b:.4f},c={self.c:.4f},d={self.d:.4f},e={self.e:.4f},f={self.f:.4f})'


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Unit:
    element_id: str
    layer: str
    bbox: BBox
    screen_cx: float
    screen_cy: float


@dataclass
class TextPlacement:
    unit_id: str
    local_x: float
    local_y: float
    font_size: int


# ---------------------------------------------------------------------------
# SVG operations
# ---------------------------------------------------------------------------
def find_layer(root: etree._Element, label: str) -> Optional[etree._Element]:
    for g in root.iter(f'{SVG}g'):
        if (g.get(f'{INK}groupmode') == 'layer'
                and g.get(f'{INK}label') == label):
            return g
    return None


def get_ancestor_transform(element: etree._Element, root: etree._Element) -> AffineMatrix:
    """Compute the cumulative transform from root to element's parent."""
    ancestors = []
    parent_map = {c: p for p in root.iter() for c in p}
    node = element
    while node is not None and node != root:
        ancestors.append(node)
        node = parent_map.get(node)

    result = AffineMatrix.identity()
    for anc in reversed(ancestors):
        t = anc.get('transform', '')
        if t:
            result = result.multiply(AffineMatrix.from_svg_transform(t))
    return result


def get_element_screen_transform(element: etree._Element, root: etree._Element) -> AffineMatrix:
    """Get the full transform chain from root to this element (excluding element's own coords)."""
    parent_map = {c: p for p in root.iter() for c in p}
    chain = []
    node = parent_map.get(element)
    while node is not None and node != root:
        chain.append(node)
        node = parent_map.get(node)

    result = AffineMatrix.identity()
    for anc in reversed(chain):
        t = anc.get('transform', '')
        if t:
            result = result.multiply(AffineMatrix.from_svg_transform(t))
    return result


def assign_ids_to_paths(root: etree._Element, layer_names: list[str]) -> int:
    """Assign ID-prefixed names to paths that lack them. Returns count of renamed paths."""
    counter = 1
    renamed = 0

    existing_ids = set()
    for elem in root.iter():
        eid = elem.get('id', '')
        if UNIT_ID_RE.match(eid):
            m = re.match(r'ID(\d+)', eid)
            if m:
                existing_ids.add(int(m.group(1)))

    if existing_ids:
        counter = max(existing_ids) + 1

    for layer_name in layer_names:
        layer = find_layer(root, layer_name)
        if layer is None:
            continue
        for path in layer.iter(f'{SVG}path'):
            pid = path.get('id', '')
            if UNIT_ID_RE.match(pid):
                continue
            while counter in existing_ids:
                counter += 1
            new_id = f'ID{counter:03d}'
            path.set('id', new_id)
            existing_ids.add(counter)
            counter += 1
            renamed += 1

    return renamed


def assign_door_ids(
    root: etree._Element,
    door_layer_name: str,
    unit_layer_names: list[str],
) -> tuple[int, int]:
    """Assign door IDs based on which unit they belong to.

    Each door gets an ID like {unit_id}_1_, {unit_id}_2_, etc.
    Returns (renamed_count, unmatched_count).
    """
    door_layer = find_layer(root, door_layer_name)
    if door_layer is None:
        return 0, 0

    # Build unit bounding boxes in screen coords
    unit_bboxes: list[tuple[str, float, float, float, float]] = []
    for layer_name in unit_layer_names:
        layer = find_layer(root, layer_name)
        if layer is None:
            continue
        layer_transform = get_element_screen_transform(layer, root)
        own_transform_str = layer.get('transform', '')
        if own_transform_str:
            layer_transform = layer_transform.multiply(
                AffineMatrix.from_svg_transform(own_transform_str)
            )
        for path in layer.iter(f'{SVG}path'):
            pid = path.get('id', '')
            if not UNIT_ID_RE.match(pid):
                continue
            bbox = compute_bbox(path.get('d', ''))
            if not bbox:
                continue
            parent_t = get_element_screen_transform(path, root)
            min_x, min_y = parent_t.apply(bbox.min_x, bbox.min_y)
            max_x, max_y = parent_t.apply(bbox.max_x, bbox.max_y)
            # Ensure min < max after transform
            if min_x > max_x:
                min_x, max_x = max_x, min_x
            if min_y > max_y:
                min_y, max_y = max_y, min_y
            unit_bboxes.append((pid, min_x, min_y, max_x, max_y))

    if not unit_bboxes:
        return 0, 0

    # For each door, find which unit contains its center
    door_counter: dict[str, int] = {}
    renamed = 0
    unmatched = 0

    door_paths = list(door_layer.iter(f'{SVG}path'))
    for door_path in door_paths:
        d_attr = door_path.get('d', '')
        door_bbox = compute_bbox(d_attr)
        if not door_bbox:
            continue

        parent_t = get_element_screen_transform(door_path, root)
        dcx, dcy = parent_t.apply(door_bbox.cx, door_bbox.cy)

        # Find the closest unit whose bbox contains this door
        best_unit = None
        best_dist = float('inf')
        MARGIN = 15  # px tolerance for doors at unit edges

        for uid, ux1, uy1, ux2, uy2 in unit_bboxes:
            if (ux1 - MARGIN <= dcx <= ux2 + MARGIN
                    and uy1 - MARGIN <= dcy <= uy2 + MARGIN):
                ucx = (ux1 + ux2) / 2
                ucy = (uy1 + uy2) / 2
                dist = (dcx - ucx) ** 2 + (dcy - ucy) ** 2
                if dist < best_dist:
                    best_dist = dist
                    best_unit = uid

        if best_unit:
            door_counter[best_unit] = door_counter.get(best_unit, 0) + 1
            new_id = f'{best_unit}_{door_counter[best_unit]}_'
            door_path.set('id', new_id)
            renamed += 1
        else:
            unmatched += 1

    return renamed, unmatched


def collect_units(root: etree._Element, layer_names: list[str]) -> list[Unit]:
    """Collect all units with valid IDs from the specified layers."""
    units = []
    for layer_name in layer_names:
        layer = find_layer(root, layer_name)
        if layer is None:
            print(f"  [WARN] Layer '{layer_name}' not found, skipping.")
            continue

        count = 0
        for path in layer.iter(f'{SVG}path'):
            pid = path.get('id', '')
            if not UNIT_ID_RE.match(pid):
                continue
            d = path.get('d', '')
            bbox = compute_bbox(d)
            if bbox is None:
                print(f"  [WARN] Could not compute bbox for '{pid}', skipping.")
                continue

            parent_transform = get_element_screen_transform(path, root)
            scx, scy = parent_transform.apply(bbox.cx, bbox.cy)

            units.append(Unit(
                element_id=pid, layer=layer_name,
                bbox=bbox, screen_cx=scx, screen_cy=scy,
            ))
            count += 1

        print(f"  Layer '{layer_name}': {count} units collected.")

    return units


def auto_detect_thresholds(units: list['Unit']) -> tuple[int, int]:
    """Find natural break points in min_dim distribution to split units into 3 groups.
    Returns (threshold_small, threshold_large)."""
    dims = sorted(u.bbox.min_dim for u in units)
    n = len(dims)

    if n < 3:
        return DEFAULT_THRESHOLD_SMALL, DEFAULT_THRESHOLD_LARGE

    # Find the two largest gaps in the sorted dimension list
    gaps = []
    for i in range(n - 1):
        gap_size = dims[i + 1] - dims[i]
        midpoint = (dims[i] + dims[i + 1]) / 2
        gaps.append((gap_size, midpoint, i))

    gaps.sort(key=lambda g: g[0], reverse=True)

    if len(gaps) >= 2:
        # Take the two biggest gaps as natural break points
        breaks = sorted([gaps[0][1], gaps[1][1]])

        # Validate: both breaks should be meaningfully different
        dim_range = dims[-1] - dims[0]
        if dim_range > 0 and (breaks[1] - breaks[0]) / dim_range > 0.05:
            return int(breaks[0]), int(breaks[1])

    # Fallback: percentile-based (33rd and 66th)
    t_small = dims[n // 3]
    t_large = dims[2 * n // 3]

    if t_small == t_large:
        t_large = t_small + 1

    return int(t_small), int(t_large)


def determine_font_size(
    bbox: BBox,
    threshold_small: int, threshold_large: int,
    font_small: int, font_medium: int, font_large: int,
) -> int:
    md = bbox.min_dim
    if md <= threshold_small:
        return font_small
    elif md <= threshold_large:
        return font_medium
    else:
        return font_large


def compute_placements(
    units: list[Unit],
    writing_inverse: AffineMatrix,
    threshold_small: int, threshold_large: int,
    font_small: int, font_medium: int, font_large: int,
) -> list[TextPlacement]:
    placements = []
    for unit in units:
        lx, ly = writing_inverse.apply(unit.screen_cx, unit.screen_cy)
        font_size = determine_font_size(
            unit.bbox, threshold_small, threshold_large,
            font_small, font_medium, font_large,
        )
        placements.append(TextPlacement(
            unit_id=unit.element_id,
            local_x=lx, local_y=ly,
            font_size=font_size,
        ))
    return placements


# ---------------------------------------------------------------------------
# Writing layer generation
# ---------------------------------------------------------------------------
def _make_id(counter: list[int], prefix: str) -> str:
    counter[0] += 1
    return f'{prefix}{counter[0]}'


def create_writing_layer(
    placements: list[TextPlacement],
    transform_str: str,
) -> etree._Element:
    """Build the Writing layer <g> with all text elements."""

    layer = etree.Element(f'{SVG}g', nsmap=NSMAP)
    layer.set(f'{INK}groupmode', 'layer')
    layer.set('id', 'Writing')
    layer.set(f'{INK}label', 'Writing')
    layer.set('display', 'inline')
    layer.set('font-weight', 'bold')
    layer.set('font-family', 'Catamaran')
    layer.set('font-size', '15')
    if transform_str:
        layer.set('transform', transform_str)
    layer.set('text-anchor', 'middle')

    text_counter = [0]
    tspan_counter = [0]

    text_style = "line-height:100%;-inkscape-font-specification:'Catamaran Bold'"
    tspan_style = 'text-align:center'

    for p in placements:
        fs = p.font_size
        cx_str = f'{p.local_x:.3f}'
        first_y = p.local_y - fs
        y_values = [first_y, first_y + fs, first_y + 2 * fs]

        text_el = etree.SubElement(layer, f'{SVG}text')
        text_el.set(f'{XML}space', 'preserve')
        text_el.set('style', text_style)
        text_el.set('x', cx_str)
        text_el.set('y', f'{y_values[0]:.3f}')
        text_el.set('id', _make_id(text_counter, 'text_w'))
        text_el.set('display', 'inline')
        text_el.set('font-size', str(fs))
        text_el.set('dominant-baseline', 'middle')

        for line_num, y_val in enumerate(y_values, start=1):
            tspan = etree.SubElement(text_el, f'{SVG}tspan')
            tspan.set(f'{SODI}role', 'line')
            tspan.set('id', _make_id(tspan_counter, 'tspan_w'))
            tspan.set('x', cx_str)
            tspan.set('y', f'{y_val:.3f}')
            tspan.set('style', tspan_style)
            tspan.set('font-size', str(fs))
            tspan.set('text-anchor', 'middle')
            tspan.set('dominant-baseline', 'middle')
            tspan.text = f'{p.unit_id}_{line_num}_'

    return layer


# ---------------------------------------------------------------------------
# Writing layer transform detection & auto-rotation
# ---------------------------------------------------------------------------
def detect_writing_transform(root: etree._Element) -> Optional[str]:
    """If a Writing layer already exists, return its transform string."""
    layer = find_layer(root, 'Writing')
    if layer is not None:
        return layer.get('transform', '')
    return None


def detect_layout_rotation(root: etree._Element, layer_names: list[str]) -> float:
    """Auto-detect the fair layout rotation by analyzing dominant edge angles
    across all unit paths. Returns the angle in degrees."""
    from collections import Counter

    angles = []
    for layer_name in layer_names:
        layer = find_layer(root, layer_name)
        if layer is None:
            continue
        for path in layer.iter(f'{SVG}path'):
            points = parse_path_points(path.get('d', ''))
            if len(points) < 3:
                continue
            clean = [points[0]]
            for p in points[1:]:
                if abs(p[0] - clean[-1][0]) > 0.1 or abs(p[1] - clean[-1][1]) > 0.1:
                    clean.append(p)
            if len(clean) < 3:
                continue
            for i in range(len(clean) - 1):
                dx = clean[i + 1][0] - clean[i][0]
                dy = clean[i + 1][1] - clean[i][1]
                length = math.sqrt(dx * dx + dy * dy)
                if length < 5:
                    continue
                angle = math.degrees(math.atan2(dy, dx))
                while angle > 90:
                    angle -= 180
                while angle < -90:
                    angle += 180
                angles.append(angle)

    if not angles:
        return 0.0

    bins = Counter(round(a) for a in angles)
    mode_angle = bins.most_common(1)[0][0]
    cluster = [a for a in angles if abs(a - mode_angle) < 10]
    return sum(cluster) / len(cluster) if cluster else 0.0


def build_writing_transform(
    existing_transform_str: Optional[str],
    rotation_deg: float,
) -> str:
    """Build the final Writing layer transform string by combining
    the existing transform (e.g. translate) with the rotation."""
    existing = AffineMatrix.from_svg_transform(existing_transform_str or '')

    has_existing_rotation = abs(existing.b) > 1e-9 or abs(existing.c) > 1e-9
    if has_existing_rotation:
        return existing_transform_str or ''

    # Extract the existing translation (if any)
    has_translate = abs(existing.e) > 1e-6 or abs(existing.f) > 1e-6

    if abs(rotation_deg) < 0.01 and not has_translate:
        return ''

    rot_str = str(int(rotation_deg)) if rotation_deg == int(rotation_deg) else f'{rotation_deg:.1f}'

    if has_translate:
        return f'translate({existing.e},{existing.f}) rotate({rot_str})'
    else:
        return f'rotate({rot_str})'


# ---------------------------------------------------------------------------
# SVG read / write
# ---------------------------------------------------------------------------
def load_svg(path: str) -> etree._ElementTree:
    parser = etree.XMLParser(
        remove_blank_text=False,
        strip_cdata=False,
        resolve_entities=False,
    )
    return etree.parse(path, parser)


def save_svg(tree: etree._ElementTree, path: str) -> None:
    tree.write(path, xml_declaration=True, encoding='UTF-8', pretty_print=False)
    print(f"  Output saved to: {path}")


def insert_writing_layer(root: etree._Element, writing: etree._Element) -> None:
    existing = find_layer(root, 'Writing')
    if existing is not None:
        parent = existing.getparent()
        idx = list(parent).index(existing)
        parent.remove(existing)
        parent.insert(idx, writing)
        print("  Replaced existing Writing layer.")
        return

    icons = find_layer(root, 'Icons')
    if icons is not None:
        parent = icons.getparent()
        idx = list(parent).index(icons)
        parent.insert(idx, writing)
    else:
        root.append(writing)
    print("  Created new Writing layer.")


# ---------------------------------------------------------------------------
# Programmatic API (used by web UI)
# ---------------------------------------------------------------------------
@dataclass
class ProcessParams:
    layers: list[str] = field(default_factory=lambda: ['Stand', 'Food', 'Service'])
    assign_door_ids: bool = False
    door_layer: str = 'Doors'
    rotation: Optional[float] = None
    threshold_small: Optional[int] = None
    threshold_large: Optional[int] = None
    font_small: int = DEFAULT_FONT_SMALL
    font_medium: int = DEFAULT_FONT_MEDIUM
    font_large: int = DEFAULT_FONT_LARGE


@dataclass
class ProcessResult:
    output_svg: bytes
    total_units: int
    renamed_paths: int
    renamed_doors: int
    unmatched_doors: int
    rotation_deg: float
    threshold_small: int
    threshold_large: int
    font_counts: dict[int, int]
    layers_found: dict[str, int]


def process_svg(input_svg: bytes, params: ProcessParams) -> ProcessResult:
    """Process an SVG file programmatically. Returns result with output bytes and stats."""
    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix='.svg', delete=False) as tmp_in:
        tmp_in.write(input_svg)
        tmp_in_path = tmp_in.name

    try:
        tree = load_svg(tmp_in_path)
    finally:
        os.unlink(tmp_in_path)

    root = tree.getroot()

    units = collect_units(root, params.layers)

    renamed = 0
    if not units:
        renamed = assign_ids_to_paths(root, params.layers)
        units = collect_units(root, params.layers)

    if not units:
        raise ValueError(
            f"No units found in layers {params.layers}. "
            "Check that the layer names match your SVG."
        )

    layers_found = {}
    for u in units:
        layers_found[u.layer] = layers_found.get(u.layer, 0) + 1

    existing_transform_str = detect_writing_transform(root)

    if params.rotation is not None:
        rotation_deg = params.rotation
    else:
        rotation_deg = detect_layout_rotation(root, params.layers)

    # Auto-detect thresholds if not provided
    t_small = params.threshold_small
    t_large = params.threshold_large
    if t_small is None or t_large is None:
        t_small, t_large = auto_detect_thresholds(units)

    writing_transform_str = build_writing_transform(existing_transform_str, rotation_deg)
    writing_matrix = AffineMatrix.from_svg_transform(writing_transform_str)
    writing_inverse = writing_matrix.inverse()

    placements = compute_placements(
        units, writing_inverse,
        t_small, t_large,
        params.font_small, params.font_medium, params.font_large,
    )

    font_counts = {}
    for p in placements:
        font_counts[p.font_size] = font_counts.get(p.font_size, 0) + 1

    renamed_doors, unmatched_doors = 0, 0
    if params.assign_door_ids:
        renamed_doors, unmatched_doors = assign_door_ids(
            root, params.door_layer, params.layers,
        )

    writing = create_writing_layer(placements, writing_transform_str)
    insert_writing_layer(root, writing)

    output_svg = etree.tostring(tree, xml_declaration=True, encoding='UTF-8', pretty_print=False)

    return ProcessResult(
        output_svg=output_svg,
        total_units=len(units),
        renamed_paths=renamed,
        renamed_doors=renamed_doors,
        unmatched_doors=unmatched_doors,
        rotation_deg=rotation_deg,
        threshold_small=t_small,
        threshold_large=t_large,
        font_counts=font_counts,
        layers_found=layers_found,
    )


# ---------------------------------------------------------------------------
# Main (CLI)
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='ID Assigner -- place ID labels on fair SVG units.',
    )
    parser.add_argument('input', help='Input SVG file path')
    parser.add_argument('-o', '--output', required=True, help='Output SVG file path')
    parser.add_argument(
        '--rotation', type=float, default=None,
        help='Writing layer rotation in degrees (e.g. -22). '
             'If omitted, auto-detects from unit edge angles.',
    )
    parser.add_argument(
        '--threshold-small', type=int, default=None,
        help=f'Max min_dim for small font. Default: auto-detect',
    )
    parser.add_argument(
        '--threshold-large', type=int, default=None,
        help=f'Max min_dim for medium font. Default: auto-detect',
    )
    parser.add_argument(
        '--font-small', type=int, default=DEFAULT_FONT_SMALL,
        help=f'Small font size in px. Default: {DEFAULT_FONT_SMALL}',
    )
    parser.add_argument(
        '--font-medium', type=int, default=DEFAULT_FONT_MEDIUM,
        help=f'Medium font size in px. Default: {DEFAULT_FONT_MEDIUM}',
    )
    parser.add_argument(
        '--font-large', type=int, default=DEFAULT_FONT_LARGE,
        help=f'Large font size in px. Default: {DEFAULT_FONT_LARGE}',
    )
    parser.add_argument(
        '--layers', nargs='+', default=['Stand', 'Food', 'Service'],
        help='Layer names to process. Default: Stand Food Service',
    )
    parser.add_argument(
        '--assign-ids', action='store_true',
        help='Auto-assign ID names to paths that lack ID prefix.',
    )
    parser.add_argument(
        '--assign-door-ids', action='store_true',
        help='Assign door IDs based on parent unit (e.g. ID310_1_, ID310_2_).',
    )
    parser.add_argument(
        '--door-layer', default='Doors',
        help='Door layer name. Default: Doors',
    )

    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  ID Assigner")
    print(f"{'='*60}")
    print(f"  Input:      {args.input}")
    print(f"  Output:     {args.output}")
    print(f"  Layers:     {args.layers}")
    print(f"  Assign IDs: {args.assign_ids}")
    ts_str = str(args.threshold_small) if args.threshold_small else 'auto'
    tl_str = str(args.threshold_large) if args.threshold_large else 'auto'
    print(f"  Fonts:      small={args.font_small}px (min_dim<={ts_str}), "
          f"medium={args.font_medium}px (<={tl_str}), "
          f"large={args.font_large}px (>{tl_str})")
    print()

    # Load
    print("[1/6] Loading SVG...")
    tree = load_svg(args.input)
    root = tree.getroot()

    # Auto-assign IDs if requested
    if args.assign_ids:
        print("[2/6] Assigning IDs (force re-assign)...")
        renamed = assign_ids_to_paths(root, args.layers)
        print(f"  Renamed {renamed} paths.")
    else:
        print("[2/6] ID re-assignment skipped.")

    # Collect units
    print("[3/6] Collecting units from layers...")
    units = collect_units(root, args.layers)

    if not units and not args.assign_ids:
        print("  No ID-prefixed units found. Auto-assigning IDs...")
        renamed = assign_ids_to_paths(root, args.layers)
        print(f"  Renamed {renamed} paths.")
        units = collect_units(root, args.layers)

    if not units:
        print("  ERROR: No units found. Check layer names.")
        sys.exit(1)
    print(f"  Total: {len(units)} units.")

    # Determine Writing layer transform
    print("[4/6] Computing transforms...")
    existing_transform_str = detect_writing_transform(root)

    if args.rotation is not None:
        rotation_deg = args.rotation
        print(f"  Rotation (from --rotation): {rotation_deg} deg")
    else:
        rotation_deg = detect_layout_rotation(root, args.layers)
        print(f"  Rotation (auto-detected from edges): {rotation_deg:.1f} deg")

    writing_transform_str = build_writing_transform(existing_transform_str, rotation_deg)
    print(f"  Writing layer transform: '{writing_transform_str}'")

    writing_matrix = AffineMatrix.from_svg_transform(writing_transform_str)
    writing_inverse = writing_matrix.inverse()

    # Auto-detect thresholds if needed
    t_small = args.threshold_small
    t_large = args.threshold_large
    if t_small is None or t_large is None:
        t_small, t_large = auto_detect_thresholds(units)
        print(f"  Auto-detected thresholds: small<={t_small}, medium<={t_large}")

    # Compute placements
    print("[5/6] Computing text placements...")
    placements = compute_placements(
        units, writing_inverse,
        t_small, t_large,
        args.font_small, args.font_medium, args.font_large,
    )

    font_counts = {}
    for p in placements:
        font_counts[p.font_size] = font_counts.get(p.font_size, 0) + 1
    for fs in sorted(font_counts):
        print(f"  Font {fs}px: {font_counts[fs]} units")

    # Assign door IDs if requested
    if args.assign_door_ids:
        print("[6/7] Assigning door IDs based on parent units...")
        renamed_doors, unmatched_doors = assign_door_ids(
            root, args.door_layer, args.layers,
        )
        print(f"  Renamed {renamed_doors} doors, {unmatched_doors} unmatched.")
    else:
        print("[6/7] Door ID assignment skipped (use --assign-door-ids to enable).")

    # Build and insert Writing layer
    print("[7/7] Creating Writing layer and saving...")
    writing = create_writing_layer(placements, writing_transform_str)
    insert_writing_layer(root, writing)

    save_svg(tree, args.output)
    print(f"\nDone! {len(placements)} text labels placed.")


if __name__ == '__main__':
    main()
