# ISO standards used in the drawing-to-PMI pipeline

Which elements of which standards the code relies on, where they are
implemented, and where it assumes or departs from the standard. Written for
drawing T00302932-00 (Solid Edge export, first-angle projection, general
tolerance "m UNI EN 22768").

The tolerance tables below were checked against the code. Statements about
what a standard says are paraphrased from memory of the standards, not quoted
from them; check the cited standard before relying on a detail.

## Summary

| Standard | Topic | Used for |
|---|---|---|
| ISO 2768-1 (EN 22768-1, UNI EN 22768) | General tolerances, linear and angular | Tolerances of dimensions without an explicit tolerance |
| ISO 286-1 / -2 | ISO limits and fits | Reading fit codes (H7, f7) and writing them as `limits_and_fits` |
| ISO 1101 | Geometrical tolerancing (GD&T) | Symbols, tolerance frames, Ø zones, TEDs, which feature a frame applies to |
| ISO 5459 | Datums and datum systems | Datum feature symbol, datum letters and their order in a frame |
| ISO 1302 (now ISO 21920-1) | Surface texture indication | Roughness symbol shape, process variants, values, the general requirement in the title block |
| ISO 129-1 | Dimensioning | R and Ø prefixes, auxiliary dimensions, ordinate and angular dimensioning, decimal comma |
| ISO 128 (128-2, 128-3) | Lines, views, sections | Line types, hidden and centre lines, imaginary intersection lines, hatching, section and detail views |
| ISO 5456-2 | Projection methods | First- or third-angle projection, from the title block symbol |
| ISO 5455 | Scales | Scale notation (2:1, 6:1, 10:1) |
| ISO 10303-242 (STEP AP242) | Product data exchange | The output: semantic PMI linked to B-rep faces |

---

## ISO 2768-1: general tolerances

The title block says `m UNI EN 22768`. UNI EN 22768 is the Italian adoption of
EN 22768, which is ISO 2768. Only the class letter (`m`) is read
([pipeline/views.py:376](../pipeline/views.py:376)). The sheet prints its own table row for class m,
and the parser reads that row ([pipeline/views.py:282](../pipeline/views.py:282)). It agrees with the
built-in ISO 2768-1 table, which is the fallback ([pipeline/views.py:39](../pipeline/views.py:39)).

**Linear sizes** (Table 1), permissible deviations in mm:

| Class | 0.5–3 | >3–6 | >6–30 | >30–120 | >120–400 | >400–1000 | >1000–2000 | >2000–4000 |
|---|---|---|---|---|---|---|---|---|
| f | ±0.05 | ±0.05 | ±0.1 | ±0.15 | ±0.2 | ±0.3 | ±0.5 | – |
| **m** | ±0.1 | ±0.1 | ±0.2 | ±0.3 | ±0.5 | ±0.8 | ±1.2 | ±2 |
| c | ±0.2 | ±0.3 | ±0.5 | ±0.8 | ±1.2 | ±2 | ±3 | ±4 |
| v | – | ±0.5 | ±1 | ±1.5 | ±2.5 | ±4 | ±6 | ±8 |

These apply to linear dimensions, diameters and ordinates.

**External radii and chamfer heights** (Table 2), used for `R` dimensions
([pipeline/annotations.py:50](../pipeline/annotations.py:50)):

| Class | 0.5–3 | >3–6 | >6 |
|---|---|---|---|
| f, **m** | ±0.2 | ±0.5 | ±1 |
| c, v | ±0.4 | ±1 | ±2 |

**Angular dimensions** (Table 3), by the length of the shorter leg
([pipeline/pmi_writer.py:241](../pipeline/pmi_writer.py:241)):

| Class | ≤10 mm | >10–50 | >50–120 | >120–400 | >400 |
|---|---|---|---|---|---|
| f, **m** | ±1° | ±0°30′ | ±0°20′ | ±0°10′ | ±0°5′ |
| c | ±1°30′ | ±1° | ±0°30′ | ±0°15′ | ±0°10′ |
| v | ±3° | ±2° | ±1° | ±0°30′ | ±0°20′ |

**How it is applied** ([pipeline/annotations.py:335](../pipeline/annotations.py:335), [pipeline/pmi_writer.py:290](../pipeline/pmi_writer.py:290)):
- It applies only where the drawing gives no explicit tolerance, fit or deviations.
- It doesn't apply to auxiliary dimensions in parentheses, or to theoretically
  exact (boxed) dimensions.

