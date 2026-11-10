# 1. Audit of the current code

Status: **draft for review.** Code state audited: commit `0fa912d` (the
commit the review used), unchanged since. Line numbers refer to that commit.

Each finding names the function and line it is based on. Where this audit
and the review disagree, the audit says so and gives the evidence.

## 1.1 The review's code claims, checked

| # | Review claim (section) | Verdict | Evidence |
|---|---|---|---|
| R1 | Only four GD&T characteristics are recognised (A2.1) | Accurate | `GDT` dict, `annotations.py:32`: position, perpendicularity, parallelism, flatness. |
| R2 | A frame row is read as symbol, optional Ø, one number, datum letters (A2.1) | Accurate, and slightly worse | `annotations.py:404-415`: each further number overwrites `tolerance`, so a row with two numbers keeps the last. Modifiers, zone symbols other than Ø, and compartment boundaries are not read. |
| R3 | A dot-containing number without tolerance becomes roughness (A2.2) | Accurate | `annotations.py:436-438`. A dimension printed `12.5` would be classified as surface texture. |
| R4 | `SERomans` filtering (A2.2) | Accurate | `annotations.py:383` drops every span in font `SERomans` (on this drawing, the engraved "GMB" text drawn on the part). |
| R5 | "Fixed font substitution" (A2.2) | Not found | No font substitution exists. The closest things are the `SERomans` filter (R4) and the fixed symbol map `textgroups.SYMBOLS` (R9). |
| R6 | Line-width role map and thresholds in page units (A2.2) | Accurate | `classify.WIDTH_ROLES` (`classify.py:43`) maps exact Solid Edge widths to roles. Nearly every threshold in every module is in PDF points (for example `ordinates.ON_LINE`, `annotations.TIP_ON_ARC`, `views.TOUCH`). |
| R7 | `leaders()` takes one stroke aligned with the arrow, `abs(dot) >= 0.995` (A2.3) | Accurate | `annotations.py:138-171`; test at `:154`. Returns `Leader(arrow, pts, far)`. One exception: `_leader_through` (`:848`), added for roughness symbols, follows one extra line (a shoulder) to the leader. |
| R8 | Greedy `used_arrows` commits too early (A2.4) | Accurate in substance | Within one kind, candidates are sorted best-first (`:467-485` linear, `:497-517` radius), so the choice is globally best-first for that kind. Across kinds a fixed pass order decides: linear, radius, located, GD&T, angles, roughness (`:464-590`). An arrow used by an earlier pass is never offered to a later one, and losing candidates are not kept. |
| R9 | `SYMBOLS` covers only the observed encoding (A1) | Accurate | `textgroups.py:17`: five characters of `SolidEdgeISO1Symbols`. Applied only when the font name contains "Symbols" (`:92`). |
| R10 | Printed and inferred values are not separated (A2.5) | Partly accurate | Linear tolerances are split: `tol` (printed) and `general_tol` (title block) are separate fields (`Annotation`, `:65-66`). But no record says which rule, table row or edition produced `general_tol`. Several inferences are stored as if printed: the roughness parameter `Ra` (`_roughness`, `:792`), the angle convention (`_angle_convention`, `:767`), view membership (`view_of`, `:388`), and `stack_values` setting a lone stacked deviation to ±value (`textgroups.py:84-86`). General tolerances for ordinates and angles are computed only at export (`pmi_writer._general_tol`, `_angular_general_tol`), outside the annotation model. |
| R11 | `faces.py` groups faces by surface parameters, not connectivity (A2.6) | Accurate | `faces._group`, `faces.py:155`: the key is the plane (normal, offset) or the cylinder (axis, centre, radius) only. Disconnected coplanar pads become one "feature", which contradicts Otto's logical surfaces (connected faces only). |
| R12 | Possible gap: Béziers extracted but maybe not classified (A1) | Worse than suspected | `pdf_extractor._segments` keeps Béziers (`:71-72`), but `classify.classify` drops them without a diagnostic: `classify.py:492-494`, comment "no beziers in the Solid Edge exports seen so far". Any exporter that writes arcs as Béziers loses every arc. |
| R13 | Registration exposes only the best transform (A1) | Partly accurate | `Registration.candidates` keeps the ranked hypotheses (`register.py:72`), but `to_records` exports only the best plus the runner-up's score (`:680-700`). |
| R14 | No automated tests or corpus (A2.7) | Accurate | No test files exist; one drawing. |
| R15 | Direct 3D matching inside the pipeline (A2.6, A3) | Accurate | `run_all.run` always runs registration, face matching and AP242 export; `make_pmi.py` exits without pythonocc. |
| R16 | 2D parsing requires STEP/OCCT (implied by Phase 6) | Not accurate | Only `register.py`, `faces.py` and `pmi_writer.py` import OCC. `pdf_extractor`, `classify`, `textgroups`, `views`, `ordinates` and `annotations` run without it (each has its own command line). Only the entry point requires OCC. |

