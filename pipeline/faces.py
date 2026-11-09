"""Match drawing annotations to model faces.

Each face is tessellated once. For a registered view its triangles are
projected onto the drawing plane (sections: clipped to the material beyond
the cutting plane first), so the distance from an annotation anchor to a
face is the 2-D distance to the face's projected region: zero inside it,
and zero on the line an edge-on plane or a cylinder's outline projects to.

Proximity alone is ambiguous (an anchor on an outline also lies inside
every face seen face-on behind it); the dimension value selects the
surface:

  ordinate   plane at the coordinate along the view axis, or a cylinder
             tangent / axis there
  radius     cylinder seen end-on with that radius
  diameter   cylinder with that diameter, near both arrow tips
  linear     a pair of planes / cylinder tangents, one at each anchor, the
             value apart along the dimension line
  gdt        the toleranced dimension's features (applies_to), else the
  datum      edge-on surface under the leader tip / triangle base

Faces lying on the same surface (one plane, or one cylinder split into
several faces) form a feature; results list features, each with its faces.
Face ids are indices in TopExp_Explorer order of the STEP shape.
"""
from dataclasses import dataclass, field
import math

import numpy as np

from OCC.Core.BRep import BRep_Tool
from OCC.Core.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.GeomAbs import GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane
from OCC.Core.TopAbs import TopAbs_REVERSED
from OCC.Core.TopLoc import TopLoc_Location
from OCC.Core.gp import gp_Pnt, gp_Vec

from register import from_records, load_model

# tessellation chord deviation (mm)
MESH_DEFLECTION = 0.01
# an anchor on a face's projection, to this distance (mm)
NEAR = 0.2
# value agreement (mm): drawing values are rounded to 0.01
VAL_TOL = 0.011
# beyond this the value is reported as a mismatch rather than a match
VAL_REPORT = 0.15
# direction agreement (cosine)
PARALLEL = 0.9995
# an ordinate's feature may lie this far along the extension line past the
# anchor found on the drawing (mm): the line can stop on construction lines
ALONG = 3.0
# a centre-line anchor may sit this far outside the circle (mm)
CENTER_REACH = 4.0


@dataclass
class Surface:
    face: int
    kind: str                   # plane cylinder cone other
    normal: np.ndarray | None = None    # plane: outward unit normal
    offset: float | None = None         # plane: normal . point
    axis: np.ndarray | None = None      # cylinder/cone: unit direction
    center: np.ndarray | None = None    # cylinder/cone: point on the axis closest to the model origin
    radius: float | None = None
    convex: bool | None = None          # cylinder: shaft (True) or hole (False)
    ref: np.ndarray | None = None       # cone: point on the axis where the radius is `radius`
    slope: float | None = None          # cone: radius change per mm along `axis`
    feature: int = -1

    def radius_at(self, P):
        """Radius at the axial position of model point P."""
        if self.kind == "cone":
            return self.radius + ((P - self.ref) @ self.axis) * self.slope
        return self.radius

    def coords(self, A):
        """[(coordinate along A, how)] of the faces' extremes / axis, A unit."""
        if self.kind == "plane" and abs(self.normal @ A) > PARALLEL:
            return [(self.offset * float(np.sign(self.normal @ A)), "plane")]
        if self.kind == "cylinder" and abs(self.axis @ A) < 1 - PARALLEL:
            c = float(self.center @ A)
            return [(c, "axis"), (c - self.radius, "tangent"), (c + self.radius, "tangent")]
        return []

    def describe(self):
        def r(a):
            return [round(float(x), 4) for x in a]
        d = {"type": self.kind}
        if self.kind == "plane":
            d.update(normal=r(self.normal), offset=round(self.offset, 4))
        elif self.kind in ("cylinder", "cone"):
            d.update(axis=r(self.axis), center=r(self.center), radius=round(self.radius, 4))
            if self.kind == "cone":
                d["half_angle_deg"] = round(math.degrees(math.atan(abs(self.slope))), 4)
            if self.convex is not None:
                d["convex"] = self.convex
        return d


@dataclass
class Faces:
    model: object
    surfaces: list              # Surface per face
    tris: list                  # per face: (m, 3, 3) triangle corners, mm
    features: list              # feature id -> [face ids]


