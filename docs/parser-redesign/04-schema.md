# 4. Drawing model schema (draft)

Status: **draft 0.1 for review. No name is frozen.**

Files:
- [`schema/drawing-model.schema.json`](schema/drawing-model.schema.json): JSON Schema, draft 2020-12.
- [`schema/validate.py`](schema/validate.py): schema check plus the cross-object invariants below.
- [`schema/examples/`](schema/examples): three payloads.

```bash
python docs/parser-redesign/schema/validate.py docs/parser-redesign/schema/examples/*.json
```

## 4.1 What the schema is

It is the parser's whole output: a typed 2D model of one drawing PDF. It is
designed for a C++ consumer, and is language-neutral (JSON Schema). The
format itself is still decision D3.

**It does not contain:**
- **3D geometry or B-rep identifiers.** There are no face, edge or OCCT ids
  anywhere; invariant I9 enforces this.
- **Bindings to Otto's PartModel.** Binding is Otto's.
- **Registration.** Optional adapters may add hypotheses under `extensions`,
  which consumers must not require.

**It does contain:**
- **Raw evidence:** PDF primitives, text runs and glyphs.
- **Reconstructed geometry, as candidates:** strokes with fitted lines and
  arcs, line-type and role candidates, symbols.
- **Leader and dimension topology:** chains of nodes, edges, terminals and
  routes.
- **Typed callouts,** with their printed form and their abstract syntax.
- **2D association candidates and the constraints over them,** with their
  status.
- **2D attachment facts:** where lines and terminals end. Otto needs these to
  choose between integral and derived targets.
- **Diagnostics.**

## 4.2 Objects

| Collection | Object | Holds |
|---|---|---|
| `source` | Source | file hash, page count, PDF producer, **exporter profile and why it was chosen**, parser version |
| `sheets` | Sheet | page index, size, **PDF→sheet transform**, title block fields (with source spans), projection method, sheet scale |
| `views` | DrawingView | kind, label, region, scale (printed / fitted / sheet), **view→sheet frame** (origin from ordinates), relations (section of, detail of) with evidence |
| `primitives` | SourcePrimitive | raw PDF paths (lines **and Béziers**), images; never modified |
| `strokes` | Stroke | chained primitives, fitted lines/arcs with residuals, line-type candidates, role candidates |
| `text_runs` | TextRun | raw text, font, geometry, **per-glyph raw code and decoded meaning with the decode rule** |
| `tokens` | Token | lexical tokens (number, symbol, modifier…) with their runs; stacked position |
| `symbols` | SymbolCandidate | arrowheads (filled/open), dots, datum triangles, frame boxes, texture symbols, origin marks |
| `chains` | GeometricChain | connected lines: nodes (end, bend, junction, contact), edges, **terminals**, role candidates, **routes** (one per branch or per ambiguous reconstruction) |
| `annotations` | Annotation | the discriminated union below |
| `associations` | AssociationCandidate | one possible 2D relation (annotation ↔ chains, terminals, another annotation), with rules, evidence, score, **status**, reason, **ambiguity origin** |
| `association_groups` | AssociationGroup | constraints over candidates ("exactly one leader for this label", "this leader serves at most one label"), with the selection and margin |
| `attachment_facts` | AttachmentFact | 2D facts such as terminal on an outline / on an extension line / **in line with a dimension line** / at a construction-line intersection |
| `diagnostics` | Diagnostic | coded findings with source ids |

**Annotation types** (`type`):
- **Dimensions:** `SizeDimension`, `LinearDimension`, `DistanceDimension`,
  `RadiusDimension`, `AngularDimension`, `OrdinateDimension`, `OrdinateOrigin`.
- **GD&T and datums:** `GeometricalTolerance`, `DatumFeatureIndication`,
  `DatumTargetIndication`.
- **Texture, edges and general specifications:** `SurfaceTextureSpecification`,
  `EdgeSpecification`, `GeneralSpecification`.
- **Notes:** `ThreadNote`, `FreeTextNote`, `UnknownAnnotation`.

**Every annotation has:**
- **Printed form:** literal, tokens, runs and symbols.
- **Syntax state:** `valid`, `partial`, `invalid` or `unparsed`, with the grammar rule and errors.
- **View candidates:** with scores. View membership is a hypothesis, not a fact.
- **Region.**

