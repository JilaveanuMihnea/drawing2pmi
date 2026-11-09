"""Parse ordinate (baseline) dimensions and fit each view's scale and origin.

An ordinate dimension is a thin axis-aligned extension line running from a
feature out to a baseline outside the view, with an unsigned value printed
just past its outer end along the same direction. Each view has one origin
per axis: a larger "0" next to a small filled circle at the end of the
origin line. A line may carry a value at both ends; then the feature is
where the line touches the outline in between (a tangent or collinear edge).

Value text  ->  extension line: one-to-one assignment (Hungarian) on how far
the line's outer end is from the text, so crowded labels do not swap.
Coordinates  ->  per view and axis, fit  pos = origin + k * signed value;
k is the view's scale in points per mm, and large residuals flag parse errors.
"""
from collections import defaultdict
from dataclasses import dataclass, field
import math
import re

import numpy as np
from scipy.optimize import linear_sum_assignment

from classify import Arc, Line, classify
from pdf_extractor import extract
from textgroups import build_groups
from views import segment

PT_PER_MM = 72 / 25.4

VALUE_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
TOL_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")
# spans just before a number that make it something other than an ordinate
PREFIXES = ("R", "Ø", "M", "⌀", "SR", "S")

# a text is axis-aligned if its baseline is within this many degrees
ANGLE_TOL = 0.5
# largest gap between a value text and the outer end of its extension line
MAX_TEXT_GAP = 10.0
# the line may run beside the text box by this much
MAX_SIDE_OFFSET = 5.0
# geometry closer than this to an extension line is on it
ON_LINE = 0.5
# an anchor at the inner end of a single-value line may sit this far from the
# end along the line (extension lines stop short of the feature)
END_ANCHOR = 4.0
# longest step in a stepped (jogged) extension line
MAX_JOG = 30.0
# outline roles an ordinate may point at; ordinates often go to corners of
# hidden or phantom contours or to hole centers, not only to visible edges
ANCHOR_ROLES = ("visible", "hidden", "thin_medium", "phantom", "center")
ANCHOR_RANK = {"endpoint": 0, "tangent": 1, "collinear": 2, "center": 3, "crossing": 4}
# residual (pt) above which a dimension is flagged as an outlier of its view fit
MAX_RESIDUAL = 1.0


@dataclass
class Origin:
    view: int
    axis: str               # "x": vertical origin line, measures along page x
    pos: float              # page coordinate of the origin line
    marker: np.ndarray      # center of the circle marker
    text: object


@dataclass
class Ordinate:
    view: int | None
    axis: str               # "x" or "y", as for Origin
    value: float            # mm, unsigned as printed
    text: object
    pos: float              # page coordinate of the extension line
    outer: np.ndarray       # line end next to the text
    inner: np.ndarray       # other end
    two_sided: bool         # a value at both ends of the line
    basic: bool = False     # boxed: theoretically exact, toleranced by GD&T
    tol: tuple | None = None            # (upper, lower) in mm
    anchors: list = field(default_factory=list)  # (point, kind) on the outline
    side: int = 0           # +1 / -1: page side of the origin
    residual: float | None = None
    outlier: bool = False

    @property
    def anchor(self):
        return self.anchors[0][0] if self.anchors else None


@dataclass
class AxisFit:
    view: int
    axis: str
    origin: float | None    # fitted origin position (page)
    k: float | None         # points per mm on the page
    expected_k: float       # from the view or sheet scale
    n: int
    rms: float | None


def _num(s):
    return float(s.replace(",", "."))


def _rect(t):
    b = t.bbox
    return np.array([b.x0, b.y0, b.x1, b.y1])


def _axis_of_text(t):
    """'x' for text running vertically (measures x), 'y' for horizontal text."""
    a = t.angle % 180
    if min(a, 180 - a) < ANGLE_TOL:
        return "y"
    if abs(a - 90) < ANGLE_TOL:
        return "x"
    return None


def _axis_lines(cl, roles=("thin", "center")):
    """Axis-aligned straight pieces of thin strokes: (axis, pos, p0, p1, stroke, index)."""
    out = []
    for s in cl.strokes:
        if s.role not in roles:
            continue
        for n, p in enumerate(s.prims):
            a = _line_axis(p)
            if a == "x":
                out.append(("x", (p.p0[0] + p.p1[0]) / 2, p.p0, p.p1, s, n))
            elif a == "y":
                out.append(("y", (p.p0[1] + p.p1[1]) / 2, p.p0, p.p1, s, n))
    return out


