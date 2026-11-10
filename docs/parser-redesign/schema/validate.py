"""Validate drawing-model payloads: JSON Schema, then the cross-object invariants.

    python validate.py examples/*.json

Needs the `jsonschema` package (Draft 2020-12). Exit status 1 if any file fails.

Invariants checked beyond the schema (numbered as in 04-schema.md):
  I1  ids are unique across the whole document (nested nodes, edges, terminals, routes too)
  I2  every id's kind prefix matches the collection it lives in
  I3  every reference resolves, to an object of an allowed kind
  I4  every annotation keeps printed source evidence (text runs or symbols)
  I5  units fit the quantity: lengths mm/inch, angles deg/rad, texture um
  I6  a transform marked invertible has a non-zero determinant
  I7  ambiguity is never serialised as a confident match
  I8  printed and derived tolerances are consistent with `effective`
  I9  no 3D / B-rep identifiers anywhere in the parser's own output
  I10 a texture parameter is either printed or derived, never both
  I11 chain routes, edges and terminals only use their own chain's nodes
"""
import json
from pathlib import Path
import sys

import jsonschema

SCHEMA = Path(__file__).with_name("drawing-model.schema.json")

# collection -> id kind
COLLECTIONS = {
    "sheets": "sheet", "views": "view", "primitives": "prim", "strokes": "stroke", "text_runs": "text",
    "tokens": "tok", "symbols": "sym", "chains": "chain", "annotations": "ann", "associations": "assoc",
    "association_groups": "group", "attachment_facts": "fact", "diagnostics": "diag",
}
NESTED = {"nodes": "node", "edges": "edge", "terminals": "term", "routes": "route"}

ANY = None
# reference field -> allowed kinds (None: any kind)
REFS = {
    "sheet_id": {"sheet"}, "view_id": {"view"}, "annotation_id": {"ann"}, "target_annotation_id": {"ann"},
    "origin_id": {"ann"}, "origin_ids": {"ann"}, "token_ids": {"tok"}, "text_run_ids": {"text"},
    "symbol_ids": {"sym"}, "box_symbol_ids": {"sym"}, "symbol_id": {"sym"}, "chain_ids": {"chain"},
    "route_ids": {"route"}, "terminal_ids": {"term"}, "node_id": {"node"}, "node_ids": {"node"},
    "from": {"node"}, "to": {"node"}, "stroke_ids": {"stroke"}, "candidate_ids": {"assoc"},
    "selected_ids": {"assoc"}, "attachment_fact_ids": {"fact"}, "diagnostic_ids": {"diag"},
    "clip_ids": {"prim"}, "clip_boundary_ids": {"stroke", "chain"}, "label_source_ids": {"text"},
    "value_source_ids": {"text"}, "subject_id": {"term", "node", "sym"},
    "source_ids": ANY, "inputs": ANY, "object_ids": ANY, "evidence_ids": ANY,
}
LENGTH_TYPES = {"SizeDimension", "LinearDimension", "DistanceDimension", "RadiusDimension"}
FORBIDDEN_KEY_PARTS = ("face", "brep", "occt", "step_entity", "topods")


