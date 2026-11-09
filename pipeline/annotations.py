"""Parse radius, diameter, linear, angle, GD&T, datum and roughness annotations.

Works on text groups (textgroups.py) that the ordinate step did not use.
Each label is tied to page geometry and checked against its value with the
view scale fitted from the ordinates (k, points per mm):

- radius / leader diameter: the leader's arrow tip lies on an arc whose page
  radius / k equals the value;
- linear / diameter: two opposed, collinear arrowheads whose tip distance / k
  equals the value; the features are where their extension lines end;
- GD&T frame rows are stacked into frames; a frame applies to the feature its
  leader arrow touches, or to the dimension it is attached to;
- datum letters are tied to their filled triangle.

A value check that passes marks the annotation "matched"; geometry found but
disagreeing is "mismatch"; a leader reaching the outline where the value
cannot be checked in 2D is "located"; nothing found is "unmatched".
"""
from collections import defaultdict
from dataclasses import dataclass, field
import math
import re

import numpy as np

from classify import Arc, Line, classify
from ordinates import PT_PER_MM, parse as parse_ordinates
from pdf_extractor import extract
from textgroups import build_groups
from views import ISO2768, _inside

GDT = {"⌖": "position", "⊥": "perpendicularity", "//": "parallelism", "▱": "flatness"}

LABEL_RE = re.compile(
    r"^(?:(?P<count>\d+)\s*[xX]\s*)?(?P<prefix>R|Ø|M|SR)?\s*"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<deg>°)?\s*"
    r"(?P<fit>[A-Za-z]{1,2}\d{1,2})?\s*(?:±\s*(?P<pm>\d+(?:[.,]\d+)?))?$")

# geometry roles a dimension may point at
FEATURE_ROLES = ("visible", "hidden", "thin_medium", "phantom", "center")
# a value check passes within this relative error, or this many mm
REL_TOL = 0.02
ABS_TOL = 0.03
# arrow tip to arc distance for radius leaders (pt)
TIP_ON_ARC = 1.5
# largest coordinate rounding error of the export (0.12 pt grid, both axes)
QUANT = 0.085

# ISO 2768-1 external radii and chamfer heights: (above, up to, +-tol) mm
ISO2768_RADII = {
    "f": [(0.5, 3, 0.2), (3, 6, 0.5), (6, math.inf, 1.0)],
    "m": [(0.5, 3, 0.2), (3, 6, 0.5), (6, math.inf, 1.0)],
    "c": [(0.5, 3, 0.4), (3, 6, 1.0), (6, math.inf, 2.0)],
    "v": [(0.5, 3, 0.4), (3, 6, 1.0), (6, math.inf, 2.0)],
}


@dataclass
class Annotation:
    id: int
    kind: str                   # radius diameter linear angle gdt datum roughness note
    group: object
    view: int | None
    value: float | None = None
    tol: tuple | None = None    # (upper, lower) mm, explicit on the drawing
    general_tol: tuple | None = None    # from the title block when no explicit tol
    fit: str | None = None      # ISO fit, e.g. H7
    count: int | None = None
    reference: bool = False     # (value): not toleranced
    gdt: dict | None = None     # rows: [{symbol, characteristic, zone, tolerance, datums}]
    tips: list = field(default_factory=list)       # arrow tips (page)
    anchors: list = field(default_factory=list)    # (page point, kind)
    measured: float | None = None                  # mm, from page geometry
    geometry: dict | None = None
    applies_to: int | None = None                  # annotation id (GD&T, datum)
    status: str = "unmatched"

    @property
    def text(self):
        return self.group.text


def _num(s):
    return float(s.replace(",", "."))


def _seg_dist(p, a, b):
    d = b - a
    L = float(d @ d)
    t = 0.0 if L == 0 else max(0.0, min(1.0, float((p - a) @ d) / L))
    return float(np.linalg.norm(p - (a + t * d)))


