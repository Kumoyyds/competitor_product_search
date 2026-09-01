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
- Evidence-insufficient LLM results fail closed as No Match. Technical LLM failures raise `MatchingError`.

## Files

- `service.py` — public single/batch APIs, Vision batching, prompt and parsing
- `attributes.py` — GTIN validation, brand/numeric evidence, multipack normalization
- `matching_config.yaml` — text/Vision models and retry settings
