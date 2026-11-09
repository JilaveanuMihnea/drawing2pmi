"""Register each drawing view with the STEP model.

The model's edges are sampled into points that remember their edge index.
For a view, every axis-aligned orientation (24: 6 view directions x 4
in-plane rotations) projects those points onto the drawing plane; the
drawing's visible geometry is converted to mm with the scale and origin
fitted from the ordinates. Translation is refined by iterative
nearest-point matching, and each orientation is scored by how well the
drawing and the projection cover each other (truncated chamfer distance).

Drawing frame (mm): x to the right, y up, z toward the viewer.
A model point P maps to (P.u + tx, P.v + ty) with depth P.w, w = u x v.
"""
from dataclasses import dataclass, field
import itertools
import math

import numpy as np
from scipy.ndimage import distance_transform_edt, minimum_filter
from scipy.spatial import cKDTree

from OCC.Core.BRepAdaptor import BRepAdaptor_Curve
from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Section
from OCC.Core.gp import gp_Dir, gp_Pln, gp_Pnt
from OCC.Core.GCPnts import GCPnts_QuasiUniformDeflection
from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCC.Core.TopExp import TopExp_Explorer, topexp
from OCC.Core.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCC.Extend.DataExchange import read_step_file

from classify import Arc, Line, classify
from ordinates import PT_PER_MM, parse as parse_ordinates
from pdf_extractor import extract

# model edges are resampled to this spacing (mm)
MODEL_STEP = 0.1
# drawing strokes are sampled at this spacing (pt)
DRAW_STEP = 0.5
# distances above this (mm) count as "not explained" in the score
CAP = 1.0
# score handicap (mm) for a section frame that breaks drafting convention
SECTION_PRIOR = 0.15
# drawing roles compared with the model's projected edges
DRAW_ROLES = ("visible", "hidden", "thin_medium", "phantom")

AXES = [np.array(a, float) for a in
        [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]]


@dataclass
class Model:
    shape: object
    edges: list                 # TopoDS_Edge, index = edge id
    pts: np.ndarray             # (n, 3) sampled points, mm
    edge_of: np.ndarray         # (n,) edge id of each point
    edge_faces: dict            # edge id -> list of face ids
    faces: list                 # TopoDS_Face, index = face id


@dataclass
class Registration:
    view: int
    u: np.ndarray               # model direction pointing right on the page
    v: np.ndarray               # model direction pointing up on the page
    t: np.ndarray               # translation (mm) in the drawing plane
    k: float                    # page points per mm
    origin: np.ndarray          # page point of drawing (0, 0)
    score: float                # truncated mean distance, mm (lower is better)
    d2m: float                  # drawing -> model part of the score
    m2d: float                  # model -> drawing part
    runner_up: float            # score of the best other orientation
    candidates: list = field(default_factory=list)  # (score, u, v, t) sorted
    how: str = "search"         # search, detail of <view>, section of <view>
    plane: tuple | None = None  # sections: (point, normal) of the cut, model mm
    origin_fitted: bool = False # both origin axes come from ordinates (own or aligned views)
    cut: np.ndarray | None = None   # sections: sampled cut contour, model mm
    layout_ok: bool | None = None   # agrees with its row/column neighbours' projection

    @property
    def w(self):
        return np.cross(self.u, self.v)

    @property
    def confident(self):
        """Independent agreement, or a clear win over the other frames.

        With an ordinate origin the model origin should land on it: a near-zero
        fitted offset confirms frame and scale independently of the shape
        score (which unmodelled drawing content, like engraved text, inflates).
        Otherwise the best frame must clearly beat the runner-up and the drawing
        must lie on the model (to 0.15 mm on average: unmodelled content such as
        break lines and view limits adds a little). Details have no runner-up (their frame is the
        parent's); they only need the drawing to sit on the model.
        """
        if self.origin_fitted and np.linalg.norm(self.t) < 0.05:
            return True
        if self.layout_ok and self.d2m <= 0.1:
            return True
        if self.d2m > 0.15:
            return False
        if math.isnan(self.runner_up):
            return True
        return (self.runner_up - self.score) / self.runner_up > 0.25

    def to_page(self, P):
        """Model points (n, 3) -> page points (n, 2)."""
        x = P @ self.u + self.t[0]
        y = P @ self.v + self.t[1]
        return np.column_stack([self.origin[0] + self.k * x, self.origin[1] - self.k * y])

    def to_drawing(self, p):
        """Page points (n, 2) -> drawing-plane mm (n, 2), model offsets removed."""
        p = np.atleast_2d(p)
        x = (p[:, 0] - self.origin[0]) / self.k - self.t[0]
        y = -(p[:, 1] - self.origin[1]) / self.k - self.t[1]
        return np.column_stack([x, y])