def _poly_dist(p, pts):
    return min(_seg_dist(p, pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _rect_dist(p, r):
    dx = max(r[0] - p[0], 0, p[0] - r[2])
    dy = max(r[1] - p[1], 0, p[1] - r[3])
    return math.hypot(dx, dy)


def _arc_dist(p, a):
    v = p - a.center
    ang = math.atan2(v[1], v[0])
    from ordinates import _angle_in
    if _angle_in(ang, a.start, a.sweep):
        return abs(np.linalg.norm(v) - a.radius)
    ends = a.sample(2)
    return float(min(np.linalg.norm(ends - p, axis=1)))


def _ok(measured, value, rel=REL_TOL):
    return abs(measured - value) <= max(rel * value, ABS_TOL)


def _radius_rel_tol(arc):
    """Relative radius uncertainty of a fitted arc.

    A short, flat arc pins its radius down poorly: the radius error scales as
    the point error over the arc's sagitta. Two sigma, never below REL_TOL.
    """
    half = min(abs(arc.sweep), math.pi) / 2
    sagitta = arc.radius * (1 - math.cos(half))
    return max(REL_TOL, 2 * QUANT / sagitta) if sagitta > 0 else 1.0


# ---------------------------------------------------------------- leaders

@dataclass
class Leader:
    arrow: object
    pts: np.ndarray             # polyline from the tip outwards
    far: np.ndarray


def leaders(cl):
    """For each arrowhead, the thin stroke running back from it along its axis.

    The leader may start at the tip, at the arrowhead's base, or pass through
    the tip; it must be collinear with the arrow (which also rules out the
    arrowhead's own outline, whose sides are ~10 degrees off its axis).
    """
    out = []
    thin = [s for s in cl.strokes if s.role in ("thin", "hidden")]
    for a in cl.arrowheads:
        best, reach = None, 0.0
        for s in thin:
            for p in s.prims:
                if not isinstance(p, Line) or p.length < 1.0:
                    continue
                d = (p.p1 - p.p0) / p.length
                if abs(float(d @ a.dir)) < 0.995:
                    continue
                if _seg_dist(a.tip, p.p0, p.p1) > 0.7 and \
                        _seg_dist(a.tip - a.dir * a.length, p.p0, p.p1) > 0.7:
                    continue
                # how far the stroke runs back from the tip
                back = float(np.max((s.pts - a.tip) @ -a.dir))
                if back > reach:
                    best, reach = s, back
        if best is None:
            continue
        pts = best.pts
        if float((pts[0] - a.tip) @ -a.dir) > float((pts[-1] - a.tip) @ -a.dir):
            pts = pts[::-1]
        out.append(Leader(a, pts, pts[-1]))
    return out


# ---------------------------------------------------------------- matching

def _arrow_pairs(cl):
    """Opposed, collinear arrowheads: candidate ends of one dimension line."""
    A = cl.arrowheads
    pairs = []
    for i in range(len(A)):
        for j in range(i + 1, len(A)):
            a, b = A[i], A[j]
            if float(a.dir @ b.dir) > -0.98:
                continue
            d = b.tip - a.tip
            D = float(np.linalg.norm(d))
            if D < 0.5:
                continue
            # arrowhead directions come from tiny triangles and are a little
            # off, so test the angle of the tip-to-tip line, not an offset
            e = d / D
            if abs(float(e @ a.dir)) < 0.998 or abs(float(e @ b.dir)) < 0.998:
                continue
            pairs.append((a, b, D))
    return pairs


def _match_linear(ann, pairs, k, used):
    """Best arrow pair for a linear or diameter label by value and placement."""
    g = ann.group
    u = np.asarray(g.dir)
    c = g.center
    best = None
    for a, b, D in pairs:
        if id(a) in used or id(b) in used:
            continue
        e = (b.tip - a.tip) / D
        if abs(float(u @ e)) < 0.98:
            continue
        # distance of the label from the dimension line, and along it
        perp = abs(float((c - a.tip) @ np.array([-e[1], e[0]])))
        s = [float((p - a.tip) @ e) for p in (a.tip, b.tip)]
        sc = float((c - a.tip) @ e)
        outside = max(min(s) - sc, sc - max(s), 0.0)
        if perp > 2.5 * g.size or outside > 8 * g.size:
            continue
        err = abs(D / k - ann.value)
        score = err / max(ann.value, 0.5) + 0.002 * (perp + outside) / g.size
        if best is None or score < best[0]:
            best = (score, a, b, D)
    return best


def _extension_anchor(tip, e, cl, view):
    """Follow the extension line through an arrow tip to the feature."""
    best = None
    for s in cl.strokes:
        if s.role != "thin":
            continue
        for p in s.prims:
            if not isinstance(p, Line) or p.length < 2:
                continue
            d = (p.p1 - p.p0) / p.length
            if abs(float(d @ e)) > 0.05:
                continue
            if _seg_dist(tip, p.p0, p.p1) > 0.7:
                continue
            far = p.p0 if np.linalg.norm(p.p0 - tip) > np.linalg.norm(p.p1 - tip) else p.p1
            if best is None or np.linalg.norm(far - tip) > np.linalg.norm(best - tip):
                best = far
    target = best if best is not None else tip
    snap = _nearest_feature(target, view, 4.0)
    if snap is not None:
        return snap, "extension" if best is not None else "direct"
    return target, "extension_end" if best is not None else "tip"


def _nearest_feature(p, view, maxd):
    if view is None:
        return None
    best, bd = None, maxd
    for s in view.strokes:
        if s.role not in FEATURE_ROLES:
            continue
        for q in s.prims:
            if isinstance(q, Line):
                d = q.p1 - q.p0
                L = float(d @ d)
                t = 0.0 if L == 0 else max(0.0, min(1.0, float((p - q.p0) @ d) / L))
                pt = q.p0 + t * d
            else:
                v = p - q.center
                n = np.linalg.norm(v)
                pt = q.center + (v / n * q.radius if n > 0 else 0)
                if _arc_dist(p, q) > maxd:
                    continue
            dd = float(np.linalg.norm(pt - p))
            if dd < bd:
                best, bd = pt, dd
    return best


def _match_radius(ann, lds, k, views, diameter=False):
    """Leader arrow whose tip sits on an arc of the right size."""
    g = ann.group
    best = None
    for ld in lds:
        # the label may sit behind the arrow, or past the tip when the leader
        # runs from the center through the arc out to the text
        ahead = np.array([ld.arrow.tip, ld.arrow.tip + ld.arrow.dir * 8 * g.size])
        d_text = min(_poly_dist(g.center, ld.pts), _rect_dist(ld.far, g.rect),
                     _poly_dist(g.center, ahead))
        if d_text > 2.2 * g.size:
            continue
        v = views.get(ann.view)
        arcs = [q for s in (v.strokes if v else []) if s.role in FEATURE_ROLES
                for q in s.prims if isinstance(q, Arc)]
        for q in arcs:
            if _arc_dist(ld.arrow.tip, q) > TIP_ON_ARC:
                continue
            m = (2 if diameter else 1) * q.radius / k
            # error in units of the arc's own uncertainty
            err = abs(m - ann.value) / max(ann.value, 0.5) / _radius_rel_tol(q)
            score = err + 0.01 * d_text / g.size
            if best is None or score < best[0]:
                best = (score, ld, q, m)
    return best


def _on_extension(pt, tip, anchor, beyond=60.0):
    """True if pt lies on the extension line through a dimension's arrow tip.

    The extension line runs from the feature (anchor) through the tip and a
    little past it; a frame leader may end anywhere along it.
    """
    d = anchor - tip
    n = np.linalg.norm(d)
    if n < 1e-6:
        return np.linalg.norm(pt - tip) < 1.0
    d = d / n
    off = pt - tip
    along = float(off @ d)
    return abs(float(off @ np.array([-d[1], d[0]]))) < 0.7 and -beyond <= along <= n + 1.0


def _reference(g, cl):
    """True if drawn parentheses enclose the label: "(5)"."""
    u = np.asarray(g.dir)
    start, end = g.extent()
    lo = hi = False
    for s in cl.strokes:
        # Solid Edge draws the parentheses as 0.48 pt arcs
        if s.role not in ("thin", "thin_medium"):
            continue
        for q in s.prims:
            if not isinstance(q, Arc) or not (0.3 * g.size < q.radius < 2.5 * g.size):
                continue
            mid = q.sample(3)[1]
            if _rect_dist(mid, g.rect) > 0.8 * g.size:
                continue
            a = float(mid @ u)
            lo |= start - 0.8 * g.size <= a <= start + 0.1 * g.size
            hi |= end - 0.1 * g.size <= a <= end + 0.8 * g.size
    return lo and hi


def _general(kind, value, title):
    """ISO 2768-1 general tolerance (upper, lower) for a value, or None.

    Ranges are "over lo up to hi", except the first, which starts at its
    lower bound (0.5 up to 3). ISO 2768-1 does not cover sizes below 0.5 mm:
    those need their tolerance written on the drawing, so they get None.
    """
    if not title or not title.tolerance_grade or value is None:
        return None
    table = ISO2768_RADII.get(title.tolerance_grade) if kind == "radius" else title.tolerance_table
    for i, (lo, hi, t) in enumerate(table or []):
        if (lo <= value if i == 0 else lo < value) and value <= hi:
            return (t, -t)
    return None


def below_general_range(kind, value, title):
    """True if ISO 2768-1 has no general tolerance for this value (below its first range)."""
    if not title or not title.tolerance_grade or value is None:
        return False
    table = ISO2768_RADII.get(title.tolerance_grade) if kind == "radius" else title.tolerance_table
    return bool(table) and value < table[0][0]


# ---------------------------------------------------------------- main

def annotate(pg, cl=None, sheet=None, ordinate_result=None):
    cl = cl or classify(pg)
    sheet, origins, dims, fits = ordinate_result or parse_ordinates(pg, cl, sheet)
    views = {v.id: v for v in sheet.views}
    title = sheet.title_block

    # k per view: fitted from the ordinates where possible
    k_of = {}
    by_view = defaultdict(list)
    for f in fits:
        if f.k:
            by_view[f.view].append(f.k)
    sheet_scale = title.scale if title and title.scale else 1.0
    for v in sheet.views:
        k_of[v.id] = float(np.mean(by_view[v.id])) if by_view[v.id] else (v.scale or sheet_scale) * PT_PER_MM

    # ordinates that their view's fit does not confirm (outliers, or views
    # without a fit) may really be linear dimensions: let them be re-tried
    weak = {id(d.text): d for d in dims if d.outlier or d.residual is None}
    exclude = {id(d.text) for d in dims if id(d.text) not in weak} | {id(o.text) for o in origins}
    exclude |= {id(t) for v in sheet.views for t in v.caption} | {id(t) for t in sheet.markers}
    rects = [r for r in (title.rect if title else None,) if r is not None]
    texts = [t for t in pg.texts if t.font != "SERomans" and not any(
        _inside(np.array([t.bbox.x0, t.bbox.y0, t.bbox.x1, t.bbox.y1]), r, 2) for r in rects)]
    groups = build_groups(texts, exclude)
    main = np.median([g.size for g in groups]) if groups else 0

    def view_of(p):
        # labels of thin views can sit well outside their outline
        best, bd = None, 200.0
        for v in sheet.views:
            d = _rect_dist(p, v.rect)
            if d < bd:
                best, bd = v.id, d
        return best

    anns = []
    for g in groups:
        tok = g.tokens
        a = Annotation(len(anns), "note", g, view_of(g.center))
        if g.size > 1.5 * main:
            anns.append(a)
            continue
        if tok[0] in GDT:
            a.kind = "gdt"
            row = {"symbol": tok[0], "characteristic": GDT[tok[0]], "zone": None,
                   "tolerance": None, "datums": []}
            for t in tok[1:]:
                if t == "Ø":
                    row["zone"] = "Ø"
                elif re.fullmatch(r"\d+(?:[.,]\d+)?", t):
                    row["tolerance"] = _num(t)
                elif re.fullmatch(r"[A-Z]", t):
                    row["datums"].append(t)
            a.gdt = {"rows": [row]}
        elif re.fullmatch(r"[A-Z]", g.text):
            a.kind = "datum"
            a.value = None
        else:
            m = LABEL_RE.match(g.text)
            if m:
                a.value = _num(m.group("value"))
                a.fit = m.group("fit")
                a.count = int(m.group("count")) if m.group("count") else None
                if m.group("pm"):
                    pm = _num(m.group("pm"))
                    a.tol = (pm, -pm)
                elif g.stack:
                    a.tol = g.stack_values()
                if m.group("deg"):
                    a.kind = "angle"
                elif m.group("prefix") == "R":
                    a.kind = "radius"
                elif m.group("prefix") == "Ø":
                    a.kind = "diameter"
                elif "." in m.group("value") and not a.tol:
                    # this sheet writes dimensions with "," and roughness values with "."
                    a.kind = "roughness"
                else:
                    a.kind = "linear"
        anns.append(a)

    # GD&T rows stacked into frames
    gdt = [a for a in anns if a.kind == "gdt"]
    merged = set()
    for a in gdt:
        if a.id in merged:
            continue
        for b in gdt:
            if b is a or b.id in merged or abs(a.group.angle - b.group.angle) > 2:
                continue
            ra, rb = a.group.rect, b.group.rect
            gap = max(ra[0] - rb[2], rb[0] - ra[2], ra[1] - rb[3], rb[1] - ra[3])
            if gap < 0.9 * a.group.size:
                a.gdt["rows"] += b.gdt["rows"]
                a.group.spans += b.group.spans
                merged.add(b.id)
    anns = [a for a in anns if a.id not in merged]
    for i, a in enumerate(anns):
        a.id = i

    pairs = _arrow_pairs(cl)
    lds = leaders(cl)
    used_arrows = set()

    # linear and diameter dimensions with arrow pairs, best value fits first
    cands = []
    for a in anns:
        if a.kind in ("linear", "diameter") and a.view is not None:
            m = _match_linear(a, pairs, k_of[a.view], used_arrows)
            if m:
                cands.append((m[0], a, m))
    for _, a, (score, p, q, D) in sorted(cands, key=lambda c: c[0]):
        if id(p) in used_arrows or id(q) in used_arrows:
            m = _match_linear(a, pairs, k_of[a.view], used_arrows)
            if not m:
                continue
            score, p, q, D = m
        k = k_of[a.view]
        a.measured = D / k
        a.tips = [p.tip, q.tip]
        a.geometry = {"dimension_line": [p.tip.tolist(), q.tip.tolist()]}
        a.status = "matched" if _ok(a.measured, a.value) else "mismatch"
        if a.status == "matched":
            used_arrows |= {id(p), id(q)}
            for sp in a.group.spans:
                if id(sp) in weak:
                    dims.remove(weak.pop(id(sp)))
            a.anchors = [_extension_anchor(t, p.dir, cl, views.get(a.view)) for t in a.tips]

    # radii, and diameters given with a leader to a circle: best fits first,
    # so a label cannot take a leader that fits another label better
    cands = []
    for a in anns:
        if a.kind == "radius" or (a.kind == "diameter" and a.status != "matched"):
            if a.view is None:
                continue
            m = _match_radius(a, lds, k_of[a.view], views, diameter=a.kind == "diameter")
            if m:
                cands.append((m[0], a))
    for _, a in sorted(cands, key=lambda c: c[0]):
        m = _match_radius(a, [l for l in lds if id(l.arrow) not in used_arrows],
                          k_of[a.view], views, diameter=a.kind == "diameter")
        if not m:
            continue
        if True:
            _, ld, q, meas = m
            a.measured = meas
            a.tips = [ld.arrow.tip]
            a.anchors = [(ld.arrow.tip, "on_arc")]
            rel = _radius_rel_tol(q)
            a.geometry = {"arc_center": q.center.tolist(), "arc_radius_pt": q.radius,
                          "arc_sweep_deg": math.degrees(q.sweep), "radius_rel_uncertainty": rel,
                          "leader": ld.pts.tolist()}
            a.status = "matched" if _ok(meas, a.value, rel) else "mismatch"
            if a.status == "matched":
                used_arrows.add(id(ld.arrow))

    # a leader that reaches the outline where no arc of the right size was
    # fitted (a large radius over a short span looks straight within the
    # export's rounding): the feature is located, the radius is left to 3D
    for a in anns:
        if a.kind not in ("radius", "diameter") or a.status != "unmatched" or a.view is None:
            continue
        g = a.group
        best = None
        for ld in lds:
            if id(ld.arrow) in used_arrows:
                continue
            ahead = np.array([ld.arrow.tip, ld.arrow.tip + ld.arrow.dir * 8 * g.size])
            d = min(_poly_dist(g.center, ld.pts), _poly_dist(g.center, ahead))
            if d <= 2.2 * g.size and _nearest_feature(ld.arrow.tip, views.get(a.view), 1.0) is not None:
                if best is None or d < best[0]:
                    best = (d, ld)
        if best:
            ld = best[1]
            a.tips = [ld.arrow.tip]
            a.anchors = [(ld.arrow.tip, "on_outline")]
            a.geometry = {"leader": ld.pts.tolist()}
            a.status = "located"
            used_arrows.add(id(ld.arrow))

    # GD&T frames: leader arrow from the frame, or attached to a dimension label
    dims_with_lines = [a for a in anns if a.kind in ("linear", "diameter", "radius") and a.status == "matched"]
    for a in anns:
        if a.kind != "gdt":
            continue
        box = a.group.rect + np.array([-0.5, -0.5, 0.5, 0.5]) * a.group.size
        for ld in lds:
            if id(ld.arrow) in used_arrows or _rect_dist(ld.far, box) > 2.0:
                continue
            a.tips = [ld.arrow.tip]
            target = _nearest_feature(ld.arrow.tip, views.get(a.view), 1.5)
            a.anchors = [(target if target is not None else ld.arrow.tip,
                          "on_feature" if target is not None else "tip")]
            used_arrows.add(id(ld.arrow))
            # a leader ending on a dimension's extension line refers to that feature
            for d in dims_with_lines:
                if d.anchors and any(_on_extension(ld.arrow.tip, np.asarray(t), np.asarray(p))
                                     for (p, _), t in zip(d.anchors, d.tips)):
                    a.applies_to = d.id
            break
        if not a.tips:
            near = [d for d in anns if d.kind in ("linear", "diameter", "radius") and
                    _rect_dist(d.group.center, box) < 1.5 * a.group.size]
            if near:
                a.applies_to = min(near, key=lambda d: _rect_dist(d.group.center, box)).id
        a.status = "matched" if (a.tips or a.applies_to is not None) else "unmatched"

    # datums: letter tied to its filled triangle
    tris = [f for f in cl.filled if f.kind == "triangle"]
    for a in anns:
        if a.kind != "datum":
            continue
        best = None
        for f in tris:
            d = _rect_dist(f.pts.mean(axis=0), a.group.rect)
            if d < 25 * 1 + 4 * a.group.size and (best is None or d < best[0]):
                best = (d, f)
        if best:
            pts = best[1].pts
            far = max(range(3), key=lambda i: np.linalg.norm(pts[i] - a.group.center))
            # the base is the side away from the letter; its middle sits on the feature
            base = [pts[i] for i in range(3) if i != min(range(3), key=lambda i: np.linalg.norm(
                pts[i] - a.group.center))]
            a.anchors = [((base[0] + base[1]) / 2, "datum_base")]
            a.status = "matched"

    _angles(anns, cl, fits, used_arrows)
    _roughness(anns, cl, views, [l for l in lds if id(l.arrow) not in used_arrows])

    # weak ordinates that no arrow pair confirmed stay ordinates
    anns = [a for a in anns if not any(id(sp) in weak for sp in a.group.spans)]
    renum = {a.id: i for i, a in enumerate(anns)}
    for a in anns:
        a.id = renum[a.id]
        if a.applies_to is not None:
            a.applies_to = renum.get(a.applies_to)

    for a in anns:
        if a.kind in ("linear", "diameter", "radius") and a.tol is None and not a.reference:
            a.general_tol = _general(a.kind, a.value, title)
        if a.kind in ("linear", "diameter", "radius"):
            a.reference = _reference(a.group, cl)
            if a.reference:
                a.general_tol = None
    general = _general_texture(pg.texts, title, len(anns))
    if general:
        anns.append(general)
    return anns


def _general_texture(texts, title, next_id):
    """The title block's general surface texture, e.g. "3.2 (1.6; 0.8)": in
    ISO 1302's simplified indication, Ra 3.2 on every surface not marked
    otherwise; the values in parentheses are the ones marked on the views."""
    field = (title.fields.get("roughness") or "").strip() if title else ""
    m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(?:\(([^)]*)\))?", field)
    if not m:
        return None
    span = next((t for t in texts if t.text.strip() == field), None)
    if span is None:
        return None
    from textgroups import Group
    a = Annotation(next_id, "roughness", Group([span]), None, value=_num(m.group(1)))
    a.geometry = {"type": "general_surface_texture", "parameter": "Ra", "unit": "µm",
                  "process": "not stated", "source": "title block",
                  "except": [_num(v) for v in re.findall(r"\d+(?:[.,]\d+)?", m.group(2) or "")]}
    a.status = "matched"
    return a


# ---------------------------------------------------------------- angles

# an angular ordinate's radial line passes this close to the origin (pt)
RADIAL_TOL = 1.0
# measured and printed angles agree to (deg)
ANGLE_TOL = 0.5


def _lines(cl, roles=("thin", "thin_medium", "center")):
    return [(s, p) for s in cl.strokes if s.role in roles for p in s.prims if isinstance(p, Line)]


def _angles(anns, cl, fits, used_arrows):
    """Angle values: angular ordinates, or angles between two legs.

    An angular ordinate is written at the end of a radial thin line through
    a view's ordinate origin, like a linear ordinate turned around the
    origin. The sheet's convention (zero direction and sense) is fitted from
    all of them together. Otherwise the value belongs to an angle dimension:
    an arc around the legs' vertex with arrow tips on it (both tips at one
    point for 0°: the legs coincide).
    """
    origin = {}
    for f in fits:
        if f.origin is not None:
            origin.setdefault(f.view, {})[f.axis] = f.origin
    origin = {v: np.array([o["x"], o["y"]]) for v, o in origin.items() if "x" in o and "y" in o}
    lines = _lines(cl)
    radial = []
    for a in anns:
        if a.kind != "angle" or a.view not in origin:
            continue
        O, c = origin[a.view], a.group.center
        best = None
        for s, p in lines:
            for near_end, far_end in ((p.p0, p.p1), (p.p1, p.p0)):
                d = np.linalg.norm(near_end - c)
                if d > 3 * a.group.size:
                    continue
                u = far_end - near_end
                L = np.linalg.norm(u)
                if L < 5:
                    continue
                u /= L
                off = abs((O - near_end)[0] * u[1] - (O - near_end)[1] * u[0])
                # the line runs from the label toward the origin
                if off <= RADIAL_TOL and (O - near_end) @ u > 0 and (best is None or d < best[0]):
                    best = (d, near_end, far_end)
        if best:
            radial.append((a, O, best[1], best[2]))
    conv = _angle_convention([(a.value, _page_angle(far - O)) for a, O, near, far in radial])
    for a, O, near, far in radial:
        meas = _apply_convention(conv, _page_angle(far - O)) if conv else None
        a.measured = meas
        a.anchors = [(far, "radial_end")]
        a.geometry = {"type": "angular_ordinate", "origin": O.tolist(), "line": [near.tolist(), far.tolist()],
                      "zero_page_dir": conv[0] if conv else None, "clockwise": conv[1] if conv else None}
        ok = meas is not None and abs((meas - a.value + 180) % 360 - 180) <= ANGLE_TOL
        a.status = "matched" if ok else "mismatch"

    arcs = [(s, p) for s in cl.strokes if s.role in ("thin", "thin_medium") for p in s.prims if isinstance(p, Arc)]
    for a in anns:
        if a.kind != "angle" or a.status != "unmatched":
            continue
        g = a.group
        heads = [h for h in cl.arrowheads if id(h) not in used_arrows and np.linalg.norm(h.tip - g.center) < 12 * g.size]
        if a.value == 0:
            pair = next(((h, k) for i, h in enumerate(heads) for k in heads[i + 1:]
                         if np.linalg.norm(h.tip - k.tip) < 1.0 and h.dir @ k.dir < -0.9), None)
            if pair:
                a.tips = [pair[0].tip, pair[1].tip]
                a.measured = 0.0
                leg = np.array([-pair[0].dir[1], pair[0].dir[0]])
                a.geometry = {"type": "angle", "vertex": None, "legs_page_dir": [leg.tolist(), leg.tolist()]}
                a.anchors = [(pair[0].tip, "tip")]
                a.status = "matched"
                used_arrows |= {id(pair[0]), id(pair[1])}
            continue
        best = None
        for s, q in arcs:
            r = np.linalg.norm(g.center - q.center)
            if q.radius < 5 or abs(r - q.radius) > 2.5 * g.size:
                continue
            on = [h for h in heads if abs(np.linalg.norm(h.tip - q.center) - q.radius) < 1.5]
            for i, h in enumerate(on):
                for k in on[i + 1:]:
                    v1, v2 = h.tip - q.center, k.tip - q.center
                    th = math.degrees(math.acos(np.clip(v1 @ v2 / np.linalg.norm(v1) / np.linalg.norm(v2), -1, 1)))
                    err = min(abs(th - a.value), abs(360 - th - a.value))
                    if best is None or err < best[0]:
                        best = (err, q, h, k, th if abs(th - a.value) <= abs(360 - th - a.value) else 360 - th)
        if best:
            err, q, h, k, th = best
            # arrowhead tips are a little off on a short arc: measure along the
            # legs (lines through the vertex and a tip) where they are drawn
            dirs = [_leg_dir(q.center, t.tip, lines) for t in (h, k)]
            if all(d is not None for d in dirs):
                c = math.degrees(math.acos(np.clip(dirs[0] @ dirs[1], -1, 1)))
                th = c if abs(c - th) <= abs(360 - c - th) else 360 - c
                err = abs(th - a.value)
            a.tips = [h.tip, k.tip]
            a.anchors = [(h.tip, "leg"), (k.tip, "leg")]
            a.measured = th
            a.geometry = {"type": "angle", "vertex": q.center.tolist(),
                          "legs_page_dir": [((h.tip - q.center) / np.linalg.norm(h.tip - q.center)).tolist(),
                                            ((k.tip - q.center) / np.linalg.norm(k.tip - q.center)).tolist()]}
            a.status = "matched" if err <= ANGLE_TOL else "mismatch"
            used_arrows |= {id(h), id(k)}


def _leg_dir(vertex, tip, lines, tol=1.5):
    """Unit direction from the vertex along the drawn leg through tip, or None."""
    u = tip - vertex
    u /= np.linalg.norm(u)
    best = None
    for s, p in lines:
        d = p.p1 - p.p0
        L = np.linalg.norm(d)
        if L < 5:
            continue
        d /= L
        # the leg (extended) passes the vertex and runs along the tip direction
        off_v = abs((vertex - p.p0)[0] * d[1] - (vertex - p.p0)[1] * d[0])
        off_t = abs((tip - p.p0)[0] * d[1] - (tip - p.p0)[1] * d[0])
        if off_v < tol and off_t < tol and abs(d @ u) > 0.99 and (best is None or L > best[0]):
            best = (L, d if d @ u > 0 else -d)
    return None if best is None else best[1]


def _page_angle(v):
    """Angle of a page vector in degrees, counter-clockwise from +x as seen (page y is down)."""
    return math.degrees(math.atan2(-v[1], v[0])) % 360


def _angle_convention(pairs):
    """(zero direction in page degrees, clockwise) that best turns line
    directions into printed values; zero at a multiple of 90°."""
    if not pairs:
        return None
    best = None
    for zero in (0, 90, 180, 270):
        for cw in (False, True):
            err = sum(abs((_apply_convention((zero, cw), d) - v + 180) % 360 - 180) for v, d in pairs)
            if best is None or err < best[0]:
                best = (err, (zero, cw))
    return best[1]


def _apply_convention(conv, d):
    zero, cw = conv
    return ((zero - d) if cw else (d - zero)) % 360


# ---------------------------------------------------------------- roughness

# legs of a surface texture symbol (ISO 1302): 60° apart, the long leg about twice the short one
ROUGH_ANGLE = (50, 70)


def _roughness(anns, cl, views, lds=()):
    """Surface texture symbols: two thin legs meeting at the tip, which
    touches the surface (or its extension line), or sits on a leader whose
    arrow does; the value is written by the symbol. A bar closing the legs
    means material removal is required, a circle inside them that it is
    prohibited."""
    lines = _lines(cl, ("thin", "thin_medium", "visible"))
    ends = defaultdict(list)
    for s, p in lines:
        for e, o in ((p.p0, p.p1), (p.p1, p.p0)):
            ends[(round(e[0], 0), round(e[1], 0))].append((e, o, p))
    symbols = []
    for key, legs in ends.items():
        for i, (e1, o1, p1) in enumerate(legs):
            for e2, o2, p2 in legs[i + 1:]:
                if np.linalg.norm(e1 - e2) > 0.5 or p1 is p2:
                    continue
                v1, v2 = o1 - e1, o2 - e1
                L1, L2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if min(L1, L2) < 3:
                    continue
                ang = math.degrees(math.acos(np.clip(v1 @ v2 / L1 / L2, -1, 1)))
                if not ROUGH_ANGLE[0] <= ang <= ROUGH_ANGLE[1] or max(L1, L2) / min(L1, L2) < 1.4:
                    continue
                short, long_ = (v1, v2) if L1 < L2 else (v2, v1)
                bis = (v1 / L1 + v2 / L2)
                symbols.append({"tip": e1, "short": short, "long": long_, "out": bis / np.linalg.norm(bis)})
    used = set()
    for a in anns:
        if a.kind != "roughness":
            continue
        g = a.group
        best = None
        for i, s in enumerate(symbols):
            if i in used:
                continue
            # the value sits beside the symbol, within its legs' reach
            mid = s["tip"] + (s["short"] + s["long"]) / 2
            d = np.linalg.norm(g.center - mid)
            if d < 3 * g.size + np.linalg.norm(s["long"]) and (best is None or d < best[0]):
                best = (d, i)
        if best is None:
            continue
        s = symbols[best[1]]
        used.add(best[1])
        tip, out, kind = s["tip"], s["out"], "roughness_tip"
        ld = _leader_through(tip, lds, lines)
        if ld is not None:
            tip, out, kind = ld.arrow.tip, -ld.arrow.dir, "leader_tip"
        a.tips = [tip]
        a.anchors = [(tip, kind)]
        a.geometry = {"type": "surface_texture", "parameter": "Ra", "unit": "µm",
                      "outward_page": out.tolist(), "process": _texture_process(s, lines, cl)}
        a.status = "matched"


def _leader_through(p, lds, lines, tol=1.0):
    """A leader whose line passes p, directly or through one more line
    (the shoulder a symbol sits on), or None."""
    for ld in lds:
        if _poly_dist(p, ld.pts) <= tol:
            return ld
    for s, q in lines:
        d = q.p1 - q.p0
        L = np.linalg.norm(d)
        if L < 1:
            continue
        t = np.clip((p - q.p0) @ d / L ** 2, 0, 1)
        if np.linalg.norm(q.p0 + t * d - p) > tol:
            continue
        for ld in lds:
            if min(np.linalg.norm(ld.far - q.p0), np.linalg.norm(ld.far - q.p1)) <= tol:
                return ld
    return None


def _texture_process(sym, lines, cl):
    tip, short = sym["tip"], sym["short"]
    end_short = tip + short
    # a bar from the short leg's end across to the long leg closes the V
    for s, p in lines:
        for e, o in ((p.p0, p.p1), (p.p1, p.p0)):
            if np.linalg.norm(e - end_short) < 0.6:
                r = o - tip
                along = r @ sym["long"] / (np.linalg.norm(sym["long"]) ** 2)
                if 0.2 < along < 1.2 and abs(r[0] * sym["long"][1] - r[1] * sym["long"][0]) / np.linalg.norm(sym["long"]) < 0.6:
                    return "material removal required"
    for s in cl.strokes:
        for q in s.prims:
            if isinstance(q, Arc) and q.is_circle and np.linalg.norm(q.center - (tip + sym["out"] * q.radius * 2)) < 1.5 * q.radius:
                return "material removal prohibited"
    return "any process"


def summary(anns):
    from collections import Counter
    c = Counter((a.kind, a.status) for a in anns)
    print("annotations by kind and status:")
    for k, v in sorted(c.items()):
        print(f"  {v:4d}  {k[0]:10} {k[1]}")
    print()
    for a in anns:
        if a.kind in ("note",):
            continue
        extra = ""
        if a.measured is not None:
            extra = f" measured={a.measured:.3f}"
        if a.gdt:
            extra += " " + "; ".join(f"{r['characteristic']} {r['zone'] or ''}{r['tolerance']} {''.join(r['datums'])}"
                                     for r in a.gdt["rows"])
        if a.applies_to is not None:
            extra += f" -> #{a.applies_to}"
        tol = f" tol={a.tol}" if a.tol else (f" gen={a.general_tol}" if a.general_tol else "")
        print(f"  #{a.id:3d} v{a.view!s:4} {a.kind:10} {a.status:9} {a.text!r:22}"
              f"{tol}{' fit=' + a.fit if a.fit else ''}{' REF' if a.reference else ''}{extra}")


def to_records(anns):
    def pt(p):
        return [round(float(p[0]), 3), round(float(p[1]), 3)]

    out = []
    for a in anns:
        if a.kind == "note":
            continue
        out.append({
            "id": a.id, "kind": a.kind, "view": a.view, "text": a.text, "status": a.status,
            "value": a.value, "tol": a.tol, "general_tol": a.general_tol, "fit": a.fit,
            "count": a.count, "reference": bool(a.reference), "gdt": a.gdt,
            "applies_to": a.applies_to, "measured": None if a.measured is None else round(a.measured, 4),
            "tips": [pt(t) for t in a.tips],
            "anchors": [{"page": pt(p), "kind": k} for p, k in a.anchors],
            "label_center": pt(a.group.center),
            **({"geometry": a.geometry} if a.kind in ("angle", "roughness") and a.geometry else {}),
        })
    return out


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--json", help="write parsed annotations to this file")
    args = ap.parse_args()
    anns = annotate(extract(args.pdf))
    summary(anns)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_records(anns), f, indent=1)
        print("\nwrote", args.json)
