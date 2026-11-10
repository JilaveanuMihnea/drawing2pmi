# 5. Leader topology and 2D association

Status: **draft for review.** This replaces two pieces of `annotations.py`:
- **Single-stroke leaders:** `leaders()` returns one stroke collinear with an
  arrow.
- **Fixed-order greedy association:** `used_arrows`, consumed by fixed passes.

Thresholds are named but not given values. Values come from export profiles
and are calibrated on the corpus (section 6.3), not invented here.

## 5.1 Leader and dimension topology

### Input

`Stroke`s from reconstruction, with role candidates, plus `SymbolCandidate`s
(arrowheads, dots, triangles, frame boxes, texture symbols). Only strokes whose
role candidates include an annotation role take part:
- leader;
- dimension, extension or reference line;
- construction line;
- centre line.

Visible outlines are not part of the graph, but are kept as **contact targets**.

### Graph construction (`drawing/leader_graph.py`)

1. **Nodes** are created at:
   - every stroke end;
   - every bend inside a stroke (turn above `BEND_ANGLE`);
   - every point where two candidate strokes meet: an end on a line is a
     T-junction, an interior crossing is an X.

   Points closer than the profile's `SNAP_MM` merge.
2. **Edges** are the stroke pieces between nodes, and keep their stroke ids.
3. **Node kinds:**

   | Kind | Where |
   |---|---|
   | `end` | degree 1 |
   | `bend` | degree 2 with a turn |
   | `junction` | degree ≥ 3, or a T |
   | `contact` | where an edge touches a visible outline, another chain, or a frame border; the contact target is recorded |

4. **Crossings are not connections.** At an X between two strokes that each
   continue straight, the connection is cut. Two lines crossing are not one
   path. The crossing is kept as an `AttachmentFact` candidate only when it is
   a contact the conventions allow (a leader ending on an extension line is a
   T, not an X).
5. **Chains** are the connected components that remain.
6. **Terminals** attach symbols to `end` nodes:
   - **Arrows:** the arrowhead tip is within `TIP_MM` of the node, and its axis
     is aligned with the incident edge. The old `leaders()` alignment test
     becomes this local score.
   - **Dots and datum triangles:** by position against the node.
   - **No symbol:** a node with no symbol gets terminal kind `none` (plain
     ends, oblique strokes).

### Routes

A route is an ordered path from the annotation side to the target side.
1. **Start points:** nodes within `ATTACH_MM` of an annotation's frame border,
   text box or symbol, or of a shelf end.
2. **Enumeration:** simple paths from start points to terminal nodes,
   bounded by `MAX_ROUTE_EDGES`.
3. **Scoring:**
   - number of bends (ISO leaders are straight or have one bend);
   - straight continuation through degree-2 nodes;
   - a shelf (reference line) adjacent to text;
   - consistent role candidates along the route.
4. **Branches:** one label with several arrows gives several resolved routes.
5. **Ambiguous junctions:** two routes within `ROUTE_MARGIN` of each other both
   stay `ambiguous`, with reasons.
6. **Pruning:** pruned routes are counted in a diagnostic, never silently
   dropped.

### Roles from structure, not width

Roles come from structural patterns. Width only adds evidence through the
profile.

| Pattern | Role candidate |
|---|---|
| Line with opposed arrow terminals, text adjacent and parallel | `dimension_line` |
| Line perpendicular to a dimension line through one of its terminals, starting with a small gap at an outline | `extension_line` |
| Line from a frame border, text box or symbol to a terminal | `leader` |
| Short horizontal line at the text end of a leader | `reference_line` |
| Thin line continuing an outline's tangent to a point where another such line meets it | `construction_line` |
| Dash-dot line through a circle centre or along a symmetry | `center_line` |

### Attachment facts emitted

2D facts only; their meaning is Otto's.
- **Terminal on an outline / extension line / dimension line / centre line:**
  contact by distance, with the contacted stroke's role.
- **Terminal in line with a dimension line:** the terminal lies on the
  dimension line's infinite extension, within `INLINE_MM`, and its direction is
  collinear. This comes from `faces._attached_side`, which already uses only
  page coordinates.
- **Dot inside a region:** a dot terminal not on any line.
- **Extension origin on an outline, or at a construction intersection:** an
  extension line's outline-side end lies on two `construction_line` chains'
  intersection (within `SNAP_MM`). That is the drawing evidence of a virtual
  sharp; Otto's L2 builds the 3D sharp.
- **Symbol on a reference line.**

## 5.2 Constraint-based association (`drawing/associate.py`)

### Candidate generation

Each annotation type generates candidates under **hard constraints**. A
candidate violating one is not generated, or is kept as `invalid` with the
rule that failed when that is informative.

