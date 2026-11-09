"""Write the matched drawing annotations into the STEP model as AP242 semantic PMI.

Input is faces.json (annotation -> model faces, from faces.py) with the
parsed values from annotations.json / ordinates.json and the view frames
from registration.json. The STEP model is read through XCAF, so its faces
come in the same TopExp order as the face ids in faces.json; every face's
surface is checked against the recorded one before anything is written.

Export policy:
  matched, ambiguous   written (ambiguous: the best candidate)
  mismatch             written, flagged: the drawing value differs from the model
  anything else        not written, listed in the report with the reason

Each dimension carries a "drawing" description tracing it back
("<source> <id>: <drawing text>") and a "check" description per flag.
Geometric tolerances have no text field in XCAF; the report traces them.

OCCT details worked around here:
  - pythonocc does not wrap XCAFDoc_GeomTolerance; its object is stored in
    child labels (1 type, 2 type of value, 3 value), as SetObject would.
  - The STEP writer negates the lower tolerance bound; it is pre-negated so
    the file holds the signed deviation (+0.1 / -0.1 -> lower_bound -0.1).
  - The STEP writer prints an ISO fit grade as the enum index + 1 (IT7 -> "9");
    the grade is shifted so the file says "7".
"""
import json
import math
import re

import numpy as np

from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.Interface import Interface_Static
from OCC.Core.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
from OCC.Core.STEPControl import STEPControl_AsIs
from OCC.Core.TCollection import TCollection_ExtendedString, TCollection_HAsciiString
from OCC.Core.TDataStd import TDataStd_Integer, TDataStd_Real
from OCC.Core.TDF import TDF_LabelSequence
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.TopAbs import TopAbs_FACE
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.XCAFDoc import XCAFDoc_Datum, XCAFDoc_Dimension, XCAFDoc_DocumentTool
from OCC.Core.gp import gp_Dir
import OCC.Core.XCAFDimTolObjects as X

EXPORT = ("matched", "ambiguous", "mismatch")

GDT_TYPES = {
    "flatness": X.XCAFDimTolObjects_GeomToleranceType_Flatness,
    "straightness": X.XCAFDimTolObjects_GeomToleranceType_Straightness,
    "circularity": X.XCAFDimTolObjects_GeomToleranceType_CircularityOrRoundness,
    "cylindricity": X.XCAFDimTolObjects_GeomToleranceType_Cylindricity,
    "parallelism": X.XCAFDimTolObjects_GeomToleranceType_Parallelism,
    "perpendicularity": X.XCAFDimTolObjects_GeomToleranceType_Perpendicularity,
    "angularity": X.XCAFDimTolObjects_GeomToleranceType_Angularity,
    "position": X.XCAFDimTolObjects_GeomToleranceType_Position,
    "concentricity": X.XCAFDimTolObjects_GeomToleranceType_Concentricity,
    "coaxiality": X.XCAFDimTolObjects_GeomToleranceType_Coaxiality,
    "symmetry": X.XCAFDimTolObjects_GeomToleranceType_Symmetry,
    "profile_line": X.XCAFDimTolObjects_GeomToleranceType_ProfileOfLine,
    "profile_surface": X.XCAFDimTolObjects_GeomToleranceType_ProfileOfSurface,
    "circular_runout": X.XCAFDimTolObjects_GeomToleranceType_CircularRunout,
    "total_runout": X.XCAFDimTolObjects_GeomToleranceType_TotalRunout,
}

FIT_RE = re.compile(r"^([A-Za-z]{1,2})(\d{1,2})$")


def _h(s):
    return TCollection_HAsciiString(str(s))


# ---------------------------------------------------------------- document

