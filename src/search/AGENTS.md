# Search Module

**Status**: Implemented (migrated from root `llm_tools/`)

## Responsibility

Finds competitor product URLs on target marketplaces (e.g., amazon.de, tesco.com) by searching Google via Serper API, then using a LangChain agent with Qwen LLM to select the best matching product URL.

## Files

- `main.py` — Pipeline entry point: reads config, splits input DataFrame, runs parallel search, combines output. Executed via `python run.py` from project root.
- `searcher.py` — Core search logic: `do_product_searching()` calls Serper API, filters by URL and brand, then uses LLM agent to pick URL. `find_url_llm()` applies this per DataFrame row.
- `utils.py` — Filtering utilities: URL validation, brand matching, text normalization, partition size calculation. Loads `brand.xlsx` at import time.
- `prompts.py` — `gen_prompt()` builds the LLM instruction template for product matching.
- `brand.xlsx` — Brand name lookup table (English, Chinese, full name variants). Needs manual maintenance when new brands appear.

## Inputs / Outputs

- **Input**: `config.yaml` (root) + `input/<filename>.xlsx` (SKU names + marketplace)
- **Output**: `output/output_partitions/result_N.xlsx` (intermediate) + `output/<output_file>.xlsx` (final)

## External Dependencies

- Qwen LLM via OpenAI-compatible endpoint (dashscope.aliyuncs.com)
- Google search via Serper API
- LangChain agents framework