**Range rules:**
- **Range bounds.** Ranges are "over lo up to hi", except the first, which
  includes its lower bound ("0.5 up to 3"). So `0,5` gets ±0.1 and `R 0,5` gets
  ±0.2.
- **Values below 0.5 mm.** ISO 2768-1 doesn't cover them; their tolerance has to
  be written on the drawing. The code gives them no general tolerance
  ([pipeline/annotations.py:335](../pipeline/annotations.py:335)). The export flags them
  ("no tolerance: below the ISO 2768-1 range"). On this drawing that is the
  `0,25` step in H-H, which has no written tolerance.

**Interpretations:**
- **Leg length for angular ordinates.** For an angular ordinate, the leg is taken
  as the feature's distance from the origin axis (52.75 mm → ±0°20′). For the 90°
  countersink, it is the cone's slant length (about 3.5 mm → ±1°).
- **ISO 2768-2 not used.** The geometrical general tolerances (classes H, K, L)
  aren't used; the title block names no class for them.

## ISO 286-1 / ISO 286-2: limits and fits

**Fit codes.** These are read from the dimension text by
[pipeline/annotations.py:34](../pipeline/annotations.py:34) and converted by
[pipeline/pmi_writer.py:204](../pipeline/pmi_writer.py:204):
- The letters give the fundamental deviation. Uppercase means a hole (H) and
  lowercase means a shaft (f).
- The number gives the IT tolerance grade.

**Deviations.** These come from the deviations printed on the drawing, not from
an ISO 286 table. On this drawing they agree with ISO 286-2:

| Dimension | Fit | Size range | Deviations |
|---|---|---|---|
| 3,5 H7 (slot) | H7 | 3–6 mm | +0.012 / 0 |
| Ø5,5 H7 (hole) | H7 | 3–6 mm | +0.012 / 0 |
| Ø115 f7 | f7 | 80–120 mm | −0.036 / −0.071 |

**Output.** Fits are written as AP242 `limits_and_fits` (for example `('H','hole','7','')`).

## ISO 1101: geometrical tolerancing

**Symbols.** Solid Edge's ISO symbol font is mapped back to the ISO 1101
characteristic symbols ([pipeline/textgroups.py:17](../pipeline/textgroups.py:17),
[pipeline/annotations.py:32](../pipeline/annotations.py:32)):

| Font character | Symbol | Characteristic | Datum needed |
|---|---|---|---|
| `l` | ⌖ | position | optional |
| `n` | ⊥ | perpendicularity | yes |
| `i` | // | parallelism | yes |
| `r` | ▱ | flatness | no |

The writer also maps the other characteristics (straightness, circularity,
cylindricity, angularity, symmetry, coaxiality/concentricity, line and surface
profile, circular and total run-out) ([pipeline/pmi_writer.py:50](../pipeline/pmi_writer.py:50)). None
of them occur on this drawing.

**The tolerance frame:**
- **Compartments.** Each row reads, in order: characteristic symbol, tolerance
  value (with `Ø` in front for a cylindrical zone), then datum letters.
- **Combined frames.** Stacked rows form one frame, for example `⊥ 0.01 A` over
  `⌖ Ø 0.01`.
- **Ø zones.** A `Ø` before the value means a cylindrical tolerance zone. It is
  written as `tolerance_zone_form('cylindrical or circular')`.

**Which feature a frame tolerances** ([pipeline/faces.py:1192](../pipeline/faces.py:1192)). This
depends on where its leader ends:

| The leader ends… | It applies to |
|---|---|
| on the outline or on an extension line, clearly away from the dimension line | that **surface** (the integral feature) |
| in line with a size dimension's dimension line | the **derived feature**: the median plane or axis of that feature of size |

On this drawing, five frames end on extension lines and get one surface each.
One of those is the ⊥/⌖ frame on the 3,5 H7 slot, so the export flags it: a
position tolerance there usually means the slot as a whole. The frame on Ø5,5 H7
applies to the hole's axis.

**Theoretically exact dimensions (TED).** A value in a rectangular frame is
exact, and is toleranced by the GD&T that refers to it
([pipeline/ordinates.py:345](../pipeline/ordinates.py:345)). Three ordinates are TEDs. They get no
general tolerance.

## ISO 5459: datums

- **Datum feature symbol.** A capital letter in a frame, joined to a filled
  triangle. The triangle's base sits on the datum feature, or on its extension
  line ([pipeline/annotations.py](../pipeline/annotations.py), datums section). Datum A sits on an
  extension line, so it resolves to the surface itself: the bottom face,
  Y = −4.75.
