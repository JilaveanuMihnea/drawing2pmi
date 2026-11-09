"""Segment a classified drawing sheet into views and parse the title block.

Views are clusters of visible outline strokes: strokes that touch are joined,
then clusters nested inside another (holes, slots, engraved text) are merged
into it. The frame and title block are found first and excluded.

Each view gets a caption ("A", "C-C", "E" ...) and a scale when one is
printed under it, and a kind: orthographic, section, detail, or isometric.
"""
from collections import defaultdict
from dataclasses import dataclass, field
import math
import re

import numpy as np
from scipy.spatial import cKDTree

from classify import Arc, Line, classify
from pdf_extractor import extract

# visible strokes closer than this belong to the same cluster
TOUCH = 1.0
# a cluster is nested in another if its bbox is inside the other's grown by this
NEST_MARGIN = 2.0
# farthest a caption may sit below its view (dimensions sit in between)
MAX_CAPTION_GAP = 260.0
# views whose outline extents match within this share a projection row/column
ALIGN_TOL = 2.0
# axonometric views have (almost) no horizontal visible edges, and at least
# this share of their edge length along one oblique direction (30 deg, 150 deg ...)
MAX_ISO_HORIZONTAL = 0.05
MIN_ISO_PEAK = 0.10

CAPTION_RE = re.compile(r"^([A-Z])(?:-([A-Z]))?$")
SCALE_RE = re.compile(r"^(\d+(?:[.,]\d+)?):(\d+(?:[.,]\d+)?)$")

# ISO 2768-1 permissible deviations for linear sizes, used when the sheet
# names a grade but has no printed table: (above, up to, tolerance) in mm
ISO2768 = {
    "f": [(0.5, 3, 0.05), (3, 6, 0.05), (6, 30, 0.1), (30, 120, 0.15),
          (120, 400, 0.2), (400, 1000, 0.3), (1000, 2000, 0.5)],
    "m": [(0.5, 3, 0.1), (3, 6, 0.1), (6, 30, 0.2), (30, 120, 0.3),
          (120, 400, 0.5), (400, 1000, 0.8), (1000, 2000, 1.2), (2000, 4000, 2.0)],
    "c": [(0.5, 3, 0.2), (3, 6, 0.3), (6, 30, 0.5), (30, 120, 0.8),
          (120, 400, 1.2), (400, 1000, 2.0), (1000, 2000, 3.0), (2000, 4000, 4.0)],
    "v": [(3, 6, 0.5), (6, 30, 1.0), (30, 120, 1.5), (120, 400, 2.5),
          (400, 1000, 4.0), (1000, 2000, 6.0), (2000, 4000, 8.0)],
}

# title block label (lowercase, as printed) -> field name
TITLE_LABELS = {
    "scale": "scale", "scala": "scale",
    "material": "material", "materiale": "material",
    "description": "description", "denominazione": "description",
    "draw. nr": "drawing_number", "dis. n°": "drawing_number",
    "weight": "weight", "peso": "weight",
    "surface roughness": "roughness", "rugosita'": "roughness",
    "dimensions without tolerance": "general_tolerance",
    "quote senza indicazione di tolleranza": "general_tolerance",
    "grado di precisione": "general_tolerance",
    "thermic treatment": "heat_treatment", "trattamento termico": "heat_treatment",
    "surface treatment": "surface_treatment", "trattamento superficiale": "surface_treatment",
    "blank": "blank", "grezzo": "blank",
    "engineer": "drawn_by", "disegn.": "drawn_by",
    "checked": "checked_by", "controll.": "checked_by",
    "date": "date", "data": "date",
    "sheet": "sheet", "foglio": "sheet",
    "order": "order", "commessa": "order",
}


def _rect_of(pts):
    return np.r_[pts.min(axis=0), pts.max(axis=0)]


def _inside(r, outer, margin=0.0):
    return (r[0] >= outer[0] - margin and r[1] >= outer[1] - margin and
            r[2] <= outer[2] + margin and r[3] <= outer[3] + margin)


def _center(r):
    return np.array([(r[0] + r[2]) / 2, (r[1] + r[3]) / 2])


