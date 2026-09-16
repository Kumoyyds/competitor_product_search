# Matching Module

**Status**: Implemented

## Responsibility

Verifies exact SKU identity between shared `InputItem` and scraping `ProductData`. Output is a binary structured verdict with evidence and reasoning; there is no score.

## Inputs / Outputs

- **Input**: `InputItem` + `ProductData`
- **Output**: `ProductMatchResult`

For the decision-order rationale (why GTIN short-circuits, why brand/numeric conflicts short-circuit, why Vision is context-only, why ambiguity fails closed), see [`docs/matching/design.md`](../../docs/matching/design.md) — this file states the invariants tersely and does not repeat the "why".

## Invariants

- Equal valid GTIN short-circuits to Match; missing/invalid GTIN on either side is unknown and does not short-circuit; a different valid GTIN is strong context but does not short-circuit to No Match.
- Confirmed brand/numeric/multipack conflict short-circuits to No Match.
- Multipack roles (`per_item`, `count`, `total`) are never compared across roles.
- One text prompt handles both remaining paths (GTIN unknown/conflict and variant pass/unknown); Vision only adds context, never a verdict.
- Vision is a batch-wide barrier before any text decision in `verify_products()`, because the text prompt consumes the visual comment. Within each stage, calls run concurrently — Vision under `compare_batch`'s own bound, text decisions under `llm.concurrency` (per-call `concurrency=` override). Results stay in input order regardless of completion order.
- Vision image counts are capped per side independently (`vision.max_considered_num_input_image` / `max_considered_num_scraping_image`); the external `max_images_per_set` is raised to `max(both)` so it never re-truncates.
- Evidence-insufficient LLM results fail closed as No Match. Technical LLM failures raise `MatchingError`, never a silent No Match.
- Every `ProductMatchResult` and terminal `MatchingError` carries an ordered node trace (`DecisionNode`: `GTIN`, `VARIANT_RULE`, `VISION`, `LLM`). A missing node means that stage did not run.
- Matching has no database of its own. The orchestrator persists the trace into `matching_decisions` in `orchestrator.db` — see [`docs/orchestrator/storage.md`](../../docs/orchestrator/storage.md).

## Files

- `service.py` — public single/batch APIs (`verify_product`, `verify_products`), ordered decision tracing, Vision batching, prompt construction and parsing
- `attributes.py` — GTIN validation (`normalize_gtin`), brand/numeric evidence (`compare_variants`, reusing `src.search.layers.brand`/`numeric`), multipack extraction and comparison (`extract_multipack`, `compare_multipacks`)
- `config.py` — yaml loader for `matching_config.yaml`
- `matching_config.yaml` — text/Vision models, per-side Vision image caps, text-decision concurrency, and retry settings — see [`README.md`](README.md) for what each key does operationally
