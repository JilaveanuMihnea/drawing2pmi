# 3. Exporter and PDF modality matrix

Status: **draft for review.** Only one exporter has been observed: Solid Edge,
on T00302932-00. Everything said about other exporters is a hypothesis to
characterise with real files, not knowledge.

ISO standards say what a drawing means, not how a CAD system or PDF
converter encodes it. The parser therefore separates:
- **ISO grammar:** fixed meaning, the same for all inputs;
- **export profiles:** font maps, line-width conventions, graphic idioms and
  reconstruction tolerances per exporter.

A profile can change how a symbol is *found*, never what it *means*.

**Proposed v1 support**, for decision D2:
- **Supported:** parsed into the schema, with tests.
- **Diagnostic:** detected and reported as unsupported, with its source
  evidence kept; no interpretation is invented.
- **Out:** outside the vector-parser promise; needs a separate path.

## 3.1 PDF content modalities

| Modality | How it is detected | Current state | Proposed v1 |
|---|---|---|---|
| Vector geometry with native text (font glyphs with Unicode) | text spans present; drawings present | Supported (T00302932) | **Supported** |
| Custom symbol fonts (codes mapped to symbols by the font) | font name not in the profile's map; glyph codes outside the expected set | Partly: one Solid Edge font, 5 codes | **Supported** through profile font maps; unknown glyphs → diagnostic |
| Subset fonts without a usable Unicode map | missing or private-use code points | Not handled | **Diagnostic** (raw code kept) |
| Text drawn as outlines (glyphs converted to paths) | many small closed filled paths in text-like clusters; no text spans in the region | Not detected: the outlines would be taken as geometry | **Diagnostic** first; recognition later (decision D2) |
| Type 3 fonts (glyphs defined as drawings) | font type | Not checked | **Diagnostic** |
| Raster-only PDF (scan) | page is one image; no drawings | Not detected | **Out**, but reported (separate OCR/vectorisation path) |
| Hybrid: raster with vector overlays | images plus drawings | Not detected | **Diagnostic** for the raster regions |
| Multiple pages / sheets | page count > 1 | **First page only** (F1) | **Supported** |
| Page rotation | `/Rotate` | Supported (`rotation_matrix`) but not recorded | **Supported**, with the transform recorded |
| Nested transforms (form XObjects) | transform chain per path | Flattened by PyMuPDF; chain not recorded | **Supported**, with the chain recorded |
| Clipping paths | clip operators | **Ignored** (F2) | **Supported** |
| Arcs and circles as Bézier curves | `c` operators | **Dropped** in classification (R12) | **Supported** |
| Arcs as chord runs (polylines) | many short collinear-ish segments | Supported (Solid Edge) | **Supported** |
| Dash patterns as PDF dash arrays | `dashes` attribute | Not used | **Supported** |
| Dashes as separate segments | collinear segment runs with gaps | Supported (Solid Edge) | **Supported** |
| Fills: arrowheads, dots, datum triangles | filled closed paths | Supported (filled only) | **Supported**, open shapes too |
| Coordinate precision / quantisation | spacing of coordinates | Solid Edge 0.12 pt grid built into thresholds | **Profile parameter** |
| Units on the sheet (mm or inch drawing) | title block or note | mm assumed | **Supported** for mm; inch → decision D2 |
| Optional content (layers) | OCG | Not read | **Diagnostic** |
| Annotations (PDF comments, markup) | page annotations | Not read | **Diagnostic** |

## 3.2 Exporters and converters

| Source | Status | What to characterise |
|---|---|---|
| **Solid Edge** (native PDF export) | **Observed** on one drawing. Idioms seen: <br>• page rotated 90° <br>• no Béziers (arcs as chord runs) <br>• dashes as separate segments <br>• 0.12 pt coordinate grid <br>• symbol font `SolidEdgeISO1Symbols` (`O l n i r`) <br>• auxiliary-dimension parentheses drawn as arcs <br>• fixed line widths per role (`WIDTH_ROLES`) <br>• dimension values with comma, roughness values with point <br>• engraved model text in `SERomans` | The same drawing exported again with other settings; other drawings from the same team |
| SolidWorks | not seen | everything (fonts, symbol encoding, arcs, dashes, line widths) |
| Siemens NX | not seen | as above |
| PTC Creo | not seen | as above |
| Autodesk Inventor | not seen | as above |
| Autodesk Fusion | not seen | as above |
| CATIA | not seen | as above |
| AutoCAD / DWG-to-PDF | not seen | as above, plus SHX text (often outlines) |
| Print-to-PDF drivers (Microsoft, Ghostscript, CUPS) | not seen | text-as-outlines frequency; Bézier arcs; flattening |
| PDF/A or optimiser conversions | not seen | font subsetting; stream rewriting |

**The most informative first fixtures:** the *same* drawing (T00302932)
exported by Solid Edge through two or three routes (native export,
print-to-PDF, PDF/A). Differences between them isolate exporter and
converter effects from drawing content.

## 3.3 What a profile contains

A profile is data, not code. Every value currently hard-coded for Solid Edge
moves into one:

| Item | Today (Solid Edge, hard-coded) |
|---|---|
| Line width → role evidence | `classify.WIDTH_ROLES` |
| Chaining and fit tolerances | `classify.SNAP`, `FIT_TOL`, `CHORD_TOL`, `MAX_GAP`, `MAX_DASH`, `DASHDOT_RATIO` |
| Symbol font maps | `textgroups.SYMBOLS` |
| Fonts carrying model geometry, not annotation | the `SERomans` filter (`annotations.py:383`) |
| Graphic idioms | auxiliary parentheses drawn as arcs (`_reference`); origin "0" with a dot (`find_origins`) |
| Locale | decimal separator; title-block label vocabulary (`views.TITLE_LABELS`) |
| Text metrics | `textgroups.LINE_HEIGHT`, `STACK_RATIO` |

Profile selection is itself evidence: detected from PDF metadata (producer
string) and confirmed by observed idioms. The chosen profile and the reason
are recorded in the output (`DrawingDocument.source.profile`). An unknown
exporter gets a generic profile and a diagnostic.
