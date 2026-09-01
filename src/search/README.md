# `search/` — Product URL Matching

## 1. What this is for

Given a product name and a target marketplace (Tesco / Argos / Amazon), this module decides whether that product exists on the marketplace and returns the matching listing URL.

It replaces the old "let an LLM agent pick a URL" approach with a **5-layer pipeline** that filters candidates progressively and only invokes the LLM on the small set that survives cheap rule-based filters. Design rationale and full spec: [search_link_algorithm_spec.md](search_link_algorithm_spec.md).

### How it works

```
search  →  domain_filter  →  base_match  →  distinguishing  →  aggregate
```

1. **search** — builds provider-specific keyword and/or `site:` queries, fires them concurrently at the first provider in the chain (DuckDuckGo / Serper / custom), and dedups results by URL.
2. **domain_filter** — drops candidates whose host isn't the target marketplace (e.g. only keep `*.tesco.com` when targeting Tesco).
3. **base_match** — for each surviving candidate, compares **brand** (rapidfuzz against `brand.xlsx`, three-state pass/fail/unknown) and **numeric attributes** (volume, weight, count, ABV, storage… extracted via quantulum3 + regex). Mismatches kill the candidate; missing info passes through as `unknown`.
4. **distinguishing** — one batched call to the configured LLM decides which surviving candidate (if any) is the same SKU, catching variant differences the rules miss (flavour, colour, version, pack size).
5. **aggregate** — picks the verdict (`match` / `no_match`) and the per-layer trace.

Short-circuiting: whenever a layer kills every candidate, the pipeline skips straight to `aggregate`. The LLM is never called when cheap rules already settled the question.

Each candidate carries a `LayerTrace` showing exactly where the decision happened — useful for debugging and tuning.

---

## 2. Basic workflow

### Batch mode (full input file)

```powershell
# 1. create the Python 3.12 environment and install locked deps (first run)
uv sync --group dev --group notebook

# 2. set API keys in .env at repo root
#    DEEPSEEK_KEY=... or QWEN_KEY=..., depending on llm.model
#    SERPER_KEY=...

# 3. run with explicit per-run arguments
uv run python -m src.search.batch --input input/products.xlsx --sku-col product_name `
    --web-col web --country-col country --output output/results.xlsx
```

### Programmatic (single product)

```python
import asyncio
from src.search import match_product

async def demo():
    result = await match_product(
        product_name="Magic Rock Saucery 4 X 330ML",
        website="tesco",
        country="uk",
    )
    print(result.verdict)                          # FinalVerdict.MATCH / NO_MATCH
    print(result.matched_candidate.url if result.matched_candidate else "not found")
    print(result.layer_trace.to_dict())            # per-layer pass/fail/unknown
    print(result.reason)                           # LLM rationale or pipeline status

