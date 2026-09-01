# Matching

Matching verifies whether a canonical `InputItem` and one qualified scraping `ProductData` describe the same exact SKU.

```python
from src.matching import verify_product

result = await verify_product(item, product, vision_enabled=False)
```

The decision order is valid equal GTIN → confirmed variant conflict → one final routed LLM prompt. Missing/invalid GTIN is unknown; a different valid GTIN is strong context but not an automatic failure. Brand and numeric evidence reuse Search semantics: discrete values are exact and continuous values allow ±10% after unit normalization. Multipacks use separate per-item/count/total slots.

Vision is batch-controlled and off by default. When enabled with images on both sides, `image_load_compression.compare_batch()` contributes visual observations to the same final prompt. Missing images or Vision failures fall back to text evidence.

Configuration lives in `matching_config.yaml`; provider routing shared with Search lives in `src/common/llm_router_config.yaml`. A model/parse failure raises `MatchingError` rather than returning a business No Match.

Use `verify_products()` for batches so image downloads are deduplicated across pairs.