## 4.3 Design choices worth reviewing

1. **Size vs distance is not decided from a bare value.** `SizeDimension` is
   used only when the syntax establishes a size: Ø, SØ, □, a fit code, or an
   ISO 14405-1 size modifier. A plain linear value is a `LinearDimension` with
   `size_or_distance: "undetermined"`; whether two features are opposed is 3D
   knowledge, so Otto decides. `DistanceDimension` is for syntax that
   establishes a distance. (Decision D5.)
2. **Printed and derived values are separate fields.** `ToleranceSpec.printed`
   holds only what is on the drawing, as literals. `ToleranceSpec.derived`
   holds values from general specifications, each with a `Derivation` (rule,
   standard, edition, inputs). `effective` says which governs. The same split
   applies to a texture parameter (`printed: null` plus a derived `Ra`) and to
   the angular-ordinate convention.
3. **Quantities keep their literal.** Every number carries `literal`, unit,
   decimal separator and decimals, so `0,012` and `0.012` round-trip and
   precision is not lost.
4. **Frames are syntax trees.** Rows → compartments (with boxes) → zone
   (shape, value, modifiers before or after the value) and ordered datum
   references with modifiers and common-datum grouping. The characteristic
   has an `unknown` value that keeps the raw glyph.
5. **Leaders are graphs, not `{arrow, pts, far}`.** A chain has nodes and
   edges, several terminals, and several routes. Branches give several routes;
   an ambiguous junction gives competing routes with status `ambiguous`.
6. **Association is candidates plus constraints.** The solver's result is
   recorded as group selections. Losing candidates stay, as `rejected` with a
   reason. A thin margin produces an `ambiguous` group and no selection.
   `ambiguity_origin` separates three causes:
   - `drawing_permits`: two readings are valid;
   - `pdf_unreadable`: the PDF is degraded;
   - `parser_limit`: the parser can't decide.
7. **2D attachment facts are explicit.** For example, "the frame's leader ends
   on an extension line of Ø5,5 H7, 10.7 mm from its dimension line". The
   parser reports the fact; reading it as "integral feature" is Otto's, with
   the ISO rule cited in the evidence.
8. **IDs are `<kind>:<n>`.** They are deterministic for the same file and
   parser version, not persistent across drawing revisions (decision D3).
   Every cross-reference uses ids, never array positions.
9. **Units:** sheet coordinates are millimetres on paper. View coordinates are
   model-size millimetres, through `DrawingView.frame`. Lengths are `mm`,
   angles `deg`, texture `um`.

## 4.4 Invariants

Checked by `validate.py` after the schema:

| # | Invariant |
|---|---|
| I1 | Ids are unique across the document, including chain nodes, edges, terminals and routes. |
| I2 | An id's kind prefix matches its collection. |
| I3 | Every reference resolves, to an object of an allowed kind. |
| I4 | Every annotation keeps printed source evidence (text runs or symbols), including unknown ones. |
| I5 | Units fit the quantity: lengths `mm`/`inch`, angles `deg`/`rad`, texture `um`. |
| I6 | A transform marked invertible has a non-zero determinant. |
| I7 | **Ambiguity is never serialised as a confident match.** <br>• No annotation has both a `resolved` and an `ambiguous` candidate for the same relation. <br>• A resolved group's selection is all `resolved`, and the rest `rejected` or `invalid`. <br>• An ambiguous group selects nothing and has two or more `ambiguous` candidates. <br>• An ambiguous candidate names its origin. |
| I8 | `effective` agrees with the content: <br>• `printed` needs a printed form; <br>• `derived` needs a derivation, and nothing printed; <br>• basic and auxiliary dimensions are `not_applicable`. |
| I9 | No field name in the parser output refers to faces, B-rep, OCCT or STEP entities (outside `extensions`). |
| I10 | A texture parameter is printed or derived, never both. |
| I11 | A chain's edges, terminals and routes use only that chain's nodes. |

Checked by the schema itself:
- **Closed objects:** `additionalProperties: false` and `unevaluatedProperties: false` everywhere in the core, so unknown fields are rejected.
- **Annotation union:** exactly one variant must match, keyed by `type`.
- **Enumerations:** every enumerated value carries an `unknown` member where the input can be unknown.