asyncio.run(demo())
```

> **In a Jupyter notebook**, the cell is already running inside an event loop, so `asyncio.run(demo())` raises `RuntimeError: asyncio.run() cannot be called from a running event loop`. Replace the last line with top-level await instead:
> ```python
> await demo()
> ```

### Validation (budget-capped sanity check)

```powershell
uv run python scripts/validate_search.py --sample 20 --budget 50
```

Stratified sample from `src/0_Data/tesco_algo.xlsx`, capped at 50 Serper calls. Writes `output/validation_report.xlsx` + prints per-layer verdict mix and agreement-rate vs the legacy URL column.

---

## 3. Input

### Batch mode

- **Where**: any `.xlsx` path passed as `input_file` / `--input`; the path is not prefixed or rewritten
- **Required columns** (name configurable):
  - `item_sku_name_de` (or whatever you pass as `sku_col`) — the product name string
  - `web` (or whatever you pass as `web_col`) — the target marketplace code, such as `tesco` or `amazon`
  - `country` (or whatever you pass as `country_col`) — the general country code (`uk` / `fr` / `de` / `nl`; each provider maps it internally)
- **Arguments**: `sku_col`, `web_col`, `country_col`, optional `output_file`, `serper_max_calls`, `concurrency` (default 16), and `progress`
- **Invalid rows**: a blank/NaN website or country cell becomes a row-level `error` with `url_search_1="not found"`; other rows continue. A missing required column raises `KeyError` before the run starts.

### Accepted `website` values

The website code is looked up in `domain_map` in [maintain/search_config.yaml](maintain/search_config.yaml). Its key is the retailer keyword used by keyword-mode queries; its value is the host used by `site:` queries and `domain_filter`. Matching is case-insensitive.

<!-- BEGIN GENERATED: websites-table -->
| `website` | Host kept by `domain_filter` |
|---|---|
| `tesco` | `tesco.com` (plus subdomains) |
| `argos` | `argos.co.uk` (plus subdomains) |
| `amazon.co.uk` | `amazon.co.uk` (plus subdomains) |
| `amazon.nl` | `amazon.nl` (plus subdomains) |
<!-- END GENERATED: websites-table -->

A `domain_map` value **ending in `.`** is a registrable-name prefix — it matches that name under any TLD. Values without the trailing dot match that exact host and its subdomains only. Look-alikes (`notamazon.de`, `amazon.de.evil.com`) are rejected either way.

An unlisted website code doesn't raise — sitename mode falls back to a keyword query, then every candidate fails `domain_filter` and the row comes back `no_match`. To support a new marketplace, add one `domain_map` entry — no code change needed.

### Accepted `country` values

Pass a plain ISO-3166-1 alpha-2 code (lower- or upper-case); each provider translates it internally — DuckDuckGo to a `region` string, Serper to a `gl` parameter. Codes explicitly mapped by at least one provider:

<!-- BEGIN GENERATED: countries-table -->
| `country` | Country | DuckDuckGo `region` | Serper `gl` |
|---|---|---|---|
| `uk` / `gb` | United Kingdom | `uk-en` | `gb` |
| `de` | Germany | `de-de` | `de` |
| `fr` | France | `fr-fr` | `fr` |
| `us` | United States | `us-en` | `us` |
| `nl` | Netherlands | `nl-nl` | `nl` |
| `jp` | Japan | `jp-ja` | `jp` |
| `es` | Spain | `es-es` | `es` |
| `it` | Italy | `it-it` | `it` |
| `pt` | Portugal | `pt-pt` | `pt` |
| `se` | Sweden | `se-sv` | `se` |
| `pl` | Poland | `pl-pl` | `pl` |
| `br` | Brazil | `br-pt` | `br` |
| `au` | Australia | `au-en` | `au` |
| `ca` | Canada | `ca-en` | `ca` |
<!-- END GENERATED: countries-table -->

An unmapped code doesn't fail — it degrades: DuckDuckGo falls back to `<code>-en` (English results in that country) and Serper forwards the code to Google as-is. Add the country to `_COUNTRY_TO_REGION` in [providers/duckduckgo.py](providers/duckduckgo.py) and `_COUNTRY_TO_GL` in [providers/serper.py](providers/serper.py) when you need the local language instead. Note `country` only steers the search engine; `website` must still name an explicit `domain_map` entry such as `amazon.nl`.

### Accepted LLM vendors

Vendor routing shared with Matching comes from [../common/llm_router_config.yaml](../common/llm_router_config.yaml); the active Search model comes from `llm.model` in [maintain/search_config.yaml](maintain/search_config.yaml).

Migration note: this routing table previously lived at `maintain/llm_router_config.yaml`. If your checkout added custom vendors there, move those entries into the shared file; the old file is no longer read.

<!-- BEGIN GENERATED: llm-table -->
Active model: `deepseek-v4-flash` (routed through `deepseek`).

| Routing keyword | Base URL | Required `.env` key |
|---|---|---|
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `QWEN_KEY` |
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_KEY` |
<!-- END GENERATED: llm-table -->

### Programmatic

Whatever you pass to `match_product(product_name, website, brand=None, country="uk")`. `brand` is optional — if provided it skips brand extraction on the query side.

### Validation script

Reads `src/0_Data/tesco_algo.xlsx` by default; override with `--input`.

### Environment (`.env` at repo root)

