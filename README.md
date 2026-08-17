# competitor_product_search
This is to help online retailer find the url of product on competitor platforms.

# core logic

- **search engine chain** — configurable, ordered list of search providers. Try free providers first, escalate to paid ones only when needed.
    - **[duckduckgo]** — free, no API key, ~1s rate limit; good for many categories but less stable.
    - **[serper]** — paid Google search via [serper.dev](https://serper.dev/login); cheapest stable option per call.
    - other possible options: *exa*, *tavily*, *brave*, *bing search api*
    - configuration lives in `src/search/maintain/search_config.yaml` (`search.provider` — string for one engine, list for ordered fallback chain)

- **LLM routing** — supported vendor routes and the selected model are generated from the maintained configs: <!-- BEGIN GENERATED: llm-inline -->`qwen`, `deepseek`; active model: `deepseek-v4-flash` via `deepseek`<!-- END GENERATED: llm-inline -->.

- **option selection** using the configured LLM to pick the matching product URL from search results, via a 5-layer LangGraph pipeline.

# How to use
1. go to the dir.
`cd competitor_product_search`

2. prepare the env.
`pip install -r requirements.txt`

3. prepare the .env file
`cp .env.sample .env`

    and then fill your api keys.
    See the generated LLM routing bullet above for the required model key; get a [Serper key](https://serper.dev/login) only when Serper is in your provider chain.

4. make sure your file is ready.
    things to check:
    - **sku name**, you should test which language works well, ex: for amazon.fr, most product names are in French, and after testing on google, french product name is indeed easier to be reached. then we should use french sku name.
    - **target web column**, currently <!-- BEGIN GENERATED: websites-inline -->`tesco`, `argos`, `amazon.co.uk`, `amazon.nl`<!-- END GENERATED: websites-inline -->. Each row can target a different marketplace. Add more in `domain_map`; its key is also the retailer keyword used by keyword-mode queries.
    - **country code column**, one of <!-- BEGIN GENERATED: countries-inline -->`uk` (= `gb`), `de`, `fr`, `us`, `nl`, `jp`, `es`, `it`, `pt`, `se`, `pl`, `br`, `au`, `ca`<!-- END GENERATED: countries-inline --> — each row can use a different country, and each search engine maps these internally to its own format (DuckDuckGo `region`, Serper `gl`). Other codes still run but fall back to English results. Full table: [src/search/README.md §3](src/search/README.md#3-input).
    - **be in .xlsx**

5. tune **src/search/maintain/search_config.yaml** when needed (provider chain, per-provider query mode, thresholds, domain map)
6. run with explicit per-run arguments (input/output are complete paths and are not prefixed):
`python -m src.search.batch --input input/products.xlsx --sku-col product_name --web-col web --country-col country --output output/result.xlsx`
7. get your result at the path passed to `--output`

# validation (sample run before full batch)
`python scripts/validate_search.py --sample 20 --budget 50`
Reads `src/0_Data/tesco_algo.xlsx`, runs a stratified sample, writes `output/validation_report.xlsx` + per-layer verdict summary. Always validate before running the full file.

# maintenance

- **brand.xlsx** under `src/search/maintain/` needs maintenance — when there are new brands, add them to the `brandname_en` column. Restart Python process after editing (lru_cache resets).

- **domain_map** in `src/search/maintain/search_config.yaml` — add new marketplaces here.

- **generated capability regions** in both READMEs are refreshed from provider code and `maintain/` YAML by `scripts/gen_capability_docs.py`; hand-edits inside `BEGIN/END GENERATED` markers are overwritten.

- always try some sample, like 20/50 before running the whole file, in case anything goes unexpectedly. **remember, every search costs** (serper: ~50000 credits / $50; duckduckgo is free but has a rate limit).

# tests
`python -m pytest` — default offline test suite, zero API cost.

`python -m pytest -m live` — explicitly run tests that use real API keys and may incur cost.

# future work to be done
1. **more search engines** — the chain is N-provider generic; just subclass `SearchProvider` and register in `make_provider()`.

2. **filtering algo**, tbh i have done almost the most for text-only dimension, vision stuff could be added in the future, with vision stuff, even efforts on reviewing process could be saved as well.

3. **local host llm**, this is mostly for the long-term internal security consideration. deployment is not complicated, go to [vllm](https://docs.vllm.ai/en/latest/)


Is there still any issue, contact **Yuding** by wechat (mylordship), all the best.
