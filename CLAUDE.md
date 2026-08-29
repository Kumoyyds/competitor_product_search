# CLAUDE.md

This file provides guidance to AI coding agents (Claude Code, Codex, etc.) when working with code in this repository. `AGENTS.md` (read by Codex) and `CLAUDE.md` (read by Claude Code) are kept byte-identical: edit either one, and a git pre-commit hook syncs the other automatically.

## Project Overview

PriceScope — a tool that helps online retailers track competitor products on marketplaces (e.g., amazon.de, tesco.com, argos.co.uk). Two implemented modules:

- **search** — takes a spreadsheet of SKU names, searches via configurable search engines (DuckDuckGo, Serper), then uses a 5-layer LangGraph pipeline with a routed LLM to select the best matching product URL.
- **scraping** — takes a product URL and extracts structured `ProductData` (price, stock, images, …) via BrightData, with an LLM parser-repair ladder that self-heals broken site parsers.

The two modules are independent (no cross-imports); the `orchestrator` module that will chain them is still a skeleton.

## Setup & Run

```bash
# Install uv first; .python-version pins Python 3.12 and uv manages .venv
uv sync --group dev --group notebook

# Copy and fill the keys you need (see "Config files" below)
cp .env.sample .env

# Enable documentation sync/generation and encoding checks (once per clone)
git config core.hooksPath .githooks

# search — run a batch (all per-run settings are flags)
uv run python -m src.search.batch --input input/products.xlsx --sku-col product_name \
    --web-col web --country-col country --output output/results.xlsx

# scraping — cold start a new site
uv run python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx
```

Both modules resolve paths relative to the repo root — run them from there.

```python
# scraping — public API
from src.scraping import scrape, ProductData, InvalidTargetResult, ScrapeFailed

result = await scrape("https://www.argos.co.uk/product/3284476")
# result is ProductData or InvalidTargetResult; ScrapeFailed raised on terminal failure
```

## Architecture

### Module Responsibility

| Module | Path | Status | Description |
|--------|------|--------|-------------|
| **search** | `src/search/` | Implemented | Product URL matching pipeline |
| **scraping** | `src/scraping/` | Implemented | Product page data extraction (M1–M27) |
| **api** | `src/api/` | Skeleton | REST API endpoints |
| **orchestrator** | `src/orchestrator/` | Skeleton | Pipeline coordination and dispatch |
| **matching** | `src/matching/` | Skeleton | Attribute-level product match scoring |
| **storage** | `src/storage/` | Skeleton | Data persistence (temp, main, archive) |
| **models** | `src/models/` | Skeleton | Shared data models (SKU, Product, MatchResult) |
| **common** | `src/common/` | Skeleton | Shared utilities (LLM client, config, logging) |

Each implemented module has its own `CLAUDE.md` with the detail that matters when working inside it — read `src/search/CLAUDE.md` or `src/scraping/CLAUDE.md` before changing either.

### Search — Data Flow

`src.search.batch.match_product_batch()` → reads the supplied Excel path → async pipeline per row (asyncio Semaphore, default 16) → LangGraph 5-layer pipeline → returns a DataFrame and optionally writes the supplied output path. Per-run settings are function arguments/CLI flags; pipeline tuning remains in `src/search/maintain/search_config.yaml`.

```
search  →  domain_filter  →  base_match (brand + numeric)  →  distinguishing (LLM)  →  aggregate
```

Short-circuits when every candidate dies at a layer — LLM is never called when cheap rules already settled the question.

Key files:

- `src/search/pipeline.py` — Public entry point `match_product()` with provider-chain fallback loop
- `src/search/graph.py` — LangGraph StateGraph wiring + conditional short-circuit edges
- `src/search/batch.py` — Parameterized Excel batch API + flag-only CLI
- `src/search/db.py` / `src/search/trace.py` — SQLite run/task tracing shared by single and batch calls
- `src/search/layers/search.py` — Search node: fans query variants across the provider
- `src/search/layers/base_match.py` — Brand + numeric extraction per candidate
- `src/search/layers/distinguishing.py` — Single batched routed-LLM call for final selection
- `src/search/maintain/brand.xlsx` — Brand name lookup table (manual maintenance required)
- `src/search/maintain/search_config.yaml` — Pipeline tuning: provider chain, thresholds, domain map, LLM config
- `src/search/providers/` — Search engine implementations (DuckDuckGo, Serper) with internal country mappings