def _canon(a):
    """Unit direction with a fixed sign."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    i = int(np.argmax(np.abs(a)))
    return a if a[i] > 0 else -a


def _surface(i, f):
    s = BRepAdaptor_Surface(f)
    rev = f.Orientation() == TopAbs_REVERSED
    t = s.GetType()
    if t == GeomAbs_Plane:
        p = s.Plane()
        n = np.array(p.Axis().Direction().Coord())
        if not p.Direct():
            n = -n
        if rev:
            n = -n
        return Surface(i, "plane", normal=n, offset=float(n @ np.array(p.Location().Coord())))
    if t in (GeomAbs_Cylinder, GeomAbs_Cone):
        g = s.Cylinder() if t == GeomAbs_Cylinder else s.Cone()
        a = _canon(g.Axis().Direction().Coord())
        loc = np.array(g.Axis().Location().Coord())
        c = loc - (loc @ a) * a
        if t == GeomAbs_Cylinder:
            # the parametric normal points away from the axis on a direct frame
            out = g.Position().Direct() != rev
            return Surface(i, "cylinder", axis=a, center=c, radius=g.Radius(), convex=out)
        d = np.array(g.Axis().Direction().Coord())
        slope = math.tan(g.SemiAngle()) * float(np.sign(d @ a))
        return Surface(i, "cone", axis=a, center=c, radius=g.RefRadius(), ref=loc, slope=slope)
    return Surface(i, "other")


def _triangles(f):
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation(f, loc)
    if tri is None:
        return np.zeros((0, 3, 3))
    tr = loc.Transformation()
    nodes = np.array([tri.Node(j).Transformed(tr).Coord() for j in range(1, tri.NbNodes() + 1)])
    idx = np.array([tri.Triangle(j).Get() for j in range(1, tri.NbTriangles() + 1)]) - 1
    return nodes[idx]


def _group(surfaces):
    """Faces on one plane or one cylinder -> one feature."""
    keys, features = {}, []
    for s in surfaces:
        if s.kind == "plane":
            key = ("plane", *np.round(s.normal, 4), round(s.offset, 3))
        elif s.kind in ("cylinder", "cone"):
            key = (s.kind, *np.round(s.axis, 4), *np.round(s.center, 3), round(s.radius, 3))
        else:
            key = ("face", s.face)
        if key not in keys:
            keys[key] = len(features)
            features.append([])
        s.feature = keys[key]
        features[s.feature].append(s.face)
    return features


def build(model):
    BRepMesh_IncrementalMesh(model.shape, MESH_DEFLECTION, False, 0.2, True)
    surfaces = [_surface(i, f) for i, f in enumerate(model.faces)]
    tris = [_triangles(f) for f in model.faces]
    return Faces(model, surfaces, tris, _group(surfaces))


# ---------------------------------------------------------------- views

def _clip(T, p0, n):
    """Triangles (m, 3, 3) clipped to the half-space (P - p0) . n >= 0."""
    h = (T - p0) @ n
    keep = T[(h >= 0).all(1)]
    out = [keep]
    for tri, hh in zip(T[((h >= 0).any(1)) & ~((h >= 0).all(1))],
                       h[((h >= 0).any(1)) & ~((h >= 0).all(1))]):
        poly = []
        for j in range(3):
            a, b, ha, hb = tri[j], tri[(j + 1) % 3], hh[j], hh[(j + 1) % 3]
            if ha >= 0:
                poly.append(a)
            if (ha >= 0) != (hb >= 0):
                poly.append(a + (b - a) * ha / (ha - hb))
        for j in range(1, len(poly) - 1):
            out.append(np.array([[poly[0], poly[j], poly[j + 1]]]))
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


@dataclass
class ViewFaces:
    reg: object
    tri2: list                  # per face: (m, 3, 2) projected triangles, mm
    depth: list                 # per face: (m,) largest depth toward the viewer
    box: np.ndarray             # per face: (x0, y0, x1, y1), nan when empty
    edge_on: np.ndarray         # per face: projects to a curve (no area)
    points: list = field(default_factory=list)  # (edge id, 2-D point, depth): edges seen end-on
    rays: list = field(default_factory=list)    # (edge id, face, 2-D point, outward unit dir)
    curves: list = field(default_factory=list)  # extended outlines of edge-on faces: (face, kind, a, b)
    vertices: list = field(default_factory=list)    # (2-D point, depth, faces): model vertices, merged when they coincide in the view
    edges: list = field(default_factory=list)       # (edge id, (n, 2) projected samples) of edges in the view


def project(faces, reg):
    tri2, depth, box, area = [], [], [], []
    for T in faces.tris:
        if reg.plane is not None and len(T):
            T = _clip(T, *reg.plane)
        if len(T) == 0:
            tri2.append(np.zeros((0, 3, 2)))
            depth.append(np.zeros(0))
            box.append([np.nan] * 4)
            area.append(0.0)
            continue
        q = np.stack([T @ reg.u, T @ reg.v], axis=-1)
        tri2.append(q)
        depth.append((T @ reg.w).max(1))
        box.append([q[..., 0].min(), q[..., 1].min(), q[..., 0].max(), q[..., 1].max()])
        e1, e2 = q[:, 1] - q[:, 0], q[:, 2] - q[:, 0]
        area.append(float(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 2))
    vf = ViewFaces(reg, tri2, depth, np.array(box), np.array(area) < 0.01)
    _end_on(faces, vf)
    _extensions(faces, vf)
    _vertices(faces, vf)
    return vf


def _vertices(faces, vf):
    reg, model = vf.reg, faces.model
    merged = {}
    for e, ce in enumerate(_curve_ends(model)):
        for P, _ in ce or []:
            if reg.plane is not None and (P - reg.plane[0]) @ reg.plane[1] < -1e-6:
                continue
            p = np.array([P @ reg.u, P @ reg.v])
            key = (round(p[0], 2), round(p[1], 2))
            m = merged.setdefault(key, [p, -math.inf, set()])
            m[1] = max(m[1], float(P @ reg.w))
            m[2].update(model.edge_faces[e])
    vf.vertices = [(p, d, sorted(f)) for p, d, f in merged.values()]


def _extensions(faces, vf):
    """Outlines extended past their ends, as (faces, kind, a, b, box).

    Edge-on faces: a plane as its line, a cylinder seen end-on as its full
    circle. Model edges (outlines on other levels, hidden ones included):
    straight edges as lines, arcs seen end-on as circles. Any other profile,
    and arcs too (drafting extends them either way; the value decides), by
    the tangent rays at its ends.
    """
    reg, model = vf.reg, faces.model
    for f, s in enumerate(faces.surfaces):
        if not vf.edge_on[f] or not len(vf.tri2[f]):
            continue
        if s.kind == "plane":
            d = np.cross(s.normal, reg.w)
            d2 = np.array([d @ reg.u, d @ reg.v])
            if np.linalg.norm(d2) > 0.5:
                vf.curves.append(((f,), "line", vf.tri2[f][0, 0], d2 / np.linalg.norm(d2), vf.box[f]))
        elif s.kind == "cylinder" and abs(s.axis @ reg.w) > PARALLEL:
            vf.curves.append(((f,), "circle", np.array([s.center @ reg.u, s.center @ reg.v]), s.radius, vf.box[f]))
    for e, f, p0, d in vf.rays:
        if faces.surfaces[f].kind != "plane":
            vf.curves.append(((f,), "ray", p0, d, vf.box[f]))
    P = model.pts
    for e, edge in enumerate(model.edges):
        idx = np.nonzero(model.edge_of == e)[0]
        if not len(idx):
            continue
        if reg.plane is not None:
            kept = (P[idx] - reg.plane[0]) @ reg.plane[1] >= -1e-6
            if kept.any():
                q = np.column_stack([P[idx[kept]] @ reg.u, P[idx[kept]] @ reg.v])
                vf.edges.append((e, q))
            if not kept.all():
                continue    # cut by the section plane: its extension is not drawn
        q = np.column_stack([P[idx] @ reg.u, P[idx] @ reg.v])
        if reg.plane is None:
            vf.edges.append((e, q))
        if np.ptp(q, axis=0).max() < 0.01:
            continue    # seen end-on: a vertex
        box = np.r_[q.min(0), q.max(0)]
        fl = model.edge_faces[e]
        own = tuple(sorted(set(f for f in fl if vf.edge_on[f]) or set(fl)))
        c = BRepAdaptor_Curve(edge)
        t = c.GetType()
        if t == GeomAbs_Line:
            d = np.array(c.Line().Direction().Coord())
            d2 = np.array([d @ reg.u, d @ reg.v])
            vf.curves.append((own, "line", q[0], d2 / np.linalg.norm(d2), box))
            continue
        if t == GeomAbs_Circle and abs(np.array(c.Circle().Axis().Direction().Coord()) @ reg.w) > PARALLEL:
            ctr = np.array(c.Circle().Location().Coord())
            vf.curves.append((own, "circle", np.array([ctr @ reg.u, ctr @ reg.v]), c.Circle().Radius(), box))
        for P0, T in _curve_ends(model)[e] or []:
            t2 = np.array([T @ reg.u, T @ reg.v])
            if np.linalg.norm(t2) > 1e-6:
                vf.curves.append((own, "ray", np.array([P0 @ reg.u, P0 @ reg.v]), t2 / np.linalg.norm(t2), box))


def _end_on(faces, vf):
    """Edges seen as points (vertices of a profile) and, for the edge-on faces
    meeting there, the direction a tangent extension leaves the face."""
    model, reg = faces.model, vf.reg
    P = model.pts
    q = np.column_stack([P @ reg.u, P @ reg.v])
    keep = np.ones(len(P), bool) if reg.plane is None else (P - reg.plane[0]) @ reg.plane[1] > -1e-6
    order = np.argsort(model.edge_of, kind="stable")
    bounds = np.searchsorted(model.edge_of[order], np.arange(len(model.edges) + 1))
    per_edge = [order[bounds[e]:bounds[e + 1]] for e in range(len(model.edges))]
    point_edges = set()
    for e, idx in enumerate(per_edge):
        if len(idx) and keep[idx].all() and np.ptp(q[idx], axis=0).max() < 0.01:
            point_edges.add(e)
            vf.points.append((e, q[idx].mean(0), float((P[idx] @ reg.w).max())))
    # the profile edges of each edge-on face, with their end points and end tangents
    ends = []
    for e, ce in enumerate(_curve_ends(model)):
        if e in point_edges or ce is None:
            continue
        for P0, T in ce:
            ends.append((e, np.array([P0 @ reg.u, P0 @ reg.v]), np.array([T @ reg.u, T @ reg.v])))
    for e, p0, _ in vf.points:
        for f in set(model.edge_faces[e]):
            if not vf.edge_on[f]:
                continue
            for e2, q0, t2 in ends:
                if f in model.edge_faces[e2] and np.linalg.norm(q0 - p0) < 0.01 and np.linalg.norm(t2) > 1e-6:
                    vf.rays.append((e, f, p0, t2 / np.linalg.norm(t2)))
                    break


_CURVE_ENDS = {}


def _curve_ends(model):
    """Per edge: [(end point, outward tangent)] at both ends (None if degenerate)."""
    key = id(model)
    if key not in _CURVE_ENDS:
        out = []
        for e in model.edges:
            c = BRepAdaptor_Curve(e)
            u0, u1 = c.FirstParameter(), c.LastParameter()
            if not (math.isfinite(u0) and math.isfinite(u1)) or u1 - u0 < 1e-9:
                out.append(None)
                continue
            r = []
            for u, sgn in ((u0, -1.0), (u1, 1.0)):
                p, d = gp_Pnt(), gp_Vec()
                c.D1(u, p, d)
                r.append((np.array(p.Coord()), sgn * np.array(d.Coord())))
            out.append(r)
        _CURVE_ENDS[key] = out
    return _CURVE_ENDS[key]


def _seg_dist(p, a, b):
    ab = b - a
    L = (ab ** 2).sum(-1)
    s = np.clip(((p - a) * ab).sum(-1) / np.where(L > 0, L, 1), 0, 1)
    return np.linalg.norm(a + ab * s[..., None] - p, axis=-1)


def _tri_dist(p, q):
    """Distance from point p (2,) to each projected triangle q (m, 3, 2)."""
    a, b, c = q[:, 0], q[:, 1], q[:, 2]
    d = np.minimum(np.minimum(_seg_dist(p, a, b), _seg_dist(p, b, c)), _seg_dist(p, c, a))

    def cross(o, x, y):
        return (x[..., 0] - o[..., 0]) * (y[..., 1] - o[..., 1]) - (x[..., 1] - o[..., 1]) * (y[..., 0] - o[..., 0])
    s1, s2, s3 = cross(a, b, p), cross(b, c, p), cross(c, a, p)
    inside = ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))
    return np.where(inside, 0.0, d)


def near(vf, x, reach=NEAR):
    """{face: (2-D distance, depth of the nearest part)} for faces within reach of drawing point x."""
    b = vf.box
    with np.errstate(invalid="ignore"):
        cand = np.nonzero((b[:, 0] - reach <= x[0]) & (x[0] <= b[:, 2] + reach) &
                          (b[:, 1] - reach <= x[1]) & (x[1] <= b[:, 3] + reach))[0]
    out = {}
    for f in cand:
        d = _tri_dist(x, vf.tri2[f])
        j = int(np.argmin(d))
        if d[j] <= reach:
            close = d <= d[j] + 0.05
            out[int(f)] = (float(d[j]), float(vf.depth[f][close].max()))
    return out


def _axis_dist(s, reg, x):
    """2-D distance from x to the projected axis of a cylinder."""
    c2 = np.array([s.center @ reg.u, s.center @ reg.v])
    a2 = np.array([s.axis @ reg.u, s.axis @ reg.v])
    if np.linalg.norm(a2) < 1e-6:
        return float(np.linalg.norm(x - c2))
    a2 /= np.linalg.norm(a2)
    r = x - c2
    return float(abs(r[0] * a2[1] - r[1] * a2[0]))


# ---------------------------------------------------------------- matching

@dataclass
class Match:
    source: str                 # ordinate | annotation | origin (an ordinate axis's zero)
    id: int
    view: int
    kind: str
    text: str
    value: float | None
    status: str = "unmatched"   # matched ambiguous mismatch unmatched skipped
    features: list = field(default_factory=list)       # [{role, feature, faces, delta, dist, surface}]
    alternatives: list = field(default_factory=list)
    note: str = ""


def _feat(fc, s, role, delta=None, dist=None, how=None):
    d = {"role": role, "feature": s.feature, "faces": fc.features[s.feature],
         "surface": s.describe()}
    if how:
        d["how"] = how
    if delta is not None:
        d["delta"] = round(float(delta), 4)
    if dist is not None:
        d["dist"] = round(float(dist), 3)
    return d


def _drawing_dir(reg, page_vec):
    """Page direction -> model direction in the view plane (unit)."""
    d = np.array([page_vec[0], -page_vec[1]], float)
    d /= np.linalg.norm(d)
    return d[0] * reg.u + d[1] * reg.v


def _bucket(delta):
    return 0 if abs(delta) <= VAL_TOL else math.ceil(abs(delta) / VAL_TOL)


# candidate kinds, most specific first: a plane or axis at the coordinate
# explains a cylinder tangent to it, or a profile vertex on it
PRIORITY = {"plane": 0, "axis": 0, "surface": 0, "tangent": 1, "vertex": 2, "sharp": 3,
            "on_profile": 4}


def _cand(items, ident, how, delta, dist, depth=0.0, extra=None):
    """items: [(Surface, how)] forming one target."""
    return {"items": items, "ident": ident, "how": how, "delta": delta, "dist": dist,
            "prio": PRIORITY[how], "extra": extra or {},
            "key": (_bucket(delta), PRIORITY[how], round(dist / 0.05), -depth)}


def _best(cands):
    """Keep the best (lowest key) candidate per target, sorted."""
    best = {}
    for c in cands:
        if c["ident"] not in best or c["key"] < best[c["ident"]]["key"]:
            best[c["ident"]] = c
    return sorted(best.values(), key=lambda c: c["key"])


def _feats(fc, c, role):
    out = []
    for s, how in c["items"]:
        f = _feat(fc, s, role, c["delta"], c["dist"], how)
        f.update(c["extra"])
        out.append(f)
    return out


def _decide(m, fc, ranked, role="target"):
    """Status from ranked candidates: the best one is ambiguous only if another
    candidate of the same kind (or a more specific one) fits the value too."""
    if not ranked:
        return
    top = ranked[0]
    if abs(top["delta"]) > VAL_REPORT:
        m.alternatives = [f for c in ranked[:3] for f in _feats(fc, c, role)]
        return
    m.features = _feats(fc, top, role)
    ok = [c for c in ranked[1:] if abs(c["delta"]) <= VAL_TOL]
    m.alternatives = [f for c in ok[:4] for f in _feats(fc, c, role)]
    if abs(top["delta"]) > VAL_TOL:
        m.status = "mismatch"
    elif any(c["prio"] <= top["prio"] and c["dist"] <= top["dist"] + 0.05 for c in ok):
        m.status = "ambiguous"
    else:
        m.status = "matched"


def _coaxial(fc, s):
    """Cylinders sharing s's axis (to the drawing's 0.01 mm)."""
    return [t for t in fc.surfaces if t.kind == "cylinder" and abs(t.axis @ s.axis) > PARALLEL and
            np.linalg.norm((t.center - s.center) - ((t.center - s.center) @ s.axis) * s.axis) < VAL_TOL]


def _curve_near(cv, x):
    """(distance from x to the curve, nearest point, unit tangent there)."""
    kind, a, b = cv[1:4]
    if kind == "circle":
        r = x - a
        n = np.linalg.norm(r)
        if n < 1e-9:
            return math.inf, None, None
        p = a + r / n * b
        return abs(n - b), p, np.array([-r[1], r[0]]) / n
    t = (x - a) @ b
    if kind == "ray" and t < 0.05:
        return math.inf, None, None
    p = a + t * b
    return float(np.linalg.norm(x - p)), p, b


def _on_faces(vf, faces, X, tol=0.02):
    """True if X lies on the projection of one of `faces` (not on an extension)."""
    for f in faces:
        q = vf.tri2[f]
        if len(q) and _tri_dist(X, q).min() <= tol:
            return True
    return False


def _intersect(c1, c2, x):
    """Intersection of two extended curves nearest x (None if they don't meet)."""
    k1, k2 = c1[1], c2[1]
    if k1 == "circle" and k2 == "circle":
        (a1, r1), (a2, r2) = c1[2:4], c2[2:4]
        d = np.linalg.norm(a2 - a1)
        if d < 1e-9 or d > r1 + r2 or d < abs(r1 - r2):
            return None
        m = (d ** 2 + r1 ** 2 - r2 ** 2) / (2 * d)
        h = math.sqrt(max(r1 ** 2 - m ** 2, 0.0))
        e = (a2 - a1) / d
        base = a1 + m * e
        pts = [base + h * np.array([-e[1], e[0]]), base - h * np.array([-e[1], e[0]])]
    elif "circle" in (k1, k2):
        circ, lin = (c1, c2) if k1 == "circle" else (c2, c1)
        ca, R = circ[2:4]
        la, ld = lin[2:4]
        t0 = (ca - la) @ ld
        foot = la + t0 * ld
        off = np.linalg.norm(ca - foot)
        if off > R:
            return None
        h = math.sqrt(R ** 2 - off ** 2)
        pts = [foot + h * ld, foot - h * ld]
        if lin[1] == "ray":
            pts = [p for p in pts if (p - la) @ ld >= 0.05]
    else:
        (a1, d1), (a2, d2) = c1[2:4], c2[2:4]
        den = d1[0] * d2[1] - d1[1] * d2[0]
        r = a2 - a1
        t1 = (r[0] * d2[1] - r[1] * d2[0]) / den
        t2 = (r[0] * d1[1] - r[1] * d1[0]) / den
        if (k1 == "ray" and t1 < 0.05) or (k2 == "ray" and t2 < 0.05):
            return None
        pts = [a1 + t1 * d1]
    return min(pts, key=lambda p: np.linalg.norm(p - x)) if pts else None


def _sharps(fc, vf, x, reach=NEAR, extend=25.0):
    """Virtual sharps at x: two edge-on faces whose extended outlines cross
    there (the corner a fillet or chamfer removes).

    Yields (point, faces1, faces2, how far the outlines had to be extended,
    how many of the two are tangent rays rather than the surfaces themselves).
    """
    near_x = []
    for cv in vf.curves:
        b = cv[4]
        gap = max(b[0] - x[0], x[0] - b[2], b[1] - x[1], x[1] - b[3], 0.0)
        if gap > extend:
            continue
        d, p, t = _curve_near(cv, x)
        if d <= reach:
            near_x.append((cv, t, gap))
    for i in range(len(near_x)):
        for j in range(i + 1, len(near_x)):
            (c1, t1, g1), (c2, t2, g2) = near_x[i], near_x[j]
            if abs(t1[0] * t2[1] - t1[1] * t2[0]) < 0.05:
                continue    # (nearly) tangent: no corner
            X = _intersect(c1, c2, x)
            if X is None or np.linalg.norm(X - x) > reach:
                continue
            # a removed corner lies off both outlines: where one outline itself
            # crosses another's extension there is no corner (a hole's circle
            # cutting an arc)
            if any(_on_faces(vf, cv[0], X) for cv in (c1, c2)):
                continue
            if True:
                yield X, c1[0], c2[0], max(g1, g2), (c1[1] == "ray") + (c2[1] == "ray")


def ordinate_coord(o, reg, fits):
    """(axis index, model coordinate the ordinate's value puts its feature at),
    or None without an origin. Signs come from page positions."""
    ax = 0 if o["axis"] == "x" else 1
    fit = fits.get((o["view"], o["axis"]))
    if fit is None:
        return None
    pp = np.array([o["page_pos"], 0.0]) if ax == 0 else np.array([0.0, o["page_pos"]])
    po = np.array([fit, 0.0]) if ax == 0 else np.array([0.0, fit])
    c_page = reg.to_drawing(pp)[0][ax]
    c0 = reg.to_drawing(po)[0][ax]
    return ax, (c0 + math.copysign(o["value"], c_page - c0) if o["value"] else c0)


def match_ordinate(o, fc, vf, fits, partner=None):
    """An ordinate: the feature at the value's coordinate along the view axis.

    The extension line ends on a plane seen edge-on, a cylinder's tangent or
    centre line, a profile vertex, or a virtual sharp (the corner a fillet
    rounds off, drawn with thin extensions). `partner` is (axis, coordinate)
    of an ordinate along the other axis ending at the same point: a point
    target (vertex, sharp) must then match both values.
    """
    reg = vf.reg
    m = Match("ordinate", o["index"], o["view"], "ordinate", o["text"], o["value"])
    ax = 0 if o["axis"] == "x" else 1
    A = reg.u if ax == 0 else reg.v
    ac = ordinate_coord(o, reg, fits)
    anchors = o["anchors"]
    if not anchors and o.get("line"):
        # the parser found nothing at the line's end (it can run along the
        # outline): try points along it, from the inner end
        a, b = (np.array(p) for p in o["line"])
        n = max(2, int(np.linalg.norm(b - a) / (reg.k * 0.5)) + 1)
        anchors = [{"page": list(b + (a - b) * t), "kind": "line"} for t in np.linspace(0, 1, n)]
        m.note = "no anchors: searched along the extension line"
    if ac is None or not anchors:
        m.note = "no ordinate origin" if ac is None else "no anchors"
        return m
    c = ac[1]

    def point_delta(X):
        d = X[ax] - c
        if partner is None:
            return d, {}
        d2 = X[partner[0]] - partner[1]
        return (d if abs(d) >= abs(d2) else math.copysign(abs(d2), d)), {"delta_other_axis": round(float(d2), 4)}

    cands = []
    for an in anchors:
        x = reg.to_drawing(np.array(an["page"]))[0]
        hits = near(vf, x, CENTER_REACH + 60 if an["kind"] == "center" else ALONG)
        for f, (dist, depth) in hits.items():
            s = fc.surfaces[f]
            for cc, how in s.coords(A):
                if how == "axis":
                    if an["kind"] != "center":
                        continue
                    if abs(s.axis @ reg.w) > PARALLEL:
                        dist = max(0.0, _axis_dist(s, reg, x) - s.radius - CENTER_REACH)
                    else:
                        dist = max(0.0, dist - CENTER_REACH)
                    if dist > NEAR:
                        continue
                    co = _coaxial(fc, s)
                    ident = ("axis", min(t.feature for t in co))
                    cands.append(_cand([(t, "axis") for t in co], ident, "axis", cc - c, dist, depth))
                elif dist <= NEAR or (dist <= ALONG and abs(cc - x[ax]) <= NEAR):
                    cands.append(_cand([(s, how)], ("face", s.feature, how), how, cc - c, dist, depth))
        for p, depth, faces in vf.vertices:
            dist = float(np.linalg.norm(p - x))
            if dist <= NEAR or (dist <= ALONG and abs(p[ax] - x[ax]) <= NEAR):
                items = [(fc.surfaces[f], "vertex") for f in faces]
                delta, extra = point_delta(p)
                cands.append(_cand(items, ("vertex", round(p[0], 2), round(p[1], 2)), "vertex",
                                   delta, dist, depth, extra))
        for X, fs1, fs2, ext, n_rays in _sharps(fc, vf, x):
            # one corner can come from several pairs (a fillet's end tangent runs
            # along the plane it blends into): one target per point, preferring
            # the surfaces themselves and the shortest extension
            items = {}
            for f in fs1 + fs2:
                items.setdefault(fc.surfaces[f].feature, (fc.surfaces[f], "sharp"))
            ident = ("sharp", round(X[0], 2), round(X[1], 2))
            delta, extra = point_delta(X)
            c_ = _cand(list(items.values()), ident, "sharp", delta, float(np.linalg.norm(X - x)),
                       extra=dict(extra, point=[round(float(v), 4) for v in X]))
            c_["key"] += (n_rays, ext)
            cands.append(c_)
        # a point on a free-form profile (its coordinates define the curve)
        for f, (dist, depth) in near(vf, x, NEAR).items():
            s = fc.surfaces[f]
            if s.kind == "other" and vf.edge_on[f]:
                cands.append(_cand([(s, "on_profile")], ("face", s.feature, "on_profile"), "on_profile",
                                   x[ax] - c, dist, depth))
    _decide(m, fc, _best(cands))
    return m


def _ann_points(reg, pts):
    return [reg.to_drawing(np.array(p))[0] for p in pts]


def _axis_2d(s, reg):
    """Projected axis of a cylinder/cone: (point, unit direction or None if seen end-on)."""
    c2 = np.array([s.center @ reg.u, s.center @ reg.v])
    a2 = np.array([s.axis @ reg.u, s.axis @ reg.v])
    n = np.linalg.norm(a2)
    return c2, (a2 / n if n > 1e-6 else None)


def match_radius(a, fc, vf):
    """Radius / diameter: a cylinder (or a cone, at the anchors' height) of
    that size, with the anchors on its outline: on the circle when seen
    end-on, on the silhouettes (or their extension lines) when seen
    side-on. A diameter whose tips match no surface may be a pitch circle
    through hole axes."""
    reg = vf.reg
    m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"])
    pts = _ann_points(reg, [x["page"] for x in a["anchors"]] or a["tips"])
    R = a["value"] / 2 if a["kind"] == "diameter" else a["value"]
    if not pts:
        m.note = "no anchors"
        return m
    reach = 40.0
    hits = [near(vf, x, reach) for x in pts]
    cands = []
    for f, s in enumerate(fc.surfaces):
        if s.kind not in ("cylinder", "cone") or any(f not in h for h in hits):
            continue
        c2, a2 = _axis_2d(s, reg)
        pos, sides = [], []
        for x in pts:
            if a2 is None:
                if s.kind == "cone":
                    break
                pos.append(abs(np.linalg.norm(x - c2) - s.radius))
                continue
            if a["kind"] == "radius":
                break   # a radius is dimensioned where the arc is seen
            r = x - c2
            along = r @ a2
            off = r[0] * a2[1] - r[1] * a2[0]
            sides.append(np.sign(off))
            P = s.center + along * s.axis
            pos.append(abs(abs(off) - s.radius_at(P)))
        else:
            if len(sides) == 2 and sides[0] == sides[1]:
                continue    # both silhouette anchors on one side: not this diameter
            if not pos:
                continue
            p = max(pos)
            if p > 1.0:
                continue
            gap = max(h[f][0] for h in hits)
            size = s.radius_at(s.center + ((np.mean(pts, 0) - c2) @ a2) * s.axis) if a2 is not None else s.radius
            delta = (size - R) * (2 if a["kind"] == "diameter" else 1)
            c = _cand([(s, None)], s.feature, "surface", delta, p, -gap)
            # a cone matches at the anchors' height only by construction; a
            # cylinder of that size is the more specific target
            c["key"] = (_bucket(delta), round(p / 0.05), s.kind == "cone", round(gap))
            cands.append(c)
    ranked = _best(cands)
    if ranked and ranked[0]["items"][0][0].kind == "cylinder":
        # the countersink or chamfer cone meeting this hole has its size there too
        top = ranked[0]["items"][0][0]
        ranked = ranked[:1] + [c for c in ranked[1:] if not (
            c["items"][0][0].kind == "cone" and abs(c["items"][0][0].axis @ top.axis) > PARALLEL and
            np.linalg.norm(np.cross(c["items"][0][0].center - top.center, top.axis)) < VAL_TOL)]
    if ranked and ranked[0]["dist"] > NEAR and abs(ranked[0]["delta"]) <= VAL_TOL:
        m.note = f"drawn {ranked[0]['dist']:.2f} mm from the model face"
    if a["kind"] == "diameter" and len(pts) == 2 and \
            (not ranked or abs(ranked[0]["delta"]) > VAL_TOL or ranked[0]["dist"] > NEAR):
        pc = _pitch_circle(fc, reg, pts, R)
        if pc:
            ranked = [pc] + ranked
            m.note = "pitch circle through hole axes"
    _decide(m, fc, ranked)
    return m


def _pitch_circle(fc, reg, pts, R):
    """Cylinders seen end-on whose axes lie on the circle with diameter tips pts."""
    mid = (pts[0] + pts[1]) / 2
    on = {}
    for s in fc.surfaces:
        if s.kind != "cylinder" or abs(s.axis @ reg.w) < PARALLEL:
            continue
        c2 = np.array([s.center @ reg.u, s.center @ reg.v])
        d = np.linalg.norm(c2 - mid) - R
        if abs(d) < 0.05:
            on.setdefault(s.feature, (s, d))
    if len(on) < 2:
        return None
    delta = 2 * float(np.mean([d for _, d in on.values()]))
    return _cand([(s, "pitch_circle") for s, _ in on.values()], ("pitch",), "axis", delta, 0.0)


def _side_targets(fc, vf, m2, s_i, pts, tol=0.1, reach=40.0):
    """Features at coordinate s_i along page-plane direction m2: planes and
    cylinder tangents / axes (via Surface.coords), and model edges seen as a
    line across m2 (a circular edge seen edge-on, e.g. where a countersink
    meets its hole). pts locate the extension line, for ranking."""
    reg = vf.reg
    M = m2[0] * reg.u + m2[1] * reg.v
    gaps = {}
    for x in pts:
        for f, (d, _) in near(vf, x, reach).items():
            gaps[f] = min(gaps.get(f, math.inf), d)
    out = []
    for f, gap in gaps.items():
        s = fc.surfaces[f]
        for cc, how in s.coords(M):
            if abs(cc - s_i) <= tol:
                out.append(([(s, how)], ("face", s.feature, how), cc, how if how != "axis" else "tangent", gap))
    for e, q in vf.edges:
        t = q @ m2
        if np.ptp(t) < 0.01 and abs(t.mean() - s_i) <= tol:
            gap = min(float(np.min(np.linalg.norm(q - x, axis=1))) for x in pts)
            if gap > reach:
                continue
            items = [(fc.surfaces[f], "edge") for f in sorted(set(fc.model.edge_faces[e]))]
            out.append((items, ("edge", e), float(t.mean()), "vertex", gap))
    return out


def match_linear(a, fc, vf):
    """Linear dimension: one target at each arrow tip's coordinate along the
    dimension line, the value apart. Tips are exact where extension-line
    anchors can be ambiguous; anchors only rank targets by proximity."""
    reg = vf.reg
    m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"])
    if len(a["tips"]) < 2:
        m.note = "needs two tips"
        return m
    tips = _ann_points(reg, a["tips"][:2])
    m2 = tips[1] - tips[0]
    m2 /= np.linalg.norm(m2)
    anchors = _ann_points(reg, [x["page"] for x in a["anchors"]])
    sides = []
    for tip in tips:
        s_i = tip @ m2
        own = [x for x in anchors if abs(x @ m2 - s_i) < 0.3]
        sides.append(_side_targets(fc, vf, m2, s_i, own + [tip]))
    cands = []
    for it1, id1, c1, h1, g1 in sides[0]:
        for it2, id2, c2, h2, g2 in sides[1]:
            if id1 == id2:
                continue
            delta = abs(c2 - c1) - a["value"]
            c = {"items": [(s, h) for s, h in it1], "items2": it2, "ident": (id1, id2), "delta": delta,
                 "dist": max(g1, g2), "prio": PRIORITY[h1] + PRIORITY[h2], "extra": {},
                 "key": (_bucket(delta), PRIORITY[h1] + PRIORITY[h2], round((g1 + g2) / 0.5))}
            cands.append(c)
    ranked = _best(cands)
    if not ranked:
        return m

    def feats(c):
        return [dict(f, role="side1") for f in _feats(fc, {**c, "items": c["items"]}, "side1")] + \
               [dict(f, role="side2") for f in _feats(fc, {**c, "items": c["items2"]}, "side2")]
    top = ranked[0]
    if abs(top["delta"]) > VAL_REPORT:
        m.alternatives = feats(top)
        return m
    m.features = feats(top)
    ok = [c for c in ranked[1:] if abs(c["delta"]) <= VAL_TOL and c["prio"] <= top["prio"]
          and c["key"][2] <= top["key"][2]]
    m.alternatives = [f for c in ok[:2] for f in feats(c)]
    m.status = "mismatch" if abs(top["delta"]) > VAL_TOL else "ambiguous" if ok else "matched"
    return m


def match_surface(a, fc, vf):
    """GD&T leader tip, datum triangle base or surface texture symbol tip:
    the edge-on surface there, or
    whose extension line it sits on (a datum on an extension line).

    The frame / label sits outside the material, so a plane whose outward
    normal points toward the label is preferred.
    """
    reg = vf.reg
    m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"])
    pts = a["tips"] or [x["page"] for x in a["anchors"]]
    if not pts:
        m.note = "no tip"
        return m
    x = reg.to_drawing(np.array(pts[0]))[0]
    geo = a.get("geometry") or {}
    if geo.get("outward_page"):
        # a surface texture symbol opens away from the surface
        out2 = reg.to_drawing(np.array(pts[0]) + 10 * np.array(geo["outward_page"]))[0] - x
    else:
        out2 = reg.to_drawing(np.array(a["label_center"]))[0] - x
    out = out2[0] * reg.u + out2[1] * reg.v
    out /= np.linalg.norm(out)
    cands = []
    for f, (dist, depth) in near(vf, x, NEAR).items():
        if not vf.edge_on[f]:
            continue    # seen face-on: covers an area, not the outline under the tip
        s = fc.surfaces[f]
        facing = float(s.normal @ out) if s.kind == "plane" else 0.0
        c = _cand([(s, None)], s.feature, "surface", 0.0, dist, depth)
        c["key"] = (round(dist / 0.05), -facing, -depth)
        cands.append(c)
    if not cands:
        # on an extension line: of an edge-on plane, or tangent to a circle
        # seen end-on (drawn along the view axes)
        for s in fc.surfaces:
            if s.kind != "cylinder" or not len(vf.tri2[s.face]):
                continue
            c2 = np.array([s.center @ reg.u, s.center @ reg.v])
            if abs(s.axis @ reg.w) > PARALLEL:
                d = min(abs(abs(x[0] - c2[0]) - s.radius), abs(abs(x[1] - c2[1]) - s.radius))
            else:
                # seen side-on: its silhouettes run along the axis, R from it
                b = vf.box[s.face]
                if max(b[0] - x[0], x[0] - b[2], b[1] - x[1], x[1] - b[3]) > 40:
                    continue
                d = abs(_axis_dist(s, reg, x) - s.radius)
            if d <= NEAR:
                c = _cand([(s, "extension")], s.feature, "surface", 0.0, d)
                c["key"] = (round(np.linalg.norm(x - c2) - s.radius), 0.0)
                cands.append(c)
        for cv in vf.curves:
            if cv[1] != "line" or len(cv[0]) != 1 or not vf.edge_on[cv[0][0]]:
                continue
            d, _, _ = _curve_near(cv, x)
            if d > NEAR:
                continue
            f = cv[0][0]
            b = cv[4]
            gap = max(b[0] - x[0], x[0] - b[2], b[1] - x[1], x[1] - b[3], 0.0)
            s = fc.surfaces[f]
            c = _cand([(s, "extension")], s.feature, "surface", 0.0, d)
            c["key"] = (round(gap), -float(s.normal @ out))
            cands.append(c)
    ranked = _best(cands)
    if ranked:
        m.features = _feats(fc, {**ranked[0], "delta": None}, "target")
        m.alternatives = [f for c in ranked[1:4] for f in _feats(fc, {**c, "delta": None}, "target")]
        m.status = "matched"
    return m


# printed angles are whole degrees here; the model's are exact to this (deg)
ANG_TOL = 0.05
ANG_REPORT = 1.0


def match_angle(a, fc, vf, views):
    geo = a.get("geometry") or {}
    if geo.get("type") == "angular_ordinate":
        return match_angular_ordinate(a, fc, vf)
    m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"])
    if a["value"] == 0:
        m.status = "skipped"
        m.note = "0° between coinciding legs: the second feature cannot be told apart"
        return m
    return match_angle_between(a, fc, vf, m)


def match_angular_ordinate(a, fc, vf):
    """The feature at the end of an angular ordinate's radial line: a hole
    axis seen end-on, a vertex, or a radial plane, at the printed angle
    about the origin."""
    reg = vf.reg
    m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"])
    geo = a["geometry"]
    O = reg.to_drawing(np.array(geo["origin"]))[0]
    near_end, far_end = (reg.to_drawing(np.array(p))[0] for p in geo["line"])
    u = far_end - near_end
    u /= np.linalg.norm(u)
    conv = (geo["zero_page_dir"], geo["clockwise"])

    def angle_of(p):
        # drawing coordinates have y up, like the page convention's angles
        d = math.degrees(math.atan2(p[1] - O[1], p[0] - O[0])) % 360
        zero, cw = conv
        return ((zero - d) if cw else (d - zero)) % 360

    def on_line(p):
        r = p - far_end
        return abs(r[0] * u[1] - r[1] * u[0]), abs(r @ u)

    cands = []
    for s in fc.surfaces:
        if s.kind == "cylinder" and abs(s.axis @ reg.w) > PARALLEL:
            c2 = np.array([s.center @ reg.u, s.center @ reg.v])
            off, along = on_line(c2)
            if off <= NEAR and along <= ALONG:
                co = _coaxial(fc, s)
                delta = (angle_of(c2) - a["value"] + 180) % 360 - 180
                cands.append(_cand([(t, "axis") for t in co], ("axis", min(t.feature for t in co)),
                                   "axis", delta, along))
    for p, depth, faces in vf.vertices:
        off, along = on_line(p)
        if off <= NEAR and along <= ALONG:
            delta = (angle_of(p) - a["value"] + 180) % 360 - 180
            cands.append(_cand([(fc.surfaces[f], "vertex") for f in faces],
                               ("vertex", round(p[0], 2), round(p[1], 2)), "vertex", delta, along))
    ranked = _best(cands)
    for c in ranked:
        c["key"] = (0 if abs(c["delta"]) <= ANG_TOL else math.ceil(abs(c["delta"]) / ANG_TOL),) + c["key"][1:]
    ranked.sort(key=lambda c: c["key"])
    if ranked and abs(ranked[0]["delta"]) <= ANG_REPORT:
        top = ranked[0]
        m.features = _feats(fc, top, "target")
        ok = [c for c in ranked[1:] if abs(c["delta"]) <= ANG_TOL and c["prio"] <= top["prio"]]
        m.alternatives = [f for c in ok[:3] for f in _feats(fc, c, "target")]
        m.status = "matched" if abs(top["delta"]) <= ANG_TOL and not ok else \
            "ambiguous" if abs(top["delta"]) <= ANG_TOL else "mismatch"
    elif ranked:
        m.alternatives = _feats(fc, ranked[0], "target")
    m.note = f"angular ordinate about the origin axis; 0° = page direction {conv[0]}°, " \
             f"{'clockwise' if conv[1] else 'counter-clockwise'}"
    return m


def match_angle_between(a, fc, vf, m):
    """Angle between two legs: a cone whose included angle it is (its
    silhouettes are the legs), or two edge-on planes, one along each leg."""
    reg = vf.reg
    tips = _ann_points(reg, a["tips"])
    vertex = reg.to_drawing(np.array(a["geometry"]["vertex"]))[0]
    # the legs' features lie between the vertex and the arrow tips
    hits = []
    for x in tips:
        h = {}
        for t in np.linspace(0, 1, 21):
            for f, v in near(vf, vertex + t * (x - vertex), NEAR).items():
                h.setdefault(f, v)
        hits.append(h)
    cands = []
    for f in set(hits[0]) & set(hits[1]):
        s = fc.surfaces[f]
        if s.kind == "cone" and abs(s.axis @ reg.w) < 1 - PARALLEL:
            inc = 2 * math.degrees(math.atan(abs(s.slope)))
            cands.append(_cand([(s, "cone")], ("cone", s.feature), "surface", inc - a["value"], 0.0))
    for f1 in hits[0]:
        for f2 in hits[1]:
            s1, s2 = fc.surfaces[f1], fc.surfaces[f2]
            if s1.kind == s2.kind == "plane" and vf.edge_on[f1] and vf.edge_on[f2] and s1.feature != s2.feature:
                ang = math.degrees(math.acos(np.clip(abs(s1.normal @ s2.normal), 0, 1)))
                delta = min(abs(ang - a["value"]), abs(180 - ang - a["value"]))
                cands.append(_cand([(s1, "leg"), (s2, "leg")], ("legs", s1.feature, s2.feature),
                                   "plane", delta, 0.0))
    ranked = _best(cands)
    for c in ranked:
        c["key"] = (0 if abs(c["delta"]) <= ANG_TOL else math.ceil(abs(c["delta"]) / ANG_TOL),) + c["key"][1:]
    ranked.sort(key=lambda c: c["key"])
    if ranked and abs(ranked[0]["delta"]) <= ANG_REPORT:
        m.features = _feats(fc, ranked[0], "target")
        m.status = "matched" if abs(ranked[0]["delta"]) <= ANG_TOL else "mismatch"
    return m


def match_origins(fc, views, fits):
    """The zero feature of each view's ordinate axes: a cylinder axis (all
    coaxial cylinders) or an edge-on plane through the origin point, at the
    origin's coordinate. The other axis's origin, when fitted too, places
    the point; a lone axis only fixes the coordinate."""
    out = []
    for (vid, axis), o_page in sorted(fits.items()):
        if vid not in views or o_page is None:
            continue
        vf = views[vid]
        reg = vf.reg
        ax = 0 if axis == "x" else 1
        A = reg.u if ax == 0 else reg.v
        other = fits.get((vid, "y" if axis == "x" else "x"))
        page = np.zeros(2)
        page[ax] = o_page
        if other is not None:
            page[1 - ax] = other
        O = reg.to_drawing(page)[0]
        m = Match("origin", len(out), vid, "origin", f"{axis} = 0", 0.0)
        cands = []
        for s in fc.surfaces:
            for cc, how in s.coords(A):
                if how == "tangent" or abs(cc - O[ax]) > VAL_REPORT:
                    continue
                if other is not None:
                    if how == "axis" and abs(s.axis @ reg.w) > PARALLEL:
                        off = _axis_dist(s, reg, O)
                    elif how == "plane" and vf.edge_on[s.face]:
                        off = 0.0      # an edge-on plane at the coordinate passes through O
                    else:
                        continue
                    if off > NEAR:
                        continue
                if how == "axis":
                    co = _coaxial(fc, s)
                    cands.append(_cand([(t, "axis") for t in co], ("axis", min(t.feature for t in co)),
                                       "axis", cc - O[ax], 0.0))
                else:
                    cands.append(_cand([(s, how)], ("face", s.feature), how, cc - O[ax], 0.0))
        _decide(m, fc, _best(cands))
        m.note = f"axis {axis}"
        out.append(m)
    return out


def match_all(fc, regs, ords, anns):
    views = {vid: project(fc, r) for vid, r in regs.items()}
    fits = {(v["view"], v["axis"]): v["origin_page"] for v in ords["views"]}
    out = match_origins(fc, views, fits)
    # x and y ordinates ending at the same point locate it in both directions
    ends = {}
    for i, o in enumerate(ords["ordinates"]):
        if o["view"] in regs and o["anchors"]:
            ac = ordinate_coord(o, regs[o["view"]], fits)
            if ac is not None:
                # where two extension lines merely cross is no shared end
                own = [a["page"] for a in o["anchors"] if a["kind"] != "crossing"]
                if own:
                    ends[i] = (o["view"], ac, regs[o["view"]].to_drawing(np.array(own)))
    for i, o in enumerate(ords["ordinates"]):
        o = dict(o, index=i)
        if o["view"] not in views:
            out.append(Match("ordinate", i, o["view"], "ordinate", o["text"], o["value"],
                             status="skipped", note="view not registered"))
            continue
        partner = None
        if i in ends:
            v, (ax, _), pts = ends[i]
            for j, (v2, ac2, pts2) in ends.items():
                if v2 == v and ac2[0] != ax and \
                        np.linalg.norm(pts[:, None] - pts2[None], axis=2).min() < 0.05:
                    partner = ac2
                    break
        out.append(match_ordinate(o, fc, views[o["view"]], fits, partner))
    by_id = {}
    later = []
    general = []
    for a in anns:
        vf = views.get(a["view"])
        if (a.get("geometry") or {}).get("type") == "general_surface_texture":
            general.append(a)
            continue
        if a["kind"] in ("angle", "roughness") and a["status"] == "unmatched":
            m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"],
                      status="skipped", note="not located on the drawing")
        elif vf is None:
            m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"],
                      status="skipped", note="view not registered")
        elif a["kind"] in ("radius", "diameter"):
            m = match_radius(a, fc, vf)
        elif a["kind"] == "linear":
            m = match_linear(a, fc, vf)
        elif a["kind"] == "roughness":
            m = match_surface(a, fc, vf)
        elif a["kind"] == "angle":
            m = match_angle(a, fc, vf, views)
        elif a["kind"] in ("gdt", "datum") and a["applies_to"] is not None:
            later.append(a)
            continue
        elif a["kind"] in ("gdt", "datum"):
            m = match_surface(a, fc, vf)
        else:
            m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"],
                      status="skipped", note="unsupported kind")
        by_id[a["id"]] = m
        out.append(m)
    for a in general:
        out.append(_general_texture(a, fc, out))
    # a frame attached to a dimension (ISO 1101): in line with the dimension
    # line it tolerances the derived feature (median plane / axis) of the
    # feature of size; on one of its extension lines, only that surface
    ann_by_id = {x["id"]: x for x in anns}
    for a in later:
        t = by_id.get(a["applies_to"])
        m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"],
                  note=f"applies to annotation {a['applies_to']}")
        if t is not None and t.features:
            m.status = t.status
            side = _attached_side(a, ann_by_id[a["applies_to"]], regs.get(a["view"]))
            if side is None:
                m.features = [dict(f, role="derived") for f in t.features]
                m.note += ": derived feature (median)" if len(t.features) > 1 else ""
            else:
                m.features = [dict(f, role="toleranced") for f in t.features if f["role"] == side]
                m.note += f": the {side} surface (leader on its extension line)"
                if any(r["characteristic"] in ("position", "symmetry", "concentricity")
                       for r in (a["gdt"] or {}).get("rows", [])):
                    m.note += "; a location tolerance usually means the feature of size"
        out.append(m)
    return out