# ---------------------------------------------------------------- model

def _iter(shape, kind):
    exp = TopExp_Explorer(shape, kind)
    while exp.More():
        yield exp.Current()
        exp.Next()


def load_model(path):
    shape = read_step_file(path)
    faces = list(_iter(shape, TopAbs_FACE))
    # unique edges (the explorer visits shared edges once per face)
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    topexp.MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    edges, pts, owner, edge_faces = [], [], [], {}
    for i in range(1, emap.Size() + 1):
        e = emap.FindKey(i)
        eid = len(edges)
        edges.append(e)
        fl = emap.FindFromIndex(i)
        edge_faces[eid] = [next(j for j, f in enumerate(faces) if f.IsSame(x)) for x in fl]
        c = BRepAdaptor_Curve(e)
        d = GCPnts_QuasiUniformDeflection(c, 0.005)
        if not d.IsDone() or d.NbPoints() < 2:
            continue
        poly = np.array([d.Value(j).Coord() for j in range(1, d.NbPoints() + 1)])
        # resample evenly so straight edges are not just two points
        seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        s = np.r_[0, np.cumsum(seg)]
        if s[-1] <= 0:
            continue
        n = max(2, int(s[-1] / MODEL_STEP) + 1)
        si = np.linspace(0, s[-1], n)
        res = np.column_stack([np.interp(si, s, poly[:, k]) for k in range(3)])
        pts.append(res)
        owner += [eid] * len(res)
    return Model(shape, edges, np.vstack(pts), np.array(owner), edge_faces, faces)


def _sample_shape_edges(shape, step=MODEL_STEP):
    pts = []
    for e in _iter(shape, TopAbs_EDGE):
        c = BRepAdaptor_Curve(e)
        d = GCPnts_QuasiUniformDeflection(c, 0.005)
        if not d.IsDone() or d.NbPoints() < 2:
            continue
        poly = np.array([d.Value(j).Coord() for j in range(1, d.NbPoints() + 1)])
        seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        sc = np.r_[0, np.cumsum(seg)]
        if sc[-1] <= 0:
            continue
        si = np.linspace(0, sc[-1], max(2, int(sc[-1] / step) + 1))
        pts.append(np.column_stack([np.interp(si, sc, poly[:, k]) for k in range(3)]))
    return np.vstack(pts) if pts else np.zeros((0, 3))


def section_points(model, p0, n):
    """Points on the cut contour of the model by the plane through p0 with normal n."""
    sec = BRepAlgoAPI_Section(model.shape, gp_Pln(gp_Pnt(*map(float, p0)), gp_Dir(*map(float, n))), False)
    sec.Approximation(True)
    sec.Build()
    return _sample_shape_edges(sec.Shape())


def orientations():
    """The 24 proper axis-aligned frames (u right, v up, w = u x v toward the viewer)."""
    out = []
    for u, v in itertools.product(AXES, AXES):
        if abs(u @ v) < 0.5:
            out.append((u, v))
    return out


# ---------------------------------------------------------------- drawing

def _view_points(view, roles=DRAW_ROLES):
    pts = []
    for s in view.strokes:
        if s.role not in roles:
            continue
        for p in s.prims:
            if isinstance(p, Arc):
                n = max(2, int(p.length / DRAW_STEP) + 1)
                pts.append(p.sample(n))
            else:
                n = max(2, int(p.length / DRAW_STEP) + 1)
                pts.append(p.p0 + np.linspace(0, 1, n)[:, None] * (p.p1 - p.p0))
    return np.vstack(pts) if pts else np.zeros((0, 2))