def _line_axis(p):
    """'x' for a vertical line (it measures x), 'y' for horizontal, else None."""
    if not isinstance(p, Line):
        return None
    d = p.p1 - p.p0
    if abs(d[0]) < 0.3 and abs(d[1]) > 1:
        return "x"
    if abs(d[1]) < 0.3 and abs(d[0]) > 1:
        return "y"
    return None


def _follow_jog(stroke, n, inner, axis):
    """Follow an extension line through jogs to the segment at the feature.

    Crowded ordinate labels are spread out by stepping the extension line:
    a short perpendicular (or oblique) piece joins two parallel pieces that
    both run inward. A long perpendicular piece is instead an L-shaped pair of
    ordinates sharing the feature corner, so it ends the walk.
    Returns (pos, inner end).
    """
    j = 1 if axis == "x" else 0
    while True:
        nxt = None
        for m in (n - 1, n + 1):
            if 0 <= m < len(stroke.prims):
                p = stroke.prims[m]
                if isinstance(p, Line) and min(np.linalg.norm(p.p0 - inner),
                                               np.linalg.norm(p.p1 - inner)) < 0.1:
                    nxt = m
        if nxt is None or _line_axis(stroke.prims[nxt]) == axis:
            break
        jog = stroke.prims[nxt]
        if jog.length > MAX_JOG:
            break
        jog_end = jog.p1 if np.linalg.norm(jog.p0 - inner) < 0.1 else jog.p0
        after = nxt + (nxt - n)
        if not (0 <= after < len(stroke.prims)) or _line_axis(stroke.prims[after]) != axis:
            break
        p = stroke.prims[after]
        if min(np.linalg.norm(p.p0 - jog_end), np.linalg.norm(p.p1 - jog_end)) > 0.1:
            break
        new_inner = p.p1 if np.linalg.norm(p.p0 - jog_end) < 0.1 else p.p0
        # keep going in the same direction along the line
        cur = stroke.prims[n]
        cur_outer = cur.p1 if np.linalg.norm(cur.p0 - inner) < 0.1 else cur.p0
        if np.sign(inner[j] - cur_outer[j]) != np.sign(new_inner[j] - jog_end[j]):
            break
        inner = new_inner
        n = after
    i = 0 if axis == "x" else 1
    return float(inner[i]), inner.copy()


def _in_frame(t, lines):
    """True if thin lines box the text in on all four sides (a GD&T frame cell)."""
    r = _rect(t)
    pad = 6.0
    sides = [False] * 4
    for a, pos, p0, p1, *_ in lines:
        lo, hi = sorted((p0[1 - (a == "y")], p1[1 - (a == "y")]))
        if a == "x" and lo <= r[1] + 1 and hi >= r[3] - 1:
            if r[0] - pad <= pos <= r[0] + 0.5:
                sides[0] = True
            if r[2] - 0.5 <= pos <= r[2] + pad:
                sides[2] = True
        if a == "y" and lo <= r[0] + 1 and hi >= r[2] - 1:
            if r[1] - pad <= pos <= r[1] + 0.5:
                sides[1] = True
            if r[3] - 0.5 <= pos <= r[3] + pad:
                sides[3] = True
    return all(sides)


def _gap_to_box(end, inner, r, axis):
    """(along-gap, side offset) of a line end to a text box.

    The text must lie beyond the end, continuing the line's direction.
    """
    j = 1 if axis == "x" else 0          # along-line coordinate index
    i = 1 - j
    direction = np.sign(end[j] - inner[j])
    lo, hi = (r[1], r[3]) if j == 1 else (r[0], r[2])
    along = (lo - end[j]) if direction > 0 else (end[j] - hi)
    plo, phi = (r[0], r[2]) if i == 0 else (r[1], r[3])
    side = max(plo - end[i], end[i] - phi, 0.0)
    return along, side


def _has_prefix(t, texts):
    """True if a diameter/radius/thread symbol sits right before t on its baseline."""
    r = _rect(t)
    h = t.size
    for u in texts:
        if u is t or u.text.strip() not in PREFIXES:
            continue
        ur = _rect(u)
        if abs(u.angle - t.angle) > 2:
            continue
        # "before" along the reading direction
        d = (np.array([(r[0] + r[2]) / 2, (r[1] + r[3]) / 2]) -
             np.array([(ur[0] + ur[2]) / 2, (ur[1] + ur[3]) / 2]))
        if np.linalg.norm(d) < 2.5 * h and float(d @ t.dir) > 0:
            return True
    return False