class PMIDocument:
    def __init__(self, step_path):
        self.doc = TDocStd_Document(TCollection_ExtendedString("pmi"))
        r = STEPCAFControl_Reader()
        r.SetGDTMode(True)
        if r.ReadFile(step_path) != IFSelect_RetDone or not r.Transfer(self.doc):
            raise RuntimeError(f"cannot read {step_path}")
        self.shapes = XCAFDoc_DocumentTool.ShapeTool(self.doc.Main())
        self.dimtol = XCAFDoc_DocumentTool.DimTolTool(self.doc.Main())
        roots = TDF_LabelSequence()
        self.shapes.GetFreeShapes(roots)
        if roots.Length() != 1:
            raise RuntimeError(f"expected one part, found {roots.Length()} root shapes")
        self.part = roots.Value(1)
        self.faces = []
        exp = TopExp_Explorer(self.shapes.GetShape(self.part), TopAbs_FACE)
        while exp.More():
            self.faces.append(exp.Current())
            exp.Next()
        self._labels = {}
        self.datum_features = {}    # letter -> face ids

    def labels(self, ids):
        seq = TDF_LabelSequence()
        for i in ids:
            if i not in self._labels:
                self._labels[i] = self.shapes.AddSubShape(self.part, self.faces[i])
            seq.Append(self._labels[i])
        return seq

    def check_faces(self, matches):
        """Every referenced face must still be the surface faces.json recorded."""
        bad = []
        for m in matches:
            for f in m["features"] + m["alternatives"]:
                for i in f["faces"]:
                    if not _same_surface(self.faces[i], f["surface"]):
                        bad.append((m["source"], m["id"], i))
        if bad:
            raise RuntimeError(f"face ids do not match the model: {bad[:5]}")

    # -------------------------------------------------------- items

    def dimension(self, kind, value, first, second=None, tol=None, fit=None,
                  direction=None, name="", flags=()):
        lab = self.dimtol.AddDimension()
        obj = X.XCAFDimTolObjects_DimensionObject()
        obj.SetType(kind)
        obj.SetValue(float(value))
        if tol is not None:
            up, lo = tol
            obj.SetUpperTolValue(float(up))
            obj.SetLowerTolValue(-float(lo))    # the writer negates it back
        if fit:
            cls = _fit_class(fit)
            if cls:
                obj.SetClassOfTolerance(*cls)
        if direction is not None:
            obj.SetDirection(gp_Dir(*map(float, direction)))
        obj.SetSemanticName(_h(name))
        # the writer drops semantic names but keeps descriptions
        obj.AddDescription(_h(name), _h("drawing"))
        for f in flags:
            obj.AddDescription(_h(f), _h("check"))
        XCAFDoc_Dimension.Set(lab).SetObject(obj)
        if second:
            self.dimtol.SetDimension(self.labels(first), self.labels(second), lab)
        else:
            self.dimtol.SetDimension(self.labels(first), TDF_LabelSequence(), lab)
        return lab

    def datum_feature(self, letter, ids):
        """The datum symbol: datum `letter` is established by these faces."""
        self.datum_features[letter] = ids
        lab = self._datum(letter)
        self.dimtol.SetDatum(self.labels(ids), lab)

    def tolerance(self, kind, value, ids, diameter=False, datums=(), name=""):
        lab = self.dimtol.AddGeomTolerance()
        # XCAFDoc_GeomTolerance::SetObject's layout (the class is not wrapped)
        TDataStd_Integer.Set(lab.FindChild(1), int(kind))
        if diameter:
            TDataStd_Integer.Set(lab.FindChild(2), int(X.XCAFDimTolObjects_GeomToleranceTypeValue_Diameter))
        TDataStd_Real.Set(lab.FindChild(3), float(value))
        self.dimtol.SetGeomTolerance(self.labels(ids), lab)
        missing = []
        for pos, letter in enumerate(datums, 1):
            if letter not in self.datum_features:
                missing.append(letter)
                continue
            d = self._datum(letter, pos)
            self.dimtol.SetDatum(self.labels(self.datum_features[letter]), d)
            self.dimtol.SetDatumToGeomTol(d, lab)
        return lab, missing

    def _datum(self, letter, position=None):
        lab = self.dimtol.AddDatum(_h(letter), _h(""), _h(letter))
        obj = X.XCAFDimTolObjects_DatumObject()
        obj.SetName(_h(letter))
        if position:
            obj.SetPosition(position)
        XCAFDoc_Datum.Set(lab).SetObject(obj)
        return lab

    def write(self, path):
        Interface_Static.SetCVal("write.step.schema", "AP242DIS")
        w = STEPCAFControl_Writer()
        w.SetDimTolMode(True)
        if not w.Transfer(self.doc, STEPControl_AsIs) or w.Write(path) != IFSelect_RetDone:
            raise RuntimeError(f"cannot write {path}")


