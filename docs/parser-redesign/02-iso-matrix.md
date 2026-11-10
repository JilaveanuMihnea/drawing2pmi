# 2. ISO construct-and-attachment matrix

Status: **draft for review. No row is verified against normative text.**

This matrix lists the drawing constructs the parser may support, family by
family. For each it gives:
- what the parser must extract;
- the attachment or reference rule as currently understood;
- the current implementation state;
- the fixtures needed.

It is a requirements map, not a statement of what the standards say. Rules
written here come from secondary knowledge and must be checked against
licensed copies before any are implemented as normative behaviour.

**Columns used throughout:**
- **Edition:** the candidate edition. Every one must be confirmed and pinned
  (decision D1). "?" means not known.
- **State:** current support, from evidence on T00302932:
  - **S:** supported and observed;
  - **P:** partial;
  - **N:** none.
- **Verified:** whether the rule was checked against licensed text. All are
  `no` today.
- **Scope:** proposed first-release scope:
  - **1:** the first release;
  - **2:** later;
  - **X:** recognise and report as unsupported, keeping the literal and its
    geometry.

Every construct, whatever its scope, needs fixtures of four kinds:
- **positive:** valid indications that must be recognised;
- **negative:** look-alikes that must not be recognised;
- **ambiguous:** layouts where two readings are valid, which must be reported
  as ambiguous;
- **exporter variants:** the same indication as each supported exporter
  writes it.

## 2.1 Families and editions

| Family | Candidate edition | Role | Verified |
|---|---|---|---|
| ISO 128-2, ISO 128-3 (lines; views, sections, cuts) | 2020 | line types, leaders, terminations, sections, hatching | no |
| ISO 129-1 (dimensions) | 2018 (+ Amd?) | dimension, extension and leader layout; dimension types | no |
| ISO 14405-1 / -2 / -3 (dimensional tolerancing) | -1:2025 (per review), -2:2018?, -3:2016? | size vs distance, size modifiers, angular sizes | no |
| ISO 286-1 / -2 (limits and fits) | 2010 | fit codes, deviations | no |
| ISO 1101 (geometrical tolerancing) | 2017 | characteristics, frames, modifiers, attachment | no |
| ISO 5459 (datums) | 2011? (newer edition?) | datum indicators, targets, references | no |
| ISO 2692 (material requirements) | 2021? | Ⓜ Ⓛ Ⓡ | no |
| ISO 5458 (patterns) | 2018 | CZ, SZ, SIM, pattern requirements | no |
| ISO 1660 (profiles) | 2017 | profile tolerancing | no |
| ISO 8015 (fundamental rules) | 2011 | invocation, defaults | no |
| ISO 17450-1 / -2 (GPS concepts) | 2011 / 2012 | integral vs derived features, operators | no |
| ISO 22081 (general specifications) | 2021 | general geometrical and size specifications | no |
| ISO 2768-1 / legacy -2 | 1989 | general tolerances | no |
| ISO 21920-1 / legacy ISO 1302 | 2021 / 2002 | surface texture indication (profile) | no |
| ISO 25178 (areal texture) | ? | areal parameters | no |
| ISO 13715 (edges) | 2019? | edges of undefined shape | no |
| ISO 6410-1 (threads), ISO 965 (thread tolerances) | ? | thread notes | no |
| ISO 5456-2, ISO 5455, ISO 7200 | 1996, 1979, 2004 | projection, scales, title block | no |
| ISO 10303-21 / -242 | AP242 ed. 1–3 | downstream adapter only | n/a |

## 2.2 Dimensions (ISO 129-1, ISO 14405, ISO 286)

