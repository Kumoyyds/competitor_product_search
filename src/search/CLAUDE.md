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

**base (brand+numeric) and distinguishing (LLM) are physically decoupled** — they live in separate modules and never import each other. Communication is only via the orchestrator state dict. Each candidate carries a `LayerTrace` (4 fields, `pass / fail / unknown / None`) and an `alive` flag. When every candidate dies at a layer, conditional edges in [graph.py](graph.py) skip downstream layers — most importantly the LLM call.

## Two config files

| Config | Location | What it controls |
|--------|----------|-----------------|
| **Pipeline config** | [maintain/search_config.yaml](maintain/search_config.yaml) | Thresholds, domain map, unit conversions, LLM model, cache path. **Edit for tuning, not per-run.** |
| **Job config** | [`config_search.yaml`](../../config_search.yaml) (repo root) | Per-run: which input file, sku column, country, target marketplace, output filename, Serper budget. **Edit before each batch run.** |

`main.py` reads `config_search.yaml` from the repo root (not the old `config.yaml`). Pipeline internals read `maintain/search_config.yaml` via `config.py`.

## File map

| File | Role |
|---|---|
| [pipeline.py](pipeline.py) | compiles the graph once at import; `match_product()` public entrypoint |
| [graph.py](graph.py) | LangGraph StateGraph wiring + conditional short-circuit edges |
| [models.py](models.py) | `Verdict`, `FinalVerdict`, `LayerTrace`, `BaseAttributes`, `RawCandidate`, `CandidateEval`, `MatchResult` |
| [config.py](config.py) | yaml loader (reads `maintain/search_config.yaml`) + `domain_for()` / `retailer_keyword_for()` helpers |
| [utils.py](utils.py) | brand-set loader (reads ONLY `brandname_en` from `maintain/brand.xlsx`), `find_literal_brands()`, accent stripping |
| [main.py](main.py) | Excel batch driver — `python run.py` reads `config_search.yaml`, runs pipeline async over rows (asyncio Semaphore, default 16), writes `output/{output_file}` with `url_search_1`, `match_verdict`, `match_layer_trace`, `match_reason` columns |
| [maintain/](maintain/) | **Human-edited files**: `brand.xlsx` + `search_config.yaml`. See README §5 for the how-to. |
| [providers/](providers/) | `SearchProvider` abstract + `SerperProvider` (aiohttp, budget-aware) + `DuckDuckGoProvider` placeholder. Add new ones by subclassing `base.SearchProvider`. |
| [cache.py](cache.py) | SQLite cache keyed on `md5(title)` for base extraction. Default path `.cache/base_extraction.sqlite`. |
| [layers/query_builder.py](layers/query_builder.py) | Rule-based query variants (raw + retailer keyword, strip parens, optional brand). **No `site:` operator** — domain filtering is its own layer. |
| [layers/search.py](layers/search.py) | search node — fans variants out with `asyncio.gather`, dedups by URL, surfaces `BudgetExhausted` |
| [layers/domain_filter.py](layers/domain_filter.py) | host-vs-`domain_map` match; sets `domain=pass/fail`, kills non-matching candidates |
| [layers/brand.py](layers/brand.py) | three-state brand. `extract_brands()` returns **all literal matches in order of first appearance** — falls back to single fuzzy result, then single uppercase-first-token heuristic. `compare_brands(list, list)` uses **any-pair-pass / all-pairs-differ-fail** so noise tokens in `brand.xlsx` (e.g. "Tropical" inside "Tetley Tropical Tea") don't drown out the real brand. |
| [layers/numeric.py](layers/numeric.py) | regex pre-pass for `ABV X%` and `N X N(ml|g)` count patterns; then quantulum3 fills volume / weight / storage / length. Comparison: discrete-equal, continuous ±10%. |
| [layers/base_match.py](layers/base_match.py) | per-candidate brand+numeric via `asyncio.to_thread`; reads/writes `cache.BaseExtractionCache` |
| [layers/distinguishing.py](layers/distinguishing.py) | **single batched** Qwen (`qwen-flash`) call; returns `{match_idx, reason}` JSON. Never asks LLM for self-reported confidence. |
| [layers/aggregate.py](layers/aggregate.py) | picks `MATCH` (any candidate with `distinguishing=pass`) or `NO_MATCH` (representative trace = deepest-reached candidate) |

