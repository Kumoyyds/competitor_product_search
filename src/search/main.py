"""Run the search pipeline over an input Excel.

Adapts the old MVP entrypoint to the new async LangGraph pipeline. Driven by
config.yaml (input/output paths, sku-name column, country, target web). Adds:

  serper_max_calls: <int>   # optional; cap total Serper calls across the run
  concurrency: <int>        # optional; per-process semaphore (default 16)

Output columns added:
  url_search_1          — matched URL (or "not found")
  match_verdict         — match / no_match
  match_layer_trace     — JSON of per-layer verdicts
  match_reason          — LLM / pipeline reason text
"""

from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pandas as pd
import yaml
from tqdm.asyncio import tqdm_asyncio

from .pipeline import match_product
from .providers import make_provider
from .utils import get_split_num


async def _run_row(provider, sem, name, web, country):
    async with sem:
        try:
            return await match_product(name, web, country=country, provider=provider)
        except Exception as e:
            return {"_error": str(e)}


async def _amain():
    with open("config_search.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    input_file_name = cfg["input_file"]
    name_col = cfg["input_sku_name_col"]
    country = cfg["country"]
    web = cfg["web"]
    output_file_name = cfg["output_file"]
    serper_max = cfg.get("serper_max_calls")
    concurrency = int(cfg.get("concurrency", 16))

    input_path = os.path.join(os.getcwd(), f"input/{input_file_name}")
    df = pd.read_excel(input_path)

    provider_name = cfg.get("search_provider", "serper")
    provider = make_provider(provider_name, max_calls=serper_max) if provider_name == "serper" \
        else make_provider(provider_name)

    sem = asyncio.Semaphore(concurrency)
    tasks = [
        _run_row(provider, sem, str(df.loc[i, name_col]), web, country)
        for i in range(len(df))
    ]
    results = await tqdm_asyncio.gather(*tasks)
    await provider.aclose()

    urls, verdicts, traces, reasons = [], [], [], []
    for r in results:
        if isinstance(r, dict) and "_error" in r:
            urls.append("not found")
            verdicts.append("error")
            traces.append("{}")
            reasons.append(r["_error"])
            continue
        urls.append(r.matched_candidate.url if r.matched_candidate else "not found")
        verdicts.append(r.verdict.value)
        traces.append(json.dumps(r.layer_trace.to_dict()))
        reasons.append(r.reason)

    df["url_search_1"] = urls
    df["match_verdict"] = verdicts
    df["match_layer_trace"] = traces
    df["match_reason"] = reasons

    out_path = os.path.join(os.getcwd(), f"output/{output_file_name}")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_excel(out_path, index=False)
    print(f"saved -> {out_path}")
    print(f"serper calls used: {provider.calls_made()}")


if __name__ == "__main__":
    asyncio.run(_amain())
