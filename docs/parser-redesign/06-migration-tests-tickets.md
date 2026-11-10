# 6. Migration plan, test plan and tickets

Status: **draft for review.**

## 6.1 Principles

- **Behaviour is frozen before it changes.** Ticket T1 snapshots today's
  outputs as a *baseline*: a regression oracle, not ground truth.
- **The legacy path stays runnable.** `make_pmi.py` and the 3D adapters stay
  runnable until the new parser plus Otto replace them. They are a comparison
  oracle.
- **The public schema is reviewed before the parser emits it** (T5 waits for
  approval).
- **Every ticket ships with its tests.** None claims ISO coverage beyond what
  its fixtures show.

## 6.2 Target module layout

Proposed, not frozen. Each new module names the current code it starts from.

| New module | Starts from | Notes |
|---|---|---|
| `pdf/raw.py` | `pdf_extractor.py` | all pages; Béziers; clips; transform chain; per-glyph text (`rawdict`); source ids |
| `profiles/` (data) + `profiles/select.py` | constants in `classify`, `textgroups`, `views`, `annotations` | Solid Edge profile first, plus a generic profile |
| `geometry/reconstruct.py` | `classify._chain`, `_link_dashes`, `fit_prims`, `mark_hatching` | Béziers handled; residuals and candidates out |
| `geometry/symbols.py` | `classify._classify_filled`, `annotations._roughness` symbol part | open arrowheads, dots, triangles, frame boxes, texture symbols |
| `text/decode.py` | `textgroups.SYMBOLS`, `symbol` | profile font maps; raw and decoded glyph kept |
| `text/groups.py` | `textgroups.build_groups`, `Group` | |
| `layout/views.py` | `views.py` | frames, views, captions, title block with sources, projection |
| `layout/ordinates.py` | `ordinates.py` | view frames (`CoordinateFrame`) from ordinate fits |
| `iso/lex.py` | the `LABEL_RE` part of `annotate` | tokens with kinds |
| `iso/dimensions.py`, `iso/gdt.py`, `iso/datums.py`, `iso/texture.py`, `iso/general.py` | the kind logic of `annotate`, `_general`, `_general_texture` | grammars per family, with `unknown` |
| `drawing/leader_graph.py` | `annotations.leaders` | section 5.1 |
| `drawing/associate.py` | the matching passes in `annotate` | section 5.2 |
| `drawing/facts.py` | `faces._attached_side`; the construction-line logic of `faces._sharps` | 2D only |
| `schema/model.py`, `schema/serialize.py`, `schema/validate.py` | `docs/parser-redesign/schema` | |
| `adapters/registration.py` | `register.py` | exports ranked hypotheses |
| `adapters/legacy_faces.py` | `faces.py` | comparison oracle |
| `adapters/step_ap242.py` | `pmi_writer.py` | consumes resolved bindings later |
| `cli/parse.py` | — | PDF → drawing-model JSON, **no OCC** |

## 6.3 Test plan and corpus design

### Layers

| Layer | What | Pass criterion |
|---|---|---|
| L1 unit | each algorithm on small synthetic inputs: chaining, dash linking, line/arc fitting (lines, chords, Béziers), arrowheads, text grouping, stacked tolerances, each grammar, graph construction, route enumeration, the solver | exact expected outputs |
| L2 contract | schema validation and invariants on every produced payload; round-trip (serialise → load → serialise is byte-identical) | `validate.py` clean; identical bytes |
| L3 regression | T00302932 today vs after each ticket: callouts found, values, associations | no unexplained differences; each difference reviewed and recorded |
| L4 gold | **hand-labelled** T00302932, independent of the current outputs: every callout (type, literal, region), every terminal location, every association | precision and recall per construct, reported, not thresholded yet |
| L5 synthetic | generated PDFs exercising each matrix row (positive / negative / ambiguous) | expected typed output, ambiguities flagged |
| L6 exporter variants | the same content through different routes | same drawing model up to declared differences |
| L7 corpus | real drawings from other exporters (decision D6) | per-construct metrics, as section 6.4 |

### Gold labels

One JSON file per drawing:
- callout id, type and literal;
- region on the sheet;
- each terminal location (sheet mm) and contact kind;
- each association pair;
- every reading where the drawing itself is ambiguous.

They are made with a labelling overlay built from `debug_overlay.py`, which
draws candidates for a human to accept, correct or add. Two people label
independently; disagreements are reviewed (decision D6).

### Synthetic generator

PDFs drawn with PyMuPDF, so the ground truth is known by construction. One
generator per matrix row, with parameters for orientation, text placement,
crowding and near-misses. **Negative cases:**
- a `12.5` that is a dimension (audit R3);
- a lone letter that is not a datum (F5);
- `M6`, which is not linear (F4);
- arrows that belong to a neighbouring dimension;
- leaders crossing outlines.

