# Remove `unit_price` / `unit` from `ProductData` + guard the API route against unit-price contamination

## Context

`ProductData` currently carries `unit_price: Optional[Decimal]` and `unit: Optional[str]`
([product_data.py:65-66](src/scraping/models/product_data.py#L65-L66)). Two reasons to drop them:

1. **Derivable.** Unit price = price ÷ pack size, and pack size is already captured in
   `variant` (`{"size": ..., "pack_qty": ...}` — already in `SCHEMA_HINT`
   [prompts.py:24](src/scraping/repair/prompts.py#L24)) or recoverable from the title.
   Storing it duplicates information the downstream matcher can compute.
2. **Doesn't scale with the M20 price contract.** With `price`, `list_price`, and
   `membership_price`, a correct design needs `unit_price`, `list_unit_price`, and
   `membership_unit_price`. A single `unit_price` is ambiguous about which price it derives
   from; three fields is complexity for no current consumer.

**No consumer exists today.** Only Amazon's API route populates the fields; nothing reads them
for matching, scoring, or output.

**But removal alone opens a hole on the API route.** Deleting the field does not delete the
upstream data — BrightData still returns `buybox_prices.unit_price = '668,26€ / kg'`, and it
stays visible in `ProductData.raw`. The restricted JSON healer
([json_healer.py](src/scraping/repair/json_healer.py)), shared by **all** `DirectAPIScraper`
subclasses including the DCA backups, feeds every JSON key to the LLM via
`_summarize_json_keys` and asks it to fill missing fields. D25 layer 3 only validates that the
proposed dotted path **resolves to a non-None value** — so a mapping of
`{"price": "buybox_prices.unit_price"}` passes today and writes `"668,26€ / kg"` into `price`.
Removing `unit_price` as a legal target makes this *more* likely, because the key loses its
legitimate home and the model will look for somewhere to put it. So this change also adds a
deterministic guard: unit-price-shaped sources may never feed `price`, `list_price`, or
`membership_price`.

**Explicitly out of scope (decided):** the pre-pass unit-price *filter* stays. `_UNIT_PRICE_RE`,
`source="dom_unit"` routing, `PriceContext.unit_price_evidence`, and the
`[UNIT PRICE EVIDENCE (not product prices)]` prompt block are a **negative** signal keeping £/kg
out of `price` on the HTML route — the same protection this plan adds on the API route.
[prepass.py:1292](src/scraping/repair/prepass.py#L1292) is a live filter in promotion-container
scoring. `verify_m14.py`'s `unit_price_evidence` assertions remain valid and untouched.

## Scope facts (verified)

- **No DB change.** `unit_price` is never a column — `ProductData` is stored as an opaque JSON
  blob in `results.product_data` / `golden_samples.expected_output`. There is no migration
  framework, and `scraping.db` currently has 0 rows in `results`, `golden_samples`, and
  `parsers`, so no data or stored-parser fallout.
- **No hand-written serializers.** All persistence goes through `model_dump` /
  `model_dump_json`, so removal propagates automatically.
- **Cold-start review panel self-updates.** `_display_fields()`
  ([coldstart.py:658-676](src/scraping/coldstart.py#L658-L676)) derives from
  `ProductData.model_fields` minus tracing fields; `_PAGE_TYPE_KEY_FIELDS` does not list them.
- **The DCA backups are already safe by construction.**
  [tesco_dca.py](src/scraping/scrapers/sites/tesco_dca.py) and
  [argos_dca.py](src/scraping/scrapers/sites/argos_dca.py) read named keys only
  (`current_price` / `original_price` / `price` / `list_price`) through `_to_decimal()`, which
  returns `None` on any non-numeric string. No code change needed there — their exposure is
  solely via the shared healer, which §7 closes.

## Changes

### 1. Model — delete the fields

[src/scraping/models/product_data.py:65-66](src/scraping/models/product_data.py#L65-L66) — remove
both lines from the price block. Nothing else in the file references them (`_sanitize_availability`
touches `availability_raw` only).

### 2. Amazon API mapping — the only producer

[src/scraping/scrapers/sites/amazon_uk.py](src/scraping/scrapers/sites/amazon_uk.py):
- Delete `_parse_unit_price()` (L24-35) — dead once the fields go.
- Delete the `unit_price, unit = _parse_unit_price(...)` call (L61-63).
- Delete `"unit_price": unit_price,` and `"unit": unit,` from the `_map_fields` return (L107-108).

`"raw": json_data` stays as-is (debug field, by design) — which is exactly why §7 is needed.

### 3. Prompts — remove the fields, **keep the negative instruction**

[src/scraping/repair/prompts.py](src/scraping/repair/prompts.py):
- `SCHEMA_HINT` L29-30: delete the `unit_price` and `unit` OPTIONAL entries. `SCHEMA_HINT` feeds
  both `parser_gen_prompt` (L207) and `json_heal_remap_prompt` (L491), so this covers both routes.
- L242-243 in `parser_gen_prompt` — **rewrite, do not delete**. The bullet currently says unit
  prices are not product prices *and* tells the model where to park them. Keep the first half:

  ```
  "- Unit prices (e.g. £/kg, /litre, /100g) are NOT product prices — ignore them "
  "entirely; never put them in `price`, `list_price`, or `membership_price`.\n"
  ```

  Deleting it outright would remove the only prompt-level instruction preventing a £/kg value
  from landing in `price`.
- Leave L250 (`unit suffixes ('/litre')` in ROBUST PRICE EXTRACTION) and L343-347
  (`_render_price_context` unit-price evidence block) unchanged — filter, not field.

### 4. Field name lists (three literal-string lists)

Remove `"unit_price"` and `"unit"` from:
- [repair/golden.py:266](src/scraping/repair/golden.py#L266) — `_matches_expected` `compare_fields`.
- [repair/agent.py:110](src/scraping/repair/agent.py#L110) — `summarize_capture` `optional` list.
- [repair/json_healer.py:131](src/scraping/repair/json_healer.py#L131) — `_extract_missing_fields`
  `key_fields`. Bonus: this set is substring-matched (`if field in err.lower()`), so dropping the
  bare `"unit"` also stops false hits on "unittest" / "unit price". An error mentioning
  "unit price" still contributes `"price"`, which is correct.

### 5. Tests (existing)

- [tests/verify_m4_m5.py](src/scraping/tests/verify_m4_m5.py) — delete the `unit_price` print
  (L63) and the two assertions on `mapped["unit_price"]` / `mapped["unit"]` (L77-78). **Add**
  `check("unit_price not mapped", "unit_price" not in mapped and "unit" not in mapped)` so the
  removal is pinned, not merely untested.
- [tests/verify_m19.py:320](src/scraping/tests/verify_m19.py#L320) — drop `"unit_price"` from the
  `("membership_price", "image_urls", "unit_price", "availability_raw")` tuple; it would fail once
  the field leaves `model_fields`.
- [tests/verify_m12.py](src/scraping/tests/verify_m12.py) — remove the report dataclass fields
  (L176-177), the population block reading `result.unit_price` / `result.unit` (L356-357, would
  `AttributeError` on a live run), and the report-printing block (L460-464).
- [tests/verify_m14.py](src/scraping/tests/verify_m14.py) — **no change**.

### 6. Docs

- [scraping_module_spec_v1_2.md:243-244](src/scraping/scraping_module_spec_v1_2.md#L243-L244) —
  delete the two schema-table rows from §5.1 (this is the live spec).
- [tests/README.md](src/scraping/tests/README.md) — L36 (M4 field list) and L201 (M12 per-URL
  report field list): drop the `unit_price` / `unit` mentions; add the `verify_m22.py` row (§8).
- `src/scraping/CLAUDE.md` — add the M22 row to the milestone table and a short M22 section.
  Edit either `CLAUDE.md` or `AGENTS.md`; the pre-commit hook (`scripts/sync_agent_docs.py`)
  syncs the byte-identical sibling.
- Leave alone: `scraping_module_spec_v1_1.md` (superseded), `scripts/plans/*.md` (historical),
  captured `verify_m*_output.log` (run artifacts), `data/html_sample/*.html` (fixtures), and the
  stored output cells in `scripts/check_database.ipynb` (stale rendered DataFrames; source cells
  are generic `SELECT *` + `json_normalize`).
- Top-level `CLAUDE.md` / `AGENTS.md` / `README.md` / `docs/` have no mention.

### 7. **New — API-route unit-price contamination guard** (the substantive addition)

Two layers, mirroring the existing D25 defence-in-depth style.

**7a. Deterministic code guard** — the one that actually holds. In
[repair/json_healer.py](src/scraping/repair/json_healer.py), add module-level constants and one
predicate:

```python
_PRICE_TARGETS = {"price", "list_price", "membership_price"}

# Source-key names that denote a per-unit rate, not a product price.
_UNIT_PRICE_KEY_RE = re.compile(
    r"(?i)(unit[_\-\s]?price|price[_\-\s]?per[_\-\s]?unit|per[_\-\s]?unit|unit[_\-\s]?cost|\bppu\b)"
)

# Value shapes like '668,26€ / kg', '£1.50/100g', '2.99 GBP per litre'.
# Deliberately NOT imported from repair/prepass.py: that regex targets DOM text with a
# leading currency symbol, whereas API payloads put the symbol after the number.
_UNIT_PRICE_VALUE_RE = re.compile(
    r"""(?ix)
    \d[\d.,]*\s*[£€$]?\s*(?:GBP|EUR|USD)?\s*(?:/|\bper\b)\s*
    (?:kg|g|l|ltr|litre|ml|cl|100\s*g|100\s*ml|oz|lb|each|unit|item|sheet)
    """
)


def _is_unit_price_source(target: str, source_path: str, value: Any) -> bool:
    """True when a price target is about to be fed from a per-unit (£/kg) source."""
    if target not in _PRICE_TARGETS:
        return False
    if _UNIT_PRICE_KEY_RE.search(source_path or ""):
        return True
    return bool(isinstance(value, str) and _UNIT_PRICE_VALUE_RE.search(value))
```

Call it in `heal_json`'s D25 validation loop (after `value = _lookup_path(...)` resolves
non-None, [json_healer.py:80-87](src/scraping/repair/json_healer.py#L80-L87)):

```python
if _is_unit_price_source(target, source_path, value):
    logger.warning(
        "json_heal: refusing unit-price source %r for price field %s (value=%r)",
        source_path, target, value,
    )
    continue
```

`continue` — skip that one target, keep the rest of the mapping. Do **not** `return None`: the
other proposed targets may be legitimate, and dropping a bad `price` correctly lets Gate 2 fail
into escalation rather than persisting a wrong number.

Also apply it in `DirectAPIScraper._apply_heal_cache`
([api_scraper.py:127-143](src/scraping/scrapers/api_scraper.py#L127-L143)), which replays a cached
mapping and already imports `_lookup_path` from the same module — import `_is_unit_price_source`
alongside it and skip contaminated entries. `_cache_heal` is a Phase-0 no-op today, so the cache
is never populated; guarding now means the protection already holds when Phase 1 implements it.

**7b. Prompt guard** — in [prompts.py](src/scraping/repair/prompts.py), one rule added to each
healer prompt:
- `json_heal_remap_prompt` system CRITICAL RULES (after L489): *"Unit-price keys (`unit_price`,
  `price_per_unit`, `unit_cost`) and per-unit values like `'668,26€ / kg'` are NOT product prices.
  NEVER map them to `price`, `list_price`, or `membership_price` — omit them from the mapping."*
- `json_heal_precheck_prompt` system (after L468): *"A per-unit rate (£/kg, /litre) is not a
  product price — if the only price-like data in the JSON is a unit price, answer
  `source_absent`."* Without this the precheck greenlights a heal that 7a then strips, burning an
  LLM call.

### 8. New verification — `verify_m22.py`

Per the module's mandatory verification discipline, add
`src/scraping/tests/verify_m22.py` (offline, no network, no LLM — mock or call the helpers
directly), named checks with `[PASS]`/`[FAIL]`, `SUMMARY: N passed, M failed`, non-zero exit:

- `ProductData.model_fields` contains neither `unit_price` nor `unit`; constructing
  `ProductData(..., unit_price=...)` does not surface the field.
- Amazon `_map_fields()` on the existing M4 fixture returns no `unit_price` / `unit` keys while
  `price` / `list_price` / `currency` are unchanged (`23.79` / `27.99` / `EUR`).
- `_is_unit_price_source` truth table: blocks `("price", "buybox_prices.unit_price", ...)`,
  `("list_price", "x.price_per_unit", ...)`, and value-shaped hits `'668,26€ / kg'`,
  `'£1.50/100g'`, `'2.99 GBP per litre'`; allows `("price", "buybox_prices.final_price", "23.79")`,
  `("membership_price", "prime_price", "19.99")`, and any non-price target.
- `heal_json` with a stubbed LLM proposing `{"price": "buybox_prices.unit_price"}` returns a
  healed dict whose `price` is **still** the pre-heal value (mapping skipped, warning logged),
  and a second stub proposing a legitimate path still heals.
- `_apply_heal_cache` with a poisoned cache entry leaves `price` untouched.
- `SCHEMA_HINT` no longer offers `unit_price` / `unit` as targets, while `parser_gen_prompt` still
  contains the "NOT product prices" instruction and both healer prompts contain the new rule.

Capture the log: `python src/scraping/tests/verify_m22.py | tee src/scraping/tests/verify_m22_output.log`.

## Verification

```bash
# 1. No live references remain outside the deliberately-kept prepass filter,
#    the ROBUST-EXTRACTION prompt text, the new guard, and historical docs/logs/fixtures.
grep -rn "unit_price\|\bunit\b" src/ --include="*.py" | grep -v "prepass.py\|verify_m14\|verify_m22\|json_healer.py"

# 2. Model no longer exposes the fields.
python -c "from src.scraping.models.product_data import ProductData; \
           assert not {'unit_price','unit'} & set(ProductData.model_fields); print('OK')"

# 3. New milestone suite.
python src/scraping/tests/verify_m22.py | tee src/scraping/tests/verify_m22_output.log

# 4. Offline suites touching changed code — all must stay green.
python src/scraping/tests/verify_m4_m5.py   # Amazon _map_fields (fixture-based)
python src/scraping/tests/verify_m14.py     # prepass filter unchanged: expect 41/41
python src/scraping/tests/verify_m15.py     # promotion/gates unchanged: expect 44/44
python src/scraping/tests/verify_m19.py     # review panel field derivation
python src/scraping/tests/verify_m20.py     # price-field contract
python src/scraping/tests/verify_m21.py     # cold-start repair loop

# 5. Live-run script edited but not executed (real BrightData + Qwen) — compile-check only.
python -m py_compile src/scraping/tests/verify_m12.py
```

Each suite ends with `SUMMARY: N passed, M failed` and exits non-zero on failure.

## Known trade-off accepted by this change

Deleting `unit_price` does not just move the computation downstream — for **variable-weight
groceries** (Tesco fresh produce, meat, cheese) the displayed £/kg is authoritative and the
title carries only an approximate weight ("approx. 450g"), so that number becomes
unrecoverable. This is accepted deliberately: there is no consumer today, and the field is
cheap to reintroduce (JSON blob storage, no migration).

## Follow-up (explicitly deferred, not in this change)

**Pack-size capture has no guarantee today.** `variant` is a free-form `Optional[dict]`; `size` /
`color` / `pack_qty` are convention only:

- `variant["size"]` / `["color"]` — written **only** by
  [amazon_uk.py:73-76](src/scraping/scrapers/sites/amazon_uk.py#L73-L76); read by nothing.
- `variant["pack_qty"]` — **written by no production code**, yet read by
  `classify_page_type` ([golden.py:69-72](src/scraping/repair/golden.py#L69-L72)) to decide the
  `multipack` bucket. So multipack classification only works when an LLM parser happens to fill
  it. Pre-existing gap, unrelated to this change.
- Both DCA scrapers never set `variant` at all.
- Amazon mis-maps `variant_attributes` `'60 Count'` into `variant["size"]` (it is a count →
  `pack_qty`), because the attribute is named "Größe".

A future change should type `Variant` as a sub-model and backfill `size` / `pack_qty`
**deterministically from `title`** via a pure function called from a `model_validator` — the same
single-choke-point pattern as `_normalize_availability`
([product_data.py:77-86](src/scraping/models/product_data.py#L77-L86)) — rather than adding
requirements to the parser prompt. Rationale for keeping it out of the LLM's hands:
`variant` is **already** in golden `compare_fields`
([golden.py:264](src/scraping/repair/golden.py#L264)) and in `summarize_capture`'s optional list
([agent.py:107-110](src/scraping/repair/agent.py#L107-L110)), so pushing the parser harder on
`variant` content raises golden-rejection and repair-round counts — and per the M12 Qwen run,
4 of 6 agent repairs already won only on the final ladder attempt. Locale note: SI symbols are
language-invariant, but `hosts.yaml` registers `amazon.de` / `amazon.fr` and `config.yaml`
currently targets `amazon.de`, so any such parser needs decimal-comma handling plus a small
EN/DE/FR multipack keyword table, and must whitelist mass/volume/count units only (excluding
cm/inch) to avoid false positives on Argos general merchandise.
