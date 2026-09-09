# Matching Module

**Status**: Implemented

## Responsibility

Verifies exact SKU identity between shared `InputItem` and scraping `ProductData`. Output is a binary structured verdict with evidence and reasoning; there is no score.

## Inputs / Outputs

- **Input**: `InputItem` + `ProductData`
- **Output**: `ProductMatchResult`

## Invariants

- Equal valid GTIN short-circuits Match; missing/invalid is unknown; different valid GTIN continues as strong context.
- Confirmed brand/numeric/multipack conflicts short-circuit No Match. Continuous values use Search's ±10%; discrete values are exact.
- Multipack roles (`per_item`, `count`, `total`) are never compared across roles. Internally inconsistent declarations are evidence, not hard verdicts.
- One text prompt handles both paths; Vision only adds context. Vision unavailable falls back to text.
- Vision is a batch-wide barrier before any text decision, because the text prompt consumes the visual comment. Within each stage the calls run concurrently: Vision under `compare_batch`'s own bound, text decisions under `llm.concurrency` (per-call `concurrency=` override). Results stay in input order regardless of completion order.
- Vision image counts are capped per side independently (`vision.max_considered_num_input_image` / `max_considered_num_scraping_image`): each side is deduplicated in order, then truncated to its leading cap, and the external `max_images_per_set` is raised to `max(both)` so it never re-truncates.
- Evidence-insufficient LLM results fail closed as No Match. Technical LLM failures raise `MatchingError`.
- Every `ProductMatchResult` and terminal `MatchingError` carries an ordered node trace. A missing node means that stage did not run, including GTIN and variant-rule short-circuits.

## Files

- `service.py` — public single/batch APIs, ordered decision tracing, Vision batching, prompt and parsing
- `attributes.py` — GTIN validation, brand/numeric evidence, multipack normalization
- `matching_config.yaml` — text/Vision models, per-side Vision image caps, text-decision concurrency, and retry settings