- **Datum order.** The letters in a frame are in order of precedence (primary,
  secondary, tertiary). The order is written as the datum's position in the AP242
  `datum_system` / `datum_reference_compartment`
  ([pipeline/pmi_writer.py:154](../pipeline/pmi_writer.py:154)). On this drawing every frame refers to
  datum A alone.

## ISO 1302 (superseded by ISO 21920-1): surface texture

Implemented in [pipeline/annotations.py:792](../pipeline/annotations.py:792).

**The symbol:**
- **Basic symbol.** Two straight lines of unequal length, about 60° apart. The
  code accepts 50–70°, with the long leg at least 1.4× the short one
  ([pipeline/annotations.py:789](../pipeline/annotations.py:789)).
- **Tip.** The vertex touches the surface, its extension line, or a leader line.
  A leader ends in an arrow on the surface
  ([pipeline/annotations.py:848](../pipeline/annotations.py:848)).
- **Process** ([pipeline/annotations.py:868](../pipeline/annotations.py:868)):
  - A bar closing the V means material removal is required. All eight on this
    sheet have it.
  - A circle inside the V means material removal is not permitted.
- **Value.** Micrometres.

**Assumption: a bare value is Ra.** This drawing prints only the number (`1.6`,
`0.8`). Older practice (ISO 1302:1992) read a lone value as Ra. ISO 1302:2002 and
ISO 21920-1 require the parameter to be written (`Ra 1.6`). The pipeline records
`Ra` as an assumption.

**The general requirement.** The title block's roughness field says
`3.2 (1.6; 0.8)`. In ISO 1302's simplified indication this means Ra 3.2 on every
surface except those marked on the views, and the values in parentheses are the
marked ones (1.6 and 0.8).
- **Parsing.** It becomes its own roughness annotation.
- **Applying it** ([pipeline/faces.py](../pipeline/faces.py), `_general_texture`). It goes on every
  model face that no symbol on the views marks: 165 of the 176 faces. The other
  11 carry the eight callouts.
- **Consistency check.** If a value marked on the views isn't in the
  parentheses, the match is flagged.

**Not in the STEP output.** OCCT can't write AP242 `surface_texture_representation`,
so the eight callouts and the general requirement stay in `faces.json` and
`pmi_report.json`.

## ISO 129-1: dimensioning

- **Prefixes.** `R` for a radius and `Ø` for a diameter
  ([pipeline/annotations.py:34](../pipeline/annotations.py:34)). A diameter is drawn either across a
  circle, or between two silhouettes or their extension lines.
- **Dimension and extension lines.** A linear dimension's line ends in arrowheads
  on extension lines. The arrow tips give the measured coordinates, which is how
  linear dimensions are matched to faces.
- **Auxiliary (reference) dimensions.** A value in parentheses, for example
  `(5)`, is informative and not toleranced ([pipeline/annotations.py:314](../pipeline/annotations.py:314)).
  Solid Edge draws the parentheses as arcs rather than text.
- **Ordinate dimensioning:**
  - **Linear.** Values written at the ends of extension lines, measured from a
    marked origin. Each view's origin and scale are fitted from them
    ([pipeline/ordinates.py](../pipeline/ordinates.py)).
  - **Angular.** A value at the end of a radial thin line through the origin. The
    sheet's convention, 0° upward and clockwise, is fitted from all such labels
    together ([pipeline/annotations.py:645](../pipeline/annotations.py:645)).
- **Angle dimensions.** An arc centred on the vertex, with arrows on the two legs.
  The 90° countersink is measured along its legs.
- **Decimal sign.** Dimensions use the decimal comma (`36,87`). On this sheet,
  roughness values use a point (`1.6`). That is a Solid Edge habit, and the parser
  uses it to tell roughness values from dimensions.

## ISO 128-2 / ISO 128-3: lines, views and sections

ISO 128-2 and 128-3 (2020) replace the older parts ISO 128-20, -24, -30, -34,
-40, -44 and -50.

**Line types** ([pipeline/classify.py:368](../pipeline/classify.py:368)):

| ISO 128 line | Application | Role in the pipeline |
|---|---|---|
| Continuous wide | visible outlines | `visible` |
| Continuous narrow | dimension, extension and leader lines, hatching, imaginary lines of intersection | `thin` |
| Dashed narrow | hidden outlines | `hidden` |
| Long-dashed dotted narrow | centre lines | `center` |
| Long-dashed double-dotted narrow | adjacent parts and extreme positions | `phantom` |
| Chain line, wide at the ends and at changes of direction | cutting planes | `cutting_plane` |

