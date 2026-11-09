"""Render extracted PDF primitives with matplotlib for visual debugging.

Later steps can call new_figure() + draw_page() and then draw their own
records on the same axes before saving.
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from pdf_extractor import extract, sample_bezier


def new_figure(pg, fig_w=14):
    fig, ax = plt.subplots(figsize=(fig_w, fig_w * pg.height / pg.width))
    ax.set_xlim(0, pg.width)
    ax.set_ylim(pg.height, 0)    # y points down, as on the page
    ax.set_aspect("equal")
    ax.axis("off")
    return fig, ax


def draw_page(ax, pg, show_text=True, lw=0.5):
    for s in pg.segments:
        pts = s.pts if s.kind == "line" else sample_bezier(s.pts)
        ax.plot(pts[:, 0], pts[:, 1], "k-", lw=lw)

    if not show_text:
        return
    # PDF points -> screen points at the current figure size
    scale = ax.figure.get_figwidth() * 72 / pg.width
    for t in pg.texts:
        r = t.bbox
        # bounding box, so you can see exactly where PyMuPDF placed each span
        ax.add_patch(mpatches.Rectangle((r.x0, r.y0), r.width, r.height,
                                        fill=False, ec="red", lw=0.4))
        ax.text((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, t.text,
                fontsize=t.size * scale, color="blue",
                rotation=t.angle, rotation_mode="anchor",
                ha="center", va="center")


ROLE_COLORS = {
    "visible": "black",
    "thin": "tab:blue",
    "thin_medium": "tab:cyan",
    "hatch": "0.8",
    "center": "tab:green",
    "hidden": "tab:orange",
    "phantom": "tab:purple",
    "cutting_plane": "red",
    "frame": "0.6",
    "title_block": "0.6",
}


def draw_classified(ax, cl, lw=0.5):
    """Draw fitted lines and arcs colored by role, and arrowhead tips.

    Lines and arcs are drawn from their fitted parameters, not the raw
    points, so a bad fit shows up as a visible mismatch.
    """
    # local import keeps draw_page usable without scipy
    from classify import Arc

    for s in cl.strokes:
        c = ROLE_COLORS.get(s.role, "magenta")
        for p in s.prims:
            pts = p.sample() if isinstance(p, Arc) else np.array([p.p0, p.p1])
            ax.plot(pts[:, 0], pts[:, 1], "-", color=c, lw=lw)
    for a in cl.arrowheads:
        ax.plot(*a.tip, "o", color="red", ms=1.2)
        tail = a.tip - a.dir * a.length
        ax.plot([tail[0], a.tip[0]], [tail[1], a.tip[1]], "-", color="red", lw=lw * 2)
    for f in cl.filled:
        pts = np.vstack([f.pts, f.pts[:1]])
        ax.plot(pts[:, 0], pts[:, 1], "-", color="magenta", lw=lw)


VIEW_COLORS = {
    "orthographic": "tab:blue",
    "section": "tab:red",
    "detail": "tab:green",
    "isometric": "0.5",
}


def draw_sheet(ax, sh):
    """Frame, title block and view boxes labelled with id, kind, caption and scale."""
    def box(r, color, lw=1.2, ls="-"):
        ax.add_patch(mpatches.Rectangle((r[0], r[1]), r[2] - r[0], r[3] - r[1],
                                        fill=False, ec=color, lw=lw, ls=ls))

    box(sh.frame, "0.3", ls="--")
    if sh.title_block:
        box(sh.title_block.rect, "tab:purple", lw=1.5)
    for v in sh.views:
        c = VIEW_COLORS.get(v.kind, "magenta")
        box(v.rect, c)
        txt = f"{v.id} {v.kind}" + (f" {v.label}" if v.label else "") + (f" {v.scale:g}:1" if v.scale else "")
        ax.text(v.rect[0], v.rect[1] - 6, txt, color=c, fontsize=7, va="bottom")
    for t in sh.markers:
        b = t.bbox
        ax.add_patch(mpatches.Rectangle((b.x0, b.y0), b.width, b.height, fill=False, ec="orange", lw=1))


def draw_ordinates(ax, origins, dims):
    """Extension lines (green = fits its view, red = outlier, orange = no anchor),
    anchors as dots, origins as dashed lines across their view."""
    for o in origins:
        ax.plot(*o.marker, "s", color="purple", ms=3)
    for d in dims:
        c = "red" if d.outlier else ("orange" if not d.anchors or d.view is None else "tab:green")
        ax.plot([d.outer[0], d.inner[0]], [d.outer[1], d.inner[1]], "-", color=c, lw=0.9)
        if d.anchor is not None:
            ax.plot(*d.anchor, "o", color=c, ms=2.5)
        if d.outlier or not d.anchors:
            ax.text(*d.inner, d.text.text.strip(), color=c, fontsize=5)


STATUS_COLORS = {"matched": "tab:green", "located": "tab:blue", "mismatch": "red", "unmatched": "orange"}


def draw_annotations(ax, anns):
    """Dimension lines / leaders, arrow tips and feature anchors, by status."""
    for a in anns:
        if a.kind == "note":
            continue
        c = STATUS_COLORS.get(a.status, "magenta")
        g = a.geometry or {}
        if "dimension_line" in g:
            p, q = np.array(g["dimension_line"])
            ax.plot([p[0], q[0]], [p[1], q[1]], "-", color=c, lw=1.0)
        if "leader" in g:
            pts = np.array(g["leader"])
            ax.plot(pts[:, 0], pts[:, 1], "-", color=c, lw=1.0)
        for p, _ in a.anchors:
            ax.plot(*p, "o", color=c, ms=2.5)
        if a.kind in ("linear", "diameter"):
            # each arrow tip to the feature its extension line reaches
            for t, (p, _) in zip(a.tips, a.anchors):
                if np.linalg.norm(np.asarray(t) - p) > 0.5:
                    ax.plot([t[0], p[0]], [t[1], p[1]], ":", color=c, lw=0.8)
        r = a.group.rect
        ax.add_patch(mpatches.Rectangle((r[0], r[1]), r[2] - r[0], r[3] - r[1], fill=False, ec=c, lw=0.8))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("out", nargs="?", default="overlay.png")
    ap.add_argument("--classified", action="store_true",
                    help="draw classified strokes colored by role instead of raw paths")
    ap.add_argument("--views", action="store_true",
                    help="draw frame, title block and view boxes on top")
    ap.add_argument("--ordinates", action="store_true",
                    help="draw parsed ordinate dimensions and anchors on top")
    ap.add_argument("--annotations", action="store_true",
                    help="draw parsed radius/diameter/linear/GD&T annotations on top")
    ap.add_argument("--region", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                    help="crop to this page rectangle (points)")
    args = ap.parse_args()

    pg = extract(args.pdf)
    fig, ax = new_figure(pg)
    cl = None
    if args.classified or args.views or args.ordinates or args.annotations:
        from classify import classify
        cl = classify(pg)
    if args.classified:
        draw_classified(ax, cl)
    else:
        draw_page(ax, pg, show_text=not (args.views or args.ordinates or args.annotations))
    if args.views or args.ordinates or args.annotations:
        from views import segment
        sheet = segment(pg, cl)
    if args.views:
        draw_sheet(ax, sheet)
    if args.ordinates or args.annotations:
        from ordinates import parse
        ores = parse(pg, cl, sheet)
    if args.annotations:
        from annotations import annotate
        draw_annotations(ax, annotate(pg, cl, sheet, ores))
    if args.ordinates:
        draw_ordinates(ax, ores[1], ores[2])
    if args.region:
        x0, y0, x1, y1 = args.region
        ax.set_xlim(x0, x1)
        ax.set_ylim(y1, y0)
    fig.tight_layout()
    fig.savefig(args.out, dpi=200)
    print("wrote", args.out)