### Scraping — Data Flow

`Router.scrape(url)` → host→site→ordered scraper list (two-hop) → try each scraper:

- **HTMLScraper route** (Tesco, Argos): BrightData Web Unlocker → HTML → invalid-target pre-detection → ordered parser list (sandbox-executed) → two gates → `ProductData`. On failure: **agent repair ladder** → candidate parser → sandbox + golden test → promote if it passes.
- **DirectAPIScraper route** (Amazon, Tesco/Argos DCA backup): BrightData Datasets/DCA API → JSON → field mapping → two gates. On gate failure: restricted JSON self-healing (remaps existing keys only, never fabricates).

When every scraper for a site is exhausted, the failure is escalated to `EscalationStore` rather than silently dropped.

Key files:

- `src/scraping/router.py` — `scrape()` entry point, host→site→scraper resolution
- `src/scraping/scrapers/` — `BaseScraper` hierarchy and per-site scrapers (`sites/`)
- `src/scraping/repair/` — LLM parser-repair ladder, sandbox execution, golden tests
- `src/scraping/validation/gate1.py` / `gate2.py` — Pydantic structure gate + cross-field feasibility gate
- `src/scraping/storage/` — SQLite stores (parsers, goldens, results, escalations, runs)
- `src/scraping/hosts.yaml` / `sites.yaml` — Host→site map and per-site scraper ordering
- `src/scraping/scraping_module_spec_v1_2.md` — Full design spec, decisions numbered D1–D29

### External Dependencies

- **LLM**: Configurable OpenAI-compatible providers. search selects via `llm.model` + the router table; scraping registers models/vendors in `src/scraping/providers.py`
- **Search**: DuckDuckGo (free, via `ddgs` lib) and/or Serper (paid, via `aiohttp`)
- **Scraping**: BrightData Web Unlocker (raw HTML), Datasets API and DCA (structured JSON)
- **Pipeline framework**: LangGraph (StateGraph)
- **Numeric extraction**: quantulum3 + regex pre-pass
- **Brand matching**: rapidfuzz
- **HTML parsing**: beautifulsoup4 + lxml (also the sandbox import whitelist)

### Config files

| File | Purpose |
|------|---------|
| `src/search/maintain/search_config.yaml` | Pipeline tuning: `search.provider` (string or ordered list for chain), per-provider `query_mode`, `strip_parens`, thresholds, domain map, LLM model |
| `src/search/maintain/llm_router_config.yaml` | Keyword → `(base_url, key_name)` routing table so switching LLM vendor/model is a single-line edit to `llm.model` |
| `src/scraping/config.py` (`ScrapingConfig`) | Repair/cold-start model + temperature ladders, sandbox limits, BrightData poll budget, DB path — see `src/scraping/CLAUDE.md` §Key Config |
| `src/scraping/hosts.yaml`, `sites.yaml` | Host→site mapping and per-site scraper order |
| `.env` (repo root) | API keys: `QWEN_KEY` / `DEEPSEEK_KEY` as selected by the configured model; `SERPER_KEY` only if Serper is in the search provider chain; `BRIGHT_DATA_KEY` for scraping |

### Data Files

- search input: any `.xlsx` path passed to `match_product_batch()` / `--input`
- search brand list: `src/search/maintain/brand.xlsx` (needs manual maintenance for new brands)
- search output: optional `.xlsx` path passed to `match_product_batch()` / `--output`
- search trace DB: `search.db` by default (single calls use `mode=single`; batches use `mode=batch`)
- scraping DB: `scraping.db` by default (`SCRAPING_DB_PATH`) — parsers, goldens, results, escalations
- scraping cold-start workbooks: `src/scraping/data/cold_start/*.xlsx` (require `page_type` + `url`)

## Code Conventions

