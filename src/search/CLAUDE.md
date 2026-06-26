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

`pipeline.match_product` accepts a shared `SearchProvider` so a single budget counter can span many calls — `main.py` and `scripts/validate_search.py` both rely on this.

## Pipeline shape (LangGraph)

```
search  →  domain_filter  →  base_match  →  distinguishing  →  aggregate
   │              │                │                │
   └──short-circuit when alive == 0 (or zero search results) ──→ aggregate
```

Per spec: **base (brand+numeric) and distinguishing (LLM) are physically decoupled** — they live in separate modules and never import each other. Communication is only via the orchestrator state dict.

Each candidate carries a `LayerTrace` (4 fields, `pass / fail / unknown / None`) and an `alive` flag. When every candidate dies at a layer, the conditional edges in [graph.py](graph.py) skip downstream layers — most importantly the LLM call.

## File map

| File | Role |
|---|---|
| [models.py](models.py) | `Verdict`, `FinalVerdict`, `LayerTrace`, `BaseAttributes`, `RawCandidate`, `CandidateEval`, `MatchResult` |
| [maintain/search_config.yaml](maintain/search_config.yaml) | All thresholds / domain map / numeric mapping & unit conversions / LLM model. **Edit here, not in code.** |
| [config.py](config.py) | yaml loader (reads `maintain/search_config.yaml`) + `domain_for()` / `retailer_keyword_for()` helpers |
| [utils.py](utils.py) | brand-set loader (reads ONLY `brandname_en` from `maintain/brand.xlsx`), `find_literal_brands()`, accent stripping |
| [providers/](providers/) | `SearchProvider` abstract + `SerperProvider` (aiohttp, budget-aware) + `DuckDuckGoProvider` placeholder. Add new ones by subclassing `base.SearchProvider`. |
| [layers/query_builder.py](layers/query_builder.py) | Rule-based query variants (raw + retailer keyword, strip parens, optional brand). **No `site:` operator** — domain filtering is its own layer. |
| [layers/search.py](layers/search.py) | search node — fans variants out with `asyncio.gather`, dedups by URL, surfaces `BudgetExhausted` |
| [layers/domain_filter.py](layers/domain_filter.py) | host-vs-`domain_map` match; sets `domain=pass/fail`, kills non-matching candidates |
| [layers/brand.py](layers/brand.py) | three-state brand. `extract_brands()` returns **a list of all literal matches** (sorted by position of first appearance) — falls back to a single fuzzy result, then a single uppercase-first-token heuristic. `compare_brands(list, list)` uses **any-pair-pass / all-pairs-differ-fail** so noise tokens that happen to be in `brand.xlsx` (e.g. "Tropical" inside "Tetley Tropical Tea") don't drown out the real brand. |
| [layers/numeric.py](layers/numeric.py) | regex pre-pass for `ABV X%` and `N X N(ml|g)` count patterns; then quantulum3 fills in volume / weight / storage / length. Comparison is discrete-equal or ±10% continuous tolerance. |
| [layers/base_match.py](layers/base_match.py) | per-candidate brand+numeric via `asyncio.to_thread`; reads/writes `cache.BaseExtractionCache` |
| [cache.py](cache.py) | SQLite cache keyed on `md5(title)` for base extraction. Path from `maintain/search_config.yaml`. |
| [layers/distinguishing.py](layers/distinguishing.py) | **single batched** Qwen (`qwen-flash`) call; returns `{match_idx, reason}` JSON. Never asks the LLM for self-reported confidence. |
| [layers/aggregate.py](layers/aggregate.py) | picks `MATCH` (any candidate with `distinguishing=pass`) or `NO_MATCH` (representative trace = deepest-reached candidate) |
| [graph.py](graph.py) | LangGraph wiring + conditional short-circuit edges |
| [pipeline.py](pipeline.py) | compiles the graph once at import; `match_product()` is the public entrypoint |
| [main.py](main.py) | Excel batch driver — `python run.py` reads `config.yaml`, runs the pipeline async over rows, writes `output/{output_file}` with `url_search_1`, `match_verdict`, `match_layer_trace`, `match_reason` columns |
| [maintain/](maintain/) | **Maintained files**: `brand.xlsx` (lookup, only `brandname_en` is read) + `search_config.yaml`. See "Files to maintain" in [README.md](README.md). |