def _view_frame(view, fits, sheet):
    """(k, origin) of a view: fitted from its ordinates where available.

    A view without ordinates on an axis takes that axis's origin from a view
    it is aligned with: views in one row share the vertical origin, views in
    one column the horizontal one.
    """
    def fit(vid, axis):
        return next((f for f in fits if f.view == vid and f.axis == axis and f.k), None)

    fx = fit(view.id, "x") or next((fit(w, "x") for w in view.column if fit(w, "x")), None)
    fy = fit(view.id, "y") or next((fit(w, "y") for w in view.row if fit(w, "y")), None)
    scale = view.scale or (sheet.title_block.scale if sheet.title_block else 1.0) or 1.0
    ks = [f.k for f in (fx, fy) if f]
    k = float(np.mean(ks)) if ks else scale * PT_PER_MM
    r = view.rect
    ox = fx.origin if fx else (r[0] + r[2]) / 2
    oy = fy.origin if fy else (r[1] + r[3]) / 2
    return k, np.array([ox, oy]), (fx is not None, fy is not None)


# ---------------------------------------------------------------- scoring

def _score(D, M, tree_m, tree_d, margin=2.0):
    """Truncated chamfer score. Model points outside the drawing's extent are
    ignored, so a detail or section is not penalised for what it crops away."""
    d1 = np.minimum(tree_m.query(D)[0], CAP).mean()
    lo, hi = D.min(0) - margin, D.max(0) + margin
    inside = np.all((M >= lo) & (M <= hi), axis=1)
    Mi = M[inside] if inside.sum() > 10 else M
    d2 = np.minimum(tree_d.query(Mi)[0], CAP).mean()
    return d1 + 0.5 * d2, d1, d2


def _search_translation(D, M2, res=0.25, top=6):
    """Global translation search for a drawing that may show only part of the model.

    The projected model is rasterised and turned into a distance map; every
    offset on a res-mm grid that keeps the drawing over the model is scored
    by the mean (capped) distance of the drawing points. Returns the best few
    offsets as starting points for ICP.
    """
    lo = M2.min(0) - 2 * CAP
    hi = M2.max(0) + 2 * CAP
    shape = np.ceil((hi - lo) / res).astype(int) + 1
    occ = np.ones(shape, bool)
    idx = np.floor((M2 - lo) / res).astype(int)
    occ[idx[:, 0], idx[:, 1]] = False
    dist = np.minimum(distance_transform_edt(occ) * res, CAP)

    # drawing points relative to their own minimum; candidate offsets place
    # that minimum anywhere such that the drawing stays inside the raster
    Dl = D - D.min(0)
    span = np.ceil((D.max(0) - D.min(0)) / res).astype(int)
    nx, ny = shape[0] - span[0], shape[1] - span[1]
    if nx <= 0 or ny <= 0:
        return [np.zeros(2)]
    rng = np.random.default_rng(1)
    Ds = Dl[rng.choice(len(Dl), size=min(len(Dl), 1500), replace=False)]
    di = np.floor(Ds / res).astype(int)
    cost = np.zeros((nx, ny))
    for x, y in di:
        cost += dist[x:x + nx, y:y + ny]
    cost /= len(di)
    # local minima, best first
    loc = (cost == minimum_filter(cost, size=9))
    cand = np.argwhere(loc)
    cand = cand[np.argsort(cost[loc])][:top]
    # drawing mm = model + t  ->  t = D.min - (lo + offset)
    return [D.min(0) - (lo + c * res) for c in cand]


def _overlaps(seg, u, v, t, D, need=0.5):
    """Does the projected segment overlap the drawing along its main axis?"""
    P1, P2 = seg
    a = np.array([P1 @ u, P1 @ v]) + t
    b = np.array([P2 @ u, P2 @ v]) + t
    ax = int(np.argmax(np.abs(b - a)))
    lo, hi = sorted((a[ax], b[ax]))
    dlo, dhi = D[:, ax].min(), D[:, ax].max()
    ov = min(hi, dhi) - max(lo, dlo)
    return ov >= need * min(hi - lo, dhi - dlo)


def _fit_translation(D, M2, t0, iters=12):
    """Translation-only ICP of the drawing points D onto projected model points M2."""
    tree = cKDTree(M2)
    t = t0.copy()
    for i in range(iters):
        dist, idx = tree.query(D - t)
        cut = max(CAP, 4 * np.median(dist)) if i < 3 else CAP
        ok = dist < cut
        if ok.sum() < 10:
            break
        step = np.median(D[ok] - t - M2[idx[ok]], axis=0)
        t = t + step
        if np.linalg.norm(step) < 1e-4:
            break
    return t