def _arrow_on(a, b, axis, cl, tol=1.0, at_end=False):
    """True if an arrowhead pointing along a -> b has its tip on segment a-b
    (with at_end, at its end b: a short dimension with its arrows outside the
    extension lines points out, onto the value).

    Arrows of another dimension may end on an extension line from the side;
    those point across the line and do not count.
    """
    j = 1 if axis == "x" else 0
    lo, hi = sorted((a[j], b[j]))
    if at_end:
        lo, hi = b[j], b[j]
    inward = np.sign(b[j] - a[j])
    for h in cl.arrowheads:
        # only arrows pointing inward (away from the text) make a dimension
        # line; a leader arrow ending on the line points back at the text
        if abs(h.dir[j]) < 0.9 or np.sign(h.dir[j]) != inward:
            continue
        if abs(h.tip[1 - j] - a[1 - j]) < tol and lo - tol <= h.tip[j] <= hi + tol:
            return True
    return False


# ---------------------------------------------------------------- origins

def find_origins(pg, cl, sheet):
    dots = [f for f in cl.filled if f.kind == "dot"]
    lines = _axis_lines(cl)
    origins = []
    for t in pg.texts:
        if t.text.strip() != "0":
            continue
        axis = _axis_of_text(t)
        if axis is None:
            continue
        c = np.array([(t.bbox.x0 + t.bbox.x1) / 2, (t.bbox.y0 + t.bbox.y1) / 2])
        if not dots:
            continue
        dot = min(dots, key=lambda f: np.linalg.norm(f.pts.mean(axis=0) - c))
        m = dot.pts.mean(axis=0)
        r = np.linalg.norm(dot.pts - m, axis=1).max()
        if np.linalg.norm(m - c) > 3 * t.size:
            continue
        # the origin line starts at the marker
        # the origin line starts a little outside the marker
        attached = [(a, p0, p1) for a, _, p0, p1, *_ in lines if a == axis and
                    min(np.linalg.norm(p0 - m), np.linalg.norm(p1 - m)) < r + 4.0]
        if not attached:
            continue
        a, p0, p1 = attached[0]
        near, far = (p0, p1) if np.linalg.norm(p0 - m) < np.linalg.norm(p1 - m) else (p1, p0)
        pos = m[0] if axis == "x" else m[1]
        v = _view_along(sheet, axis, pos, near, far - near)
        if v is not None and not any(o.view == v.id and o.axis == axis and abs(o.pos - pos) < 1
                                     for o in origins):
            origins.append(Origin(v.id, axis, pos, m, t))
    return origins


def _view_along(sheet, axis, pos, start, direction, margin=2.0):
    """First view hit by a ray from start, among views spanning pos across the ray."""
    j = 1 if axis == "x" else 0
    sgn = np.sign(direction[j])
    best, dist = None, math.inf
    for v in sheet.views:
        if v.kind == "isometric":
            continue
        lo, hi = (v.rect[0], v.rect[2]) if axis == "x" else (v.rect[1], v.rect[3])
        if not (lo - margin <= pos <= hi + margin):
            continue
        near_edge, far_edge = (v.rect[1], v.rect[3]) if j == 1 else (v.rect[0], v.rect[2])
        d = (near_edge - start[j]) if sgn > 0 else (start[j] - far_edge)
        if sgn > 0 and start[j] > far_edge or sgn < 0 and start[j] < near_edge:
            continue
        d = max(d, 0.0)
        if d < dist:
            best, dist = v, d
    return best


# ---------------------------------------------------------------- values

