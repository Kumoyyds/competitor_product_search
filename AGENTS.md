# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PriceScope — a tool that helps online retailers find competitor product URLs on marketplaces (e.g., amazon.de, tesco.com). Takes a spreadsheet of SKU names, searches via configurable search engines (DuckDuckGo, Serper), then uses a 5-layer LangGraph pipeline with a routed LLM to select the best matching product URL.

## Setup & Run

```bash
# Use Python 3.12 (not 3.14 — many dependencies lack pre-built wheels for 3.14)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Copy and fill the key for the configured LLM; SERPER_KEY only if you use Serper
cp .env.sample .env

# Run a batch (all per-run settings are flags)
python -m src.search.batch --input input/products.xlsx --sku-col product_name \
    --web-col web --country-col country --output output/results.xlsx
```

## Architecture

### Data Flow

`src.search.batch.match_product_batch()` → reads the supplied Excel path → async pipeline per row (asyncio Semaphore, default 16) → LangGraph 5-layer pipeline → returns a DataFrame and optionally writes the supplied output path. Per-run settings are function arguments/CLI flags; pipeline tuning remains in `src/search/maintain/search_config.yaml`.

### Pipeline Layers (LangGraph)

```
search  →  domain_filter  →  base_match (brand + numeric)  →  distinguishing (LLM)  →  aggregate
```

Short-circuits when every candidate dies at a layer — LLM is never called when cheap rules already settled the question.

### Module Responsibility

| Module | Path | Status | Description |
|--------|------|--------|-------------|
| **search** | `src/search/` | Implemented | Product URL matching pipeline |
| **api** | `src/api/` | Skeleton | REST API endpoints |
| **orchestrator** | `src/orchestrator/` | Skeleton | Pipeline coordination and dispatch |
| **scraping** | `src/scraping/` | Skeleton | Product page data extraction |
| **matching** | `src/matching/` | Skeleton | Attribute-level product match scoring |
| **storage** | `src/storage/` | Skeleton | Data persistence (temp, main, archive) |
| **models** | `src/models/` | Skeleton | Shared data models (SKU, Product, MatchResult) |
| **common** | `src/common/` | Skeleton | Shared utilities (LLM client, config, logging) |

### Key Files in Search Module

- `src/search/pipeline.py` — Public entry point `match_product()` with provider-chain fallback loop
- `src/search/graph.py` — LangGraph StateGraph wiring + conditional short-circuit edges
- `src/search/batch.py` — Parameterized Excel batch API + flag-only CLI.
- `src/search/db.py` / `src/search/trace.py` — SQLite run/task tracing shared by single and batch calls.
- `src/search/layers/search.py` — Search node: fans query variants across the provider
- `src/search/layers/base_match.py` — Brand + numeric extraction per candidate
- `src/search/layers/distinguishing.py` — Single batched routed-LLM call for final selection
- `src/search/maintain/brand.xlsx` — Brand name lookup table (manual maintenance required)
- `src/search/maintain/search_config.yaml` — Pipeline tuning: provider chain, thresholds, domain map, LLM config
- `src/search/providers/` — Search engine implementations (DuckDuckGo, Serper) with internal country mappings

### External Dependencies

- **LLM**: Configurable OpenAI-compatible provider selected by `llm.model` and the router table
- **Search**: DuckDuckGo (free, via `ddgs` lib) and/or Serper (paid, via `aiohttp`)
- **Pipeline framework**: LangGraph (StateGraph)
- **Numeric extraction**: quantulum3 + regex pre-pass
- **Brand matching**: rapidfuzz

### Config files

| File | Purpose |
|------|---------|
| `src/search/maintain/search_config.yaml` | Pipeline tuning: `search.provider` (string or ordered list for chain), thresholds, domain map, LLM model, cache path |
| `src/search/maintain/llm_router_config.yaml` | Keyword → `(base_url, key_name)` routing table so switching LLM vendor/model is a single-line edit to `llm.model` |
| `.env` (repo root) | API keys: `QWEN_KEY` or `DEEPSEEK_KEY` as selected by `llm.model`; `SERPER_KEY` only if Serper is in the provider chain |

### Data Files

- Input: any `.xlsx` path passed to `match_product_batch()` / `--input`
- Brand list: `src/search/maintain/brand.xlsx` (needs manual maintenance for new brands)
- Cache: `.cache/base_extraction.sqlite` (auto-managed; keyed on md5(title))
- Final output: optional `.xlsx` path passed to `match_product_batch()` / `--output`
- Trace DB: `search_db.sqlite` by default (single calls use `mode=single`; batches use `mode=batch`)

## Code Conventions

- `src/` uses implicit namespace package (no `src/__init__.py`)
- Each module under `src/` has its own `CLAUDE.md` with module-specific details
- Imports within a module use relative paths (e.g., `from .providers import make_provider_chain`)
- Cross-module imports use absolute paths (e.g., `from src.search.pipeline import match_product`)
- Dependencies managed via `requirements.txt` (pip freeze format)
- **New search providers** must include a `_COUNTRY_TO_*` mapping (see `SerperProvider._COUNTRY_TO_GL` and `DuckDuckGoProvider._COUNTRY_TO_REGION`) to translate general country-code arguments to the format the API expects
