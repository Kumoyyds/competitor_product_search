# Matching

Matching verifies whether a canonical `InputItem` and one qualified scraping `ProductData` describe the same exact SKU.

```python
from src.matching import verify_product

result = await verify_product(item, product, vision_enabled=False)
```

The decision order is valid equal GTIN → confirmed variant conflict → one final routed LLM prompt. Missing/invalid GTIN is unknown; a different valid GTIN is strong context but not an automatic failure. Brand and numeric evidence reuse Search semantics: discrete values are exact and continuous values allow ±10% after unit normalization. Multipacks use separate per-item/count/total slots.

Vision is batch-controlled and off by default. When enabled with images on both sides, `image_load_compression.compare_batch()` contributes visual observations to the same final prompt. Missing images or Vision failures fall back to text evidence.

Each side has its own cap on how many images are sent, set in `matching_config.yaml`:

```yaml
vision:
  max_considered_num_input_image: 2      # InputItem.image_urls
  max_considered_num_scraping_image: 2   # ProductData.image_urls
```

Each side's URLs are deduplicated in order, then the leading `min(len(urls), cap)` are used; both caps must be integers >= 1. These override `image_load_compression`'s own shared `compare.max_images_per_set`, so values above its default of 6 are honoured rather than silently truncated. `verify_product()` / `verify_products()` accept `max_considered_num_input_image` and `max_considered_num_scraping_image` to override the file per call.

Batched text decisions run concurrently under one bound, set in `matching_config.yaml`:

```yaml
llm:
  concurrency: 8   # in-flight text-LLM decisions per verify_products() call
```

It must be an integer >= 1; `verify_products()` accepts `concurrency=` to override the file per call. Vision still completes for the whole batch before any text decision starts, because the text prompt consumes the visual comment. Callers that already bound their own fan-out (the orchestrator passes its `--concurrency`) should pass it here too rather than relying on the file default.

Configuration lives in `matching_config.yaml`; provider routing shared with Search lives in `src/common/llm_router_config.yaml`. A model/parse failure raises `MatchingError` rather than returning a business No Match.

Use `verify_products()` for batches so image downloads are deduplicated across pairs and text decisions overlap.