### Exporter transformations

Applied to the same content:
- arcs as Béziers vs chord runs vs native arcs;
- dashes as dash arrays vs separate segments;
- text as fonts vs outlines (Ghostscript `-dNoOutputFonts`, available here);
- page rotation;
- nested transforms;
- clipping;
- coordinate quantisation.

### Metrics

Reported per construct and per modality, never only in aggregate:
- callout detection precision and recall;
- typing accuracy;
- token-level literal accuracy;
- terminal location error (mm);
- association precision and recall;
- **ambiguity recall:** truly ambiguous cases reported as ambiguous;
- **false-confident rate:** a wrong association reported as `resolved`. This
  is the critical one;
- diagnostic coverage of unsupported constructs;
- runtime;
- determinism (byte-identical repeat runs).

**Thresholds are set after the corpus exists** (review F).

## 6.4 Tickets

Each ticket is bounded, ships with tests, and lists its acceptance criteria.

| # | Ticket | Depends on | Acceptance |
|---|---|---|---|
| **T0** | Pre-coding deliverables: this folder | — | reviewed by Otto owners; decisions recorded |
| **T1** | **Baseline snapshots and unit tests** of current algorithms (chain, dash link, fit, arrowheads, text groups, stacked tolerances, ordinate assignment, `_general`) | — | `pytest` green; T00302932 outputs snapshotted; current known mismatches listed |
| **T2** | **Gold labels for T00302932** and the labelling overlay | — | every callout labelled by two people; disagreements resolved; current pipeline scored against it (first honest numbers) |
| **T3** | **Ingestion:** all pages, Béziers through to classification, clipping, transform chain, per-glyph text, source ids; diagnostics for images, outline text and unknown operators | T1 | synthetic Bézier-arc, multi-page, clipped and rotated fixtures pass; T00302932 regression unchanged |
| **T4** | **Export profiles:** move every Solid Edge constant to a Solid Edge profile; generic profile; profile selection with evidence | T3 | T00302932 unchanged under the Solid Edge profile; generic profile runs and reports the profile it chose |
| **T5** | **Schema in code:** data classes, serialiser, validator in CI; adapter from current records (section 4.6) | schema approved (D3) | adapter output for T00302932 validates; round-trip byte-identical |
| **T6a** | **Grammar: dimensions** (prefixes R/SR/CR/Ø/SØ/□, count, deviations, limits, fits, auxiliary, TED, ISO 14405-1 modifiers as tokens) | T4, T5 | matrix §2.2 rows in scope 1 have positive/negative fixtures; F4 fixed; R3 fixed (no kind from the decimal separator) |
| **T6b** | **Grammar: GD&T frames and datums** (14 characteristics + unknown, compartments from boxes, zone shapes and modifiers, ordered and common datum references) | T4, T5 | matrix §2.3 and §2.4 rows in scope 1; R1, R2, F5 fixed; row order follows the drawing |
| **T6c** | **Grammar: surface texture and general specifications** (printed vs derived parameter, variants, title-block general requirements as `GeneralSpecification` with derivations) | T4, T5 | matrix §2.5 and §2.7 rows in scope 1; R10 fixed (derivations recorded) |
| **T7** | **Leader graph** (§5.1): nodes, edges, terminals (open arrows and dots too), routes, roles from structure | T3, T4 | synthetic elbow, shelf, branch, crossing and junction fixtures; T00302932 leaders found ⊇ current |
| **T8** | **Attachment facts** (§5.1): in line with a dimension line, on an extension line, construction-line intersections, dots | T7 | facts match the gold set; `faces._attached_side` results reproduced in 2D |
| **T9** | **Association solver** (§5.2) replacing `used_arrows` | T6a–c, T7, T8 | gold-set association recall ≥ the current pipeline's; every synthetic ambiguous fixture reported `ambiguous`; zero false-confident results on the fixtures |
| **T10** | **2D parser CLI without OCC**; the legacy path moved behind `adapters/`; registration exports all hypotheses | T5, T9 | `parse.py drawing.pdf` runs in an environment without pythonocc; `make_pmi.py` still reproduces today's AP242 output from the adapters |
| **T11** | **Exporter-variant fixtures** (§6.3) and the modality diagnostics | T3, T4 | each transformation of the synthetic set either parses equally or yields the declared diagnostic |
| **T12** | **Corpus validation** on drawings from other exporters | D2, D6, T9 | per-construct metrics report; thresholds proposed for owner approval |

**Order:**
- T1 and T2 can start now: they change no behaviour.
- T3 and T4 follow.
- T5 waits for schema approval.
- T6a–c and T7 can run in parallel.
- T8 needs T7.
- T9 needs T6a–c, T7 and T8.
- T10 needs T5 and T9.
- T11 needs T3 and T4.
- T12 needs drawings from Otto.
