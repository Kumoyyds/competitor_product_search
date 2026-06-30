# competitor_product_search
This is to help online retailer find the url of product on competitor platforms.

# core logic

- **search engine chain** — configurable, ordered list of search providers. Try free providers first, escalate to paid ones only when needed.
    - **[duckduckgo]** — free, no API key, ~1s rate limit; good for many categories but less stable.
    - **[serper]** — paid Google search via [serper.dev](https://serper.dev/login); cheapest stable option per call.
    - other possible options: *exa*, *tavily*, *brave*, *bing search api*
    - configuration lives in `src/search/maintain/search_config.yaml` (`search.provider` — string for one engine, list for ordered fallback chain)

- **option selection** using LLM (Qwen) to pick the matching product URL from search results, via a 5-layer LangGraph pipeline.

# How to use
1. go to the dir.
`cd competitor_product_search`

2. prepare the env.
`pip install -r requirements.txt`

3. prepare the .env file
`cp .env.sample .env`

    and then fill your api keys.
    get your api keys: [qwen](https://bailian.console.aliyun.com/), [serper](https://serper.dev/login) (serper only needed if it's in your provider chain)

4. make sure your file is ready.
    things to check:
    - **sku name**, you should test which language works well, ex: for amazon.fr, most product names are in French, and after testing on google, french product name is indeed easier to be reached. then we should use french sku name.
    - **target web**, ex: amazon.fr, tesco.com, etc. make sure you give the right one, and make sure the suffix is correct.
    - **country code**, ex: uk, fr, de, nl — each search engine maps these internally to its own format.
    - **be in .xlsx**

5. put your file in the **input/** directory
6. set **config_search.yaml** (per-run: input file, country, target web, output file) and **src/search/maintain/search_config.yaml** (pipeline tuning: provider chain, thresholds)
7. run
`python run.py`
8. get your result in the dir **output/** (result.xlsx)

# validation (sample run before full batch)
`python scripts/validate_search.py --sample 20 --budget 50`
Reads `src/0_Data/tesco_algo.xlsx`, runs a stratified sample, writes `output/validation_report.xlsx` + per-layer verdict summary. Always validate before running the full file.

# maintenance

- **brand.xlsx** under `src/search/maintain/` needs maintenance — when there are new brands, add them to the `brandname_en` column. Restart Python process after editing (lru_cache resets).

- **domain_map** in `src/search/maintain/search_config.yaml` — add new marketplaces here.

- always try some sample, like 20/50 before running the whole file, in case anything goes unexpectedly. **remember, every search costs** (serper: ~50000 credits / $50; duckduckgo is free but has a rate limit).

# tests
`python -m pytest tests/unit/search/ -v` — 30 offline unit tests, zero API cost.

# future work to be done
1. **more search engines** — the chain is N-provider generic; just subclass `SearchProvider` and register in `make_provider()`.

2. **filtering algo**, tbh i have done almost the most for text-only dimension, vision stuff could be added in the future, with vision stuff, even efforts on reviewing process could be saved as well.

3. **local host llm**, this is mostly for the long-term internal security consideration. deployment is not complicated, go to [vllm](https://docs.vllm.ai/en/latest/)


Is there still any issue, contact **Yuding** by wechat (mylordship), all the best.