## Key invariants

- **Three-state semantics** — brand and numeric layers only `FAIL` when *confirmed different*. Missing data → `UNKNOWN`, never `FAIL`. The LLM in distinguishing makes the final call.
- **No `site:` operator** in queries — relying on it would couple search to domain logic and some providers (DuckDuckGo) ignore it.
- **`BRANDS_FUZZY_SAFE` subset** = brands with `len ≥ 4 AND has letter`. Anything shorter or purely numeric only matches via literal word-boundary regex. Without this guard, fuzzy on "ABC" matches everything.
- **Brand list is cached via `lru_cache` per process** — edits to `maintain/brand.xlsx` require a Python restart to take effect. Batch jobs (`run.py`, `validate_search.py`) start fresh, so this is automatic for them.
- **Multi-brand extraction** — `extract_brands()` returns every literal hit, not the longest/first. `compare_brands()` uses any-pair-pass + all-pairs-differ-fail. Designed for titles where a non-brand word collides with `maintain/brand.xlsx`.
- **Numeric regex pre-pass runs before quantulum3** — quantulum3 misses ABV and treats the leading number in `4 X 330ml` as dimensionless. The regex pre-pass owns `abv_percent` and `count`; quantulum3 fills the rest.
- **Cache hit on `md5(title)`** — same title is never re-extracted within a run (or across runs sharing the SQLite file at `.cache/base_extraction.sqlite`).

## Config knobs (maintain/search_config.yaml)

| Section | Important keys |
|---|---|
| `search` | `provider` (`serper` / `duckduckgo`), `k` (results per query), `query_variants`, `retailer_keywords` |
| `domain_map` | website name → host. Add new marketplaces here. |
| `brand` | `fuzzy_same_threshold` (default 88), `fuzzy_differ_threshold` (default 40) |
| `numeric` | `continuous_tolerance` (default 0.10), `entity_to_attr`, `unit_conversions`, `discrete_attrs`, `ambiguity_rules` |
| `llm` | `model` (`qwen-flash`), `base_url`, `temperature`, `timeout_s` |
| `cache` | `sqlite_path` |

`config.yaml` (repo root) is unchanged — it still drives `main.py` (input file, sku-name column, country, target web, output file). New optional keys it understands: `serper_max_calls`, `concurrency`, `search_provider`.

## Running

End-to-end batch:
```
python run.py
```

Unit tests (offline, mocks Serper + LLM — zero API cost):
```
python -m pytest tests/unit/search/ -v
```

Budget-capped validation against `src/0_Data/tesco_algo.xlsx`:
```
python scripts/validate_search.py --sample 20 --budget 50
```
Writes `output/validation_report.xlsx`. The script prints a numeric-only pre-pass (no Serper calls), then runs the pipeline within the call budget, then prints per-layer verdict counts and agreement-rate vs the legacy `url_search_1` column.

## Environment

Required env vars (loaded from `.env`):
- `QWEN_KEY` — DashScope API key for the Qwen LLM (distinguishing layer)
- `SERPER_KEY` — google.serper.dev key for the search provider

Python 3.12. New deps relative to the old code: `quantulum3`, `rapidfuzz`, `langgraph`, `aiohttp` — all in `requirements.txt`.

## Adding things

- **New search provider**: subclass `providers.base.SearchProvider`, register in `providers/__init__.py::make_provider`, set `search.provider` in yaml.
- **New marketplace**: add to `domain_map` + `search.retailer_keywords` in `maintain/search_config.yaml`. No code change needed.
- **New numeric attribute**: add an `entity_to_attr` mapping (or a custom regex in `layers/numeric.py::extract_numerics`) + a `unit_conversions` table + decide discrete-vs-continuous in `discrete_attrs`.
- **Brand list grows**: append rows to `maintain/brand.xlsx` `brandname_en`. The `lru_cache` on the loader is per-process; restart picks up new rows. See "Files to maintain" in [README.md](README.md) for the full guide.