def _same_surface(face, rec, tol=1e-3):
    s = BRepAdaptor_Surface(face)
    t = s.GetType()
    if rec["type"] == "plane":
        if t != GeomAbs_Plane:
            return False
        n = np.array(s.Plane().Axis().Direction().Coord())
        return abs(abs(n @ np.array(rec["normal"])) - 1) < tol
    if rec["type"] == "cylinder":
        return t == GeomAbs_Cylinder and abs(s.Cylinder().Radius() - rec["radius"]) < tol
    if rec["type"] == "cone":
        return t == GeomAbs_Cone and abs(s.Cone().RefRadius() - rec["radius"]) < tol
    return t not in (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone)


def _fit_class(fit):
    """'H7' -> (is_hole, form variance, grade) for SetClassOfTolerance, or None."""
    m = FIT_RE.match(fit.strip())
    if not m:
        return None
    letters, grade = m.group(1), int(m.group(2))
    variance = getattr(X, f"XCAFDimTolObjects_DimensionFormVariance_{letters.upper()}", None)
    # the writer prints the grade as enum index + 1 (IT01 = 0 -> "1"): shift so the file reads `grade`
    if variance is None or grade < 1:
        return None
    return letters[0].isupper(), variance, X.XCAFDimTolObjects_DimensionGrade(grade - 1)


# ---------------------------------------------------------------- export

def _faces(feats, role=None):
    ids = []
    for f in feats:
        if role is None or f["role"] == role:
            ids += [i for i in f["faces"] if i not in ids]
    return ids


def _material_between(f1, f2):
    """Two parallel planes facing away from each other: the part's thickness."""
    s1, s2 = f1["surface"], f2["surface"]
    if s1["type"] != "plane" or s2["type"] != "plane":
        return False
    n1, n2 = np.array(s1["normal"]), np.array(s2["normal"])
    if n1 @ n2 > -0.999:
        return False
    # point on each plane: n * offset; the other plane lies behind its normal
    p1, p2 = n1 * s1["offset"], n2 * s2["offset"]
    return (p2 - p1) @ n1 < 0 and (p1 - p2) @ n2 < 0


# ISO 2768-1 general angular tolerances (deg) by the shorter leg's length (mm)
ISO2768_ANGLES = {
    "f": [(10, 1.0), (50, 0.5), (120, 1 / 3), (400, 1 / 6), (math.inf, 1 / 12)],
    "m": [(10, 1.0), (50, 0.5), (120, 1 / 3), (400, 1 / 6), (math.inf, 1 / 12)],
    "c": [(10, 1.5), (50, 1.0), (120, 0.5), (400, 0.25), (math.inf, 1 / 6)],
    "v": [(10, 3.0), (50, 2.0), (120, 1.0), (400, 0.5), (math.inf, 1 / 3)],
}


def _angular_general_tol(leg, title):
    if leg is None or not title or not title.tolerance_grade:
        return None
    for upto, t in ISO2768_ANGLES.get(title.tolerance_grade, []):
        if leg <= upto:
            return (t, -t)
    return None


def _feature_radius(feats, org):
    """Distance of a target's axis from the origin axis (the angle's leg length)."""
    a = np.array(org["surface"].get("center") or [0, 0, 0])
    for f in feats:
        c = f["surface"].get("center")
        if c is not None:
            ax = np.array(org["surface"]["axis"])
            d = np.array(c) - a
            return float(np.linalg.norm(d - (d @ ax) * ax))
    return None