- `src/` uses implicit namespace package (no `src/__init__.py`)
- Modules under `src/` may carry their own `CLAUDE.md` with module-specific details; every `CLAUDE.md` has a byte-identical `AGENTS.md` sibling maintained by the pre-commit hook (`scripts/sync_agent_docs.py`), so edit either file freely — the other follows
- Imports within a module use relative paths (e.g., `from .providers import make_provider_chain`)
- Cross-module imports use absolute paths (e.g., `from src.search.pipeline import match_product`)
- Direct dependencies are declared in `pyproject.toml`; `uv.lock` locks the resolved environment. Add dependencies with `uv add` (use `--group dev` or `--group notebook` when appropriate).
- **SQLite database files use the `.db` suffix** — never `.sqlite` or `.sqlite3`. Name each database after its module (`scraping.db`, `search.db`); SQLite creates WAL/SHM sidecars as `<name>.db-wal` / `<name>.db-shm`. When adding a database, decide whether it is tracked or ignored in `.gitignore`.
- **Every column in the search or scraping DDL needs a `--` meaning comment** on its definition line or immediately above it. Tables, indexes, and views also need a preceding purpose comment. `scripts/gen_storage_docs.py` builds `docs/search_storage.md` and `docs/scraping_storage.md` from an in-memory SQLite database, and the pre-commit hook rejects undocumented columns or stale generated regions.
- **New search providers** must include a `_COUNTRY_TO_*` mapping (see `SerperProvider._COUNTRY_TO_GL` and `DuckDuckGoProvider._COUNTRY_TO_REGION`) to translate general country-code arguments to the format the API expects
- **New scraping sites** are registered in `hosts.yaml` / `sites.yaml` and brought online via `uv run python -m src.scraping.coldstart`; new LLM vendors go in `src/scraping/providers.py`

## Documentation Discipline (mandatory)

Any change that alters how a human operates or maintains this project MUST update the README in the same commit. README = how a human runs and maintains it; CLAUDE.md = the architecture and design an agent needs.

- **Triggers**: CLI command/flag/entry point added, renamed, or removed; config key changed in `search_config.yaml`, `ScrapingConfig`, or `.env.sample`; a manually maintained data file changed in location, schema, or maintenance rules (`src/search/maintain/brand.xlsx`, `src/scraping/hosts.yaml`, `sites.yaml`, cold-start workbooks); input/output paths, formats, or required columns changed; interactive keys or exit codes changed; a new manual step required (hook install, DB migration, re-run procedure).
- **Not triggers**: internal refactors, prompt tuning, operator-invisible bug fixes, test-only changes.
- **Which README**: the nearest module README (e.g. `src/scraping/README.md`, `src/search/README.md`); also the root `README.md` when project-level usage or entry points change.
- **What to write**: the operational delta (old → new command/config), whether existing setups need migration, and who maintains any new manual file — not a code changelog. Correct stale instructions instead of appending to them.

## File Encoding Rules

All text files (markdown, Python, YAML, …) are **UTF-8 without BOM**.

- Read and write every file as UTF-8. Never transcode, convert, or "repair" a file's encoding.
- When editing a file that contains non-ASCII characters (`— – → § ✓ ├ └ │` etc.), preserve the existing bytes exactly. Do not open/re-save through another codepage.
- The classic failure: opening a UTF-8 file and re-saving it as GBK/CP936 (Chinese-Windows default) mangles every non-ASCII char into mojibake and prepends a UTF-8 BOM. Corrupted files show rare CJK glyphs where ASCII symbols should be, a leading BOM, or the Unicode replacement character (U+FFFD).
- If a file you are about to edit looks mojibake'd (or `uv run python scripts/check_encoding.py --all` flags it), STOP and report it to the user — do not edit around it or silently rewrite the file. The authoritative mojibake marker list lives in `scripts/check_encoding.py` (`MOJIBAKE`).
- Never add a UTF-8 BOM (`EF BB BF`).
- The pre-commit hook (`scripts/check_encoding.py`) rejects staged files with a BOM, invalid UTF-8, or mojibake. If your commit is blocked, fix the file's encoding — do not bypass the hook.
