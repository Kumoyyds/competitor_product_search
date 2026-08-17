# `src/search/` — product-URL matching pipeline

Implements [`search_link_algorithm_spec.md`](search_link_algorithm_spec.md). Given `(product_name, website, brand?)`, decide whether the product exists on the target marketplace and return the matching URL.

## Entry point

```python
from src.search import match_product   # async

result = await match_product("Magic Rock Saucery 4 X 330ML", "tesco", country="uk")
# result.verdict        : FinalVerdict.MATCH | NO_MATCH
# result.matched_candidate.url
# result.layer_trace    : per-layer pass/fail/unknown/None
# result.reason         : LLM rationale or pipeline status text
```

`pipeline.match_product` accepts a shared `SearchProvider` (or list of them) so a single budget counter can span many calls. `batch.py` and `scripts/validate_search.py` both rely on this. A standalone call creates its own `mode=single` DB run unless `record=False` or DB tracing is disabled.

## Pipeline shape (LangGraph)

```
search  →  domain_filter  →  base_match  →  distinguishing  →  aggregate
   │              │                │                │
   └──short-circuit when alive == 0 (or zero search results) ──→ aggregate
```

**base (brand+numeric) and distinguishing (LLM) are physically decoupled** — they live in separate modules and never import each other. Communication is only via the orchestrator state dict. Each candidate carries a `LayerTrace` (4 fields, `pass / fail / unknown / None`) and an `alive` flag. When every candidate dies at a layer, conditional edges in [graph.py](graph.py) skip downstream layers — most importantly the LLM call.

## Configuration

| Config | Location | What it controls |
|--------|----------|-----------------|
| **Pipeline config** | [maintain/search_config.yaml](maintain/search_config.yaml) | Thresholds, domain map, unit conversions, LLM model, cache path. **Edit for tuning, not per-run.** |
| **LLM router config** | [maintain/llm_router_config.yaml](maintain/llm_router_config.yaml) | Keyword → `(base_url, key_name)` table. Switching LLM vendor/model is a single-line edit to `llm.model` above; add a vendor entry here only when introducing a brand-new vendor. |

Per-run settings are arguments to `match_product()` / `match_product_batch()` or flags to `python -m src.search.batch`. There is no per-run YAML. Pipeline internals read `maintain/search_config.yaml` via `config.py`.

## File map

| File | Role |
|---|---|
| [pipeline.py](pipeline.py) | compiles the graph once at import; `match_product()` public entrypoint |
| [batch.py](batch.py) | parameterized `match_product_batch()` API and flag-only CLI |
| [db.py](db.py) / [trace.py](trace.py) | shared run/task scopes and SQLite trace persistence |
| [graph.py](graph.py) | LangGraph StateGraph wiring + conditional short-circuit edges |
| [models.py](models.py) | `Verdict`, `FinalVerdict`, `LayerTrace`, `BaseAttributes`, `RawCandidate`, `CandidateEval`, `MatchResult` |
| [config.py](config.py) | yaml loader (reads `maintain/search_config.yaml`) + `domain_for()` / `query_mode_for()` / `strip_parens_enabled()` helpers + `resolve_llm_route()` (keyword-routes `llm.model` via `maintain/llm_router_config.yaml`) |
| [utils.py](utils.py) | brand-set loader (reads ONLY `brandname_en` from `maintain/brand.xlsx`), `find_literal_brands()`, accent stripping |
| [maintain/](maintain/) | **Human-edited files**: `brand.xlsx` + `search_config.yaml`. See README §5 for the how-to. |
| [providers/](providers/) | `SearchProvider` abstract + `DuckDuckGoProvider` (active, free, rate-limit retry) + `SerperProvider` (active, paid, aiohttp). Both have internal `_COUNTRY_TO_*` mappings. Add new ones by subclassing `base.SearchProvider`. |
| [cache.py](cache.py) | SQLite cache keyed on extractor version + country + title for base extraction. Default path `.cache/base_extraction.sqlite`. |
| [layers/query_builder.py](layers/query_builder.py) | Builds provider-specific `keyword` / `sitename` / `both` queries plus optional parenthesis-free variants. Missing domain mappings make sitename mode fall back to keyword. |
| [layers/search.py](layers/search.py) | search node — fans variants out with `asyncio.gather`, dedups by URL, surfaces `BudgetExhausted` |
| [layers/domain_filter.py](layers/domain_filter.py) | host-vs-`domain_map` and configured product-path match; sets `domain=pass/fail`, kills wrong-host and gallery/category candidates. A `domain_map` value ending in `.` is a registrable-name prefix matching any TLD (`amazon.` → `amazon.de` / `amazon.co.uk`); without it, exact host + subdomains |
| [layers/url_rules.py](layers/url_rules.py) | strips configured tracking query parameters before candidate dedup and validates single-product URL shapes for configured websites |
| [layers/brand.py](layers/brand.py) | three-state brand. `extract_brands()` returns **all literal matches in order of first appearance** — falls back to single fuzzy result, then single uppercase-first-token heuristic. `compare_brands(list, list)` uses **any-pair-pass / all-pairs-differ-fail** so noise tokens in `brand.xlsx` (e.g. "Tropical" inside "Tetley Tropical Tea") don't drown out the real brand. |
| [layers/numeric.py](layers/numeric.py) | Regex pre-pass for ABV, pack/count, and screen-inch patterns; then quantulum3 fills volume / weight / storage / power / voltage / charge / length. Comparison: discrete-equal, continuous ±10%. |
| [layers/base_match.py](layers/base_match.py) | per-candidate brand+numeric via `asyncio.to_thread`; reads/writes `cache.BaseExtractionCache` |
| [layers/distinguishing.py](layers/distinguishing.py) | **single batched** routed-LLM call; returns `{match_idx, reason}` JSON. Never asks the LLM for self-reported confidence. |
| [layers/aggregate.py](layers/aggregate.py) | picks `MATCH` (any candidate with `distinguishing=pass`) or `NO_MATCH` (representative trace = deepest-reached candidate) |

