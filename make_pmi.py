"""Turn a 2D drawing and its 3D model into a STEP AP242 file with semantic PMI.

The dimensions, tolerances, fits, GD&T and datums on the drawing are read,
matched to the model's faces, and written into a copy of the STEP model.

    python make_pmi.py DRAWING.pdf MODEL.stp [-o OUTPUT_DIR] [--no-overlays]

Needs pythonocc-core (run it with the `occ` micromamba environment), plus
PyMuPDF, NumPy, SciPy and Matplotlib.

Output (default: output/<model name>/):
    <model>_pmi.stp            the model with PMI
    pmi_report.json            every annotation: written or not, why, and flags to check
    registration_overlay.png   the model drawn over each view
    faces_overlay.png          the matched faces drawn over each view
    log.txt                    each stage's detailed summary
    intermediate/              the stages' JSON output

The code is in pipeline/; docs/ISO_STANDARDS_USED.md lists the ISO rules used.
"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))


def main():
    ap = argparse.ArgumentParser(description="Drawing (PDF) + model (STEP) -> STEP AP242 with PMI.")
    ap.add_argument("pdf", help="the drawing, a vector PDF (first page is used)")
    ap.add_argument("step", help="the part's STEP model")
    ap.add_argument("-o", "--out", help="output folder (default: output/<model name>)")
    ap.add_argument("--no-overlays", action="store_true", help="skip the PNG overlays")
    args = ap.parse_args()

    for p in (args.pdf, args.step):
        if not Path(p).is_file():
            sys.exit(f"not found: {p}")
    try:
        import OCC  # noqa: F401
    except ImportError:
        sys.exit("pythonocc-core is not installed in this Python. Use the occ environment:\n"
                 "  micromamba activate occ")

    from run_all import run
    out = Path(args.out) if args.out else ROOT / "output" / Path(args.step).stem
    run(args.pdf, args.step, out, overlays=not args.no_overlays)


if __name__ == "__main__":
    main()