def register_view(view, model, fits, sheet, roles=DRAW_ROLES, keep=5,
                  frames=None, pts3=None, origin=None, t_inits=None, how="search", anchor3=None,
                  penalties=None, segment3=None):
    """Best (u, v, t) for a view.

    frames: candidate (u, v) pairs (default: the 24 axis-aligned ones);
    pts3: model points to project (default: all edge samples);
    origin: page point of the drawing origin (default: from the ordinates);
    anchor3: a model point expected near the middle of the view along the
    in-plane axis it lies on (the cutting line's middle for sections);
    penalties: per-frame handicap (mm) added when ranking, not to the score;
    segment3: (P1, P2) model segment that must overlap the drawing (a section
    shows material along its own cutting line, nowhere else).
    """
    k, org, fitted = _view_frame(view, fits, sheet)
    if origin is not None:
        org = np.asarray(origin, float)
    P = _view_points(view, roles)
    if len(P) < 20:
        return None
    D = np.column_stack([(P[:, 0] - org[0]) / k, -(P[:, 1] - org[1]) / k])
    pts3 = model.pts if pts3 is None else pts3
    rng = np.random.default_rng(0)
    sub = pts3[rng.choice(len(pts3), size=min(len(pts3), 20000), replace=False)]
    tree_d = cKDTree(D)

    results = []
    frames = frames or orientations()
    penalties = penalties or [0.0] * len(frames)
    for (u, v), pen in zip(frames, penalties):
        M2 = np.column_stack([sub @ u, sub @ v])
        best = None
        inits = list(t_inits or []) + [np.zeros(2),
                                       (D.min(0) + D.max(0)) / 2 - (M2.min(0) + M2.max(0)) / 2]
        if anchor3 is not None:
            dc = (D.min(0) + D.max(0)) / 2
            mc = (M2.min(0) + M2.max(0)) / 2
            a2 = np.array([anchor3 @ u, anchor3 @ v])
            inits += [np.array([dc[0] - a2[0], dc[1] - mc[1]]),
                      np.array([dc[0] - mc[0], dc[1] - a2[1]])]
        if origin is None and not all(fitted):
            # no ordinate origin: the view may be cropped, search globally
            inits += _search_translation(D, M2)
        for t0 in inits:
            t = _fit_translation(D, M2, np.asarray(t0, float))
            if segment3 is not None and not _overlaps(segment3, u, v, t, D):
                continue
            sc = _score(D, M2 + t, cKDTree(M2 + t), tree_d)
            if best is None or sc[0] < best[0][0]:
                best = (sc, t)
        if best is None:
            continue
        results.append((best[0], u, v, best[1], pen))
    if not results:
        return None
    results.sort(key=lambda r: r[0][0] + r[4])

    (sc, d1, d2), u, v, t, _ = results[0]
    M2 = np.column_stack([pts3 @ u, pts3 @ v]) + t
    sc, d1, d2 = _score(D, M2, cKDTree(M2), tree_d)
    return Registration(view.id, u, v, t, k, org, float(sc), float(d1), float(d2),
                        float(results[1][0][0] + results[1][4] - results[0][4]) if len(results) > 1 else math.nan,
                        [(float(r[0][0]), r[1], r[2], r[3]) for r in results[:keep]], how,
                        origin_fitted=origin is None and all(fitted))


# ---------------------------------------------------------------- details

def _boundary(strokes):
    """Union bbox of dash-dot cutting-plane strokes (a detail boundary)."""
    pts = [s.pts for s in strokes]
    if not pts:
        return None
    P = np.vstack(pts)
    return np.r_[P.min(0), P.max(0)]


