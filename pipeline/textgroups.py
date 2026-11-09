"""Group text spans into annotation strings.

PDF text comes as separate spans: "R" and "11", or "Ø", "5,5" and "H7", or
one span per cell of a GD&T frame row. Spans on the same baseline that
follow each other closely form one group. Small spans printed right after a
group (stacked upper/lower deviations) are attached to it as a stack.

Symbol-font characters are mapped to their meaning (Solid Edge ISO symbol
font: verified visually on the frames of T00302932).
"""
from dataclasses import dataclass, field
import math

import numpy as np

# SolidEdgeISO1Symbols character -> symbol
SYMBOLS = {
    "O": "Ø",
    "l": "⌖",     # position
    "n": "⊥",     # perpendicularity
    "i": "//",    # parallelism
    "r": "▱",     # flatness
}

# spans this much smaller than the group's text are stacked deviations
STACK_RATIO = 0.7
# glyph box height relative to font size
LINE_HEIGHT = 1.02


@dataclass
class Group:
    spans: list                 # TextSpans in reading order
    stack: list = field(default_factory=list)   # small spans after the group
    used: bool = False

    @property
    def size(self):
        return max(s.size for s in self.spans)

    @property
    def dir(self):
        return self.spans[0].dir

    @property
    def angle(self):
        return self.spans[0].angle

    @property
    def tokens(self):
        return [symbol(s) for s in self.spans]

    @property
    def text(self):
        return " ".join(self.tokens)

    @property
    def center(self):
        pts = np.array([[(s.bbox.x0 + s.bbox.x1) / 2, (s.bbox.y0 + s.bbox.y1) / 2] for s in self.spans])
        return pts.mean(axis=0)

    @property
    def rect(self):
        b = [s.bbox for s in self.spans + self.stack]
        return np.array([min(r.x0 for r in b), min(r.y0 for r in b),
                         max(r.x1 for r in b), max(r.y1 for r in b)])

    def extent(self):
        """(start, end) along the reading direction."""
        ex = [_along(s, self.spans[0]) for s in self.spans]
        return min(e[0] for e in ex), max(e[1] for e in ex)

    def stack_values(self):
        """(upper, lower) from the stacked deviation spans, or None."""
        if not self.stack:
            return None
        vals = []
        for s in self.stack:
            try:
                vals.append((_up(s, self.spans[0]), float(s.text.strip().replace(",", "."))))
            except ValueError:
                return None
        vals.sort(key=lambda v: -v[0])
        if len(vals) == 1:
            v = vals[0][1]
            return (v, -v)
        return (vals[0][1], vals[-1][1])


def symbol(span):
    t = span.text.strip()
    if "Symbols" in span.font:
        return "".join(SYMBOLS.get(c, c) for c in t)
    return t


def _up_vec(dir_):
    # page y points down: "up" of the text is the reading direction turned left
    return np.array([dir_[1], -dir_[0]])


def _up(span, ref=None):
    """Baseline position, measured in the frame of `ref`'s direction.

    Spans of one label can differ by a degree in angle; projecting both on
    their own axes would turn that into a large position error far from the
    page origin, so a group always uses one reference direction.
    """
    d = (ref or span).dir
    return float(np.asarray(span.origin) @ _up_vec(d))


def _along(span, ref=None):
    """(start, end) of a span along the reading direction of `ref` (default: its own).

    The bbox of rotated text is axis-aligned and inflated, so the text width
    is recovered from it using the known glyph height.
    """
    u = np.asarray((ref or span).dir)
    b = span.bbox
    w, h = b.x1 - b.x0, b.y1 - b.y0
    c, s = abs(u[0]), abs(u[1])
    H = LINE_HEIGHT * span.size
    W = (w - H * s) / c if c >= s else (h - H * c) / s
    start = float(np.asarray(span.origin) @ u)
    return start, start + max(W, 0.0)


def build_groups(texts, exclude=()):
    """Group spans into Groups; spans whose id is in `exclude` are skipped."""
    spans = [t for t in texts if id(t) not in exclude and t.text.strip()]
    if not spans:
        return []
    # the common annotation size: most frequent
    sizes = [round(t.size, 1) for t in spans]
    main = max(set(sizes), key=sizes.count)

    normal = [t for t in spans if t.size >= STACK_RATIO * main]
    small = [t for t in spans if t.size < STACK_RATIO * main]

    # bucket by angle, then by baseline, then chain along the line
    groups = []
    used = set()
    order = sorted(normal, key=lambda t: (round(t.angle), round(_up(t), 0), _along(t)[0]))
    for t in order:
        if id(t) in used:
            continue
        g = [t]
        used.add(id(t))
        grew = True
        while grew:
            grew = False
            end = max(_along(x, t)[1] for x in g)
            start = min(_along(x, t)[0] for x in g)
            for u in order:
                if id(u) in used or abs(u.angle - t.angle) > 2:
                    continue
                if abs(_up(u, t) - _up(t)) > 0.35 * t.size:
                    continue
                a0, a1 = _along(u, t)
                if -0.5 * t.size <= a0 - end <= 1.6 * t.size or -0.5 * t.size <= start - a1 <= 1.6 * t.size:
                    g.append(u)
                    used.add(id(u))
                    grew = True
        g.sort(key=lambda x: _along(x, t)[0])
        groups.append(Group(g))

    # stacked deviations: attach to the group they follow
    for s in small:
        best, best_gap = None, math.inf
        for g in groups:
            if abs(s.angle - g.angle) > 2:
                continue
            ref = g.spans[0]
            _, end = g.extent()
            gap = _along(s, ref)[0] - end
            base = _up(ref)
            if -0.3 * g.size <= gap <= 2.0 * g.size and -0.4 * g.size <= _up(s, ref) - base <= 1.2 * g.size:
                if gap < best_gap:
                    best, best_gap = g, gap
        if best is not None:
            best.stack.append(s)
    return groups