## 1.2 Further findings not in the review

| # | Finding | Evidence | Consequence |
|---|---|---|---|
| F1 | Only the first page is read | `pdf_extractor.extract(pdf_path, page_no=0)`, `:85` | Multi-sheet drawings lose every later sheet silently. |
| F2 | Clipping paths and graphics-state structure are ignored | `page.get_drawings()` without `extended=True` (`:92`) | Clipped geometry (broken views, detail circles) is treated as fully visible. |
| F3 | Text is read per span, not per glyph | `get_text("dict")` (`:107`) | No per-glyph boxes or codes: a mis-decoded glyph inside a span cannot be located; text drawn as outlines is not detected at all. |
| F4 | Prefixes `M` and `SR` fall through to "linear" | `LABEL_RE` (`:34`) accepts them; the kind logic (`:430-440`) handles only `°`, `R`, `Ø` and the decimal point | A thread note `M6` or a spherical radius `SR 5` would be parsed as a linear dimension. |
| F5 | Any lone capital letter is a datum | `:416` | A note or label consisting of one letter becomes a datum. |
| F6 | `annotate()` mutates the ordinate list it is given | `dims.remove(weak.pop(...))`, `:488` | Hidden coupling: ordinates written before and after annotation differ. `run_all` writes them after (`run_all.py`) to stay consistent. |
| F7 | View membership is "nearest view rectangle within 200 pt" | `view_of`, `:388` | A label between two views can be assigned to the wrong one; no alternative is kept. |
| F8 | Two 2D facts are computed in 3D code | `faces._attached_side` (`faces.py:1225`) decides "frame leader in line with the dimension line" from page coordinates only. `faces._sharps` (`:575`) finds where thin construction lines meet, but from model edges rather than drawn strokes. | Both are drawing facts Otto needs from the parser (handoff §5). |
| F9 | `faces.py` duplicates Otto L2 derived geometry | axes (`_coaxial`), virtual sharps (`_sharps`, `_extensions`), median planes (`_material_between` in `pmi_writer.py:227`) | Otto's `BuildDerived` already builds axes, candidate medians and virtual sharps with evidence. |
| F10 | Title-block labels are this company's | `views.TITLE_LABELS`, `:51` (Italian/English labels) | Another title block yields no scale, tolerance class or projection. |
| F11 | Projection-method detection is geometric and unverified on third-angle sheets | `views._projection`, `:316` | Only first-angle has been observed. |
| F12 | The roughness symbol test accepts 50-70° legs, long leg ≥ 1.4× short | `ROUGH_ANGLE`, `annotations.py:789`, and `_roughness` | Shape thresholds are untested on other exporters. |

## 1.3 Module by module

Disposition key:
- **Keep:** the algorithm stays, with tests, evidence output and profile-driven parameters.
- **Refactor:** the idea stays, the structure changes.
- **Replace:** new design; the old code becomes a regression oracle.
- **Adapter:** moves out of the parser into an optional downstream component.

### `pdf_extractor.py` (158 lines): PDF ingestion

| Function | What it does | Disposition | Issues |
|---|---|---|---|
| `Segment`, `Path`, `TextSpan`, `PageData` | raw records | Refactor | Add document, page, source IDs and the transform chain (PDF space → rotated page → sheet). Keep `path_id`. |
| `_segments` | path items → line and Bézier segments (`l`, `c`, `re`, `qu`) | Keep | Raises on unknown operators instead of reporting a diagnostic. |
| `extract` | one page: drawings, text spans, rotation | Refactor | F1, F2, F3; the page rotation matrix is applied but not recorded. |
| `sample_bezier`, `summary` | helpers | Keep | |

### `classify.py` (525 lines): geometric reconstruction