def register_detail(view, sheet, cl, regs, model, fits):
    """A detail keeps its parent's orientation; its offset comes from matching
    its boundary (circle or rectangle) to the same boundary in the parent."""
    letter = view.label
    marker = next((m for m in sheet.markers if m.text.strip() == letter), None)
    k_d, _, _ = _view_frame(view, fits, sheet)
    mine = [s for s in cl.strokes if s.role == "cutting_plane" and s.linetype != "continuous"
            and _rect_in(s.pts, view.rect, 60)]
    if marker is None or not mine:
        return None
    bd = _boundary([max(mine, key=lambda s: np.ptp(s.pts, axis=0).max())])
    size_d = bd[2:] - bd[:2]
    mc = np.array([(marker.bbox.x0 + marker.bbox.x1) / 2, (marker.bbox.y0 + marker.bbox.y1) / 2])

    best = None
    for pid, pr in regs.items():
        if pid == view.id:
            continue
        ratio = pr.k / k_d
        cands = [s for s in cl.strokes if s.role == "cutting_plane" and s.linetype != "continuous"
                 and np.linalg.norm(s.pts - mc, axis=1).min() < 150
                 and np.allclose(np.ptp(s.pts, axis=0), size_d * ratio, rtol=0.12, atol=2)]
        if cands:
            bp = _boundary(cands)
            d = np.linalg.norm((bp[:2] + bp[2:]) / 2 - mc)
            if best is None or d < best[0]:
                best = (d, pid, bp)
    if best is None:
        return None
    _, pid, bp = best
    par = regs[pid]
    # boundary center in the parent's model-plane coordinates
    c_mm = par.to_drawing(((bp[:2] + bp[2:]) / 2)[None])[0]
    c_page = (bd[:2] + bd[2:]) / 2
    # choose the detail origin so its boundary center maps to the same model point
    origin = np.array([c_page[0] - k_d * c_mm[0], c_page[1] + k_d * c_mm[1]])
    return register_view(view, model, fits, sheet, frames=[(par.u, par.v)], origin=origin,
                         t_inits=[np.zeros(2)], how=f"detail of view {pid}")


def _rect_in(pts, r, margin):
    lo, hi = pts.min(0), pts.max(0)
    return lo[0] >= r[0] - margin and lo[1] >= r[1] - margin and hi[0] <= r[2] + margin and hi[1] <= r[3] + margin


# ---------------------------------------------------------------- sections

def register_section(view, sheet, cl, regs, model, fits):
    """Cut the model with the plane drawn in the parent view and register the
    section view against the cut contour plus the edges beyond the plane."""
    letter = view.label.split("-")[0]
    marks = [m for m in sheet.markers if m.text.strip() == letter]
    tips = []
    for m in marks:
        c = np.array([(m.bbox.x0 + m.bbox.x1) / 2, (m.bbox.y0 + m.bbox.y1) / 2])
        h = min(cl.arrowheads, key=lambda h: np.linalg.norm(h.tip - c))
        if np.linalg.norm(h.tip - c) < 80:
            tips.append(h)
    if len(tips) < 2:
        return None
    T1, T2 = tips[0].tip, tips[1].tip
    adir = (tips[0].dir + tips[1].dir) / 2
    # parent: the registered view whose box (grown) holds both tips
    par = None
    for pid, pr in regs.items():
        pv = next(v for v in sheet.views if v.id == pid)
        r = pv.rect + np.array([-100, -100, 100, 100])
        if all(r[0] <= T[0] <= r[2] and r[1] <= T[1] <= r[3] for T in (T1, T2)):
            if par is None or (pv.rect[2] - pv.rect[0]) < par[1]:
                par = (pr, pv.rect[2] - pv.rect[0])
    if par is None:
        return None
    par = par[0]

    def to_model_dir(dpage):
        return par.u * dpage[0] - par.v * dpage[1]     # page y points down

    q1, q2 = par.to_drawing(np.array([T1, T2]))
    p0 = par.u * q1[0] + par.v * q1[1]                 # on the plane (any depth)
    d = to_model_dir((T2 - T1) / np.linalg.norm(T2 - T1))
    d /= np.linalg.norm(d)
    a = to_model_dir(adir / np.linalg.norm(adir))      # viewing direction
    n = a - (a @ d) * d - (a @ par.w) * par.w
    n /= np.linalg.norm(n)
    cut = section_points(model, p0, n)
    # the section draws the cut contour (hatched) and the edges beyond the plane
    beyond = model.pts[(model.pts - p0) @ n > 1e-6]
    pts3 = np.vstack([cut, beyond]) if len(cut) else beyond
    w_s = -n                                           # the viewer looks along n
    frames = []
    for u in (d, -d, par.w, -par.w):
        frames.append((u, np.cross(w_s, u)))
    # drafting convention: a section is drawn as projected along its arrows,
    # so the cutting line keeps its page direction (a horizontal cut stays
    # horizontal, left to right; a vertical one stays vertical, bottom to top).
    # Rotated sections (F-F, cut at 45 degrees) are the exception: prior, not rule.
    e = (T2 - T1) / np.linalg.norm(T2 - T1)
    if abs(e[0]) >= abs(e[1]):
        want_u = par.u
        pref = (want_u, np.cross(w_s, want_u))
    else:
        want_v = par.v
        pref = (np.cross(want_v, w_s), want_v)
    penalties = [0.0 if (np.allclose(u, pref[0], atol=1e-6) and np.allclose(v, pref[1], atol=1e-6))
                 or abs(e[0]) > 0.2 and abs(e[1]) > 0.2 else SECTION_PRIOR for u, v in frames]
    r = _fit_section(view, fits, sheet, frames, penalties, pts3,
                     (p0, par.u * q2[0] + par.v * q2[1]), f"section of view {par.view}")
    if r:
        r.plane = (p0, n)
        r.cut = cut
    return r


