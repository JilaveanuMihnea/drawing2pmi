"""Classify extracted PDF primitives into drawing entities.

Solid Edge exports every arc as a run of short chords and every dashed or
dash-dot line as separate collinear pieces, mostly one segment per path.
So classification works in three stages:

1. chain segments of equal width that share endpoints into polylines;
2. link polylines whose ends continue each other across a small gap
   (the gaps of a dashed line), recording the dash pattern;
3. fit each stroke as a sequence of straight lines and circular arcs,
   then assign a role from line width and line type.

Filled paths are classified separately (arrowheads, dots, masks).
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math

import numpy as np
from scipy.spatial import cKDTree

from pdf_extractor import extract

# endpoints closer than this are the same point
SNAP = 0.05
# largest gap inside a dashed line, and the allowed bend across it (degrees)
MAX_GAP = 6.0
MAX_GAP_TURN = 25.0
# only pieces up to this length can be dashes (longest seen: ~49 pt center dashes)
MAX_DASH = 80.0
# max deviation of points from a fitted line or circle; coordinates in the
# Solid Edge export are quantized to 0.12 pt, which alone gives up to ~0.085
FIT_TOL = 0.15
# max sagitta of a chord against its fitted arc: tessellation chords are short,
# so a long straight edge whose endpoints happen to lie on a circle fails this
CHORD_TOL = 0.25
# an arc's chords never turn more than this at a vertex (degrees)
MAX_ARC_TURN = 30.0
# in a dash-dot line the long dashes are at least this many times the short ones
DASHDOT_RATIO = 2.5

# stroke width -> role for continuous lines (from the per-width renders)
WIDTH_ROLES = {
    0.12: "title_block",
    0.24: "thin",           # dimension, extension and leader lines (hatching is split off)
    0.48: "thin_medium",
    0.72: "visible",
    0.96: "frame",
    2.04: "cutting_plane",  # section cuts and detail view boundaries
}


@dataclass
class Line:
    p0: np.ndarray
    p1: np.ndarray

    @property
    def length(self):
        return float(np.linalg.norm(self.p1 - self.p0))


@dataclass
class Arc:
    center: np.ndarray
    radius: float
    start: float        # angle of the first point, radians, page frame (y down)
    sweep: float        # signed; +-2*pi for a full circle

    @property
    def length(self):
        return abs(self.sweep) * self.radius

    @property
    def is_circle(self):
        return abs(abs(self.sweep) - 2 * math.pi) < 1e-3

    def sample(self, n=None):
        n = n or max(8, int(self.length / 2) + 1)     # ~2 pt steps
        a = self.start + np.linspace(0, self.sweep, n)
        return self.center + self.radius * np.column_stack([np.cos(a), np.sin(a)])


@dataclass
class Stroke:
    id: int
    width: float
    pts: np.ndarray             # vertices in order, gaps included
    closed: bool
    dashes: list[float]         # lengths of the drawn pieces; one entry if continuous
    path_ids: list[int]
    prims: list = field(default_factory=list)   # Line / Arc
    linetype: str = "continuous"                # continuous, dashed, dashdot, dashdotdot
    role: str = ""


@dataclass
class Arrowhead:
    tip: np.ndarray
    dir: np.ndarray     # unit vector, from base towards tip
    length: float
    path_id: int


@dataclass
class Filled:
    kind: str           # dot, mask, triangle, quad, shape
    pts: np.ndarray
    path_id: int


@dataclass
class Classified:
    strokes: list[Stroke]
    arrowheads: list[Arrowhead]
    filled: list[Filled]


# ---------------------------------------------------------------- chaining

def _chain(segs):
    """Join segments that share endpoints into polylines.

    segs: list of (pts (2,2), path_id). Chains break at nodes where more or
    fewer than two segments meet. Returns list of (vertices, closed, path_ids).
    """
    ends = np.array([s[0] for s in segs]).reshape(-1, 2)    # 2 per segment
    node = np.arange(len(ends))
    for i, j in cKDTree(ends).query_pairs(SNAP):
        a, b = node[i], node[j]
        while node[a] != a:
            a = node[a]
        while node[b] != b:
            b = node[b]
        node[max(a, b)] = min(a, b)
    for i in range(len(node)):
        while node[node[i]] != node[i]:
            node[i] = node[node[i]]
        node[i] = node[node[i]]

    at = defaultdict(list)          # node -> endpoint indices
    for e, n in enumerate(node):
        at[n].append(e)

    used = np.zeros(len(segs), bool)
    chains = []

    def walk(e):
        """Walk from endpoint e (its segment not yet used) until the chain ends."""
        pts, pids = [ends[e]], []
        closed = False
        while True:
            s = e // 2
            used[s] = True
            other = e ^ 1
            pts.append(ends[other])
            pids.append(segs[s][1])
            nxt = [x for x in at[node[other]] if x != other]
            if len(nxt) != 1 or used[nxt[0] // 2]:
                closed = len(nxt) == 1 and nxt[0] == start
                break
            e = nxt[0]
        return np.array(pts), closed, pids

    # open chains start at nodes of degree != 2
    for n, es in at.items():
        if len(es) == 2:
            continue
        for e in es:
            if not used[e // 2]:
                start = e
                chains.append(walk(e))
    # what is left are closed loops
    for e in range(len(ends)):
        if not used[e // 2]:
            start = e
            chains.append(walk(e))
    return chains


def _end_tangent(pts, at_end):
    """Outward unit tangent at one end of a polyline, or None if degenerate."""
    p = pts if at_end else pts[::-1]
    d = p[-1] - p[-2]
    n = np.linalg.norm(d)
    return d / n if n > 1e-6 else None


def _link_dashes(chains):
    """Merge open chains that continue each other across a short gap."""
    open_ids = [i for i, c in enumerate(chains) if not c[1] and _polylen(c[0]) <= MAX_DASH]
    if len(open_ids) < 2:
        return [(c[0], c[1], [_polylen(c[0])], c[2]) for c in chains]

    # one entry per open end: (chain index, at_end)
    end_keys = [(i, at_end) for i in open_ids for at_end in (False, True)]
    end_pts = np.array([chains[i][0][-1 if a else 0] for i, a in end_keys])
    tans = [_end_tangent(chains[i][0], a) for i, a in end_keys]
    cos_max = math.cos(math.radians(MAX_GAP_TURN))

    cand = []
    for a, b in cKDTree(end_pts).query_pairs(MAX_GAP):
        if end_keys[a][0] == end_keys[b][0]:
            continue
        d = end_pts[b] - end_pts[a]
        gap = np.linalg.norm(d)
        if gap < SNAP:
            continue        # touching ends belong to a junction, not a dash gap
        d /= gap
        ta, tb = tans[a], tans[b]
        # each tangent must point across the gap towards the other end;
        # a dot too short to have a direction only constrains the other side
        ok = [t is None or float(t @ v) > cos_max for t, v in ((ta, d), (tb, -d))]
        if all(ok) and (ta is not None or tb is not None):
            cand.append((gap, a, b))

    # greedy mutual matching, shortest gaps first
    cand.sort()
    partner = {}
    for _, a, b in cand:
        if a not in partner and b not in partner:
            partner[a] = b
            partner[b] = a

    key_index = {k: n for n, k in enumerate(end_keys)}
    seen = set()
    out = []

    def follow(i, entry_at_end):
        """Walk a run of chains starting at chain i, entered through the given end."""
        pts_all, dashes, pids = [], [], []
        closed = False
        cur, entry = i, entry_at_end
        while True:
            seen.add(cur)
            pts = chains[cur][0]
            if entry:
                pts = pts[::-1]
            pts_all.append(pts)
            dashes.append(_polylen(pts))
            pids += chains[cur][2]
            nxt = partner.get(key_index[(cur, not entry)])     # leave through the far end
            if nxt is None:
                break
            cur, entry = end_keys[nxt]
            if cur in seen:
                closed = cur == i
                break
        return np.vstack(pts_all), closed, dashes, pids

    for i in open_ids:
        if i in seen:
            continue
        # start from a chain end with no partner, so the walk covers the whole run
        for at_end in (False, True):
            if key_index[(i, at_end)] not in partner:
                out.append(follow(i, at_end))
                break
    # runs where every end has a partner are dashed loops
    for i in open_ids:
        if i not in seen:
            out.append(follow(i, False))

    for i, c in enumerate(chains):
        if i not in seen:
            out.append((c[0], c[1], [_polylen(c[0])], c[2]))
    return out


def _polylen(pts):
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


# ---------------------------------------------------------------- fitting

def _line_dev(pts):
    d = pts[-1] - pts[0]
    n = np.linalg.norm(d)
    if n < 1e-9:
        return float(np.linalg.norm(pts - pts[0], axis=1).max())
    nrm = np.array([-d[1], d[0]]) / n
    return float(np.abs((pts - pts[0]) @ nrm).max())


def _fit_circle(pts):
    """Algebraic (Kasa) circle fit; returns center, radius, max deviation."""
    x, y = pts[:, 0], pts[:, 1]
    A = np.column_stack([x, y, np.ones_like(x)])
    b = x * x + y * y
    (cx2, cy2, c), *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = cx2 / 2, cy2 / 2
    r2 = c + cx * cx + cy * cy
    if r2 <= 0:
        return None, None, math.inf
    r = math.sqrt(r2)
    dev = float(np.abs(np.hypot(x - cx, y - cy) - r).max())
    return np.array([cx, cy]), r, dev


def _chord_dev(pts, center, r):
    mid = (pts[1:] + pts[:-1]) / 2
    return float(np.abs(np.linalg.norm(mid - center, axis=1) - r).max())


def _smooth(pts):
    """True if every vertex turns the same way by less than MAX_ARC_TURN."""
    d = np.diff(pts, axis=0)
    cross = d[:-1, 0] * d[1:, 1] - d[:-1, 1] * d[1:, 0]
    dot = (d[:-1] * d[1:]).sum(axis=1)
    turn = np.degrees(np.arctan2(cross, dot))
    return bool(np.all(np.abs(turn) < MAX_ARC_TURN) and
                (np.all(turn >= -1e-3) or np.all(turn <= 1e-3)))


def _arc_from(pts, center, r, closed):
    a = np.unwrap(np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0]))
    sweep = a[-1] - a[0]
    if closed:
        sweep = math.copysign(2 * math.pi, sweep)
    return Arc(center, r, float(a[0]), float(sweep))


def fit_prims(pts, closed):
    """Split a polyline into maximal straight lines and circular arcs."""
    # drop repeated vertices
    keep = np.r_[True, np.linalg.norm(np.diff(pts, axis=0), axis=1) > 1e-6]
    pts = pts[keep]
    n = len(pts)
    if n < 2:
        return []

    if closed and n >= 8 and _smooth(np.vstack([pts, pts[1:2]])):
        c, r, dev = _fit_circle(pts)
        if dev < FIT_TOL and r > FIT_TOL and _chord_dev(pts, c, r) < CHORD_TOL:
            return [_arc_from(pts, c, r, True)]

    prims = []
    i = 0
    while i < n - 1:
        # longest arc starting at i (needs 4+ points, else any 3 points "fit")
        best = None
        j = i + 3
        while j < n:
            seg = pts[i:j + 1]
            if _line_dev(seg) < FIT_TOL or not _smooth(seg):
                break
            c, r, dev = _fit_circle(seg)
            if dev >= FIT_TOL or _chord_dev(seg, c, r) >= CHORD_TOL:
                break
            best = (j, c, r)
            j += 1
        if best:
            j, c, r = best
            prims.append(_arc_from(pts[i:j + 1], c, r, False))
            i = j
            continue
        # otherwise the longest straight run starting at i
        j = i + 1
        while j + 1 < n and _line_dev(pts[i:j + 2]) < FIT_TOL:
            j += 1
        prims.append(Line(pts[i].copy(), pts[j].copy()))
        i = j
    return prims


# ---------------------------------------------------------------- line types

def linetype(dashes):
    """Line type from the lengths of the drawn pieces of a stroke.

    Solid Edge's dash-dot "dots" are short dashes (about 3-5 pt here), so
    long and short pieces are told apart by ratio, not absolute length.
    The first and last pieces may be cut short where the line ends.
    """
    if len(dashes) == 1:
        return "continuous"
    d = np.array(dashes)
    inner = d[1:-1] if len(d) > 3 else d
    long = inner.max()
    short = inner < long / DASHDOT_RATIO
    if not short.any():
        return "dashed"
    # two short pieces between long dashes = phantom line
    runs = np.diff(np.flatnonzero(np.r_[True, np.diff(short.astype(int)) != 0, True]))
    starts = np.r_[0, np.cumsum(runs)[:-1]]
    short_runs = [r for r, s0 in zip(runs, starts) if short[s0]]
    return "dashdotdot" if np.median(short_runs) >= 2 else "dashdot"


def stroke_role(width, lt):
    base = WIDTH_ROLES.get(round(width, 2), f"w{width:.2f}")
    if lt == "continuous":
        return base
    if lt == "dashed":
        return "hidden"
    if lt == "dashdot":
        return "cutting_plane" if base == "cutting_plane" else "center"
    return "phantom"


def mark_hatching(strokes, min_lines=4, max_spacing=12.0, spacing_tol=0.25):
    """Re-label thin straight strokes that form hatch patterns as "hatch".

    Hatching is a family of parallel, evenly spaced thin lines. Axis-aligned
    families are skipped, because ordinate extension lines also come as
    parallel horizontal/vertical runs.
    """
    fam = defaultdict(list)
    for s in strokes:
        # hatch lines cut by an outline can get linked across the cut as "dashed"
        if s.role not in ("thin", "hidden") or len(s.prims) != 1 or not isinstance(s.prims[0], Line):
            continue
        p = s.prims[0]
        d = p.p1 - p.p0
        ang = math.degrees(math.atan2(d[1], d[0])) % 180
        if min(ang % 90, 90 - ang % 90) < 5:
            continue
        fam[round(ang)].append((s, ang))

    for members in fam.values():
        ang = math.radians(np.mean([a for _, a in members]))
        nrm = np.array([-math.sin(ang), math.cos(ang)])
        # hatch lines cut by an outline give several strokes per offset: group them into rows
        members.sort(key=lambda m: float(m[0].prims[0].p0 @ nrm))
        rows, offs = [], []
        for m in members:
            o = float(m[0].prims[0].p0 @ nrm)
            if offs and o - offs[-1] < 0.3:
                rows[-1].append(m)
            else:
                rows.append([m])
                offs.append(o)
        offs = np.array(offs)
        # split into runs of near-constant spacing
        run = [0]
        for k in range(1, len(rows) + 1):
            if k < len(rows):
                gap = offs[k] - offs[k - 1]
                gaps = np.diff(offs[run]) if len(run) > 1 else None
                ref = np.median(gaps) if gaps is not None else gap
                if 0.1 < gap <= max_spacing and abs(gap - ref) <= spacing_tol * ref:
                    run.append(k)
                    continue
            if len(run) >= min_lines:
                for r in run:
                    for m in rows[r]:
                        m[0].role = "hatch"
            run = [k]


# ---------------------------------------------------------------- filled

def _polygon(path):
    return np.array([s.pts[0] for s in path.segments])


def _classify_filled(path):
    pts = _polygon(path)
    if path.fill == (1.0, 1.0, 1.0):
        return Filled("mask", pts, path.id)
    if len(pts) == 3:
        sides = [np.linalg.norm(pts[(k + 1) % 3] - pts[(k + 2) % 3]) for k in range(3)]
        k = int(np.argmin(sides))                 # tip is opposite the shortest side
        tip = pts[k]
        base = (pts[(k + 1) % 3] + pts[(k + 2) % 3]) / 2
        d = tip - base
        L = float(np.linalg.norm(d))
        if L > 2 * sides[k]:                       # slender: an arrowhead
            return Arrowhead(tip, d / L, L, path.id)
        return Filled("triangle", pts, path.id)
    if len(pts) >= 8:
        c, r, dev = _fit_circle(pts)
        if dev < 0.1 * max(r, 1e-6):
            return Filled("dot", pts, path.id)
    if len(pts) == 4:
        return Filled("quad", pts, path.id)
    return Filled("shape", pts, path.id)


# ---------------------------------------------------------------- main

def classify(pg):
    strokes, arrowheads, filled = [], [], []

    by_width = defaultdict(list)
    for p in pg.paths:
        if p.fill is not None and p.color is None:
            r = _classify_filled(p)
            (arrowheads if isinstance(r, Arrowhead) else filled).append(r)
            continue
        for s in p.segments:
            if s.kind == "line":
                by_width[round(p.width, 2)].append((s.pts, p.id))
            # no beziers in the Solid Edge exports seen so far

    for w, segs in sorted(by_width.items()):
        for pts, closed, dashes, pids in _link_dashes(_chain(segs)):
            st = Stroke(len(strokes), w, pts, closed, dashes, sorted(set(pids)))
            st.prims = fit_prims(pts, closed)
            st.linetype = linetype(dashes)
            st.role = stroke_role(w, st.linetype)
            strokes.append(st)

    mark_hatching(strokes)
    return Classified(strokes, arrowheads, filled)


def summary(cl):
    print(f"{len(cl.strokes)} strokes, {len(cl.arrowheads)} arrowheads, {len(cl.filled)} other filled")
    print("\nstrokes by (width, linetype, role):")
    for k, v in sorted(Counter((s.width, s.linetype, s.role) for s in cl.strokes).items()):
        print(f"  {v:5d}  {k}")
    prims = Counter()
    for s in cl.strokes:
        for p in s.prims:
            prims[(s.role, "circle" if isinstance(p, Arc) and p.is_circle else type(p).__name__)] += 1
    print("\nfitted primitives by role:")
    for k, v in sorted(prims.items()):
        print(f"  {v:5d}  {k}")
    print("\nfilled:", Counter(f.kind for f in cl.filled))


if __name__ == "__main__":
    import sys
    summary(classify(extract(sys.argv[1])))