Line types are told apart by the dash-to-gap pattern
([pipeline/classify.py:40](../pipeline/classify.py:40)), not by ISO dash lengths. Line widths come from
the file and don't follow the ISO 2:1 wide-to-narrow ratio
([pipeline/classify.py:43](../pipeline/classify.py:43)).

**Imaginary lines of intersection.** Where a fillet removes a sharp corner,
narrow lines extend the adjoining surfaces to their theoretical intersection.
Dimensions often go to that point. These "virtual sharps" are reconstructed by
extending model faces and edges ([pipeline/faces.py:254](../pipeline/faces.py:254),
[pipeline/faces.py:575](../pipeline/faces.py:575)). About a third of the ordinates use them.

**Hatching.** Equally spaced narrow lines mark cut material in sections
([pipeline/classify.py:401](../pipeline/classify.py:401)).

**Views and sections:**
- **Labels.** Views carry capital letters (`A`, `B`). Sections are labelled `C-C`
  and so on, and detail views by a letter with their scale
  ([pipeline/views.py:34](../pipeline/views.py:34), [pipeline/views.py:392](../pipeline/views.py:392)).
- **Cutting planes.** A section's plane is drawn as a cutting line with arrows
  showing the viewing direction ([pipeline/register.py:435](../pipeline/register.py:435)). The arrows
  set the section's orientation. A drafting-convention preference settles the
  remaining choice of frame.
- **Detail views.** An enlarged detail takes the orientation of the view it is
  taken from.

## ISO 5456-2: projection methods

**First- or third-angle.** The title block carries the projection-method symbol:
a truncated cone shown with its end view as two concentric circles. In
first-angle projection (method E) the end view sits on the side away from the end
it shows. This sheet is first-angle ([pipeline/views.py:316](../pipeline/views.py:316)).

**Layout check.** The relative positions of unlabelled orthographic views are
checked against that method.

## ISO 5455: scales

**Notation.** Enlargement scales are written as `2:1`, `6:1` and `10:1`, in the
title block and under view captions.

**Non-preferred scale.** 6:1 (details E and F-F) isn't a preferred ISO 5455
scale. The preferred enlargements are 2:1, 5:1, 10:1, 20:1 and 50:1. The pipeline
doesn't enforce the series; it fits each view's actual scale from its ordinates.

## ISO 10303-242 (STEP AP242): the output

**Schema.** `T00302932-00_pmi.stp` is written in schema
`AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF` (edition 1) by OCCT's XCAF
writer ([pipeline/pmi_writer.py](../pipeline/pmi_writer.py)).

**Entities written:**

| What | AP242 entities |
|---|---|
| Sizes | `dimensional_size` ('radius', 'diameter', 'thickness'), `angular_size` |
| Locations | `dimensional_location` ('linear distance'), `angular_location` |
| Tolerances on dimensions | `plus_minus_tolerance`, `tolerance_value`, `limits_and_fits` |
| GD&T | `flatness_tolerance`, `parallelism_tolerance`, `perpendicularity_tolerance`, `position_tolerance`, `geometric_tolerance_with_datum_reference`, `tolerance_zone_form` |
| Datums | `datum_feature`, `datum`, `datum_system`, `datum_reference_compartment` |
| Links to geometry | `shape_aspect`, `composite_shape_aspect` (multi-face features) |
| Traceability | `descriptive_representation_item` ('drawing', 'check') on dimensions |

**Conventions from the CAx-IF recommended practices** (not ISO texts):
- **Signed bounds.** A tolerance's lower bound is a signed deviation: ±0.1 gives
  a lower bound of −0.1. OCCT negates the lower bound when writing, so the writer
  compensates.
- **Fit grade.** It is written as its number ('7'). OCCT would otherwise write it
  off by two, as '9' for IT7.
- **Angles.** They are in the file's plane-angle unit, which is radians.

**What AP242 can't express directly:**
- **Angular ordinates** have no exact form. They are written as angular locations
  from the origin axis, with a note naming the 0° direction and the sense.
- **Virtual sharps** can't be referenced. Dimensions to them point at the faces
  that form the corner, and are flagged.

## Not used

- **ISO 2768-2:** geometrical general tolerances (H, K, L).
- **ISO 8015:** the independency principle, and default specification operators.
- **ISO 14405-1:** size specification modifiers such as Ⓔ (envelope), LP and GG.
- **ISO 1101 material condition modifiers** (Ⓜ, Ⓛ) and **projected zones** (Ⓟ):
  none on this drawing, so none are parsed.
- **ISO 7200:** title block fields. The parser reads this company's own labels
  (Italian and English) rather than relying on the ISO 7200 layout.