def _cone_leg(surface):
    """Length of a cone's generatrix from the apex to its reference radius (mm)."""
    if surface.get("radius") is None or not surface.get("half_angle_deg"):
        return None
    return surface["radius"] / math.sin(math.radians(surface["half_angle_deg"]))


def _axis_name(v):
    i = int(np.argmax(np.abs(v)))
    return ("+" if v[i] > 0 else "-") + "XYZ"[i]


NO_GENERAL = "no tolerance: below the ISO 2768-1 range (0.5 mm) and none written on the drawing"


def _below_general(value, title, kind="linear"):
    from annotations import below_general_range
    return below_general_range(kind, value, title)


def _general_tol(value, title, kind="linear"):
    from annotations import _general
    return _general(kind, value, title)


def export(doc, matches, anns, ords, regs, title):
    """Add PMI for every exportable match. Returns the report entries."""
    ann = {a["id"]: a for a in anns}
    report = []

    def done(m, what, flags=()):
        report.append({"source": m["source"], "id": m["id"], "view": m["view"], "kind": m["kind"],
                       "text": m["text"], "status": m["status"], "exported": what,
                       "flags": list(flags), "note": m["note"]})

    def skip(m, reason):
        report.append({"source": m["source"], "id": m["id"], "view": m["view"], "kind": m["kind"],
                       "text": m["text"], "status": m["status"], "exported": None,
                       "reason": reason, "note": m["note"]})

    def flags_of(m):
        fl = []
        if m["status"] == "ambiguous":
            fl.append("ambiguous: best of %d candidates" % (1 + len({tuple(f["faces"]) for f in m["alternatives"]})))
        if m["status"] == "mismatch":
            d = max((abs(f.get("delta") or 0) for f in m["features"]), default=0)
            fl.append(f"value differs from the model by {d:.3f} mm")
        return fl

    def label(m, extra=""):
        s = f"{m['source']} {m['id']}: {m['text']}"
        if m["status"] == "mismatch":
            s += " [differs from model]"
        return s + extra

    # datum features first: tolerances refer to them
    for m in matches:
        if m["kind"] == "datum" and m["status"] in EXPORT:
            doc.datum_feature(m["text"].strip(), _faces(m["features"]))
            done(m, "datum feature", flags_of(m))

    # ordinate origins: one representative of the zero feature per view axis
    origin = {}
    dimensioned = {i for m in matches if m["source"] == "annotation" and m["kind"] == "diameter"
                   and m["status"] in EXPORT for f in m["features"] for i in f["faces"]}
    for m in matches:
        if m["source"] != "origin" or m["status"] not in EXPORT:
            continue
        feats = m["features"]
        pick = next((f for f in feats if set(f["faces"]) & dimensioned), None) or \
            max(feats, key=lambda f: f["surface"].get("radius") or 0)
        origin[(m["view"], m["note"].split()[-1])] = pick

    ord_list = ords["ordinates"]
    for m in matches:
        if m["kind"] in ("datum",) or m["source"] == "origin":
            continue
        if m["status"] not in EXPORT:
            skip(m, _reason(m))
            continue
        fl = flags_of(m)
        feats = m["features"]
        if m["source"] == "ordinate":
            o = ord_list[m["id"]]
            org = origin.get((m["view"], o["axis"]))
            if org is None:
                skip(m, "no matched origin for this view axis")
                continue
            r = regs[m["view"]]
            direction = r["u_right"] if o["axis"] == "x" else r["v_up"]
            tol = tuple(o["tol"]) if o.get("tol") else (None if o.get("basic") else _general_tol(o["value"], title))
            if tol is None and not o.get("basic") and _below_general(o["value"], title):
                fl.append(NO_GENERAL)
            how = feats[0].get("how")
            extra = ""
            if how == "sharp":
                extra = " (virtual sharp: intersection of the extended faces)"
                fl.append("target is a virtual sharp; referenced by the faces forming it")
            elif how == "vertex":
                extra = " (vertex where these faces meet)"
            elif how == "axis":
                extra = " (axis)"
            if o.get("basic"):
                extra += " (basic)"
            doc.dimension(X.XCAFDimTolObjects_DimensionType_Location_LinearDistance, o["value"],
                          org["faces"], _faces(feats), tol=tol, direction=direction,
                          name=label(m, extra), flags=fl)
            done(m, "linear distance from the origin axis", fl)
            continue

        a = ann[m["id"]]
        tol = tuple(a["tol"]) if a.get("tol") else (tuple(a["general_tol"]) if a.get("general_tol") else None)
        if a.get("reference"):
            tol = None
        elif tol is None and m["kind"] in ("linear", "diameter", "radius") and \
                _below_general(a["value"], title, m["kind"]):
            fl.append(NO_GENERAL)
        extra = " (reference)" if a.get("reference") else ""
        if m["kind"] in ("radius", "diameter"):
            if any(f.get("how") == "pitch_circle" for f in feats):
                skip(m, "pitch circle through hole axes: no surface to attach a size to")
                continue
            kind = X.XCAFDimTolObjects_DimensionType_Size_Radius if m["kind"] == "radius" \
                else X.XCAFDimTolObjects_DimensionType_Size_Diameter
            if m["note"].startswith("drawn"):
                fl.append(m["note"])
            doc.dimension(kind, a["value"], _faces(feats), tol=tol, fit=a.get("fit"), name=label(m, extra),
                          flags=fl)
            done(m, m["kind"], fl)
        elif m["kind"] == "linear":
            s1 = [f for f in feats if f["role"] == "side1"]
            s2 = [f for f in feats if f["role"] == "side2"]
            if _material_between(s1[0], s2[0]):
                kind, what = X.XCAFDimTolObjects_DimensionType_Size_Thickness, "thickness"
                doc.dimension(kind, a["value"], _faces(s1) + _faces(s2), tol=tol, fit=a.get("fit"),
                              name=label(m, extra), flags=fl)
            else:
                kind, what = X.XCAFDimTolObjects_DimensionType_Location_LinearDistance, "linear distance"
                doc.dimension(kind, a["value"], _faces(s1), _faces(s2), tol=tol, fit=a.get("fit"),
                              name=label(m, extra), flags=fl)
            done(m, what, fl)
        elif m["kind"] == "angle":
            geo = a.get("geometry") or {}
            if geo.get("type") == "angular_ordinate":
                org = origin.get((m["view"], "x"))
                if org is None:
                    skip(m, "no matched origin for this view")
                    continue
                r = regs[m["view"]]
                zero = math.radians(geo["zero_page_dir"])
                zdir = np.cos(zero) * np.array(r["u_right"]) + np.sin(zero) * np.array(r["v_up"])
                sense = "clockwise" if geo["clockwise"] else "counter-clockwise"
                look = _axis_name(-np.array(r["w_toward_viewer"]))
                leg = _feature_radius(m["features"], org)
                tol = _angular_general_tol(leg, title)
                note = f"angular position about the origin axis; 0° along {_axis_name(zdir)}, {sense} looking along {look}"
                fl.append(note)
                doc.dimension(X.XCAFDimTolObjects_DimensionType_Location_Angular, math.radians(a["value"]),
                              org["faces"], _faces(feats),
                              tol=None if tol is None else tuple(math.radians(t) for t in tol),
                              name=label(m), flags=fl)
                done(m, "angular location from the origin axis", fl)
            else:
                cone = [f for f in feats if f["surface"]["type"] == "cone"]
                tol = _angular_general_tol(_cone_leg(cone[0]["surface"]) if cone else None, title)
                tol = tuple(math.radians(t) for t in a["tol"]) if a.get("tol") else \
                    (None if tol is None else tuple(math.radians(t) for t in tol))
                if cone:
                    doc.dimension(X.XCAFDimTolObjects_DimensionType_Size_Angular, math.radians(a["value"]),
                                  _faces(cone), tol=tol, name=label(m, " (included angle)"), flags=fl)
                    done(m, "angular size", fl)
                else:
                    legs = [f["faces"] for f in feats]
                    doc.dimension(X.XCAFDimTolObjects_DimensionType_Location_Angular, math.radians(a["value"]),
                                  legs[0], legs[1], tol=tol, name=label(m), flags=fl)
                    done(m, "angular location", fl)
        elif m["kind"] == "roughness":
            geo = a.get("geometry") or {}
            where = f"the {len(_faces(feats))} faces not marked on the views (title block, " \
                    f"except {geo.get('except')})" if geo.get("type") == "general_surface_texture" \
                else f"faces {_faces(feats)}"
            skip(m, f"surface texture {geo.get('parameter', 'Ra')} {a['value']} µm "
                    f"({geo.get('process', '')}) on {where}: OCCT cannot write surface texture to STEP")
            continue
        elif m["kind"] == "gdt":
            rows = (a.get("gdt") or {}).get("rows", [])
            ids = _faces(feats)
            written, missing = [], []
            for row in rows:
                t = GDT_TYPES.get(row["characteristic"])
                if t is None or row.get("tolerance") is None:
                    missing.append(f"unsupported row {row}")
                    continue
                _, miss = doc.tolerance(t, row["tolerance"], ids, diameter=row.get("zone") == "Ø",
                                        datums=row.get("datums") or [], name=label(m))
                written.append(row["characteristic"])
                missing += [f"datum {d} has no datum feature" for d in miss]
            if any(f["role"] == "derived" for f in feats):
                fl.append("applies to the derived feature (median); written on its faces")
            if "the side" in m["note"]:
                fl.append(m["note"].split(": ", 1)[1])
            done(m, ", ".join(written) or None, fl + missing)
        else:
            skip(m, f"no PMI mapping for {m['kind']}")
    return report