| Construct | Parser must extract | Attachment / reference rule (to verify) | State | Scope |
|---|---|---|---|---|
| Linear dimension: dimension line, two terminators, two extension lines | nominal; both terminations; both extension-line origins; text placement | ends refer to an outline, an extension of one, an axis, or an intersection of extended outlines | S (`_arrow_pairs`, `_match_linear`) | 1 |
| Inclined / aligned linear dimension | as above, with the measuring direction | as above | S (F-F, 45°) | 1 |
| Text outside the arrows, reversed arrows, small dimensions | as above | arrows may point inward from outside | P (`_arrow_on(at_end)`) | 1 |
| Dimension line interrupted by text masking | one dimension line from several pieces | | N | 1 |
| Chain dimensions | each dimension separately; the shared extension lines | | P (not distinguished) | 1 |
| Parallel / baseline dimensions | each dimension; the common origin line | | P | 1 |
| Running / superimposed dimensions (single line, origin mark) | origin; each value with its terminator | origin marked by a small circle | N | 2 |
| Ordinate (coordinate) dimensions, values at extension-line ends | value, axis, origin, extension line, endpoint candidates | value is a coordinate from a marked origin | S (`ordinates.py`) | 1 |
| Polar (angular) ordinates | value, origin, radial line, zero direction and sense | | S (`_angles`) | 1 |
| Angular dimension (arc, two legs) | nominal angle; vertex; legs; terminators | legs are lines or their extensions | S (`_angles`, `_leg_dir`) | 1 |
| Angle between coinciding legs (0°) | as above, flagged | | P (parsed; second leg unresolvable) | 1 |
| Radius `R`, with leader to the arc | nominal; leader; terminal on the arc | the leader passes through or points at the centre | S (`_match_radius`) | 1 |
| Spherical radius `SR`, controlled radius `CR` | prefix kept | | N (F4: parsed as linear) | 1 |
| Diameter `Ø`: across a circle, between silhouettes, by leader | nominal; terminals; leader | | S | 1 |
| Spherical diameter `SØ` | prefix kept | | N | 1 |
| Square `□` | prefix kept | | N | 2 |
| Arc length (arc symbol over the value) | nominal; arc | | N | 2 |
| Chamfer notation (`C1`, `1×45°`) | size and angle | | N | 2 |
| Count `n×` | count; the repeated indication | the group of features belongs to Otto L3 | P (`LABEL_RE` count) | 1 |
| Auxiliary dimension `(…)` | value; auxiliary status | no tolerance applies | S (`_reference`, Solid Edge arcs) | 1 |
| Not-to-scale (underlined value) | value; underline | | N | 2 |
| Theoretically exact dimension (framed value) | value; TED status | toleranced only by GD&T | S for ordinates (`_in_frame`) | 1 |
| Symmetric tolerance `±t` | deviations | | S | 1 |
| Stacked deviations `+a / −b` | both, printed | | S (`_stacked_tol`, `stack_values`) | 1 |
| Limits (upper value over lower value) | both limits | | N | 1 |
| Single-limit `max` / `min` | limit and kind | | N | 2 |
| Fit code `H7`, `f7`, with or without deviations | fit literal; fundamental deviation; grade; printed deviations | case gives hole or shaft | S (`LABEL_RE` fit) | 1 |
| Fit pair `H7/g6` | both codes | | N | 2 |
| ISO 14405-1 size modifiers (Ⓔ, LP, GG, GX, …, ACS, SCS, count, /length) | each modifier token, in printed order | modifiers change the size specification operator | N | 1 (parse) / X (semantics) |
| Size vs distance classification (ISO 14405-1 vs -2) | the printed form only | **Otto decides** from geometry (opposed planes = size); the parser reports `undetermined` unless the syntax decides (Ø, fit, size modifier) | N | 1 |
| Decimal separator (comma or point) and units | literal; separator; unit | units from the title block or a note | P (comma assumed) | 1 |

## 2.3 Geometrical tolerancing (ISO 1101, 2692, 5458, 1660)

| Construct | Parser must extract | Attachment / reference rule (to verify) | State | Scope |
|---|---|---|---|---|
| Characteristic symbols: straightness, flatness, roundness, cylindricity, line profile, surface profile, parallelism, perpendicularity, angularity, position, concentricity/coaxiality, symmetry, circular run-out, total run-out | characteristic enum, with `unknown` | | P (4 of 14: R1) | 1 |
| Frame structure: compartments, rows | each compartment's tokens and box geometry | | P (rows as text groups; compartments not read: R2) | 1 |
| Tolerance value and zone shape (Ø, SØ) | value; zone shape | | P (Ø only) | 1 |
| Zone and feature modifiers (CZ, SZ, UZ, OZ, VA, Ⓟ, Ⓜ, Ⓛ, Ⓡ, Ⓕ, Ⓐ, Ⓒ Ⓖ Ⓝ Ⓧ Ⓣ, filters, …) | each modifier with its position in the frame | position in the frame is significant | N | 1 (parse) / X (semantics) |
| Datum reference compartments: single, common (`A-B`), ordered (`A`, `B`, `C`), modifiers in brackets | ordered compartments; letters; grouping; modifiers | | P (letters only) | 1 |
| Composite or stacked frames | rows and their relation | | P (rows merged) | 1 |
| Indicators after the frame (intersection plane, orientation plane, direction feature, collection plane) | each indicator's symbol and datum | | N | 2 |
| Adjacent indications (`n×`, `CZ` above or beside the frame; notes like "ALL OVER") | text and position | | N | 1 |
| All-around / all-over symbols on the leader | symbol; attachment | | N | 2 |
| Between indication (↔ with letters) | both letters | | N | 2 |
| Frame attached by a leader to an outline or extension line | leader route; terminal; contact | arrow on an outline or its extension, clearly away from a dimension line: **the integral feature** | P (`faces._attached_side`, 3D code: F8) | 1 |
| Frame attached in line with a dimension line | dimension reference; the in-line fact | **the derived feature** (axis, median) | P (as above) | 1 |
| Frame attached to a dimension's label (no leader) | dimension reference | to verify | P (`annotate`, nearest label) | 1 |
| Frame with leader terminated by a dot on a surface | dot terminal; surface region | the surface itself | N | 1 |
| Theoretically exact dimensions referenced by a frame | TED links | | P | 1 |
| Pattern requirements (ISO 5458): SIM, CZ, SZ, n× | tokens and the frames they join | groups live in Otto L3 | N | 1 (parse) |
| Profile tolerancing (ISO 1660): UZ, all-around, between | tokens | target may be several features | N | 2 |