def find_ordinates(pg, cl, sheet, groups=None):
    """Ordinates from text groups that are a lone number (plus stacked deviations).

    Grouping first keeps out numbers that belong to longer labels: "R 11",
    "Ø 5,5 H7", "5,5 ±0,1" and the cells of GD&T frame rows.
    """
    groups = groups if groups is not None else build_groups(pg.texts)
    lines = _axis_lines(cl, roles=("thin",))
    texts = [g.spans[0] for g in groups if len(g.spans) == 1]
    big = max((t.size for t in texts), default=0)

    cands = []
    for t in texts:
        if not VALUE_RE.match(t.text.strip()):
            continue
        axis = _axis_of_text(t)
        if axis is None or t.text.strip() == "0" and t.size >= 0.9 * big:
            continue
        if _has_prefix(t, pg.texts):
            continue
        cands.append((t, axis))

    # a lone boxed number is a basic (theoretically exact) dimension; GD&T
    # frame cells never reach here because their rows group into one label
    boxed = {id(t) for t, _ in cands if _in_frame(t, lines)}

    # cost matrix: text x line end
    ends = []
    for a, pos, p0, p1, st, n in lines:
        ends.append((a, pos, p0, p1, st, n))      # p0 is the outer end
        ends.append((a, pos, p1, p0, st, n))
    INF = 1e6
    cost = np.full((len(cands), len(ends)), INF)
    for i, (t, axis) in enumerate(cands):
        r = _rect(t)
        for k, (a, pos, outer, inner, _, _) in enumerate(ends):
            if a != axis:
                continue
            along, side = _gap_to_box(outer, inner, r, axis)
            if -1.0 <= along <= MAX_TEXT_GAP and side <= MAX_SIDE_OFFSET:
                cost[i, k] = abs(along - 5.5) + 2 * side
    rows, cols = linear_sum_assignment(cost)

    dims = []
    used_text = {}
    for i, k in zip(rows, cols):
        if cost[i, k] >= INF:
            continue
        t, axis = cands[i]
        a, pos, outer, inner, st, n = ends[k]
        pos, inner = _follow_jog(st, n, inner, axis)
        if _arrow_on(outer, inner, axis, cl) or _arrow_on(inner, outer, axis, cl, at_end=True):
            continue        # a dimension line with arrows: linear, not ordinate
        d = Ordinate(None, axis, _num(t.text.strip()), t, float(pos),
                     outer.copy(), inner, False)
        d.tol = _stacked_tol(t, pg.texts)
        d.basic = id(t) in boxed
        dims.append(d)
        used_text[id(t)] = d

    # lines with a value at both ends
    by_line = defaultdict(list)
    for d in dims:
        key = (d.axis, round(d.pos, 1),
               tuple(np.round(np.sort(np.r_[d.outer, d.inner].reshape(2, 2), axis=0), 1).ravel()))
        by_line[key].append(d)
    for ds in by_line.values():
        if len(ds) > 1:
            for d in ds:
                d.two_sided = True

    for d in dims:
        v = _view_along(sheet, d.axis, d.pos, d.outer, d.inner - d.outer)
        d.view = v.id if v else None
        if v:
            d.anchors = _anchors(d, v)
    return dims


def _stacked_tol(t, texts):
    """Upper/lower deviation spans printed small, right after the value."""
    r = _rect(t)
    small = [u for u in texts if u is not t and u.size < 0.7 * t.size
             and TOL_RE.match(u.text.strip()) and abs(u.angle - t.angle) < 2]
    after = []
    for u in small:
        ur = _rect(u)
        d = np.array([(ur[0] + ur[2]) / 2 - (r[0] + r[2]) / 2, (ur[1] + ur[3]) / 2 - (r[1] + r[3]) / 2])
        along = float(d @ t.dir)
        if 0 < along < 1.5 * t.size + (r[2] - r[0] + r[3] - r[1]) / 2 and np.linalg.norm(d) < 2.5 * t.size:
            after.append(u)
    if not after:
        return None
    # upper value is printed above the lower one (perpendicular to reading direction)
    up = np.array([t.dir[1], -t.dir[0]])     # page "up" relative to the text
    after.sort(key=lambda u: -float(np.array([(u.bbox.x0 + u.bbox.x1) / 2,
                                              (u.bbox.y0 + u.bbox.y1) / 2]) @ up))
    vals = [_num(u.text.strip()) for u in after[:2]]
    if len(vals) == 1:
        return (vals[0], -vals[0]) if vals[0] > 0 else (0.0, vals[0])
    return (max(vals), min(vals))


# ---------------------------------------------------------------- anchors

