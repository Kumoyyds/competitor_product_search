# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PriceScope — a tool that helps online retailers find competitor product URLs on marketplaces (e.g., amazon.de, tesco.com). Takes a spreadsheet of SKU names, searches via configurable search engines (DuckDuckGo, Serper), then uses a 5-layer LangGraph pipeline with Qwen LLM to select the best matching product URL.

## Setup & Run

```bash
# Use Python 3.12 (not 3.14 — many dependencies lack pre-built wheels for 3.14)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Copy and fill API keys (QWEN_KEY is required; SERPER_KEY only if you use Serper)
cp .env.sample .env

# Run
python run.py
```

## Architecture

### Data Flow

`run.py` → `src.search.main` → reads `config_search.yaml` (per-run: input file, country, target web, output file) + `src/search/maintain/search_config.yaml` (pipeline tuning: provider chain, thresholds, domain map) → reads input Excel → async pipeline per row (asyncio Semaphore, default 16) → LangGraph 5-layer pipeline → final Excel output.

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
- `src/search/main.py` — Excel batch driver. Run via `python run.py`.
- `src/search/layers/search.py` — Search node: fans query variants across the provider
- `src/search/layers/base_match.py` — Brand + numeric extraction per candidate
- `src/search/layers/distinguishing.py` — Single batched LLM call (Qwen) for final selection
- `src/search/maintain/brand.xlsx` — Brand name lookup table (manual maintenance required)
- `src/search/maintain/search_config.yaml` — Pipeline tuning: provider chain, thresholds, domain map, LLM config
- `src/search/providers/` — Search engine implementations (DuckDuckGo, Serper) with internal country mappings

### External Dependencies

- **LLM**: Qwen (via OpenAI-compatible endpoint at dashscope.aliyuncs.com)
- **Search**: DuckDuckGo (free, via `ddgs` lib) and/or Serper (paid, via `aiohttp`)
- **Pipeline framework**: LangGraph (StateGraph)
- **Numeric extraction**: quantulum3 + regex pre-pass
- **Brand matching**: rapidfuzz

### Config files

| File | Purpose |
|------|---------|
| `config_search.yaml` (repo root) | Per-run: input file, SKU column, country (uk/fr/de/nl/...), target web, output file, optional `serper_max_calls`, optional `concurrency` |
| `src/search/maintain/search_config.yaml` | Pipeline tuning: `search.provider` (string or ordered list for chain), thresholds, domain map, LLM model, cache path |
| `.env` (repo root) | API keys: `QWEN_KEY` (required), `SERPER_KEY` (only if Serper is in chain) |

### Data Files

- Input: `input/<filename>.xlsx` (SKU names, configured in `config_search.yaml`)
- Brand list: `src/search/maintain/brand.xlsx` (needs manual maintenance for new brands)
- Cache: `.cache/base_extraction.sqlite` (auto-managed; keyed on md5(title))
- Final output: `output/<output_file>.xlsx`

## Code Conventions

- `src/` uses implicit namespace package (no `src/__init__.py`)
- Each module under `src/` has its own `CLAUDE.md` with module-specific details
- Imports within a module use relative paths (e.g., `from .providers import make_provider_chain`)
- Cross-module imports use absolute paths (e.g., `from src.search.pipeline import match_product`)
- Dependencies managed via `requirements.txt` (pip freeze format)
- **New search providers** must include a `_COUNTRY_TO_*` mapping (see `SerperProvider._COUNTRY_TO_GL` and `DuckDuckGoProvider._COUNTRY_TO_REGION`) to translate general country codes from `config_search.yaml` to the format the API expects