## 2.4 Datums (ISO 5459)

| Construct | Parser must extract | Attachment / reference rule (to verify) | State | Scope |
|---|---|---|---|---|
| Datum feature indicator: letter in a frame, triangle (filled or open) | letter; frame; triangle type; base contact | triangle on an outline or its extension: **the surface**. On a dimension line: **the derived feature**. | P (`annotate` datums; filled triangle only) | 1 |
| Indicator joined by a leader | leader route; terminal | | N | 1 |
| Datum targets: symbol (circle split, identifier and size), point (×), line, area | identifier; size; target geometry and location | | N | 2 |
| Common datums, datum systems, modifiers in the frame | parsed in §2.3 | construction of datum systems is Otto's | P | 1 |

## 2.5 Surface texture and edges (ISO 21920-1 / ISO 1302; ISO 25178; ISO 13715)

| Construct | Parser must extract | Attachment / reference rule (to verify) | State | Scope |
|---|---|---|---|---|
| Basic symbol (two legs about 60° apart) | symbol geometry; tip; orientation | tip on the surface, its extension line, or a leader or shelf | S (`_roughness`) | 1 |
| Material removal required (bar) / prohibited (circle) | variant | | S (bar observed) / P (circle untested) | 1 |
| All-around circle | flag | | N | 2 |
| Requirement text: parameter designation (Ra, Rz, …), value, limit (U/L), filter, evaluation length | each token; parameter **as printed** | without a printed parameter, a value is legacy practice: report `parameter: null` with a candidate `Ra` derivation | P (`Ra` assumed: R10) | 1 |
| Manufacturing method, lay symbols (=, ⊥, X, M, C, R, P), allowance | tokens and positions | | N | 2 |
| Symbol on a leader with an arrow | leader route; terminal | | S (`_leader_through`, one shoulder only) | 1 |
| General requirement in the title block, with exceptions in parentheses | value; exceptions; scope | | S (`_general_texture`) | 1 |
| Areal parameters (ISO 25178: Sa, Sq, …) | as tokens | | N | X |
| Edge symbol (ISO 13715): square with ± sizes | values; positions; target edge | not every edge note is a chamfer | N | 2 |

## 2.6 Lines, views and sections (ISO 128, ISO 5456-2, ISO 5455, ISO 7200)

| Construct | Parser must extract | Attachment / reference rule (to verify) | State | Scope |
|---|---|---|---|---|
| Line types: continuous wide/narrow, dashed, chain, double-dashed chain | type candidates with scores | type by pattern; width only as profile evidence | P (one label, width map: R6) | 1 |
| Leader terminations: arrow (filled or open), dot, none | terminal kind | arrow ends on an outline; a dot ends inside a surface | P (filled arrows only) | 1 |
| Reference line (shelf) after a leader | shelf; attachment | | P (roughness only) | 1 |
| Imaginary lines of intersection (thin extensions to a theoretical corner) | the construction lines; their intersection point | dimensions to virtual sharps end here | N in 2D (3D only: F8) | 1 |
| Centre lines, centre marks | lines; centre points | | P (role only) | 1 |
| Hatching | regions; hatch families | | S (`mark_hatching`) | 1 |
| Cutting plane line, arrows, letters | line; arrow directions; letters | | P (registration only) | 1 |
| Break lines, partial and broken views | break geometry; view limits | | P (`_is_break_line`, in registration) | 1 |
| Section, detail, auxiliary, rotated views | kind; label; scale; parent; clipping boundary | | P (no auxiliary or rotated views) | 1 |
| Projection method symbol | first or third angle | | S (first angle only: F11) | 1 |
| Scales (`2:1`, `6:1`) | literal; value; source | | S | 1 |
| Title block fields | label and value spans | layout varies by organisation | P (F10) | 1 |

## 2.7 General and invoking specifications (ISO 8015, ISO 22081, ISO 2768)

| Construct | Parser must extract | Attachment / reference rule (to verify) | State | Scope |
|---|---|---|---|---|
| Invocation of ISO 8015 / independency | literal and source | | N | 1 |
| General tolerance reference (ISO 2768-1 class, ISO 22081 indications) | standard; class; literal; source | the cited standard and edition govern | P (`m` from `UNI EN 22768`) | 1 |
| Printed general tolerance table | rows | the printed table governs over the built-in one | S (`_tolerance_table`) | 1 |
| Legacy ISO 2768-2 classes (H, K, L) | class literal | not interchangeable with ISO 22081 | N | 1 (parse) |
| Derived general tolerance of a dimension | value and the **derivation**: rule, table row, edition | | P (no provenance: R10) | 1 |

## 2.8 Notes

| Construct | Parser must extract | State | Scope |
|---|---|---|---|
| Thread notes (`M6`, `M6×1-6H`, depth) | tokens; thread designation | N (F4) | 2 |
| Free text notes | text and location | P (dropped as "note") | 1 |
| Unknown symbols or constructs | literal, glyphs, geometry, diagnostic | N | 1 |
