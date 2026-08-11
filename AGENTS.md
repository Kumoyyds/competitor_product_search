# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Batch-find the product-page URL on a competitor marketplace (e.g. `amazon.de`, `tesco.com`) for a list of SKU names supplied in an Excel file. Each SKU goes through: Google search (via Serper) → URL/brand filtering → LLM agent picks the best matching URL.

## Run

```bash
pip install -r requirements.txt
cp .env.sample .env          # fill in QWEN_KEY and SERPER_KEY
# edit config.yaml (see below), put input .xlsx in input/
python main.py               # result lands in output/<output_file>
```

There is no test suite, linter, or build step — this is a one-shot script.

## Config

[config.yaml](config.yaml) is mandatory. All five keys must be present or `main.py` raises:

- `input_file` — filename inside `input/` (must be `.xlsx`)
- `input_sku_name_col` — column in the input sheet holding the SKU name. Match the language to the target marketplace's region (French names for `amazon.fr`, etc.) — search quality depends on this.
- `country` — Serper country code (`uk`, `fr`, `de`, `nl`, ...)
- `web` — target marketplace domain, e.g. `amazon.de`. Used both as the Serper `site:` filter and as the substring [check_url](llm_tools/other_func.py) requires in returned URLs.
- `output_file` — filename written into `output/`

## Architecture

Pipeline lives in [main.py](main.py) and orchestrates two modules under [llm_tools/](llm_tools/):

1. **Chunking** ([main.py](main.py) + [get_split_num](llm_tools/other_func.py)) — the input dataframe is split into `get_split_num(n)` chunks. `get_split_num` rounds `n` down to (leading-digit × power-of-10), so 537 rows → 500 chunks of ~1 row each. Chunks are then grouped into rounds of ≤500, and each round runs a `ThreadPoolExecutor(max_workers=16)` over [find_url_llm](llm_tools/llm_func.py). Per-round partitions land in `output/output_partitions/result_{i}.xlsx` and are concatenated at the end.

2. **Per-SKU search** ([do_product_searching](llm_tools/llm_func.py)) — for each SKU:
   - Build query `"{product_name}, site: {marketplace}"` and call [GoogleSerperAPIWrapper](llm_tools/llm_func.py) (`k=5`).
   - Format each hit as a Markdown link: `**N. [title](url)**\nsnippet`. Downstream parsing in [check_url](llm_tools/other_func.py) / [get_pro_name](llm_tools/other_func.py) depends on this exact format — changing it breaks filtering.
   - **URL filter**: drop hits whose URL doesn't contain the `web` substring.
   - **Brand filter**: extract brands from the SKU name via [get_brand](llm_tools/other_func.py) (regex word-boundary match against `brands_set` loaded from [llm_tools/brand.xlsx](llm_tools/brand.xlsx)). Keep a hit only if its title OR snippet matches one of those brands (after [remove_accents](llm_tools/other_func.py)). If the SKU has no known brand, all hits pass.
   - Wrap the filtered results into a LangChain `ZERO_SHOT_REACT_DESCRIPTION` agent with two tools: `initial_search` (returns the pre-filtered list) and `search_refine` (re-runs Serper with a refined query). The agent is prompted by [gen_prompt](llm_tools/prompt.py) to return the single best URL or `not found`.
   - Up to 3 retries on exception, else returns `'failed'`. `'not found'` is a valid (non-error) outcome.

3. **LLM** — Qwen `qwen-plus-latest` via DashScope's OpenAI-compatible endpoint (`langchain_openai.ChatOpenAI`, `temperature=0.1`). Swapping models means changing [llm_tools/llm_func.py:20](llm_tools/llm_func.py#L20).

## Maintenance notes (from README)

- **[llm_tools/brand.xlsx](llm_tools/brand.xlsx)** must be kept current — it's the source of truth for brand filtering. Add new brands here when they appear in SKU lists. Columns used: `brandname_en`, `brandname_cn`, `brandname_full`.
- **Every Serper search costs credits** (≈50,000 / $50 on the cheapest tier). Always dry-run on ~50–100 rows before processing a full file, especially after changing `web`, `country`, or the SKU language.
- The `output/output_partitions/` directory must exist before `main.py` runs — it isn't created automatically.

## Working directory caveat

[other_func.py:59](llm_tools/other_func.py#L59) loads `brand.xlsx` via `os.getcwd()`, and [main.py](main.py) uses `os.getcwd()` for `input/` and `output/` paths. The script must be invoked from the project root — running it from elsewhere will fail to find these files.