## Key invariants

- **Three-state semantics** — brand and numeric layers only `FAIL` when *confirmed different*. Missing data → `UNKNOWN`, never `FAIL`. LLM in distinguishing makes the final call.
- **No `site:` operator** in queries — coupling search to domain and some providers (DuckDuckGo) ignore it.
- **`BRANDS_FUZZY_SAFE` subset** = brands `len ≥ 4 AND has letter`. Shorter/pure-numeric only match via literal word-boundary regex. Without this guard, fuzzy on "ABC" matches everything.
- **Brand list `lru_cache`-d per process** — edits to `maintain/brand.xlsx` require a Python restart. Batch jobs (`run.py`, `validate_search.py`) start fresh, so automatic.
- **Multi-brand extraction** — `extract_brands()` returns every literal hit, not just the longest/first. `compare_brands()` uses any-pair-pass + all-pairs-differ-fail. Titles where a non-brand word collides with `brand.xlsx` (e.g. "Tetley ... Tropical Tea") are safe.
- **Numeric regex pre-pass before quantulum3** — quantulum3 misses ABV and treats the leading number in `4 X 330ml` as dimensionless. Regex owns `abv_percent` and `count`; quantulum3 fills the rest.
- **Cache key = `md5(title)`** — same title never re-extracted within a run (or across runs sharing `.cache/base_extraction.sqlite`).

## Config knobs in maintain/search_config.yaml

| Section | Important keys |
|---|---|
| `search` | `provider` (`serper` / `duckduckgo`), `k` (results per query), `query_variants`, `retailer_keywords` |
| `domain_map` | website name → host. Add new marketplaces here. |
| `brand` | `fuzzy_same_threshold` (default 88), `fuzzy_differ_threshold` (default 40) |
| `numeric` | `continuous_tolerance` (default 0.10), `entity_to_attr`, `unit_conversions`, `discrete_attrs`, `ambiguity_rules` |
| `llm` | `model` (`qwen-flash`), `base_url`, `temperature`, `timeout_s` |
| `cache` | `sqlite_path` (default `.cache/base_extraction.sqlite`) |

## Config knobs in config_search.yaml (repo root)

Per-run job config read by `main.py`. Keys: `input_file`, `input_sku_name_col`, `country`, `web`, `output_file`. Optional: `serper_max_calls`, `concurrency` (default 16), `search_provider` (default `serper`).

## Running

End-to-end batch:
```
python run.py
```
Reads `config_search.yaml` for job params, runs pipeline async over rows, writes `output/{output_file}`.

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
- `QWEN_KEY` — DashScope API key for the Qwen LLM (distinguishing layer)
- `SERPER_KEY` — google.serper.dev key for the search provider

Python 3.12. Deps beyond the old code: `quantulum3`, `rapidfuzz`, `langgraph`, `aiohttp` — all in `requirements.txt`.

## Adding things

- **New search provider**: subclass `providers.base.SearchProvider`, register in `providers/__init__.py::make_provider`, set `search.provider` in `maintain/search_config.yaml`.
- **New marketplace**: add `domain_map` entry + `search.retailer_keywords` entry in `maintain/search_config.yaml`. No code change needed.
- **New numeric attribute**: add `entity_to_attr` mapping (or custom regex in `layers/numeric.py::extract_numerics`) + `unit_conversions` table + decide discrete-vs-continuous in `discrete_attrs`.
- **Brand list grows**: append rows to `maintain/brand.xlsx` `brandname_en`. `lru_cache` is per-process; restart picks up new rows. See "Files to maintain" in [README.md](README.md) for the full guide.