| Annotation | Relation | Hard constraints |
|---|---|---|
| Linear / size dimension text | `dimension_line` | text adjacent to the chain and parallel to it; the chain has two terminals (or one terminal plus an extension contact for a half-dimension) |
| | `extension_line` | through a terminal of the selected dimension line, perpendicular to it |
| Radius / diameter by leader | `leader` | the route starts at the text; the terminal is on or points at an arc or circle |
| Ordinate value | `ordinate_extension_line` | the value at a line end, the line parallel to a view axis (current `ordinates.py` logic) |
| Angle | `angle_legs` | an arc centred on the legs' intersection, with terminals on it |
| Frame | `frame_leader` | the route starts on the frame border |
| | `frame_to_dimension` | a leader terminal on the dimension's extension or dimension line, or the frame adjacent to the dimension text (to verify, ISO 1101) |
| Datum indicator | `datum_leader` or a direct triangle contact | the triangle on a line, or on a leader from the letter box |
| Texture symbol value | `label_to_symbol` | text within the symbol's reach, in its orientation |
| | `texture_symbol_leader` | the symbol's tip on a reference line of a leader |
| Frame datum letter | `datum_to_frame` | the letter is indicated by a `DatumFeatureIndication` |

### Soft evidence

Each feature is recorded as an `Evidence` item with its rule id:
- distance from the text to the line;
- angle between the text baseline and the line;
- **measured vs nominal:** measured length ÷ view scale, compared with the
  printed value, within the export's quantisation;
- route straightness;
- side of the text (ISO places text above or beside dimension lines);
- the profile's own conventions.

The score is a weighted sum, with weights from the profile and calibrated on
the corpus. **A score is never proof of ISO validity**: every resolved
association names the rules that support it.

### Constraints between candidates

| Constraint | Rule |
|---|---|
| A label needs its relation | `exactly_one` per (annotation, required relation); `at_least_one` for leaders that may branch |
| A terminal belongs to one annotation | `at_most_one` per terminal, **except** where shared use is legitimate |
| Legitimate sharing (not constrained) | <br>• one extension line serving two chained dimensions; <br>• one extension line carrying several indications at the same point (T00302932 view D-D: the Ø5,5 H7 frame's arrow and an Ra 1.6 symbol's tip touch the same point of one extension line); <br>• a dimension line serving a size and its fit annotation |
| Frame ↔ dimension | at most one dimension per frame row group, unless a pattern indication (`n×`) says otherwise |

**Open:** which constructs may legitimately share a terminal must be checked
against ISO 129-1 and ISO 1101 (matrix row "multiple frames per feature").

### Solving

1. **Decompose.** Build the conflict graph: candidates linked by a shared
   constraint. Solve each connected component separately; on T00302932 most
   components are a single label.
2. **Optimise.** Per component, find the best assignment satisfying the hard
   constraints and maximising the total score. This is a small 0-1 integer
   program: SciPy's `milp` (HiGHS) is already available, or exhaustive search
   when the component is small. Ties break by id, so results are
   deterministic.
3. **Measure the margin.** Find the second-best feasible assignment that
   differs in at least one choice. The margin is the score gap.
4. **Decide:**

   | Situation | Status |
   |---|---|
   | margin ≥ `DOUBT_MARGIN` | `resolved`: selected candidates are `resolved`, the rest `rejected` with a reason ("lost to assoc:N by margin m") |
   | margin < `DOUBT_MARGIN` | `ambiguous`: candidates that differ between the top assignments are `ambiguous`, and the group selects nothing |
   | no feasible assignment | `infeasible`: the annotation's syntax state stays as parsed, and a diagnostic names the unmet constraint |

5. **Name the ambiguity origin:**

   | Origin | When |
   |---|---|
   | `drawing_permits` | the alternatives are all valid readings of a well-formed drawing |
   | `pdf_unreadable` | the alternatives come from degraded evidence: a missing stroke, a masked line |
   | `parser_limit` | a rule the parser lacks |

**Why not greedy:** the current passes commit by kind order. A radius leader
taken by an earlier linear pass is never reconsidered (audit R8). The solver
sees every competing use of a terminal at once, and reports when the drawing
does not decide.

### Determinism and bounds

- Candidate generation is bounded per annotation (`MAX_CANDIDATES`).
  Truncation is a diagnostic.
- Component size is bounded; above the bound, beam search with a diagnostic.
- No randomness. Output order is by id, and ids are assigned in a fixed order
  (sheet, then source order). The same input gives byte-identical output.

## 5.3 What stays from the current code

| Current | Becomes |
|---|---|
| `leaders()` alignment test (`cos ≥ 0.995`) | terminal-attachment score |
| `_arrow_pairs`, `_match_linear` | dimension-line candidate generation and measured-vs-nominal evidence |
| `_extension_anchor`, `ordinates._anchors` | extension-line candidates and their outline contacts |
| `_match_radius`, `_radius_rel_tol` | radius and diameter leader candidates and arc-size evidence |
| `_angles`, `_leg_dir` | angle-leg candidates |
| `_roughness`, `_leader_through` | texture symbol candidates and shelf routes |
| `faces._attached_side` | the in-line-with-dimension-line fact |
| `faces._decide` status pattern | the doubt rule, generalised to margins |