def _text_rect(t):
    b = t.bbox
    return np.array([b.x0, b.y0, b.x1, b.y1])


def _prim_pts(p, step=2.0):
    if isinstance(p, Arc):
        return p.sample()
    n = max(2, int(p.length / step) + 1)
    return p.p0 + np.linspace(0, 1, n)[:, None] * (p.p1 - p.p0)


@dataclass
class View:
    id: int
    rect: np.ndarray            # x0, y0, x1, y1 of the visible outline
    strokes: list               # all strokes inside: visible, hidden, center, hatch...
    kind: str = "orthographic"  # orthographic, section, detail, isometric
    label: str | None = None    # "A", "C-C", "E" ...
    scale: float | None = None  # drawing units per model unit; None = sheet scale
    caption: list = field(default_factory=list)     # TextSpans of the caption
    # ids of views projected from this one: same vertical extent (row) or
    # same horizontal extent (column) of the outline
    row: list = field(default_factory=list)
    column: list = field(default_factory=list)


@dataclass
class TitleBlock:
    rect: np.ndarray
    fields: dict
    scale: float | None
    projection: str | None              # first_angle, third_angle
    tolerance_grade: str | None         # ISO 2768 grade letter
    tolerance_table: list               # (above, up to, +-tol) in mm


@dataclass
class Sheet:
    frame: np.ndarray                   # inner border rect
    title_block: TitleBlock | None
    views: list[View]
    markers: list                       # section/view letters that are not captions


# ---------------------------------------------------------------- frame

def find_frame(cl, page_w, page_h):
    """Inner border rectangle from the long frame lines."""
    xs, ys = [], []
    for s in cl.strokes:
        if s.role != "frame":
            continue
        for p in s.prims:
            if not isinstance(p, Line) or p.length < 0.3 * min(page_w, page_h):
                continue
            d = p.p1 - p.p0
            if abs(d[0]) < 0.5:
                xs.append(p.p0[0])
            elif abs(d[1]) < 0.5:
                ys.append(p.p0[1])
    xs, ys = np.unique(np.round(xs, 1)), np.unique(np.round(ys, 1))
    if len(xs) < 2 or len(ys) < 2:
        return np.array([0, 0, page_w, page_h])
    # with a double border the inner lines are the second from each side
    k = 1 if len(xs) >= 4 else 0
    m = 1 if len(ys) >= 4 else 0
    return np.array([xs[k], ys[m], xs[-1 - k], ys[-1 - m]])


# ---------------------------------------------------------------- clusters