| Function | What it does | Disposition | Issues |
|---|---|---|---|
| `_chain`, `_end_tangent` | join touching segments into strokes | Keep | `SNAP` in pt; per-width grouping means a stroke whose width changes is split. |
| `_link_dashes` | link dash segments into dashed strokes | Keep | `MAX_GAP`, `MAX_GAP_TURN`, `MAX_DASH` are Solid Edge calibrations. |
| `_line_dev`, `_fit_circle`, `_chord_dev`, `_smooth`, `_arc_from`, `fit_prims` | greedy line/arc fitting of a chord polyline | Keep | Tuned to a 0.12 pt export grid (`FIT_TOL`, `CHORD_TOL`). Output has no fit residual per primitive. |
| `linetype` | dash/dash-dot/continuous from gap ratios | Keep | Single label; no score or alternatives. |
| `stroke_role` | width + line type → role | Replace with profile lookup | R6. |
| `mark_hatching` | detect hatch line families | Keep | |
| `_polygon`, `_classify_filled` | arrowheads, dots, triangles from filled paths | Keep | Unfilled (open) arrowheads are not detected. |
| `classify` | orchestration | Refactor | R12 (Béziers dropped). |

### `textgroups.py` (183 lines): text grouping

| Function | What it does | Disposition | Issues |
|---|---|---|---|
| `SYMBOLS`, `symbol` | decode Solid Edge symbol font | Replace with profile font maps | R9. Raw glyph not kept beside the decoded one. |
| `Group` and its properties; `stack_values` | baseline groups; stacked deviations | Keep | A lone stacked value becomes ±value (R10). |
| `_up_vec`, `_up`, `_along`, `build_groups` | rotated-text geometry and grouping | Keep | Thresholds relative to text size (good); `STACK_RATIO` is a calibration. |

### `views.py` (516 lines): views and title block

| Function | What it does | Disposition | Issues |
|---|---|---|---|
| `find_frame`, `_clusters`, `_merge_nested`, `segment` | sheet frame, view regions from visible-stroke clusters | Keep | Rectangle regions only; broken or partial views are not modelled. |
| `_directions`, `_is_axonometric`, `_kind` | isometric and view-kind detection | Keep | |
| `find_title_block`, `_grid_lines`, `_cell_of`, `parse_title_block` | title block cells → fields | Refactor | F10. Fields should keep their source spans. |
| `_parse_scale`, `_parse_tol`, `_tolerance_table`, `ISO2768` | scale, general-tolerance class and table | Refactor | The table becomes a derivation with provenance (R10). |
| `_projection` | projection symbol → first/third angle | Keep | F11. |
| `_captions`, `CAPTION_RE`, `SCALE_RE` | view captions and scales | Keep | Single-letter captions only. |

### `ordinates.py` (638 lines): ordinate dimensions and view frames

| Function | What it does | Disposition | Issues |
|---|---|---|---|
| `find_origins` | "0" origin markers with dots | Keep | Solid Edge idiom. |
| `_axis_lines`, `_line_axis`, `_follow_jog`, `_gap_to_box`, `_in_frame`, `_has_prefix` | candidate extension lines, jogs, boxed values | Keep | Axis-aligned ordinates only. |
| `find_ordinates` | Hungarian assignment of values to lines | Refactor | Assignment is final; alternatives are not kept. |
| `_arrow_on` | reject lines that carry arrows | Keep | |
| `_anchors`, `_angle_in` | where an extension line meets the outline | Keep | Produces exactly the "endpoint candidates" the handoff asks for. |
| `_stacked_tol` | stacked deviations | Keep | |
| `fit_axes` | robust fit of origin and scale per view axis | Keep | The view coordinate frame the schema needs (`CoordinateFrame`). |
| `parse`, `summary`, `to_records` | orchestration and output | Refactor | F6. |

### `annotations.py` (942 lines): callout recognition and 2D association