def _anchors(d, view):
    """Points where the extension line meets the view's outline geometry.

    Returns (point, kind, role) sorted best first. For a line with one value
    the feature is at its inner end; for a line with values at both ends it is
    wherever the line touches the outline (tangent or collinear edge).
    """
    j = 1 if d.axis == "x" else 0      # along-line coordinate
    i = 1 - j
    lo, hi = sorted((d.outer[j], d.inner[j]))
    lo, hi = lo - END_ANCHOR, hi + END_ANCHOR
    pos = d.pos
    out = []

    def add(pt, kind, role):
        if abs(pt[i] - pos) < ON_LINE and lo <= pt[j] <= hi:
            q = np.array(pt, dtype=float)
            q[i] = pos
            out.append((q, kind, role))

    for s in view.strokes:
        if s.role not in ANCHOR_ROLES:
            continue
        for p in s.prims:
            if isinstance(p, Line):
                if abs(p.p0[i] - pos) < ON_LINE and abs(p.p1[i] - pos) < ON_LINE:
                    add((p.p0 + p.p1) / 2, "collinear", s.role)
                    continue
                for e in (p.p0, p.p1):
                    add(e, "endpoint", s.role)
                # crossing
                a, b = p.p0[i] - pos, p.p1[i] - pos
                if a * b < 0:
                    t = a / (a - b)
                    add(p.p0 + t * (p.p1 - p.p0), "crossing", s.role)
                continue
            # arc
            for e in p.sample(2):
                add(e, "endpoint", s.role)
            add(p.center, "center", s.role)
            for sgn in (-1, 1):
                if abs(p.center[i] + sgn * p.radius - pos) < ON_LINE:
                    ang = (0.0 if sgn > 0 else math.pi) if i == 0 else \
                          (math.pi / 2 if sgn > 0 else -math.pi / 2)
                    if _angle_in(ang, p.start, p.sweep):
                        pt = p.center.copy()
                        pt[i] += sgn * p.radius
                        add(pt, "tangent", s.role)
            off = pos - p.center[i]
            if abs(off) < p.radius - ON_LINE:
                h = math.sqrt(p.radius ** 2 - off ** 2)
                for sgn in (-1, 1):
                    pt = p.center.copy()
                    pt[i] = pos
                    pt[j] += sgn * h
                    v = pt - p.center
                    if _angle_in(math.atan2(v[1], v[0]), p.start, p.sweep):
                        add(pt, "crossing", s.role)

    if not out:
        return []
    if not d.two_sided:
        near = [a for a in out if abs(a[0][j] - d.inner[j]) < END_ANCHOR]
        return sorted(near, key=lambda a: (ANCHOR_RANK[a[1]], abs(a[0][j] - d.inner[j])))
    touch = [a for a in out if a[1] in ("tangent", "collinear")]
    return sorted(touch or out, key=lambda a: ANCHOR_RANK[a[1]])


def _angle_in(a, start, sweep):
    if abs(sweep) >= 2 * math.pi - 1e-6:
        return True
    rel = (a - start) % (2 * math.pi) if sweep > 0 else (start - a) % (2 * math.pi)
    return rel <= abs(sweep) + 1e-6


# ---------------------------------------------------------------- fit

def fit_axes(sheet, origins, dims):
    """Fit origin and points-per-mm for each view and axis."""
    fits = []
    groups = defaultdict(list)
    for d in dims:
        if d.view is not None:
            groups[(d.view, d.axis)].append(d)
    sheet_scale = sheet.title_block.scale if sheet.title_block and sheet.title_block.scale else 1.0
    views = {v.id: v for v in sheet.views}

    for (vid, axis), ds in sorted(groups.items()):
        v = views[vid]
        expected = (v.scale or sheet_scale) * PT_PER_MM
        o0 = next((o.pos for o in origins if o.view == vid and o.axis == axis), None)
        if o0 is None:
            # no origin marker (e.g. a detail view measured from its parent's
            # origin): fit pos = a + b * value, assuming all on one side
            vals = [d for d in ds if d.value > 0]
            if len(vals) < 2:
                fits.append(AxisFit(vid, axis, None, None, expected, len(ds), None))
                continue
            A = np.column_stack([np.ones(len(vals)), [d.value for d in vals]])
            (a, b), *_ = np.linalg.lstsq(A, np.array([d.pos for d in vals]), rcond=None)
            o0 = a      # virtual origin, then fit as usual

        for d in ds:
            d.side = int(np.sign(d.pos - o0)) if abs(d.pos - o0) > ON_LINE else 0
        # robust start from the marker origin and the median ratio, then refit on inliers
        o = o0
        ratios = [abs(d.pos - o0) / d.value for d in ds if d.value > 0]
        k = float(np.median(ratios)) if ratios else expected
        for _ in range(3):
            good = [d for d in ds if d.value > 0 and
                    abs(d.pos - (o + d.side * d.value * k)) < MAX_RESIDUAL]
            if len(good) < 2:
                break
            A = np.column_stack([np.ones(len(good)), [d.side * d.value for d in good]])
            (o, k), *_ = np.linalg.lstsq(A, np.array([d.pos for d in good]), rcond=None)
        for d in ds:
            d.residual = float(d.pos - (o + d.side * d.value * k))
            d.outlier = abs(d.residual) >= MAX_RESIDUAL
        inl = [d.residual for d in ds if not d.outlier]
        rms = float(np.sqrt(np.mean(np.square(inl)))) if inl else None
        fits.append(AxisFit(vid, axis, float(o), float(k), expected, len(ds), rms))
    return fits


