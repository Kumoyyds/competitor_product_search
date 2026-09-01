# PriceScope

PriceScope helps online retailers track competitor products on marketplaces such as amazon.de, tesco.com, and argos.co.uk: first find the matching product URL, then extract its live product data. The repository and directory are named `competitor_product_search`; they are the same project.

## Modules

| Module | Path | Status | Docs |
|---|---|---|---|
| Search | [src/search/](src/search/) | Implemented | [Search README](src/search/README.md) |
| Scraping | [src/scraping/](src/scraping/) | Implemented | [Scraping README](src/scraping/README.md) |
| Orchestrator | [src/orchestrator/](src/orchestrator/) | Implemented | [Orchestrator README](src/orchestrator/README.md) |
| API | [src/api/](src/api/) | Skeleton | No README yet |
| Matching | [src/matching/](src/matching/) | Implemented | [Matching README](src/matching/README.md) |
| Models | [src/models/](src/models/) | Implemented | Shared contracts |
| Common | [src/common/](src/common/) | Partial | Shared Search/Matching LLM routing |

## How the pieces fit

```text
InputItem → search → product URL → scraping → ProductData → matching → Valid/Failure
```

Search and scraping remain independently callable. The orchestrator now chains them into New Input and Rerun workflows; matching verifies exact product identity before a new URL becomes Valid.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the repository root. The checked-in `.python-version` pins Python 3.12, and `uv` creates and manages `.venv` automatically:

```bash
uv sync --group dev --group notebook
cp .env.sample .env
git config core.hooksPath .githooks    # once per clone
```

The hook synchronizes `CLAUDE.md` / `AGENTS.md`, regenerates capability and storage-schema documentation, then checks staged text files for UTF-8 encoding problems.

All commands resolve paths relative to the repository root. Fill in `.env` according to the configured services:

| Key | Needed by |
|---|---|
| `QWEN_KEY` / `DEEPSEEK_KEY` | Search and scraping, for whichever provider the configured model routes to |
| `SERPER_KEY` | Search, only when Serper is in the provider chain |
| `BRIGHT_DATA_KEY` | Scraping |
| `ORCHESTRATOR_DB_PATH` | Optional orchestrator database override; defaults to `orchestrator.db` |

## Search — find the product URL

The search module uses a configurable search-engine chain: DuckDuckGo is free, while Serper provides paid Google search. A five-layer LangGraph pipeline progressively filters candidates, then uses a routed LLM to select the best matching product URL.

Supported vendor routes and the active model are generated from the maintained configuration: <!-- BEGIN GENERATED: llm-inline -->`qwen`, `deepseek`; active model: `deepseek-v4-flash` via `deepseek`<!-- END GENERATED: llm-inline -->.

Supported marketplaces: <!-- BEGIN GENERATED: websites-inline -->`tesco`, `argos`, `amazon.co.uk`, `amazon.nl`<!-- END GENERATED: websites-inline -->.

Supported country codes: <!-- BEGIN GENERATED: countries-inline -->`uk` (= `gb`), `de`, `fr`, `us`, `nl`, `jp`, `es`, `it`, `pt`, `se`, `pl`, `br`, `au`, `ca`<!-- END GENERATED: countries-inline -->.

```bash
uv run python -m src.search.batch --input input/products.xlsx --sku-col product_name \
    --web-col web --country-col country --output output/results.xlsx
```

Always validate a sample of 20 or 50 rows before a full batch: every search has a cost.

Full usage, input columns, validation, and maintenance: [src/search/README.md](src/search/README.md).

## Scraping — extract the product data

The scraping module fetches a marketplace page through BrightData, parses it with a per-site parser, validates the result through two gates, and returns `ProductData`. If a parser breaks, an LLM repair ladder can generate and test a replacement; terminal failures are escalated rather than silently dropped.

```python
from src.scraping import scrape

result = await scrape("https://www.argos.co.uk/product/3284476")
```

Cold start, site onboarding, configuration, and the complete API: [src/scraping/README.md](src/scraping/README.md).

## End-to-end workflows

New Input accepts `.xlsx`, `.csv`, or a JSON array. Required fields are `title`, `country` (or `region`), and `site_name`; optional fields are text-formatted `gtin` and `image_urls` (a JSON array string or one URL in spreadsheets).

```bash
uv run python -m src.orchestrator new --input input/products.xlsx --vision
uv run python -m src.orchestrator rerun --batch-id b-... --search-title "Selected title"
```

The Python API exposes `await run_new_input(...)` and `await rerun(...)`. Results are append-only in `orchestrator.db`; exit code 0 means all Valid, 2 means completed with row failures, and 1 means a fatal invocation error. See the [Orchestrator README](src/orchestrator/README.md).

The Search LLM routing table moved from `src/search/maintain/llm_router_config.yaml` to `src/common/llm_router_config.yaml`. Existing installations with custom vendor entries must copy those entries to the new shared file; no database migration is required.

The REST `api` module remains planned and is not part of these workflows.

## Tests

```bash
uv run pytest         # offline, zero API cost
uv run pytest -m live # real keys; may cost money
```

## Repository layout

```text
src/       Implemented modules and future module skeletons
docs/      Architecture and design documentation
scripts/   Documentation, encoding, and validation utilities
input/     Example or job input workbooks
output/    Generated batch and validation results
tests/     Project-level test suite
*.db       SQLite run, trace, and scraping data stores
```

## Documentation map

- [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) — agent-facing architecture; the pre-commit hook keeps them byte-identical.
- [docs/architecture.md](docs/architecture.md) — current and planned project architecture.
- [docs/scraping_design.md](docs/scraping_design.md) — scraping design overview.
- [docs/search_storage.md](docs/search_storage.md) — generated search tracing schema, relationships, migrations, and query examples.
- [docs/scraping_storage.md](docs/scraping_storage.md) — generated scraping schema, relationships, migrations, and query examples.
- [docs/orchestrator_storage.md](docs/orchestrator_storage.md) — generated batch lineage, Valid, and Failure schema reference.
- [src/scraping/scraping_module_spec_v1_2.md](src/scraping/scraping_module_spec_v1_2.md) — detailed scraping specification.
- Per-module agent guidance: [src/search/CLAUDE.md](src/search/CLAUDE.md), [src/scraping/CLAUDE.md](src/scraping/CLAUDE.md), and their byte-identical `AGENTS.md` siblings.

## Roadmap

1. Add more search engines.
2. Add the REST API and progress endpoints over orchestrator batches.
3. Support self-hosted LLMs through [vLLM](https://docs.vllm.ai/en/latest/).

## Contact

For questions, contact **Yuding** on WeChat (`mylordship`).
