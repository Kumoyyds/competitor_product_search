# `search/` — Product URL Matching

## 1. What this is for

Given a product name and a target marketplace (Tesco / Argos / Amazon), this module decides whether that product exists on the marketplace and returns the matching listing URL.

It replaces the old "let an LLM agent pick a URL" approach with a **5-layer pipeline** that filters candidates progressively and only invokes the LLM on the small set that survives cheap rule-based filters. Design rationale and full spec: [search_link_algorithm_spec.md](search_link_algorithm_spec.md).

### How it works

```
search  →  domain_filter  →  base_match  →  distinguishing  →  aggregate
```

1. **search** — fires query variants at the first provider in the chain (DuckDuckGo / Serper / custom), dedups results by URL.
2. **domain_filter** — drops candidates whose host isn't the target marketplace (e.g. only keep `*.tesco.com` when targeting Tesco).
3. **base_match** — for each surviving candidate, compares **brand** (rapidfuzz against `brand.xlsx`, three-state pass/fail/unknown) and **numeric attributes** (volume, weight, count, ABV, storage… extracted via quantulum3 + regex). Mismatches kill the candidate; missing info passes through as `unknown`.
4. **distinguishing** — one batched LLM call (Qwen `qwen-flash`) decides which surviving candidate (if any) is the same SKU, catching variant differences the rules miss (flavour, colour, version, pack size).
5. **aggregate** — picks the verdict (`match` / `no_match`) and the per-layer trace.

Short-circuiting: whenever a layer kills every candidate, the pipeline skips straight to `aggregate`. The LLM is never called when cheap rules already settled the question.

Each candidate carries a `LayerTrace` showing exactly where the decision happened — useful for debugging and tuning.

---

## 2. Basic workflow

### Batch mode (full input file)

