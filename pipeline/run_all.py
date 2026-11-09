"""Run every stage for one drawing and model, in one process.

    drawing (PDF) ──► extract ─► classify ─► views ─► ordinates ─► annotations ──┐
    model (STEP) ──► register views ──► match annotations to faces ─► AP242 PMI ◄┘

The drawing is read once and every stage works on the same objects. The
JSON files written to <out>/intermediate are the same as each module's own
--json output, for inspection or for rerunning one stage alone.
"""
from contextlib import contextmanager
import ctypes
import json
import os
from pathlib import Path
import sys
import time

import annotations
import faces
import ordinates
import pmi_writer
import register
from classify import classify
from pdf_extractor import extract


_libc = ctypes.CDLL(None)


@contextmanager
def _logged(log):
    """Send stdout (Python's and OCCT's C++ output) to the log file."""
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(log.fileno(), 1)
    old, sys.stdout = sys.stdout, log
    try:
        yield
    finally:
        log.flush()
        _libc.fflush(None)      # OCCT writes through C stdio: flush it while it still goes to the log
        sys.stdout = old
        os.dup2(saved, 1)
        os.close(saved)


def _dump(obj, path):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def run(pdf, step, out_dir, overlays=True):
    pdf, step, out_dir = Path(pdf), Path(step), Path(out_dir)
    work = out_dir / "intermediate"
    work.mkdir(parents=True, exist_ok=True)
    name = step.stem
    out_step = out_dir / f"{name}_pmi.stp"
    report_path = out_dir / "pmi_report.json"
    log_path = out_dir / "log.txt"

    t_all = time.time()
    print(f"{pdf.name} + {step.name} -> {out_dir}/")
    with open(log_path, "w") as log:
        def stage(i, title, fn):
            print(f"  {i}/5 {title:<28}", end="", flush=True)
            t = time.time()
            print(f"\n==== {title}\n", file=log, flush=True)
            with _logged(log):
                info = fn()
            print(f"{info}  ({time.time() - t:.0f} s)", flush=True)

        st = {}

        def read_drawing():
            pg = extract(str(pdf))
            cl = classify(pg)
            ordinate_result = ordinates.parse(pg, cl)
            anns = annotations.annotate(pg, cl, ordinate_result=ordinate_result)
            # annotate() takes over ordinates that turn out to be linear dimensions:
            # write the ordinates after it, so none is listed twice
            ords = ordinates.to_records(*ordinate_result)
            ann_rec = annotations.to_records(anns)
            ordinates.summary(*ordinate_result)
            annotations.summary(anns)
            _dump(ords, work / "ordinates.json")
            _dump(ann_rec, work / "annotations.json")
            st.update(pg=pg, cl=cl, ordinate_result=ordinate_result, ords=ords, anns=ann_rec)
            sheet = ordinate_result[0]
            n_views = sum(1 for v in sheet.views if v.kind != "isometric")
            return f"{n_views} views, {len(ords['ordinates'])} ordinates, {len(ann_rec)} annotations"

        def register_views():
            model = register.load_model(str(step))
            sheet, regs = register.register(st["pg"], model, st["cl"], st["ordinate_result"])
            register.summary(sheet, regs)
            records = register.to_records(sheet, regs)
            _dump(records, work / "registration.json")
            st.update(model=model, sheet=sheet, regs=regs, reg_rec=records)
            unsure = [r["label"] or str(r["view"]) for r in records if not r["confident"]]
            missing = [v for v in sheet.views if v.kind != "isometric" and v.id not in regs]
            info = f"{len(regs)} views placed"
            if unsure:
                info += f", NOT CONFIDENT: {', '.join(unsure)}"
            if missing:
                info += f", NOT PLACED: {', '.join(v.label or str(v.id) for v in missing)}"
            return info

        def match_faces():
            fc = faces.build(st["model"])
            matches = faces.match_all(fc, st["regs"], st["ords"], st["anns"])
            faces.summary(matches)
            records = faces.to_records(matches)
            _dump(records, work / "faces.json")
            st.update(fc=fc, matches=matches, match_rec=records)
            n = {}
            for m in records:
                if m["source"] != "origin":
                    n[m["status"]] = n.get(m["status"], 0) + 1
            return ", ".join(f"{v} {k}" for k, v in sorted(n.items(), key=lambda kv: -kv[1]))

        def write_pmi():
            doc = pmi_writer.PMIDocument(str(step))
            exportable = [m for m in st["match_rec"] if m["status"] in pmi_writer.EXPORT]
            doc.check_faces(exportable)
            regs = {r["view"]: r for r in st["reg_rec"]}
            report = pmi_writer.export(doc, st["match_rec"], st["anns"], st["ords"], regs,
                                       st["sheet"].title_block)
            doc.write(str(out_step))
            _dump(report, report_path)
            pmi_writer.summary(report)
            st["report"] = report
            written = sum(1 for r in report if r["exported"])
            flagged = sum(1 for r in report if r["exported"] and r["flags"])
            return f"{written} items written ({flagged} flagged), {len(report) - written} not written"

        def draw():
            if not overlays:
                return "skipped"
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from debug_overlay import draw_page, new_figure
            rects = {v.id: v.rect for v in st["sheet"].views}
            for fname, paint in (
                    ("registration_overlay.png",
                     lambda ax: register.draw_registration(ax, st["regs"], st["model"], st["sheet"])),
                    ("faces_overlay.png",
                     lambda ax: faces.draw_matches(ax, st["fc"], st["regs"], st["matches"], rects))):
                fig, ax = new_figure(st["pg"])
                draw_page(ax, st["pg"], show_text=False, lw=0.3)
                paint(ax)
                fig.tight_layout()
                fig.savefig(out_dir / fname, dpi=200)
                plt.close(fig)
            return "registration_overlay.png, faces_overlay.png"

        stage(1, "reading the drawing", read_drawing)
        stage(2, "placing the model in views", register_views)
        stage(3, "matching faces", match_faces)
        stage(4, "writing AP242 PMI", write_pmi)
        stage(5, "drawing overlays", draw)

    print(f"done in {time.time() - t_all:.0f} s")
    print(f"  {out_step}")
    print(f"  {report_path}   (what was not written, and why)")
    print(f"  {log_path}")
    return out_step