def _reason(m):
    if m["status"] == "skipped":
        return m["note"] or "skipped"
    deltas = [f["delta"] for f in m["alternatives"] if f.get("delta") is not None]
    why = f"no model feature at this value (closest candidate {min(deltas, key=abs):+.3f} mm off)" \
        if deltas else "no model geometry where the drawing points"
    return f"{why}; {m['note']}" if m["note"] else why


def summary(report):
    import collections
    out = collections.Counter((r["kind"], "written" if r["exported"] else "not written") for r in report)
    for k in sorted(out):
        print(f"  {k[0]:10} {k[1]:12} {out[k]}")
    flagged = [r for r in report if r["exported"] and r["flags"]]
    print(f"\n  {len(flagged)} written with flags, "
          f"{sum(1 for r in report if not r['exported'])} not written (see the report)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("step")
    ap.add_argument("--pdf", required=True, help="the drawing (for the title block)")
    ap.add_argument("--faces", default="faces.json")
    ap.add_argument("--annotations", default="annotations.json")
    ap.add_argument("--ordinates", default="ordinates.json")
    ap.add_argument("--registration", default="registration.json")
    ap.add_argument("-o", "--out", help="default: <model name>_pmi.stp")
    ap.add_argument("--report", default="pmi_report.json")
    args = ap.parse_args()

    from classify import classify
    from pdf_extractor import extract
    from views import segment
    pg = extract(args.pdf)
    title = segment(pg, classify(pg)).title_block

    def load(p):
        with open(p) as f:
            return json.load(f)
    matches, anns, ords = load(args.faces), load(args.annotations), load(args.ordinates)
    regs = {r["view"]: r for r in load(args.registration)}

    doc = PMIDocument(args.step)
    doc.check_faces([m for m in matches if m["status"] in EXPORT])
    report = export(doc, matches, anns, ords, regs, title)
    if not args.out:
        import os
        args.out = os.path.splitext(os.path.basename(args.step))[0] + "_pmi.stp"
    doc.write(args.out)
    with open(args.report, "w") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    summary(report)
    print("\nwrote", args.out, "and", args.report)
