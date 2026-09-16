# Matching

Matching verifies whether a canonical `InputItem` and one qualified scraping `ProductData`
describe the same exact SKU.

```python
from src.matching import verify_product

result = await verify_product(item, product, vision_enabled=False)
```

Decision order (GTIN short-circuit, brand/numeric/multipack rules, Vision-as-context, one
routed text-LLM decision, fail-closed on ambiguity) is business logic, not operator config —
see [`docs/matching/design.md`](../../docs/matching/design.md) for the full rationale. A
model/parse failure raises `MatchingError` rather than returning a business No Match.

Use `verify_products()` for batches so image downloads are deduplicated across pairs and text
decisions overlap:

```python
from src.matching import verify_products

results = await verify_products([(item1, product1), (item2, product2)], vision_enabled=True)
```

Persisted decision history (`matching_decisions`) is not stored by this module — the
orchestrator writes it into `orchestrator.db`. See
[`docs/orchestrator/storage.md`](../../docs/orchestrator/storage.md) for that schema.

## Setup

Vision preprocessing depends on `image_load_compression`, which is **not** installed from
PyPI — it is a local editable sibling package (see `pyproject.toml`'s
`[tool.uv.sources]`: `image-load-compression = { path = "../image-load-compression",
editable = true }`). A fresh clone needs `../image-load-compression` checked out as a sibling
directory next to this repo before `uv sync` can resolve it. Without that sibling checkout,
`vision_enabled=True` calls fail at import time; `vision_enabled=False` calls do not need it.

## Configuration (`matching_config.yaml`)

```yaml
llm:
  model: deepseek-v4-flash   # text-decision model; routed via src/common/llm_router_config.yaml
  temperature: 0.1
  timeout_s: 60
  max_retries: 2
  concurrency: 16            # in-flight text-LLM decisions per verify_products() call

vision:
  model: qwen3.8-flash       # Vision model; also routed via src/common/llm_router_config.yaml
  max_considered_num_input_image: 2      # InputItem.image_urls
  max_considered_num_scraping_image: 2   # ProductData.image_urls
```

- **`llm.model`** — text-decision model id, resolved by keyword match against
  `src/common/llm_router_config.yaml` (shared with Search and Scraping).
- **`llm.temperature`**, **`llm.timeout_s`**, **`llm.max_retries`** — sampling and retry
  settings for the text-decision call.
- **`llm.concurrency`** — bound on in-flight text-LLM decisions within one `verify_products()`
  call. Must be an integer >= 1. `verify_products()` accepts `concurrency=` to override the
  file per call — callers that already bound their own fan-out (the orchestrator passes its
  `--concurrency`) should pass it here too rather than relying on the file default. The code's
  own fallback if this key is entirely absent from the file is `8`; the current checked-in
  value above (`16`) is what actually runs unless you override it.
- **`vision.model`** — Vision model id, also resolved via `src/common/llm_router_config.yaml`.
- **`vision.max_considered_num_input_image`** / **`max_considered_num_scraping_image`** — per
  side cap on how many images are sent to Vision. Each side's URLs are deduplicated in order,
  then the leading `min(len(urls), cap)` are used; both caps must be integers >= 1. These
  override `image_load_compression`'s own shared `compare.max_images_per_set`, so values above
  its default of 6 are honoured rather than silently truncated. `verify_product()` /
  `verify_products()` accept `max_considered_num_input_image` and
  `max_considered_num_scraping_image` to override the file per call.

Provider routing (base URL, API key env var) shared with Search and Scraping lives in
`src/common/llm_router_config.yaml`, not here.