| Function | What it does | Disposition | Issues |
|---|---|---|---|
| `LABEL_RE`, `GDT`, kind logic in `annotate` | lexical classification of labels | Replace with ISO grammar | R1, R2, R3, F4, F5. |
| `leaders` | arrow → one collinear stroke | Replace with leader graph | R7. Keep the arrow-axis test as one local score. |
| `_arrow_pairs`, `_match_linear` | opposed arrow pairs for linear and diameter dimensions | Keep as evidence generators | Greedy consumption (R8). |
| `_extension_anchor`, `_on_extension`, `_nearest_feature` | follow extension lines to the outline | Keep | |
| `_match_radius`, `_radius_rel_tol`, `_arc_dist` | radius leader to arc; size check | Keep as evidence generator | |
| `_reference` | parentheses drawn as arcs → auxiliary dimension | Keep, profile-driven | The arc idiom is Solid Edge's. |
| `_general`, `below_general_range`, `ISO2768_RADII` | ISO 2768-1 general tolerances | Refactor into a derivation step | R10. |
| `_general_texture` | title-block general roughness | Refactor | Must become a `GeneralSpecification` with a derivation. |
| `_angles`, `_leg_dir`, `_angle_convention`, `_apply_convention`, `_page_angle` | angular ordinates and angle dimensions | Keep as evidence generators | The fitted convention is an inference (R10). |
| `_roughness`, `_texture_process`, `_leader_through` | surface texture symbols | Refactor | R3, F12. Parameter `Ra` is assumed. |
| `view_of` (inside `annotate`) | view membership | Refactor | F7. |
| `annotate` | orchestration of all passes | Replace with association solver | R8, F6. |
| `summary`, `to_records` | output | Replace with schema serialisation | |

### `register.py` (766 lines): view-to-model registration

Adapter. It stays as an optional registration component that emits ranked
hypotheses (R13).

| Function | Disposition | Issues |
|---|---|---|
| `load_model`, `_sample_shape_edges`, `section_points` | Adapter | |
| `orientations`, `_view_points`, `_view_frame`, `_score`, `_search_translation`, `_overlaps`, `_fit_translation`, `register_view` | Adapter | 24 axis-aligned orientations only: an auxiliary view at an arbitrary angle cannot be registered. |
| `register_detail`, `register_section`, `_is_break_line`, `_fit_section`, `check_layout` | Adapter | Section viewing direction comes from cutting-plane arrows (a 2D fact the parser should emit). |
| `Registration`, `to_records`, `from_records`, `summary`, `draw_registration` | Adapter | Export all `candidates`, not only the best. |

### `faces.py` (1335 lines): 3D face matching

Adapter, used as a research and compatibility validator. It is never part of
the parser's output.

| Functions | Disposition | Issues |
|---|---|---|
| `Surface`, `_surface`, `_triangles`, `build`, `_group` | Adapter | R11. |
| `project`, `_clip`, `_vertices`, `_extensions`, `_end_on`, `_curve_ends`, `near`, `_tri_dist`, `_axis_dist` | Adapter | Projection by tessellation, not exact raycast. |
| `_sharps`, `_intersect`, `_curve_near`, `_on_faces` | Adapter | F9. Their 2D counterpart (construction-line intersections) moves to the parser (F8). |
| `match_ordinate`, `match_radius`, `match_linear`, `match_surface`, `match_angle*`, `match_origins`, `_general_texture`, `match_all` | Adapter | |
| `_attached_side` | **Move to the parser** | F8: it uses page coordinates only. |
| `_decide`, `_best`, `_cand`, `_bucket`, `PRIORITY` | Adapter | Status logic ("ambiguous only if a same-kind candidate also fits") is a useful pattern for the 2D association too. |

### `pmi_writer.py` (531 lines): AP242 export

Adapter. Its future input is resolved Otto bindings, not `faces.json`. The
three OCCT workarounds in its docstring stay documented:
- the unwrapped `XCAFDoc_GeomTolerance`;
- the negated lower bound;
- the grade written off by two.

### `run_all.py`, `make_pmi.py`, `debug_overlay.py`

- **`run_all.py` and `make_pmi.py`:** they stay as the legacy end-to-end path,
  which is the regression oracle during migration. A new entry point runs the
  2D parser alone, without OCC.
- **`debug_overlay.py`:** keep. It extends to draw the new schema objects
  (chains, terminals, association candidates).

## 1.4 What the audit implies

1. **Reusable without change of idea:** almost all of the 2D geometry (chaining, fitting, dash linking, arrowheads, hatching, text grouping, view segmentation, ordinate assignment, extension anchors). It needs evidence output, profiles and tests, not rewrites.
2. **To be replaced:** the parts that decide meaning:
   - the lexical classification of labels;
   - the single-stroke leader model;
   - the fixed-order greedy association.
3. **The 3D half becomes an optional downstream component:** `register.py`, `faces.py` and `pmi_writer.py`, about 2,600 lines. Two of its 2D facts move into the parser (F8).