def _is_break_line(stroke, max_seg=20.0):
    """Zig-zag break symbol: short segments turning sharply back and forth."""
    pts = stroke.pts
    if len(pts) < 4:
        return False
    d = np.diff(pts, axis=0)
    L = np.linalg.norm(d, axis=1)
    cross = d[:-1, 0] * d[1:, 1] - d[:-1, 1] * d[1:, 0]
    turn = np.degrees(np.arctan2(cross, (d[:-1] * d[1:]).sum(axis=1)))
    sharp = np.abs(turn) > 45
    flips = np.sum(sharp[1:] & sharp[:-1] & (np.sign(turn[1:]) != np.sign(turn[:-1])))
    return flips >= 2 and np.median(L) < max_seg


def _fit_section(view, fits, sheet, frames, penalties, cut, seg, how):
    """Register a section view against the model's cut contour and the edges
    beyond the plane (`cut` holds both).

    A partial section also draws limit lines at the ends of the cutting line
    and zig-zag break lines; those are not model geometry and are left out. Along the cutting line the offset comes
    from the cutting line itself (its middle at the view's middle); across it
    (the depth the parent view could not see) a 1-D search finds it. Frames are
    ranked by how well the drawn contour lies on the cut (d2m) plus the
    drafting-convention handicap.
    """
    k, org, _ = _view_frame(view, fits, sheet)
    keep = [s for s in view.strokes if s.role == "visible" and not _is_break_line(s)]
    P = []
    for s in keep:
        for p in s.prims:
            n = max(2, int(p.length / DRAW_STEP) + 1)
            P.append(p.sample(n) if isinstance(p, Arc) else p.p0 + np.linspace(0, 1, n)[:, None] * (p.p1 - p.p0))
    if not P or len(cut) < 10:
        return None
    P = np.vstack(P)
    D0 = np.column_stack([(P[:, 0] - org[0]) / k, -(P[:, 1] - org[1]) / k])
    P1, P2 = seg

    results = []
    for (u, v), pen in zip(frames, penalties):
        M2 = np.column_stack([cut @ u, cut @ v])
        tree_m = cKDTree(M2)
        a = np.array([P1 @ u, P1 @ v])
        b = np.array([P2 @ u, P2 @ v])
        ax = int(np.argmax(np.abs(b - a)))
        ox = 1 - ax
        t = np.zeros(2)
        t[ax] = (D0[:, ax].min() + D0[:, ax].max()) / 2 - (a[ax] + b[ax]) / 2
        # drop limit lines: drawn where the cutting line ends
        ends = np.array([a[ax], b[ax]]) + t[ax]
        D = D0[np.min(np.abs(D0[:, ax][:, None] - ends[None]), axis=1) > 0.05]
        if len(D) < 10:
            D = D0

        def d2m(tt):
            return np.minimum(tree_m.query(D - tt)[0], CAP).mean()

        lo = D[:, ox].min() - M2[:, ox].max()
        hi = D[:, ox].max() - M2[:, ox].min()
        grid = []
        for g in np.arange(lo, hi + 0.05, 0.05):
            tt = t.copy()
            tt[ox] = g
            grid.append((d2m(tt), tt))
        grid.sort(key=lambda x: x[0])
        # a broken-out section may show only part of its cutting line, so the
        # centred start can be off along the cut: add global 2-D candidates
        starts = [g[1] for g in grid[:5]] + _search_translation(D, M2)
        best = None
        for t0 in starts:
            tt = _fit_translation(D, M2, t0)
            if not _overlaps(seg, u, v, tt, D):
                continue
            sc = d2m(tt)
            if best is None or sc < best[0]:
                best = (sc, tt)
        if best:
            results.append((best[0] + pen, best[0], u, v, best[1], D))
    if not results:
        return None
    results.sort(key=lambda r: r[0])
    rank, d1, u, v, t, D = results[0]
    M = np.column_stack([cut @ u, cut @ v]) + t
    _, _, d2 = _score(D, M, cKDTree(M), cKDTree(D))
    runner = results[1][0] - (rank - d1) if len(results) > 1 else math.nan
    return Registration(view.id, u, v, t, k, org, float(d1), float(d1), float(d2), float(runner),
                        [(float(r[1]), r[2], r[3], r[4]) for r in results], how)


