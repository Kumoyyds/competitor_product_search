# URL hygiene + gallery-page rejection in the search pipeline

## Context

Investigation of run `1b82f20e73ee4c4e8a7b8e388a0aed72` (81 tasks, 35 matched, `input/tesco_algos_amazon_test.xlsx`, chain `duckduckgo→serper`) turned up two defects. Both are traced to root cause below; neither has any code guarding against it today.

### Issue 1 — `?srsltid=...` on the matched URL (row 39, task 35)

`matched_url = https://www.tesco.com/shop/en-GB/products/313169581?srsltid=AfmBOorYkZqG...`

**Root cause:** [serper.py:97](src/search/providers/serper.py#L97) copies Google's `item["link"]` verbatim. `srsltid` is Google's per-impression Shopping/free-listing click token. Nothing in the pipeline ever touches a candidate URL — grep for `urlparse` finds only host extraction in [domain_filter.py:39](src/search/layers/domain_filter.py#L39) and [trace.py:232](src/search/trace.py#L232). There is no normalization step anywhere.

**Is it a big problem?** No — not a correctness bug. The URL resolves to the identical product page. But it is worth fixing:
- 4 of 35 matched URLs in this run carry `srsltid`; 117 candidates overall do. Argos row 8 also carries `?utm_custom6=LIA&gStoreCode=5476`.
- The token is impression-scoped, so the *same* product yields a *different* URL string every run — cross-run diffing and downstream dedup of the output Excel break.
- [search.py:46](src/search/layers/search.py#L46) dedups candidates on the exact URL string, so two impressions of one product page would occupy two candidate slots and two LLM lines. (Checked: this did **not** actually happen in this run — the duplicate-base URLs found were genuinely different Amazon listing pages. The risk is latent, not observed.)

### Issue 2 — a gallery page was returned as the match (row 66, task 23)

`matched_url = https://www.amazon.nl/drukspuit-metaal/s?k=drukspuit+metaal` for SKU "Snoerloze Drukspuit". This is an Amazon **search-results** page, not an SKU page. Row 64 has the same defect (`/muesli-bewaardoos/s?k=...`) — 2 of 35 matches, ~6% false-positive rate, both on amazon.nl.

**Root causes, in order of importance:**

1. **The LLM never sees the URL.** `_build_user_msg` ([distinguishing.py:36-56](src/search/layers/distinguishing.py#L36-L56)) emits only `title`, `brand`, `numeric`, `snippet`. The recorded prompt for task 23 confirms it — candidate `[2]` reads `title: Drukspuit Metaal`, which is indistinguishable from a product name. The model cannot tell a gallery page from an SKU page because the one field that would reveal it is withheld.
2. **Serper snippets for Amazon search pages describe one arbitrary product on the page.** Candidate `[2]`'s snippet was `"Stanley Druksproeier 5 L, drukspuit, ... heroplaadbare batterij lithium ..."`, so the LLM answered `"This is a rechargeable lithium-battery cordless pressure sprayer"` — describing a product that is not what that URL shows.
3. **No layer checks URL shape.** `domain_filter` matches host only, so `/s?k=`, `/b?node=`, `/gp/browse`, `/collections/`, tesco `/search?` all sail through. `base_match` then returns `brand=unknown, numeric=unknown` for these titles (three-state semantics never `FAIL` on missing data), so every gallery page arrives alive at the LLM. In this attempt 9 of 9 alive candidates were Amazon pages and 6 of them were gallery/category pages.
4. [search_link_algorithm_spec.md](src/search/search_link_algorithm_spec.md) §layer-2 specifies host matching only — the gap is in the spec too.

**Intended outcome:** candidate URLs are normalized once at ingest, non-product page shapes are killed deterministically before the LLM is paid for them, and the LLM sees the URL as a second net for sites with no shape rule.

## Approach

Deterministic URL rules first (free, no LLM cost, no new pipeline node), LLM prompt hardening second.

Confirmed with the user: gallery pages are **killed** at `domain_filter` (so row 66 correctly becomes `no_match` rather than a wrong URL), and the fix is **forward-only** — no backfill of already-written outputs or existing `search_db.sqlite` rows.

### 1. New module `src/search/layers/url_rules.py`

Two pure helpers, both config-driven with hardcoded fallbacks:

- `clean_url(url: str) -> str` — drops tracking query params, keeps everything else, drops a trailing `?` when nothing remains, preserves path/fragment. Must be idempotent and must **not** strip semantically meaningful params (`k=`, `node=`, `rh=` on Amazon search pages; `th=1`/`psc=1` variant selectors on Amazon SKU pages). Denylist, never allowlist.
- `is_product_url(website: str, url: str) -> bool` — `True` when the site has no configured rule (preserves today's behaviour for unmapped sites), otherwise matches the configured path regex.

### 2. Config in [maintain/search_config.yaml](src/search/maintain/search_config.yaml)

```yaml
url_rules:
  # Tracking params stripped from every candidate URL at ingest.
  # A trailing "*" is a prefix match ("utm_*" covers utm_source, utm_custom6, ...).
  strip_query_params:
    [srsltid, gclid, gbraid, wbraid, dclid, fbclid, msclkid, ttclid,
     igshid, mc_cid, mc_eid, "utm_*", ref, ref_]
  # Path regex a URL must match to count as a single-product page on that site.
  # Websites absent here skip the shape check entirely.
  product_path:
    tesco:        '/products/\d+'
    argos:        '/product/\d+'
    amazon:       '/(dp|gp/product)/[A-Z0-9]{10}'
    amazon.co.uk: '/(dp|gp/product)/[A-Z0-9]{10}'
    amazon.nl:    '/(dp|gp/product)/[A-Z0-9]{10}'
```

Keys mirror `domain_map` keys (the `website` argument). `gStoreCode` is deliberately **not** stripped — it changes which store's page renders; leaving it in config makes that the maintainer's call.

### 3. Apply `clean_url` in `search_node`

[layers/search.py:45-49](src/search/layers/search.py#L45-L49): clean each `raw.url` **before** the `seen_urls` dedup check, so normalization also improves dedup. One place covers every provider present and future — do not patch `SerperProvider` individually.

### 4. Apply `is_product_url` in `domain_filter_node`

[layers/domain_filter.py:37-46](src/search/layers/domain_filter.py#L37-L46): after the host check passes, run the shape check; on failure set `trace.domain = Verdict.FAIL` and `alive = False`.

Deliberately reusing the `domain` trace field rather than adding a fifth layer: a new field would mean changes to `LayerTrace` + `LayerTrace.depth()` ([models.py:20-40](src/search/models.py#L20-L40)), a new `candidates` column and a new graph node/edge — disproportionate. The existing `_after_domain` short-circuit ([graph.py:128](src/search/graph.py#L128)) then skips `base_match` and the LLM for free.

To keep the two rejection kinds distinguishable when debugging, have `domain_filter_node` return `domain_rejects: {"host": n, "not_product_page": n}` in state and have `_instrument` ([graph.py:69-73](src/search/graph.py#L69-L73)) fold it into the `domain_filter` node event's `detail` JSON.

### 5. Harden the distinguishing prompt

[layers/distinguishing.py](src/search/layers/distinguishing.py):
- `_build_user_msg` — add a `url: {c.raw.url}` line per candidate (cheap once tracking params are stripped).
- `_PROMPT_HEADER` — add: *"Each candidate must be a single-product page. Reject any candidate whose URL is a search-results, category, browse, or brand-store page, and never infer the product from a snippet that describes one item on a listing page."*

This is the net for marketplaces with no `product_path` rule; it is not the primary defence.

### 6. Docs

Update the layer-2 row in [src/search/CLAUDE.md](src/search/CLAUDE.md) (and the mirrored `AGENTS.md` — a pre-commit hook syncs them), plus the `domain_filter` section of [search_link_algorithm_spec.md](src/search/search_link_algorithm_spec.md), to state that layer 2 is host **and** URL-shape filtering. Add `url_rules` to the config-knobs table.

No `cache.EXTRACTOR_VERSION` bump — the base-extraction cache is keyed on title/country, not URL.

## Files touched

| File | Change |
|---|---|
| `src/search/layers/url_rules.py` | **new** — `clean_url`, `is_product_url` |
| `src/search/maintain/search_config.yaml` | **new** `url_rules` section |
| `src/search/layers/search.py` | clean URL before dedup |
| `src/search/layers/domain_filter.py` | shape check + `domain_rejects` counts |
| `src/search/graph.py` | fold `domain_rejects` into node-event `detail` |
| `src/search/layers/distinguishing.py` | `url:` line + header sentence |
| `src/search/CLAUDE.md`, `AGENTS.md`, `search_link_algorithm_spec.md` | doc updates |

## Verification

1. **New unit tests** `tests/unit/search/test_url_rules.py`:
   - `srsltid`, `utm_custom6`, `gclid` stripped; `?k=drukspuit+metaal`, `?node=16462610031`, `?th=1` preserved; trailing `?` dropped; `clean_url` idempotent.
   - `is_product_url` — `.../products/313169581` ✓, `.../shop/en-GB/search?q=x` ✗ (tesco); `/dp/B09TRRYFMY` ✓, `/drukspuit-metaal/s?k=...` ✗, `/b?node=...` ✗ (amazon.nl); unmapped website → always ✓.
2. **Extend** `tests/unit/search/test_domain_filter.py` with the same amazon/tesco cases end-to-end through `domain_filter_node`, asserting `trace.domain == FAIL and alive is False` for gallery URLs and that existing tests (`test_domain_filter_passes_amazon_any_tld` uses `/dp/B000` — short ASIN, adjust the fixture to a real 10-char ASIN) still pass.
3. **Extend** `tests/unit/search/test_pipeline_shortcircuit.py`: all candidates being gallery pages must short-circuit to `aggregate` with the LLM never invoked.
4. `python -m pytest tests/unit/search/ -v` — offline, no API cost.
5. **End-to-end on the two regressed rows.** Build a 3-row xlsx from `input/tesco_algos_amazon_test.xlsx` (rows 39, 64, 66) and run:
   ```
   python -m src.search.batch --input <tmp>.xlsx --sku-col <name> \
       --web-col web --country-col country --output output/urlfix_check.xlsx
   ```
   Expect: row 39 → `https://www.tesco.com/shop/en-GB/products/313169581` with no query string; rows 64 and 66 → either a real `/dp/<ASIN>` URL or `no_match` (`no_match` is the correct outcome here — a gallery page is not a match).
6. **Confirm in the trace DB** for the new `run_id`:
   ```sql
   SELECT matched_url FROM tasks WHERE run_id='<new>' AND matched_url LIKE '%srsltid%';   -- 0 rows
   SELECT matched_url FROM tasks WHERE run_id='<new>' AND matched_url LIKE '%/s?k=%';     -- 0 rows
   SELECT node, detail FROM node_events WHERE run_id='<new>' AND node='domain_filter';    -- shows not_product_page counts
   ```