def _general_texture(a, fc, matches):
    """The title block's general roughness covers every face no surface
    texture symbol on the views marks (ISO 1302 simplified indication)."""
    marked, values = set(), set()
    for m in matches:
        if m.kind == "roughness" and m.status in ("matched", "ambiguous"):
            values.add(m.value)
            for feat in m.features:
                marked.update(feat["faces"])
    m = Match("annotation", a["id"], a["view"], a["kind"], a["text"], a["value"], status="matched")
    seen = set()
    for s in fc.surfaces:
        if s.face in marked or s.feature in seen:
            continue
        seen.add(s.feature)
        faces = [f for f in fc.features[s.feature] if f not in marked]
        d = _feat(fc, s, "target", how="general")
        d["faces"] = faces
        m.features.append(d)
    n = sum(len(f["faces"]) for f in m.features)
    exc = set(a["geometry"].get("except") or [])
    m.note = f"general requirement from the title block: {n} of {len(fc.surfaces)} faces not marked on the views"
    if values - exc:
        m.note += f"; marked values {sorted(values - exc)} are not listed in the title block's parentheses"
    return m


def _attached_side(frame, dim, reg, inline=1.0):
    """None if the frame's leader tip is in line with the dimension line (within
    `inline` mm), else "side1"/"side2": the dimension end whose extension line
    it ends on. Single-feature dimensions (radius, diameter) give None."""
    if dim["kind"] != "linear" or len(dim["tips"]) < 2 or not frame["tips"] or reg is None:
        return None
    t1, t2 = (np.array(p, float) for p in dim["tips"][:2])
    x = np.array(frame["tips"][0], float)
    m = (t2 - t1) / np.linalg.norm(t2 - t1)
    r = x - t1
    if abs(r[0] * m[1] - r[1] * m[0]) <= inline * reg.k:
        return None
    return "side1" if abs(r @ m) <= abs((x - t2) @ m) else "side2"