def _clusters(strokes):
    """Group strokes whose geometry comes within TOUCH of each other."""
    pts, owner = [], []
    for i, s in enumerate(strokes):
        for p in s.prims:
            q = _prim_pts(p)
            pts.append(q)
            owner += [i] * len(q)
    if not pts:
        return []
    pts = np.vstack(pts)
    owner = np.array(owner)

    parent = list(range(len(strokes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in cKDTree(pts).query_pairs(TOUCH):
        ra, rb = find(owner[a]), find(owner[b])
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    groups = defaultdict(list)
    for i in range(len(strokes)):
        groups[find(i)].append(strokes[i])
    return list(groups.values())


def _merge_nested(clusters):
    """Merge clusters whose bbox lies inside a larger cluster's bbox."""
    items = [(_rect_of(np.vstack([s.pts for s in c])), c) for c in clusters]
    items.sort(key=lambda it: -(it[0][2] - it[0][0]) * (it[0][3] - it[0][1]))
    merged = []
    for r, c in items:
        for m in merged:
            if _inside(r, m[0], NEST_MARGIN):
                m[1].extend(c)
                break
        else:
            merged.append([r, list(c)])
    return merged


def _directions(strokes, bin_deg=3):
    """Share of visible straight edge length per direction bin (degrees, 0-180)."""
    hist = defaultdict(float)
    for s in strokes:
        if s.role != "visible":
            continue
        for p in s.prims:
            if isinstance(p, Line):
                d = p.p1 - p.p0
                a = round(math.degrees(math.atan2(d[1], d[0])) % 180 / bin_deg) * bin_deg % 180
                hist[a] += p.length
    total = sum(hist.values())
    return {a: l / total for a, l in hist.items()} if total else {}


def _is_axonometric(strokes):
    """Orthographic views of a part show horizontal edges; isometric ones don't."""
    h = _directions(strokes)
    if not h:
        return False
    oblique = max((v for a, v in h.items() if a not in (0, 90)), default=0)
    return h.get(0, 0) < MAX_ISO_HORIZONTAL and oblique >= MIN_ISO_PEAK


# ---------------------------------------------------------------- title block

def find_title_block(cl, frame, views_rects):
    """The title block is the outline cluster touching the frame's bottom-right corner."""
    best = None
    for r, _ in views_rects:
        touches_right = abs(r[2] - frame[2]) < 2
        touches_bottom = abs(r[3] - frame[3]) < 2
        if touches_right and touches_bottom:
            if best is None or (r[2] - r[0]) * (r[3] - r[1]) > (best[2] - best[0]) * (best[3] - best[1]):
                best = r
    return best


def _cell_of(pt, hlines, vlines):
    """Smallest grid cell around pt, from axis-aligned title block lines."""
    x, y = pt
    left = max((l[0] for l in vlines if l[0] <= x and l[1] <= y <= l[2]), default=-math.inf)
    right = min((l[0] for l in vlines if l[0] >= x and l[1] <= y <= l[2]), default=math.inf)
    top = max((l[0] for l in hlines if l[0] <= y and l[1] <= x <= l[2]), default=-math.inf)
    bottom = min((l[0] for l in hlines if l[0] >= y and l[1] <= x <= l[2]), default=math.inf)
    return (left, top, right, bottom)


def _grid_lines(cl, rect):
    """Horizontal and vertical lines inside rect as (pos, from, to)."""
    hl, vl = [], []
    for s in cl.strokes:
        for p in s.prims:
            if not isinstance(p, Line):
                continue
            pr = _rect_of(np.array([p.p0, p.p1]))
            if not _inside(pr, rect, 1.0):
                continue
            if pr[3] - pr[1] < 0.5:
                hl.append((pr[1], pr[0], pr[2]))
            elif pr[2] - pr[0] < 0.5:
                vl.append((pr[0], pr[1], pr[3]))
    return hl, vl


def _parse_scale(text):
    m = SCALE_RE.match(text.strip())
    if not m:
        return None
    a, b = (float(g.replace(",", ".")) for g in m.groups())
    return a / b


def _parse_tol(text):
    m = re.search(r"±\s*(\d+(?:[.,]\d+)?)", text)
    return float(m.group(1).replace(",", ".")) if m else None


def _tolerance_table(texts, grade):
    """Read the printed general tolerance table row for `grade`, if any.

    The table has a header row of size ranges ("> 3", "≤ 6" ...) and one row
    per grade, each starting with the bold grade letter.
    """
    letter = next((t for t in texts if t.text.strip() == grade and "Bold" in t.font), None)
    if letter is None:
        return None
    lr = _text_rect(letter)
    row = [t for t in texts
           if _parse_tol(t.text) is not None and abs(_center(_text_rect(t))[1] - _center(lr)[1]) < 4]
    if not row:
        return None
    # upper limits of each column: the "≤ N" pairs sit right under the "> M" headers
    uppers = []
    for t in texts:
        s = t.text.strip()
        if re.fullmatch(r"\d+(?:[.,]\d+)?", s) and any(
                u.text.strip() == "≤" and abs(_text_rect(u)[2] - _text_rect(t)[0]) < 2 and
                abs(_center(_text_rect(u))[1] - _center(_text_rect(t))[1]) < 4 for u in texts):
            uppers.append((_center(_text_rect(t))[0], float(s.replace(",", "."))))
    if not uppers:
        return None
    uppers.sort()
    table = []
    for t in sorted(row, key=lambda t: _center(_text_rect(t))[0]):
        cx = _center(_text_rect(t))[0]
        k = min(range(len(uppers)), key=lambda i: abs(uppers[i][0] - cx))
        above = uppers[k - 1][1] if k > 0 else 0.5
        table.append((above, uppers[k][1], _parse_tol(t.text)))
    return table


def _projection(cl, rect):
    """First or third angle, from the projection symbol in the title block.

    The symbol is a truncated cone drawn as a trapezoid next to its end view
    (two concentric circles). In first angle projection the end view is on
    the side opposite the end it shows: circles right of the trapezoid.
    """
    circles = [(p.center, p.radius) for s in cl.strokes for p in s.prims
               if isinstance(p, Arc) and p.is_circle and _inside(_rect_of(p.sample()), rect)]
    for c, r in circles:
        if not any(np.linalg.norm(c2 - c) < 0.5 and abs(r2 - r) > 0.5 for c2, r2 in circles):
            continue
        # trapezoid: straight lines just beside the circles
        near = [p for s in cl.strokes for p in s.prims if isinstance(p, Line)
                and abs((p.p0[1] + p.p1[1]) / 2 - c[1]) < 2 * r
                and 0 < abs((p.p0[0] + p.p1[0]) / 2 - c[0]) < 6 * r
                and p.length < 4 * r]
        if not near:
            continue
        side = np.mean([(p.p0[0] + p.p1[0]) / 2 - c[0] for p in near])
        return "first_angle" if side < 0 else "third_angle"
    return None


def parse_title_block(cl, texts, rect, tol_rect=None):
    hl, vl = _grid_lines(cl, rect)
    inside = [t for t in texts if _inside(_text_rect(t), rect, 2.0)]

    def is_label(t):
        return t.font.startswith("Helvetica") and t.size < 8.5

    # each value goes to the label in the same grid cell
    cells = defaultdict(lambda: {"labels": [], "values": []})
    for t in inside:
        cell = _cell_of(_center(_text_rect(t)), hl, vl)
        cells[cell]["labels" if is_label(t) else "values"].append(t)

    fields = {}
    for key, cell in cells.items():
        names = {TITLE_LABELS.get(t.text.strip().lower()) for t in cell["labels"]} - {None}
        if len(names) != 1:
            continue
        vals = list(cell["values"])
        if not vals:
            # label in its own header strip: take the value-only cells stacked below it
            left, _, right, bottom = key
            while True:
                below = next((k for k, c in cells.items() if k[0] == left and k[2] == right
                              and k[1] == bottom and c["values"] and not c["labels"]), None)
                if below is None:
                    break
                vals += cells[below]["values"]
                bottom = below[3]
        if not vals:
            continue
        vals.sort(key=lambda t: (round(t.bbox.y0), t.bbox.x0))
        fields[names.pop()] = " ".join(t.text.strip() for t in vals)

    scale = _parse_scale(fields.get("scale", ""))

    grade = None
    gt = fields.get("general_tolerance", "")
    m = re.search(r"\b([fmcv])\b", gt)
    if m:
        grade = m.group(1)
    table = None
    if grade:
        table = _tolerance_table(texts if tol_rect is None else
                                 [t for t in texts if _inside(_text_rect(t), tol_rect, 2.0)], grade)
        table = table or ISO2768.get(grade)

    return TitleBlock(rect, fields, scale, _projection(cl, rect), grade, table or [])


# ---------------------------------------------------------------- captions

def _captions(texts, views):
    """Attach caption labels (and scales printed under them) to views."""
    cands = []
    for t in texts:
        m = CAPTION_RE.match(t.text.strip())
        if m and abs(t.angle) < 1 and t.size > 15:
            cands.append(t)

    used = set()
    for t in sorted(cands, key=lambda t: t.bbox.y0):
        r = _text_rect(t)
        c = _center(r)
        best, gap_best = None, MAX_CAPTION_GAP
        for v in views:
            if not (v.rect[0] <= c[0] <= v.rect[2]):
                continue
            gap = r[1] - v.rect[3]
            if 0 < gap < gap_best and v.label is None:
                best, gap_best = v, gap
        if best is None:
            continue
        # a caption is unique; a lone letter is a marker unless nothing else claims the view
        best.label = t.text.strip()
        best.caption = [t]
        used.add(id(t))
        for s in texts:
            sr = _text_rect(s)
            # the scale line's box may overlap the caption's by a point or two
            if (_parse_scale(s.text) and -0.25 * t.size <= sr[1] - r[3] < 1.5 * t.size and
                    abs(_center(sr)[0] - c[0]) < 2 * t.size):
                best.scale = _parse_scale(s.text)
                best.caption.append(s)
    return [t for t in cands if id(t) not in used]


def _kind(v, markers):
    if v.label and "-" in v.label:
        return "section"
    if v.label and v.scale is not None:
        return "detail"
    if _is_axonometric(v.strokes):
        return "isometric"
    if any(s.role == "hatch" for s in v.strokes):
        return "section"
    return "orthographic"


# ---------------------------------------------------------------- main

def segment(pg, cl=None):
    cl = cl or classify(pg)
    frame = find_frame(cl, pg.width, pg.height)

    outline = [s for s in cl.strokes if s.role in ("visible", "title_block")
               and _inside(_rect_of(s.pts), frame, 1.0)]
    groups = _merge_nested(_clusters(outline))

    tb_rect = find_title_block(cl, frame, groups)
    # tables stacked on top of the title block (general tolerances) belong to it
    tol_rect = None
    if tb_rect is not None:
        for r, _ in groups:
            if r is not tb_rect and abs(r[2] - frame[2]) < 2 and abs(r[3] - tb_rect[1]) < 2:
                tol_rect = r

    views = []
    for r, members in groups:
        if tb_rect is not None and (r is tb_rect or r is tol_rect or _inside(r, tb_rect, 2)):
            continue
        # border ticks and other strays touching the frame
        if (r[0] - frame[0] < 2 or frame[2] - r[2] < 2 or
                r[1] - frame[1] < 2 or frame[3] - r[3] < 2):
            continue
        if max(r[2] - r[0], r[3] - r[1]) < 5:
            continue
        views.append(View(len(views), r, []))

    # every stroke goes to the smallest view containing it
    for s in cl.strokes:
        sr = _rect_of(s.pts)
        host = [v for v in views if _inside(sr, v.rect, NEST_MARGIN)]
        if host:
            min(host, key=lambda v: (v.rect[2] - v.rect[0]) * (v.rect[3] - v.rect[1])).strokes.append(s)

    title = parse_title_block(cl, pg.texts, tb_rect, tol_rect) if tb_rect is not None else None
    markers = _captions(pg.texts, views)
    for v in views:
        v.kind = _kind(v, markers)
    for v in views:
        if v.kind == "isometric":
            continue
        for w in views:
            if w is v or w.kind == "isometric" or (v.scale or 0) != (w.scale or 0):
                continue
            if abs(v.rect[1] - w.rect[1]) < ALIGN_TOL and abs(v.rect[3] - w.rect[3]) < ALIGN_TOL:
                v.row.append(w.id)
            if abs(v.rect[0] - w.rect[0]) < ALIGN_TOL and abs(v.rect[2] - w.rect[2]) < ALIGN_TOL:
                v.column.append(w.id)
    return Sheet(frame, title, views, markers)


def summary(sh):
    print("frame:", np.round(sh.frame, 1))
    tb = sh.title_block
    if tb:
        print("title block:", np.round(tb.rect, 1))
        for k, v in sorted(tb.fields.items()):
            print(f"  {k:18} {v}")
        print("  scale:", tb.scale, " projection:", tb.projection,
              " general tolerance grade:", tb.tolerance_grade)
        print("  tolerance table:", tb.tolerance_table)
    print(f"\n{len(sh.views)} views:")
    for v in sh.views:
        roles = defaultdict(int)
        for s in v.strokes:
            roles[s.role] += 1
        print(f"  {v.id:2d} {v.kind:12} label={v.label!s:5} scale={v.scale!s:5} "
              f"rect={np.round(v.rect).astype(int).tolist()} row={v.row} col={v.column}")
    print("\nmarkers:", [(t.text.strip(), tuple(np.round(_center(_text_rect(t))).astype(int)))
                         for t in sh.markers])


if __name__ == "__main__":
    import sys
    summary(segment(extract(sys.argv[1])))