def walk(obj, path=""):
    """Yield (path, key, value) for every key in nested dicts/lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


def kind_of(id_):
    return id_.split(":", 1)[0]


def check(doc):
    errors = []
    err = errors.append

    # I1, I2: collect ids
    ids = {}
    for coll, kind in COLLECTIONS.items():
        for i, item in enumerate(doc.get(coll, [])):
            iid = item.get("id")
            if kind_of(iid) != kind:
                err(f"I2 {coll}[{i}] id {iid!r} should have kind '{kind}'")
            if iid in ids:
                err(f"I1 duplicate id {iid!r}")
            ids[iid] = item
            if coll == "chains":
                for sub, skind in NESTED.items():
                    for j, s in enumerate(item.get(sub, [])):
                        sid = s.get("id")
                        if kind_of(sid) != skind:
                            err(f"I2 {iid}.{sub}[{j}] id {sid!r} should have kind '{skind}'")
                        if sid in ids:
                            err(f"I1 duplicate id {sid!r}")
                        ids[sid] = s

    # I3: references (the `id` key itself is a definition, not a reference)
    for path, key, value in walk(doc):
        if key not in REFS or path.startswith(".extensions"):
            continue
        if key in ("from", "to") and ".edges[" not in path:
            continue    # a transform's frame names, not node references
        allowed = REFS[key]
        refs = value if isinstance(value, list) else [value]
        for r in refs:
            if r is None:
                continue
            if r not in ids:
                err(f"I3 {path}.{key}: {r!r} does not resolve")
            elif allowed is not ANY and kind_of(r) not in allowed:
                err(f"I3 {path}.{key}: {r!r} is a '{kind_of(r)}', expected {sorted(allowed)}")

    for a in doc.get("annotations", []):
        aid, t = a["id"], a["type"]
        # I4
        p = a["printed"]
        if not (p["text_run_ids"] or p["symbol_ids"]):
            err(f"I4 {aid}: no printed source (text runs or symbols)")
        # I5
        unit = (a.get("nominal") or {}).get("unit")
        if t in LENGTH_TYPES and unit not in ("mm", "inch"):
            err(f"I5 {aid}: {t} nominal unit {unit!r} is not a length")
        if t == "AngularDimension" and unit not in ("deg", "rad"):
            err(f"I5 {aid}: angle nominal unit {unit!r}")
        if t == "OrdinateDimension":
            want = ("deg", "rad") if a["coordinate"] == "polar_angle" else ("mm", "inch")
            if unit not in want:
                err(f"I5 {aid}: ordinate ({a['coordinate']}) unit {unit!r}")
        for req in a.get("requirements", []):
            if req["value"]["unit"] != "um":
                err(f"I5 {aid}: texture value unit {req['value']['unit']!r}, expected 'um'")
            # I10
            par = req["parameter"]
            if par["printed"] is not None and par.get("derived") is not None:
                err(f"I10 {aid}: texture parameter both printed and derived")
        # I8
        tol = a.get("tolerance")
        if tol:
            eff, printed, derived = tol["effective"], tol["printed"], tol["derived"]
            if eff == "printed" and printed["form"] == "none":
                err(f"I8 {aid}: effective 'printed' but nothing printed")
            if eff == "derived" and not derived:
                err(f"I8 {aid}: effective 'derived' but no derivation")
            if eff == "derived" and printed["form"] != "none":
                err(f"I8 {aid}: a printed tolerance exists but the derived one is effective")
            if a.get("status") in ("basic", "auxiliary") and eff != "not_applicable":
                err(f"I8 {aid}: {a['status']} dimension must have effective 'not_applicable'")

    # I6
    def det_ok(aff, where):
        a, b, c, d = aff["matrix"][:4]
        if aff["invertible"] and abs(a * d - b * c) < 1e-12:
            err(f"I6 {where}: marked invertible but determinant is 0")
    for s in doc.get("sheets", []):
        det_ok(s["pdf_to_sheet"], s["id"])
    for v in doc.get("views", []):
        if v.get("frame"):
            det_ok(v["frame"]["view_to_sheet"], v["id"])

    # I7: ambiguity
    assoc = {c["id"]: c for c in doc.get("associations", [])}
    by_rel = {}
    for c in assoc.values():
        by_rel.setdefault((c["annotation_id"], c["relation"]), set()).add(c["status"])
    for (aid, rel), sts in by_rel.items():
        if "resolved" in sts and "ambiguous" in sts:
            err(f"I7 {aid} {rel}: both resolved and ambiguous candidates")
    for g in doc.get("association_groups", []):
        sel = g.get("selected_ids", [])
        sts = [assoc[c]["status"] for c in g["candidate_ids"] if c in assoc]
        if g["status"] == "resolved":
            if g["rule"] in ("exactly_one", "at_least_one") and not sel:
                err(f"I7 {g['id']}: resolved but nothing selected")
            for c in sel:
                if assoc.get(c, {}).get("status") != "resolved":
                    err(f"I7 {g['id']}: selected {c} is not 'resolved'")
            for c in set(g["candidate_ids"]) - set(sel):
                if assoc.get(c, {}).get("status") not in ("rejected", "invalid"):
                    err(f"I7 {g['id']}: unselected {c} must be 'rejected' or 'invalid'")
        if g["status"] == "ambiguous":
            if sel:
                err(f"I7 {g['id']}: ambiguous group must not select")
            if sts.count("ambiguous") < 2:
                err(f"I7 {g['id']}: ambiguous group needs at least two ambiguous candidates")
    for c in assoc.values():
        if c["status"] == "ambiguous" and c["ambiguity_origin"] == "none":
            err(f"I7 {c['id']}: ambiguous candidate needs an ambiguity_origin")
        if c["status"] == "resolved" and c["ambiguity_origin"] != "none":
            err(f"I7 {c['id']}: resolved candidate with ambiguity_origin {c['ambiguity_origin']!r}")

    # I9: no 3D identifiers in the parser's own output
    for path, key, _ in walk({k: v for k, v in doc.items() if k != "extensions"}):
        if any(part in key.lower() for part in FORBIDDEN_KEY_PARTS):
            err(f"I9 {path}.{key}: 3D/B-rep field in the parser output")

    # I11: chain-local topology
    for ch in doc.get("chains", []):
        nodes = {n["id"] for n in ch["nodes"]}
        for e in ch["edges"]:
            if e["from"] not in nodes or e["to"] not in nodes:
                err(f"I11 {ch['id']}: edge {e['id']} uses a node of another chain")
        for t in ch["terminals"]:
            if t["node_id"] not in nodes:
                err(f"I11 {ch['id']}: terminal {t['id']} on a node of another chain")
        for r in ch["routes"]:
            if not set(r["node_ids"]) <= nodes:
                err(f"I11 {ch['id']}: route {r['id']} leaves the chain")
    return errors


def main(paths):
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    failed = False
    for p in paths:
        doc = json.loads(Path(p).read_text())
        problems = [f"schema {'/'.join(map(str, e.absolute_path))}: {e.message}"
                    for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))]
        if not problems:
            problems = check(doc)
        status = "ok" if not problems else f"{len(problems)} problem(s)"
        print(f"{p}: {status}")
        for msg in problems[:50]:
            print("   ", msg)
        failed |= bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