```powershell
# 1. activate venv & install deps (first run only)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. set API keys in .env at repo root
#    QWEN_KEY=...
#    SERPER_KEY=...

# 3. edit config_search.yaml — point input_file at your spreadsheet, set web/country/output_file

# 4. run
python run.py
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

### Validation (budget-capped sanity check)

```powershell
python scripts/validate_search.py --sample 20 --budget 50
```

Stratified sample from `src/0_Data/tesco_algo.xlsx`, capped at 50 Serper calls. Writes `output/validation_report.xlsx` + prints per-layer verdict mix and agreement-rate vs the legacy URL column.

---

## 3. Input

### Batch mode

- **Where**: `input/{input_file}.xlsx` (filename set in [config_search.yaml](../../config_search.yaml))
- **Required columns** (name configurable):
  - `item_sku_name_de` (or whatever you set as `input_sku_name_col`) — the product name string
- **config_search.yaml keys consumed**:
  - `input_file`, `input_sku_name_col`, `country` (general country code: `uk` / `fr` / `de` / `nl`; each search engine maps it internally), `web` (target marketplace, e.g. `amazon.de`), `output_file`
  - Optional: `serper_max_calls` (cap total Serper calls), `concurrency` (default 16)
### Programmatic

Whatever you pass to `match_product(product_name, website, brand=None, country="uk")`. `brand` is optional — if provided it skips brand extraction on the query side.

### Validation script

Reads `src/0_Data/tesco_algo.xlsx` by default; override with `--input`.

### Environment (`.env` at repo root)

- `QWEN_KEY` — DashScope key for the Qwen LLM (distinguishing layer), required
- `SERPER_KEY` — google.serper.dev key, only needed if Serper is in the provider chain

---

## 4. Output

### Batch mode

`output/{output_file}.xlsx` — your input file with these extra columns appended:

| Column | Content |
|---|---|
| `url_search_1` | matched product URL, or `"not found"` |
| `match_verdict` | `match` / `no_match` / `error` |
| `match_layer_trace` | JSON: `{"domain": "pass", "brand": "pass", "numeric": "unknown", "distinguishing": "pass"}` — each is `pass`, `fail`, `unknown`, or `null` (layer never reached) |
| `match_reason` | LLM rationale (when distinguishing ran) or short pipeline status text |

### Validation script

`output/validation_report.xlsx` with columns: `product_name`, `legacy_url`, `new_verdict`, `new_url`, `layer_trace_json`, `candidates_considered`, `reason`. Console also prints the per-layer verdict counts and per-provider call counts.

### Cache (auto-managed)

`.cache/base_extraction.sqlite` — SQLite cache of brand + numeric extraction per title. Safe to delete; will rebuild on next run.

---

## 5. Files to maintain

**All human-edited files live in [`maintain/`](maintain/)** — the only folder you should normally touch when keeping the pipeline current.

| File | When / how to update |
|---|---|
| **[maintain/brand.xlsx](maintain/brand.xlsx)** | Add a row whenever a brand isn't being recognised; remove a row to drop a false-positive brand. Only the `brandname_en` column is read — other columns are ignored. After saving, **restart the Python process** (the brand list is `lru_cache`-d for the lifetime of the process; batch runs `python run.py` start fresh, so this is automatic). What's safe to add: normal brands ("Kopparberg"), short brands ("AEG", "7Up"), digit-bearing brands ("19 Crimes"), and even common English words ("Tropical", "Green") — the multi-brand any-pair-match comparison handles collisions correctly. Pure-numeric brands ("555") work but use sparingly — they may collide with codes/prices in titles. |
| **[maintain/search_config.yaml](maintain/search_config.yaml)** | Tune without touching code. Key sections: `domain_map` (add new marketplaces here), `search.retailer_keywords`, `brand.fuzzy_same_threshold` / `fuzzy_differ_threshold` (88 / 40 default), `numeric.continuous_tolerance` (±10%), `numeric.entity_to_attr` + `unit_conversions` + `discrete_attrs` (to support new attributes/units), `llm.model` (currently `qwen-flash`), `cache.sqlite_path`. Restart after editing. |
| **[maintain/llm_router_config.yaml](maintain/llm_router_config.yaml)** | Keyword → `(base_url, key_name)` routing table for the `distinguishing` layer's LLM. Add an entry here when introducing a new LLM vendor — no code change needed. |
| **[config_search.yaml](../../config_search.yaml)** (repo root) | Per-run job config: which input file, which marketplace, country code, output filename, optional Serper budget. |

### Common maintenance tasks

| Want to… | Do |
|---|---|
| Recognise a new brand | Append to `maintain/brand.xlsx` `brandname_en`, save, restart |
| Drop a noisy brand | Delete that row in `maintain/brand.xlsx`, save, restart |
| Add a new marketplace | In `maintain/search_config.yaml`: add `domain_map: { <name>: <host> }` and `search.retailer_keywords: { <name>: <keyword> }` |
| Make brand matching stricter / looser | Raise / lower `brand.fuzzy_same_threshold` in `maintain/search_config.yaml` |
| Allow more slop in weights/volumes | Raise `numeric.continuous_tolerance` |
| Support a new unit (e.g. `floz`) | Add it under the relevant attribute in `numeric.unit_conversions` |
| Support a brand-new numeric attribute | Add entry to `numeric.entity_to_attr` + `unit_conversions` + decide discrete-vs-continuous in `numeric.discrete_attrs` |
| Switch LLM model/vendor | Edit `llm.model` in `maintain/search_config.yaml` (single line). It's routed to a `base_url`/API key via keyword match against `maintain/llm_router_config.yaml` — add a new vendor entry there first if it's not `qwen`/`deepseek` yet. |

### Things that drift over time

- **Brand list** is the main one — new SKUs from new brands appear constantly. Periodically scan rows where `match_layer_trace` shows `"brand": "unknown"` despite a brand-looking token in the product name, and add those brands.
- **Numeric mapping** (`entity_to_attr` + `unit_conversions`) is fine for groceries / beverages / basic electronics. Adding a new category (e.g. screen sizes for monitors, voltages for batteries) means adding entries here.
- **Domain map** needs an entry per new marketplace you support.

### Validating after a maintenance edit

```powershell
python -m pytest tests/unit/search/ -v
```
Tests skip cleanly if a referenced brand was removed from `brand.xlsx` — so brand-list edits won't break the suite.

### What does NOT need maintenance

- Code under `layers/`, `providers/`, `graph.py`, `pipeline.py` — only edit when changing algorithm behaviour.
- The SQLite cache — auto-managed at `.cache/base_extraction.sqlite`.

---

## 6. Script map

```
[main.py] ──→ [pipeline.py] ──→ [graph.py] ──→ [layers/search] ──→ [layers/query_builder]
   │               │                │              │
   │               │                │              └──→ [providers/serper]  (or [providers/duckduckgo])
   │               │                │
   │               │                ├──→ [layers/domain_filter]
   │               │                │
   │               │                ├──→ [layers/base_match] ──→ [layers/brand]
   │               │                │         │                    └──→ [utils.py]
   │               │                │         ├──→ [layers/numeric]
   │               │                │         └──→ [cache.py]
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

## File map (quick)

| Path | Purpose |
|---|---|
| [pipeline.py](pipeline.py) | Public `match_product(...)` entrypoint |
| [graph.py](graph.py) | LangGraph wiring of the 5 layers |
| [layers/](layers/) | One file per layer (`search`, `domain_filter`, `brand`, `numeric`, `base_match`, `distinguishing`, `aggregate`) + `query_builder` |
| [providers/](providers/) | `DuckDuckGoProvider` (active, free) + `SerperProvider` (active, paid) — chainable; add new search providers here |
| [models.py](models.py) | Data classes (`MatchResult`, `LayerTrace`, `CandidateEval`, …) |
| [config.py](config.py) | Loader for `maintain/search_config.yaml`; `resolve_llm_route()` keyword-routes `llm.model` via `maintain/llm_router_config.yaml` |
| [cache.py](cache.py) | SQLite cache for base extraction |
| [utils.py](utils.py) | Brand-set loader + word-boundary literal matcher (reads `maintain/brand.xlsx`) |
| [maintain/](maintain/) | **Maintained files** — `brand.xlsx` + `search_config.yaml` + `llm_router_config.yaml`. See §5 above for the how-to. |
| [main.py](main.py) | Excel batch driver invoked by `python run.py` |
| [search_link_algorithm_spec.md](search_link_algorithm_spec.md) | Full design rationale |
| [CLAUDE.md](CLAUDE.md) | Code-internals reference for AI assistants / developers |

Unit tests live at [tests/unit/search/](../../tests/unit/search/). Run: `python -m pytest tests/unit/search/ -v` (offline — no API cost).