def check_layout(sheet, regs):
    """Check adjacent aligned orthographic views against the projection method.

    First angle: the view left of another shows it from its right (+u), the
    view right of it from its left (-u); the view above shows it from below
    (-v), the view below from above (+v). Third angle mirrors that. Each view
    gets layout_ok = True if every adjacent pair it is in agrees.
    """
    proj = sheet.title_block.projection if sheet.title_block else None
    if proj is None:
        return
    sign = 1 if proj == "first_angle" else -1
    # named views ("A", "B": seen along a view arrow) are placed freely, so
    # only unlabeled projected views follow the layout rule
    ortho = {vid: r for vid, r in regs.items()
             if (lambda v: v.kind == "orthographic" and not v.label)(
                 next(v for v in sheet.views if v.id == vid))}
    views = {v.id: v for v in sheet.views}
    verdict = {}
    for vid, r in ortho.items():
        v = views[vid]
        for nbrs, horiz in ((v.row, True), (v.column, False)):
            for nid in nbrs:
                if nid not in ortho:
                    continue
                n = views[nid]
                a = 0 if horiz else 1
                if (n.rect[a] + n.rect[a + 2]) / 2 <= (v.rect[a] + v.rect[a + 2]) / 2:
                    continue        # handle each pair once, from its left/top member
                # is n the nearest neighbour on that side?
                between = [x for x in nbrs if x in ortho and x != nid and
                           (v.rect[a] + v.rect[a + 2]) / 2 < (views[x].rect[a] + views[x].rect[a + 2]) / 2
                           < (n.rect[a] + n.rect[a + 2]) / 2]
                if between:
                    continue
                rn = ortho[nid]
                if horiz:   # n is right of v: n shows v from its left, v shows n from its right
                    ok = (np.allclose(rn.w, -sign * r.u) and np.allclose(rn.v, r.v))
                else:       # n is below v (page y grows downward): n shows v from above
                    ok = (np.allclose(rn.w, sign * r.v) and np.allclose(rn.u, r.u))
                for x in (vid, nid):
                    verdict[x] = verdict.get(x, True) and ok
    for vid, ok in verdict.items():
        regs[vid].layout_ok = ok


def register(pg, model, cl=None, ordinate_result=None):
    """Orthographic views first, then details (parent's orientation), then
    sections (planes from cutting lines in already registered views)."""
    cl = cl or classify(pg)
    sheet, origins, dims, fits = ordinate_result or parse_ordinates(pg, cl)
    regs = {}
    for v in sheet.views:
        if v.kind == "orthographic":
            r = register_view(v, model, fits, sheet)
            if r:
                regs[v.id] = r
    check_layout(sheet, regs)
    pending = [v for v in sheet.views if v.kind in ("detail", "section")]
    # a section may be cut in a detail and vice versa: repeat until no progress
    while pending:
        done = []
        for v in pending:
            r = (register_detail if v.kind == "detail" else register_section)(v, sheet, cl, regs, model, fits)
            if r:
                regs[v.id] = r
                done.append(v)
        if not done:
            break
        pending = [v for v in pending if v not in done]
    return sheet, regs


def _axis_name(a):
    i = int(np.argmax(np.abs(a)))
    return ("+" if a[i] > 0 else "-") + "XYZ"[i]