The validator was tested by mutating the clear example in ten ways: an
ambiguous member in a resolved group, a `face_id` field, a missing derivation,
a dangling reference, a reference of the wrong kind, a wrong unit, a missing
source, a selection in an ambiguous group, a duplicate id, and a texture
parameter both printed and derived. Each one is rejected by its check.

## 4.5 The examples

| File | Content |
|---|---|
| [`clear.json`](schema/examples/clear.json) | **Real callouts from T00302932** (view D-D, datum A in view 9). <br>• `Ø 5,5 H7 +0,012/0` as a `SizeDimension` with a printed fit and deviations. <br>• Its dimension line and extension lines, with the extension origins on the outline. <br>• The two-row frame ⌖ Ø0.01 / ⊥ 0.01 A as a syntax tree (rows in drawing order: ⌖ on top). Its leader ends on the extension line, recorded as a fact. <br>• `Ra 1.6` with `parameter.printed: null` and a derived `Ra`, plus a diagnostic. <br>• Datum A, its triangle on the outline, and the frame's reference to it. <br>Text runs, tips and anchors are real; arrowhead, box and symbol outlines are approximated. |
| [`ambiguous.json`](schema/examples/ambiguous.json) | **Synthetic**, modelled on view 0. <br>• Two `R 3` labels and two R3 leaders admit two assignments with a margin of 0.04. Every candidate stays `ambiguous`, the groups select nothing, and the origin is `drawing_permits`. <br>• A leader through a junction has two competing routes. <br>• Each R3 shows a derived ISO 2768-1 general tolerance, linked to its title-block `GeneralSpecification`. |
| [`unsupported.json`](schema/examples/unsupported.json) | **Synthetic.** <br>• A frame whose characteristic glyph is in no font map (`unknown`, syntax `partial`, raw glyph kept). <br>• A size with the Ⓔ modifier: recognised, not interpreted. <br>• Text drawn as outlines, kept as an `UnknownAnnotation` with its geometry. <br>• A raster image. <br>• A datum letter that is referenced but never indicated. Each carries a coded diagnostic. |

## 4.6 Mapping from the current records

How today's `annotations.json` / `ordinates.json` fields map, for the
transitional adapter (ticket T5):

| Current | Schema |
|---|---|
| `kind: radius / diameter / linear / angle / roughness / gdt / datum` | `type`; `linear` becomes `LinearDimension` unless a fit code or modifier makes it a `SizeDimension` |
| `value` | `nominal` (the literal is recovered from the tokens) |
| `tol` | `tolerance.printed` (`symmetric` / `deviations`) |
| `general_tol` | `tolerance.derived[]`, with a `Derivation` naming the ISO 2768-1 row |
| `fit` | `tolerance.printed.fit` |
| `reference` | `status: auxiliary` |
| ordinate `basic` | `status: basic` |
| `gdt.rows` | `frame.rows` (characteristic, zone, datum references); rows reordered top to bottom |
| `applies_to` | an `AssociationCandidate` with relation `frame_to_dimension`, plus `AttachmentFact`s (from `faces._attached_side`, moved to 2D) |
| `tips`, `anchors` | `chains[].terminals`, `attachment_facts` |
| `measured` | `Evidence` of kind `measured_vs_nominal` on the association |
| `status: matched / mismatch / located / unmatched` | association `status`, plus diagnostics; `mismatch` is evidence, not a status |
| `view` | `view_candidates` |
| `faces.json`, `registration.json` | **not mapped**; they move to adapters (`extensions`, or a separate file) |

## 4.7 Open naming and structure questions

- **Distance vs linear dimensions:** should `DistanceDimension` and
  `LinearDimension` be one type with a `size_or_distance` field (`size`,
  `distance`, `undetermined`)?
- **Primitives, strokes and symbols:** should they live per sheet rather than
  at the document root? The root keeps references simple; per sheet scales
  better for large multi-sheet files.
- **Raw evidence by default:** should `primitives` and `text_runs` always be
  included, or behind an option? They make payloads large (T00302932 has
  9,358 paths, 10,257 segments and 531 text spans), but the review asks never
  to drop source evidence.
- **Scores:** should they stay uncalibrated relative values, or wait until the
  corpus allows calibration?
