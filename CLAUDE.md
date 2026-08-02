# CLAUDE.md

This file provides guidance to AI coding agents (Claude Code, Codex, etc.) when working with code in this repository. `AGENTS.md` (read by Codex) and `CLAUDE.md` (read by Claude Code) are kept byte-identical: edit either one, and a git pre-commit hook syncs the other automatically.

## Project Overview

PriceScope — a tool that helps online retailers find competitor product URLs on marketplaces (e.g., amazon.de, tesco.com). Takes a spreadsheet of SKU names, searches Google via Serper API, then uses an LLM agent (Qwen via LangChain) to select the best matching product URL.

## Setup & Run

```bash
# Use Python 3.12 (not 3.14 — many dependencies lack pre-built wheels for 3.14)
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Copy and fill API keys (QWEN_KEY, SERPER_KEY)
cp .env.sample .env

# Run
python run.py

# Enable the CLAUDE.md <-> AGENTS.md auto-sync hook (once per clone)
git config core.hooksPath .githooks
```

Configuration is in `config.yaml` — set input file name, SKU name column, country code, target web domain, and output file name before running.

## Architecture

### Data Flow (current MVP)

`run.py` → `src.search.main` (via runpy) → reads `config.yaml` → reads input Excel → splits DataFrame → `ThreadPoolExecutor` (16 workers) → `find_url_llm()` → `do_product_searching()` per row (Serper API + Qwen LLM) → combine partitions → final Excel output.

### Planned Full Flow

API → Orchestrator → Search + Scraping → Matching → Storage → Models

See `docs/architecture.md` for detailed diagrams.

### Module Responsibility

| Module | Path | Status | Description |
|--------|------|--------|-------------|
| **search** | `src/search/` | Implemented | Google+LLM product URL finder (migrated from `llm_tools/`) |
| **api** | `src/api/` | Skeleton | REST API endpoints |
| **orchestrator** | `src/orchestrator/` | Skeleton | Pipeline coordination and dispatch |
| **scraping** | `src/scraping/` | Skeleton | Product page data extraction |
| **matching** | `src/matching/` | Skeleton | Attribute-level product match scoring |
| **storage** | `src/storage/` | Skeleton | Data persistence (temp, main, archive) |
| **models** | `src/models/` | Skeleton | Shared data models (SKU, Product, MatchResult) |
| **common** | `src/common/` | Skeleton | Shared utilities (LLM client, config, logging) |

### Key Files in Search Module

- `src/search/main.py` — Orchestrator script (config, split, parallel search, merge). Run via `python run.py`.
- `src/search/searcher.py` — Core logic: `do_product_searching()` + `find_url_llm()`
- `src/search/utils.py` — Filtering utilities: URL validation, brand matching, text normalization
- `src/search/prompts.py` — `gen_prompt()` LLM prompt builder
- `src/search/brand.xlsx` — Brand name lookup table (manual maintenance required)

### External Dependencies

- **LLM**: Qwen (via OpenAI-compatible endpoint at dashscope.aliyuncs.com), configured in `searcher.py`
- **Search**: Google search via Serper API (`GoogleSerperAPIWrapper` from langchain-community)
- **Agent framework**: LangChain (agents, tools)

### Data Files

- Input: `input/<filename>.xlsx` (SKU names, configured in `config.yaml`)
- Brand list: `src/search/brand.xlsx` (needs manual maintenance for new brands)
- Output partitions: `output/output_partitions/result_N.xlsx`
- Final output: `output/<output_file>.xlsx`

## Code Conventions

- `src/` uses implicit namespace package (no `src/__init__.py`)
- Modules under `src/` may carry their own `CLAUDE.md` with module-specific details; every `CLAUDE.md` has a byte-identical `AGENTS.md` sibling maintained by the pre-commit hook (`scripts/sync_agent_docs.py`), so edit either file freely — the other follows
- Imports within a module use relative paths (e.g., `from .searcher import find_url_llm`)
- Cross-module imports use absolute paths (e.g., `from src.search.searcher import ...`)
- Dependencies managed via `requirements.txt` (pip freeze format)

## File Encoding Rules

All text files (markdown, Python, YAML, …) are **UTF-8 without BOM**.

- Read and write every file as UTF-8. Never transcode, convert, or "repair" a file's encoding.
- When editing a file that contains non-ASCII characters (`— – → § ✓ ├ └ │` etc.), preserve the existing bytes exactly. Do not open/re-save through another codepage.
- The classic failure: opening a UTF-8 file and re-saving it as GBK/CP936 (Chinese-Windows default) mangles every non-ASCII char into mojibake and prepends a UTF-8 BOM. Corrupted files show rare CJK glyphs where ASCII symbols should be, a leading BOM, or the Unicode replacement character (U+FFFD).
- If a file you are about to edit looks mojibake'd (or `python3 scripts/check_encoding.py --all` flags it), STOP and report it to the user — do not edit around it or silently rewrite the file. The authoritative mojibake marker list lives in `scripts/check_encoding.py` (`MOJIBAKE`).
- Never add a UTF-8 BOM (`EF BB BF`).
- The pre-commit hook (`scripts/check_encoding.py`) rejects staged files with a BOM, invalid UTF-8, or mojibake. If your commit is blocked, fix the file's encoding — do not bypass the hook.