def summary(sheet, regs):
    views = {v.id: v for v in sheet.views}
    print("view            right up   toward   t (mm)            score   d2m    m2d    next   ok    how")
    for vid, r in regs.items():
        v = views[vid]
        name = f"{vid} {v.kind[:5]} {v.label or ''}"
        print(f"  {name:13} {_axis_name(r.u):5} {_axis_name(r.v):4} {_axis_name(r.w):7}  "
              f"({r.t[0]:+7.3f},{r.t[1]:+7.3f})  {r.score:.3f}  {r.d2m:.3f}  {r.m2d:.3f}  {r.runner_up:.3f}  "
              f"{'yes' if r.confident else 'CHECK':5} {r.how}"
              f"{'' if r.layout_ok is None else ', layout ' + ('ok' if r.layout_ok else 'WRONG')}")


def to_records(sheet, regs):
    views = {v.id: v for v in sheet.views}

    def vec(a):
        return [round(float(x), 6) for x in a]

    out = []
    for vid, r in regs.items():
        v = views[vid]
        out.append({
            "view": vid, "label": v.label, "kind": v.kind, "how": r.how,
            "u_right": vec(r.u), "v_up": vec(r.v), "w_toward_viewer": vec(r.w),
            "t_mm": vec(r.t), "pt_per_mm": round(r.k, 5), "origin_page": vec(r.origin),
            "score_mm": round(r.score, 4), "d2m_mm": round(r.d2m, 4), "m2d_mm": round(r.m2d, 4),
            "runner_up_mm": None if math.isnan(r.runner_up) else round(r.runner_up, 4),
            "confident": bool(r.confident), "origin_from_ordinates": r.origin_fitted,
            "layout_ok": r.layout_ok,
            "plane": None if r.plane is None else {"point": vec(r.plane[0]), "normal": vec(r.plane[1])},
        })
    return out


def from_records(records):
    """Registrations back from to_records() output (view id -> Registration)."""
    regs = {}
    for r in records:
        plane = r.get("plane")
        regs[r["view"]] = Registration(
            view=r["view"], u=np.array(r["u_right"]), v=np.array(r["v_up"]), t=np.array(r["t_mm"]),
            k=r["pt_per_mm"], origin=np.array(r["origin_page"]), score=r["score_mm"],
            d2m=r["d2m_mm"], m2d=r["m2d_mm"],
            runner_up=math.nan if r["runner_up_mm"] is None else r["runner_up_mm"],
            how=r["how"], origin_fitted=r["origin_from_ordinates"], layout_ok=r["layout_ok"],
            plane=None if plane is None else (np.array(plane["point"]), np.array(plane["normal"])))
    return regs


def draw_registration(ax, regs, model, sheet=None, step=3, color="tab:red", margin=20):
    """Projected model edges drawn on the page, per registered view, clipped
    to the view's box (a detail at 6:1 would otherwise cover the sheet)."""
    rects = {v.id: v.rect for v in sheet.views} if sheet else {}
    for vid, r in regs.items():
        P = model.pts[::step]
        q = r.to_page(P)
        if r.plane is not None:
            # a section shows the cut contour and what lies beyond the plane
            p0, n = r.plane
            q = q[(P - p0) @ n > -1e-6]
            if r.cut is not None and len(r.cut):
                q = np.vstack([q, r.to_page(r.cut)])
        if vid in rects:
            b = rects[vid]
            q = q[(q[:, 0] > b[0] - margin) & (q[:, 0] < b[2] + margin) &
                  (q[:, 1] > b[1] - margin) & (q[:, 1] < b[3] + margin)]
        ax.plot(q[:, 0], q[:, 1], ".", color=color, ms=0.4)


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("step")
    ap.add_argument("--json", help="write view registrations to this file")
    ap.add_argument("--overlay", help="write a PNG with the projected model on the page")
    args = ap.parse_args()

    pg = extract(args.pdf)
    model = load_model(args.step)
    print(f"model: {len(model.edges)} edges, {len(model.faces)} faces, {len(model.pts)} points; "
          f"extent {np.round(model.pts.min(0), 2)} .. {np.round(model.pts.max(0), 2)}\n")
    sheet, regs = register(pg, model)
    summary(sheet, regs)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_records(sheet, regs), f, indent=1)
        print("\nwrote", args.json)
    if args.overlay:
        import matplotlib
        matplotlib.use("Agg")
        from debug_overlay import draw_page, new_figure
        fig, ax = new_figure(pg)
        draw_page(ax, pg, show_text=False, lw=0.3)
        draw_registration(ax, regs, model, sheet)
        fig.tight_layout()
        fig.savefig(args.overlay, dpi=200)
        print("wrote", args.overlay)