- The model API key shown in [Accepted LLM vendors](#accepted-llm-vendors) for the route selected by `llm.model`
- `SERPER_KEY` — google.serper.dev key, only needed if Serper is in the provider chain

---

## 4. Output

### Batch mode

The path supplied as `output_file` / `--output` contains your input with these extra columns appended. The Python API can omit `output_file` and use the returned DataFrame directly.

| Column | Content |
|---|---|
| `url_search_1` | matched product URL, or `"not found"` |
| `match_verdict` | `match` / `no_match` / `error` |
| `match_layer_trace` | JSON: `{"domain": "pass", "brand": "pass", "numeric": "unknown", "distinguishing": "pass"}` — each is `pass`, `fail`, `unknown`, or `null` (layer never reached) |
| `match_reason` | LLM rationale (when distinguishing ran) or short pipeline status text |

### Validation script

`output/validation_report.xlsx` with columns: `product_name`, `legacy_url`, `new_verdict`, `new_url`, `layer_trace_json`, `candidates_considered`, `reason`. Console also prints the per-layer verdict counts and per-provider call counts.

---

## 5. Files to maintain

**All human-edited files live in [`maintain/`](maintain/)** — the only folder you should normally touch when keeping the pipeline current.

| File | When / how to update |
|---|---|
| **[maintain/brand.xlsx](maintain/brand.xlsx)** | Add a row whenever a brand isn't being recognised; remove a row to drop a false-positive brand. Only the `brandname_en` column is read — other columns are ignored. After saving, **restart the Python process** (the brand list is `lru_cache`-d for the lifetime of the process; CLI batch runs start fresh, so this is automatic). What's safe to add: normal brands ("Kopparberg"), short brands ("AEG", "7Up"), digit-bearing brands ("19 Crimes"), and even common English words ("Tropical", "Green") — the multi-brand any-pair-match comparison handles collisions correctly. Pure-numeric brands ("555") work but use sparingly — they may collide with codes/prices in titles. |
| **[maintain/search_config.yaml](maintain/search_config.yaml)** | Tune without touching code. Key sections: `domain_map` (key = retailer keyword, value = accepted host / `site:` value), `search.query_mode`, `search.strip_parens`, `brand.fuzzy_same_threshold` / `fuzzy_differ_threshold` (88 / 40 default), `numeric.continuous_tolerance` (±10%), `numeric.entity_to_attr` + `unit_conversions` + `discrete_attrs` (to support new attributes/units), `llm.model`, and `db`. Restart after editing. |
| **[../common/llm_router_config.yaml](../common/llm_router_config.yaml)** | Shared Search/Matching keyword → `(base_url, key_name)` routing table. Add an entry when introducing a new LLM vendor. |

Per-run job settings are passed directly to `match_product_batch()` or its CLI; there is no per-run YAML file.

The in-memory and Excel entry points use the same internal batch executor. The Excel entry point only validates and converts rows to batch requests, then formats the ordered results back into workbook columns; it does not maintain a second provider/concurrency implementation.

### Common maintenance tasks

| Want to… | Do |
|---|---|
| Recognise a new brand | Append to `maintain/brand.xlsx` `brandname_en`, save, restart |
| Drop a noisy brand | Delete that row in `maintain/brand.xlsx`, save, restart |
| Add a new marketplace | In `maintain/search_config.yaml`, add `domain_map: { <retailer-keyword>: <host> }` |
| Change query construction | Set a provider's `search.query_mode` to `keyword`, `sitename`, or `both`; toggle `search.strip_parens` for parenthesis-free variants |
| Make brand matching stricter / looser | Raise / lower `brand.fuzzy_same_threshold` in `maintain/search_config.yaml` |
| Allow more slop in weights/volumes | Raise `numeric.continuous_tolerance` |
| Support a new unit (e.g. `floz`) | Add it under the relevant attribute in `numeric.unit_conversions` |
| Support a brand-new numeric attribute | Add entry to `numeric.entity_to_attr` + `unit_conversions` + decide discrete-vs-continuous in `numeric.discrete_attrs` |
| Switch LLM model/vendor | Edit `llm.model` in `maintain/search_config.yaml`; add new vendor routing in `src/common/llm_router_config.yaml`. |

### Things that drift over time

- **Brand list** is the main one — new SKUs from new brands appear constantly. Periodically scan rows where `match_layer_trace` shows `"brand": "unknown"` despite a brand-looking token in the product name, and add those brands.
- **Numeric mapping** (`entity_to_attr` + `unit_conversions`) is fine for groceries / beverages / basic electronics. Adding a new category (e.g. screen sizes for monitors, voltages for batteries) means adding entries here.
- **Domain map** needs an entry per new marketplace you support.
- **Generated capability regions** in this README and the root README are refreshed from provider code and the maintained YAML files by `scripts/gen_capability_docs.py`; hand-edits inside `BEGIN/END GENERATED` markers are overwritten.

### Validating after a maintenance edit

```powershell
uv run pytest
```
Tests skip cleanly if a referenced brand was removed from `brand.xlsx` — so brand-list edits won't break the suite.

### What does NOT need maintenance

- Code under `layers/`, `providers/`, `graph.py`, `pipeline.py` — only edit when changing algorithm behaviour.

---

## 6. Script map

```
[batch.py] ──→ [pipeline.py] ──→ [graph.py] ──→ [layers/search] ──→ [layers/query_builder]
   │               │                │              │
   │               │                │              └──→ [providers/serper]  (or [providers/duckduckgo])
   │               │                │
   │               │                ├──→ [layers/domain_filter]
   │               │                │
   │               │                ├──→ [layers/base_match] ──→ [layers/brand]
   │               │                │         │                    └──→ [utils.py]
   │               │                │         └──→ [layers/numeric]
   │               │                │
   │               │                ├──→ [layers/distinguishing]
   │               │                └──→ [layers/aggregate]
   │               │
   │               ├──→ [config.py] ──→ maintain/search_config.yaml
   │               ├──→ [models.py]
   │               └──→ [providers/__init__] ──→ [providers/serper]
   │                                             ├──→ [providers/duckduckgo]
   │                                             ├──→ make_provider()
   │                                             └──→ make_provider_chain()
   │
   └──→ [providers/__init__]  (make_provider_chain for shared budget chain)

[utils.py] ──→ maintain/brand.xlsx
```

Key: `──→` = imports/calls. `graph.py` wires the 5 layers via LangGraph conditional edges; `base_match` delegates brand+numeric to separate files and never imports `distinguishing`.

---

## Storage

Search run/task tracing is stored in `search.db` by default. See [Search storage reference](../../docs/search_storage.md) for the authoritative column definitions, constraints, relationships, JSON shapes, views, compatibility behavior, and example queries. The generated schema regions are rebuilt from `db.py`; do not edit them by hand.

---

## File map (quick)

| Path | Purpose |
|---|---|
| [pipeline.py](pipeline.py) | Public `match_product(...)` entrypoint; standalone calls create `mode=single` traces by default |
| [batch.py](batch.py) | Public file `match_product_batch(...)`, typed `match_products(...)`, and file CLI |
| [db.py](db.py) / [trace.py](trace.py) | SQLite run/task persistence and task-local trace collection |
| [graph.py](graph.py) | LangGraph wiring of the 5 layers |
| [layers/](layers/) | One file per layer (`search`, `domain_filter`, `brand`, `numeric`, `base_match`, `distinguishing`, `aggregate`) + `query_builder` |
| [providers/](providers/) | `DuckDuckGoProvider` (active, free) + `SerperProvider` (active, paid) — chainable; add new search providers here |
| [models.py](models.py) | Data classes (`MatchResult`, `LayerTrace`, `CandidateEval`, …) |
| [config.py](config.py) | Loader for `maintain/search_config.yaml`; delegates LLM routing to `src/common` |
| [utils.py](utils.py) | Brand-set loader + word-boundary literal matcher (reads `maintain/brand.xlsx`) |
| [maintain/](maintain/) | **Maintained files** — `brand.xlsx` + `search_config.yaml`. Provider routing is shared under `src/common`. |
| [search_link_algorithm_spec.md](search_link_algorithm_spec.md) | Full design rationale |
| [CLAUDE.md](CLAUDE.md) | Code-internals reference for AI assistants / developers |

Unit tests live at [tests/unit/search/](../../tests/unit/search/). Run `uv run pytest` for the default offline, zero-cost suite. Use `uv run pytest -m live` only when API-backed tests are intended; they require keys and may incur cost.
