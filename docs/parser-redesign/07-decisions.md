# 7. Decisions needed from the Otto owners

Status: **open.** Each decision lists the options and a recommendation. Nothing
here has been chosen; the recommendations are proposals.

| # | Decision | Options | Recommendation | Blocks |
|---|---|---|---|---|
| D1 | **Standards editions, and who verifies against licensed text** | (a) pin current editions (ISO 1101:2017, ISO 14405-1:2025, ISO 21920-1:2021, ISO 22081:2021, …); (b) also support editions cited by older supplier drawings | (a) for release 1, with cited legacy conventions (ISO 1302 value-only texture, ISO 2768-2) **parsed and labelled as legacy**, not interpreted. Someone with the licensed texts verifies every matrix row before its ticket closes. | T6a–c acceptance; matrix "Verified" column |
| D2 | **Supported PDF modalities and exporters** | (a) native vector + native text only; (b) + text as outlines; (c) + raster via a separate OCR path | (a) for release 1, with diagnostics for everything else (03-modality-matrix). Exporters: Solid Edge first, plus the two or three that Otto's suppliers use most. Inch drawings: decide whether in scope. | T11, T12 |
| D3 | **Serialization, ABI, ID and version scheme** | JSON + JSON Schema / protobuf / FlatBuffers; ids `<kind>:<n>` vs UUIDs vs content hashes | JSON + JSON Schema (human-inspectable; a C++ consumer can generate types or use a validator). Ids `<kind>:<n>`, deterministic per file and parser version; no persistence across revisions in release 1. Schema version semver, `0.x` until frozen. | T5 |
| D4 | **Who owns view registration** | (a) Mihnea ships `register.py` as an optional adapter; (b) Otto reimplements on its side | (a) during development: it exists and works on T00302932. It is changed to export all ranked hypotheses with residuals. Ownership after release 1: owners decide. | T10 |
| D5 | **What the parser resolves in 2D vs leaves to Otto** | e.g. size vs distance; integral vs derived target; which feature a leader means; virtual sharps | Parser: syntax, 2D topology, associations, **attachment facts** (in line with a dimension line, on an extension line, construction intersection). Otto: semantic targets, size vs distance when the syntax does not say, 3D sharps and medians. Recorded in the schema as `LinearDimension.size_or_distance: undetermined`. | T5, T8 |
| D6 | **Corpus and labelling** | who supplies drawings (with their STEP files), how many per exporter, who labels | Otto supplies 10–20 real drawings across 3+ exporters; two people label each; disagreements reviewed. Without this, acceptance criteria 2 and 8 cannot be met. | T2 scale-out, T12 |
| D7 | **Fate of the AP242 path** | keep `make_pmi.py` as a regression oracle / retire it | Keep as an oracle until Otto's binder produces bindings; then rebuild AP242 export on bindings. | T10 |
| D8 | **L4 as the first binding target** (review G5, handoff §9.4) | — | Not the parser's to decide. The parser's output is independent of the answer (no 3D content). Listed so that it is not assumed. | Otto binding |
| D9 | **Normative source for Otto's callout envelopes** (review G7) | — | Same verification process as D1, so the parser's matrix and Otto's envelopes cite the same clauses. | Otto, and the parser's attachment-fact rules |
| D10 | **Doubt margin and score calibration** | fixed margins now / calibrated on the corpus | Named thresholds now (`DOUBT_MARGIN`, `ROUTE_MARGIN`); values calibrated on T2 + T12 data and approved by the owners. | T9 |
| D11 | **Raw evidence in the payload** | always / optional / by reference to a sidecar | Always in release 1 (the review asks never to drop source evidence); revisit if payload size becomes a problem. | T5 |

## Questions back to the reviewers

1. **Review A1 mentions "fixed font substitution"** in the code. I could not
   find any font substitution. Is the `SERomans` filter or the symbol map what
   was meant (audit R5)?
2. **Review A2.5 says printed and inferred values are not separated.**
   Linear tolerances already are (`tol` vs `general_tol`). What is missing is
   their provenance, and several other inferences. Is the audit's reading
   (R10) the intended one?
3. **Are there drawings in Otto's corpus** that already have native AP242 PMI
   in their STEP files? They would give independent ground truth for
   association and binding (review B1: "importing native PMI as a comparison
   fixture").