# ---------------------------------------------------------------- output

def summary(matches):
    import collections
    c = collections.Counter((m.source, m.kind if m.source == "annotation" else "ordinate", m.status)
                            for m in matches)
    for k in sorted(c):
        print(f"  {k[0]:10} {k[1]:10} {k[2]:10} {c[k]}")
    print()
    for m in matches:
        if m.status in ("matched", "skipped"):
            continue
        best = m.features or m.alternatives
        desc = "; ".join(f"{f['surface']['type']} f{f['faces']} d={f.get('delta')}" for f in best[:2])
        print(f"  {m.status:9} {m.source[:3]} {m.id:3} v{m.view} {m.kind:8} {m.text:14} {desc} {m.note}")


def to_records(matches):
    return [{"source": m.source, "id": m.id, "view": m.view, "kind": m.kind, "text": m.text,
             "value": m.value, "status": m.status, "features": m.features,
             "alternatives": m.alternatives, "note": m.note} for m in matches]


def draw_matches(ax, fc, regs, matches, rects=None, margin=20):
    """Edges of each match's faces drawn in its view, clipped to the view's
    box (a detail at 6:1 would otherwise cover the sheet)."""
    colors = {"matched": "tab:green", "ambiguous": "tab:orange", "mismatch": "tab:red"}
    model = fc.model
    face_pts = {}
    for i, fl in model.edge_faces.items():
        for f in fl:
            face_pts.setdefault(f, []).append(i)
    for m in matches:
        if m.status not in colors or m.view not in regs:
            continue
        reg = regs[m.view]
        for feat in m.features:
            for f in feat["faces"]:
                sel = np.isin(model.edge_of, face_pts.get(f, []))
                P = model.pts[sel]
                if reg.plane is not None:
                    P = P[(P - reg.plane[0]) @ reg.plane[1] > -1e-6]
                q = reg.to_page(P)
                if rects and m.view in rects:
                    b = rects[m.view]
                    q = q[(q[:, 0] > b[0] - margin) & (q[:, 0] < b[2] + margin) &
                          (q[:, 1] > b[1] - margin) & (q[:, 1] < b[3] + margin)]
                ax.plot(q[:, 0], q[:, 1], ".", color=colors[m.status], ms=0.6)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("step")
    ap.add_argument("--registration", default="registration.json")
    ap.add_argument("--ordinates", default="ordinates.json")
    ap.add_argument("--annotations", default="annotations.json")
    ap.add_argument("--json", help="write matched faces to this file")
    ap.add_argument("--overlay", help="write a PNG with matched faces on the page")
    ap.add_argument("--pdf", help="the drawing, needed for --overlay")
    args = ap.parse_args()
    if args.overlay and not args.pdf:
        ap.error("--overlay needs --pdf")

    model = load_model(args.step)
    fc = build(model)
    with open(args.registration) as f:
        regs = from_records(json.load(f))
    with open(args.ordinates) as f:
        ords = json.load(f)
    with open(args.annotations) as f:
        anns = json.load(f)
    print(f"model: {len(model.faces)} faces, {len(fc.features)} features, "
          f"{sum(len(t) for t in fc.tris)} triangles\n")
    matches = match_all(fc, regs, ords, anns)
    summary(matches)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_records(matches), f, indent=1)
        print("\nwrote", args.json)
    if args.overlay:
        import matplotlib
        matplotlib.use("Agg")
        from debug_overlay import draw_page, new_figure
        from pdf_extractor import extract
        from classify import classify
        from views import segment
        pg = extract(args.pdf)
        sheet = segment(pg, classify(pg))
        fig, ax = new_figure(pg)
        draw_page(ax, pg, show_text=False, lw=0.3)
        draw_matches(ax, fc, regs, matches, {v.id: v.rect for v in sheet.views})
        fig.tight_layout()
        fig.savefig(args.overlay, dpi=200)
        print("wrote", args.overlay)
