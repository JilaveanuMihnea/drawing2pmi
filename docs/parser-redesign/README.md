# Parser redesign: pre-coding deliverables

These answer the Otto architecture team's review of commit `0fa912d`
("Drawing2PMI → Otto: Technical Review", 9 November 2026). They are read
against "Otto's PartModel: architecture for drawing-parser integration".

**Status: draft for review.** Nothing here is frozen, and no ISO rule in
these files has been checked against normative text. No code has been changed.

| # | Deliverable (review §F) | File |
|---|---|---|
| 1 | Function-by-function audit, including a check of the review's own code claims | [01-audit.md](01-audit.md) |
| 2 | ISO construct-and-attachment matrix, with edition and verification columns | [02-iso-matrix.md](02-iso-matrix.md) |
| 3 | Exporter and PDF modality matrix, and what an export profile contains | [03-modality-matrix.md](03-modality-matrix.md) |
| 4 | Typed schema, examples, invariants and validator | [04-schema.md](04-schema.md), [schema/](schema) |
| 5 | Leader topology model and constraint-based association algorithm | [05-leaders-and-association.md](05-leaders-and-association.md) |
| 6 | Migration plan, test plan and corpus design, ticket breakdown | [06-migration-tests-tickets.md](06-migration-tests-tickets.md) |
| 7 | Decisions required from the Otto owners | [07-decisions.md](07-decisions.md) |

## In short

- **Keep the 2D geometry.** Almost all of it is reusable as evidence
  generators with profiles and tests: PDF ingestion, chaining, fitting, dash
  linking, arrowheads, text grouping, views, ordinates, extension anchors.
- **Replace the meaning-making parts:**
  - lexical classification of labels → ISO grammars;
  - single-stroke leaders → a leader graph;
  - fixed-order greedy matching → a constraint solver that keeps ambiguity.
- **Move the 3D half to optional adapters:** registration, face matching and
  AP242 export, about 2,600 lines. Two 2D facts move out of it into the parser.
- **The output becomes a typed drawing model** with no B-rep identifiers.
  Validated examples are in `schema/examples`.
- **Two tickets change no behaviour and can start immediately:** baseline
  tests (T1) and gold labels (T2).
- **Owner decisions** are needed on editions, modalities, serialization,
  registration ownership, the 2D/3D boundary and the corpus.

## The review's code claims

Most of the review's code findings are accurate. Three need adjustment
(details in 01-audit.md §1.1):
- **Béziers (R12):** they are not just possibly unclassified; they are dropped
  silently.
- **Font substitution (R5):** none exists.
- **OCCT dependency (R16):** the 2D modules already run without OCCT; only the
  entry point needs it.

## Validate the examples

```bash
python docs/parser-redesign/schema/validate.py docs/parser-redesign/schema/examples/*.json
```

This needs `jsonschema` (installed in the `occ` environment).