## Key invariants

- **Three-state semantics** — brand and numeric layers only `FAIL` when *confirmed different*. Missing data → `UNKNOWN`, never `FAIL`. LLM in distinguishing makes the final call.
- **`site:` is provider-controlled** — `search.query_mode` selects `keyword`, `sitename`, or concurrent `both` per provider; unconfigured providers default to `keyword`.
- **`BRANDS_FUZZY_SAFE` subset** = brands `len ≥ 4 AND has letter`. Shorter/pure-numeric only match via literal word-boundary regex. Without this guard, fuzzy on "ABC" matches everything.
- **Brand list `lru_cache`-d per process** — edits to `maintain/brand.xlsx` require a Python restart. CLI batch jobs and `validate_search.py` start fresh, so automatic.
- **Every top-level match is traced by default** — standalone calls create one `mode=single` run/task; batch rows reuse the batch task context and do not create nested runs. Pass `record=False` for an unrecorded standalone call.
- **Multi-brand extraction** — `extract_brands()` returns every literal hit, not just the longest/first. `compare_brands()` uses any-pair-pass + all-pairs-differ-fail. Titles where a non-brand word collides with `brand.xlsx` (e.g. "Tetley ... Tropical Tea") are safe.
- **Numeric regex pre-pass before quantulum3** — quantulum3 misses ABV and treats the leading number in `4 X 330ml` as dimensionless. Regex owns `abv_percent` and `count`; quantulum3 fills the rest.
- **Cache key includes `cache.EXTRACTOR_VERSION`** — bump it whenever brand/numeric extraction behavior changes so stale rows become unreachable without a migration.

## Config knobs in maintain/search_config.yaml

| Section | Important keys |
|---|---|
| `search` | `provider` (`serper` / `duckduckgo` as string for single engine, or `[duckduckgo, serper]` as ordered list for fallback chain), `k` (results per query), provider-keyed `query_mode`, `strip_parens` |
| `domain_map` | website name → host. Add new marketplaces here. |
| `url_rules` | tracking-query denylist plus optional website → single-product path regex. Websites without a path rule keep host-only filtering. |
| `brand` | `fuzzy_same_threshold` (default 88), `fuzzy_differ_threshold` (default 40) |
| `numeric` | `continuous_tolerance` (default 0.10), `entity_to_attr`, `unit_conversions`, `discrete_attrs`, `ambiguity_rules` |
| `llm` | `model` (the one line to edit to switch vendor/model, routed via `maintain/llm_router_config.yaml`), `temperature`, `timeout_s` |
| `cache` | `sqlite_path` (default `.cache/base_extraction.sqlite`) |

## Batch arguments

`match_product_batch()` takes the full input/output paths, SKU, website, and country column names, plus optional Serper budget and concurrency. Website and country are resolved per row. Provider-chain strategy remains in `maintain/search_config.yaml` unless the caller supplies providers.

## Running

End-to-end batch:
```
python -m src.search.batch --input input/products.xlsx --sku-col product_name \
    --web-col web --country-col country --output output/results.xlsx
```
The input path is passed unchanged. Blank website/country cells become row-level errors without stopping the batch. Omit `--output` when calling the Python API to return the enriched DataFrame without writing Excel.

Unit tests (offline, mocks Serper + LLM — zero API cost):
```
python -m pytest tests/unit/search/ -v
```

Budget-capped validation against `src/0_Data/tesco_algo.xlsx`:
```
python scripts/validate_search.py --sample 20 --budget 50
```
Writes `output/validation_report.xlsx`. Prints numeric pre-pass, then runs pipeline within the call budget, then per-layer verdict counts and agreement vs legacy `url_search_1`.

## Environment

Required env vars in `.env` at repo root:
- `QWEN_KEY` — DashScope API key for the Qwen LLM (distinguishing layer), required while `llm.model` routes to `qwen` in [maintain/llm_router_config.yaml](maintain/llm_router_config.yaml)
- `SERPER_KEY` — google.serper.dev key, only needed if Serper is in the provider chain
- `DEEPSEEK_KEY` — only needed if `llm.model` is switched to route to `deepseek`

Python 3.12. Deps beyond the old code: `quantulum3`, `rapidfuzz`, `langgraph`, `aiohttp` — all in `requirements.txt`.

## Adding things

- **New search provider**: subclass `providers.base.SearchProvider`, register in `providers/__init__.py::make_provider`, add `search.provider` in `maintain/search_config.yaml`. **Must include an internal `_COUNTRY_TO_*` mapping** (see `SerperProvider._COUNTRY_TO_GL` and `DuckDuckGoProvider._COUNTRY_TO_REGION`) so general country-code arguments (uk, fr, de, nl, ...) translate to whatever format the API expects. Include a helper (`_to_gl`, `_to_region`, etc.) called inside `search()`.
- **New marketplace**: add one `domain_map` entry in `maintain/search_config.yaml`; its key is the keyword query term and its value is the accepted host / `site:` operand. No code change needed.
- **New numeric attribute**: add `entity_to_attr` mapping (or custom regex in `layers/numeric.py::extract_numerics`) + `unit_conversions` table + decide discrete-vs-continuous in `discrete_attrs`.
- **Brand list grows**: append rows to `maintain/brand.xlsx` `brandname_en`. `lru_cache` is per-process; restart picks up new rows. See "Files to maintain" in [README.md](README.md) for the full guide.
