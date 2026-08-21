# PriceScope

PriceScope helps online retailers track competitor products on marketplaces such as amazon.de, tesco.com, and argos.co.uk: first find the matching product URL, then extract its live product data. The repository and directory are named `competitor_product_search`; they are the same project.

## Modules

| Module | Path | Status | Docs |
|---|---|---|---|
| Search | [src/search/](src/search/) | Implemented | [Search README](src/search/README.md) |
| Scraping | [src/scraping/](src/scraping/) | Implemented | [Scraping README](src/scraping/README.md) |
| Orchestrator | [src/orchestrator/](src/orchestrator/) | Skeleton | No README yet |
| API | [src/api/](src/api/) | Skeleton | No README yet |
| Matching | [src/matching/](src/matching/) | Skeleton | No README yet |
| Storage | [src/storage/](src/storage/) | Skeleton | No README yet |
| Models | [src/models/](src/models/) | Skeleton | No README yet |
| Common | [src/common/](src/common/) | Skeleton | No README yet |

## How the pieces fit

```text
SKU name → search → product URL → scraping → ProductData → matching → score
```

Search and scraping are independent today, with no cross-imports. The `orchestrator` that will chain them is still a skeleton, so nothing runs end-to-end yet; run each implemented module on its own. See the planned design in [docs/architecture.md](docs/architecture.md).

## Setup

Use Python 3.12, not Python 3.14: several dependencies do not yet ship pre-built wheels for 3.14. From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate              # macOS / Linux
# .\.venv\Scripts\Activate.ps1           # Windows PowerShell
pip install -r requirements.txt
cp .env.sample .env
git config core.hooksPath .githooks    # once per clone
```

Both implemented modules resolve paths relative to the repository root. Fill in `.env` according to the configured services:

| Key | Needed by |
|---|---|
| `QWEN_KEY` / `DEEPSEEK_KEY` | Search and scraping, for whichever provider the configured model routes to |
| `SERPER_KEY` | Search, only when Serper is in the provider chain |
| `BRIGHT_DATA_KEY` | Scraping |

## Search — find the product URL

The search module uses a configurable search-engine chain: DuckDuckGo is free, while Serper provides paid Google search. A five-layer LangGraph pipeline progressively filters candidates, then uses a routed LLM to select the best matching product URL.

Supported vendor routes and the active model are generated from the maintained configuration: <!-- BEGIN GENERATED: llm-inline -->`qwen`, `deepseek`; active model: `deepseek-v4-flash` via `deepseek`<!-- END GENERATED: llm-inline -->.

Supported marketplaces: <!-- BEGIN GENERATED: websites-inline -->`tesco`, `argos`, `amazon.co.uk`, `amazon.nl`<!-- END GENERATED: websites-inline -->.

Supported country codes: <!-- BEGIN GENERATED: countries-inline -->`uk` (= `gb`), `de`, `fr`, `us`, `nl`, `jp`, `es`, `it`, `pt`, `se`, `pl`, `br`, `au`, `ca`<!-- END GENERATED: countries-inline -->.

```bash
python -m src.search.batch --input input/products.xlsx --sku-col product_name \
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

## Planned modules

- `orchestrator` — skeleton; will coordinate the modules into an end-to-end workflow.
- `api` — skeleton; will expose REST endpoints.
- `matching` — skeleton; will score product attributes after extraction.
- `storage` — skeleton; will provide project-level persistence.
- `models` — skeleton; will define shared SKU, product, and match-result models.
- `common` — skeleton; will provide shared configuration, LLM, and logging utilities.

## Tests

```bash
python -m pytest         # offline, zero API cost
python -m pytest -m live # real keys; may cost money
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
- [src/scraping/scraping_module_spec_v1_2.md](src/scraping/scraping_module_spec_v1_2.md) — detailed scraping specification.
- Per-module agent guidance: [src/search/CLAUDE.md](src/search/CLAUDE.md), [src/scraping/CLAUDE.md](src/scraping/CLAUDE.md), and their byte-identical `AGENTS.md` siblings.

## Roadmap

1. Add more search engines.
2. Add vision-based filtering to complement text matching.
3. Support self-hosted LLMs through [vLLM](https://docs.vllm.ai/en/latest/).
4. Chain the implemented modules through `orchestrator`.

## Contact

For questions, contact **Yuding** on WeChat (`mylordship`).
