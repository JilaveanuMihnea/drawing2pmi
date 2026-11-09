"""Extract vector paths and text spans from a drawing PDF page.

All coordinates are in the displayed (rotated) page frame: x to the right,
y downward, in PDF points. Nothing here interprets the drawing; it only
returns raw primitives with their style for later classification.
"""
from collections import Counter
from dataclasses import dataclass, field
import math

import numpy as np
import pymupdf


@dataclass
class Segment:
    kind: str           # "line" or "bezier"
    pts: np.ndarray     # (2, 2) endpoints for lines, (4, 2) control points for beziers
    path_id: int


@dataclass
class Path:
    id: int
    width: float        # stroke width; 0 for fill-only paths
    color: tuple | None  # stroke color, None if not stroked
    fill: tuple | None   # fill color, None if not filled
    dashes: str | None
    closed: bool
    rect: pymupdf.Rect
    segments: list[Segment] = field(default_factory=list)


@dataclass
class TextSpan:
    text: str
    bbox: pymupdf.Rect
    origin: np.ndarray  # baseline start point
    size: float
    font: str
    dir: np.ndarray     # unit baseline direction

    @property
    def angle(self):
        """Counter-clockwise angle of the baseline on screen, in degrees."""
        return math.degrees(math.atan2(-self.dir[1], self.dir[0]))


@dataclass
class PageData:
    width: float
    height: float
    rotation: int
    paths: list[Path]
    texts: list[TextSpan]

    @property
    def segments(self):
        return [s for p in self.paths for s in p.segments]


def _xy(p, m):
    p = pymupdf.Point(p) * m
    return (p.x, p.y)


def _segments(item, m, path_id):
    op = item[0]
    if op == "l":
        return [Segment("line", np.array([_xy(item[1], m), _xy(item[2], m)]), path_id)]
    if op == "c":
        return [Segment("bezier", np.array([_xy(p, m) for p in item[1:5]]), path_id)]
    if op == "re":
        r = item[1]
        corners = [r.tl, r.tr, r.br, r.bl, r.tl]
    elif op == "qu":
        q = item[1]
        corners = [q.ul, q.ur, q.lr, q.ll, q.ul]
    else:
        raise ValueError(f"unknown path item {op!r}")
    return [Segment("line", np.array([_xy(a, m), _xy(b, m)]), path_id)
            for a, b in zip(corners, corners[1:])]


def extract(pdf_path, page_no=0):
    page = pymupdf.open(pdf_path)[page_no]
    m = page.rotation_matrix
    # directions only need the linear part of the matrix
    lin = pymupdf.Matrix(m.a, m.b, m.c, m.d, 0, 0)

    paths = []
    for i, d in enumerate(page.get_drawings()):
        path = Path(
            id=i,
            width=d["width"] or 0.0,
            color=d["color"],
            fill=d["fill"],
            dashes=d["dashes"],
            closed=bool(d["closePath"]),
            rect=d["rect"] * m,
        )
        for item in d["items"]:
            path.segments += _segments(item, m, i)
        paths.append(path)

    texts = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            d = pymupdf.Point(line["dir"]) * lin
            for span in line["spans"]:
                if not span["text"].strip():
                    continue
                texts.append(TextSpan(
                    text=span["text"],
                    bbox=pymupdf.Rect(span["bbox"]) * m,
                    origin=np.array(_xy(span["origin"], m)),
                    size=span["size"],
                    font=span["font"],
                    dir=np.array([d.x, d.y]),
                ))

    return PageData(page.rect.width, page.rect.height, page.rotation, paths, texts)


def sample_bezier(pts, n=20):
    t = np.linspace(0, 1, n)[:, None]
    return ((1-t)**3)*pts[0] + 3*((1-t)**2)*t*pts[1] + 3*(1-t)*t**2*pts[2] + t**3*pts[3]


def summary(pg):
    """Print style statistics, useful for picking classification thresholds."""
    print(f"page {pg.width} x {pg.height}, rotation {pg.rotation}")
    print(f"{len(pg.paths)} paths, {len(pg.segments)} segments, {len(pg.texts)} text spans")

    print("\npaths by (width, dashes, filled):")
    styles = Counter((round(p.width, 2), p.dashes, p.fill is not None) for p in pg.paths)
    for k, v in styles.most_common():
        print(f"  {v:6d}  {k}")

    print("\nline length by width (pt):")
    length = Counter()
    for p in pg.paths:
        for s in p.segments:
            if s.kind == "line":
                length[round(p.width, 2)] += np.linalg.norm(s.pts[1] - s.pts[0])
    for k, v in sorted(length.items()):
        print(f"  {k:5.2f}  {v:9.0f}")

    print("\nspans by (font, size):")
    for k, v in Counter((t.font, round(t.size, 1)) for t in pg.texts).most_common():
        print(f"  {v:4d}  {k}")


if __name__ == "__main__":
    import sys
    summary(extract(sys.argv[1]))