def parse(pg, cl=None, sheet=None):
    cl = cl or classify(pg)
    sheet = sheet or segment(pg, cl)
    origins = find_origins(pg, cl, sheet)
    dims = find_ordinates(pg, cl, sheet, build_groups(pg.texts))
    fits = fit_axes(sheet, origins, dims)
    return sheet, origins, dims, fits


def summary(sheet, origins, dims, fits):
    views = {v.id: v for v in sheet.views}

    def vname(i):
        v = views.get(i)
        return "-" if v is None else f"{i}{'(' + v.label + ')' if v.label else ''}"

    print(f"{len(origins)} origins:")
    for o in origins:
        print(f"  view {vname(o.view):6} {o.axis} at {o.pos:.1f}")
    print(f"\n{len(dims)} ordinate dimensions, "
          f"{sum(d.view is None for d in dims)} without a view, "
          f"{sum(not d.anchors for d in dims)} without an anchor, "
          f"{sum(d.outlier for d in dims)} outliers")
    print("\nper-axis fits (k = pt/mm):")
    for f in fits:
        if f.k is None:
            print(f"  view {vname(f.view):6} {f.axis}: n={f.n:2d}  no origin")
            continue
        print(f"  view {vname(f.view):6} {f.axis}: n={f.n:2d}  origin={f.origin:8.2f}  "
              f"k={f.k:.4f} (expected {f.expected_k:.4f}, {100 * (f.k / f.expected_k - 1):+.2f}%)  "
              f"rms={'-' if f.rms is None else f'{f.rms:.3f}'} pt")
    bad = [d for d in dims if d.outlier or not d.anchors or d.view is None]
    if bad:
        print("\nto check:")
        for d in bad:
            why = [w for w, c in (("outlier", d.outlier), ("no anchor", not d.anchors),
                                  ("no view", d.view is None)) if c]
            res = f"{d.residual:+.2f}" if d.residual is not None else "-"
            print(f"  {d.text.text.strip():7} view {vname(d.view):6} {d.axis} pos={d.pos:7.1f} "
                  f"res={res:>6} {', '.join(why)}")


def to_records(sheet, origins, dims, fits):
    """JSON-ready records: per view axis fits and one entry per ordinate."""
    views = {v.id: v for v in sheet.views}
    fit_of = {(f.view, f.axis): f for f in fits}

    def pt(p):
        return None if p is None else [round(float(p[0]), 3), round(float(p[1]), 3)]

    out = {"views": [], "ordinates": []}
    for (vid, axis), f in sorted(fit_of.items()):
        v = views[vid]
        out["views"].append({
            "view": vid, "label": v.label, "kind": v.kind, "axis": axis,
            "origin_page": None if f.origin is None else round(f.origin, 3),
            "pt_per_mm": None if f.k is None else round(f.k, 5),
            "scale": None if f.k is None else round(f.k / PT_PER_MM, 4),
            "n": f.n, "rms_pt": None if f.rms is None else round(f.rms, 4),
        })
    for d in dims:
        out["ordinates"].append({
            "view": d.view, "axis": d.axis, "value": d.value,
            # signed by page side of the origin; the model axis direction is
            # only known after registration
            "signed_value": d.side * d.value if d.side else d.value,
            "tol": d.tol, "text": d.text.text.strip(), "page_pos": round(d.pos, 3),
            "line": [pt(d.outer), pt(d.inner)],
            "anchors": [{"page": pt(a[0]), "kind": a[1], "role": a[2]} for a in d.anchors],
            "two_sided": d.two_sided, "basic": d.basic,
            "residual_pt": None if d.residual is None else round(d.residual, 4),
            "outlier": d.outlier,
        })
    return out


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--json", help="write parsed records to this file")
    args = ap.parse_args()
    res = parse(extract(args.pdf))
    summary(*res)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_records(*res), f, indent=1)
        print("\nwrote", args.json)
