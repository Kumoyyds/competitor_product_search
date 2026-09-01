"""Shared in-memory batch execution plus the Excel input/output adapter."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass

import pandas as pd
from tqdm.asyncio import tqdm_asyncio

from . import config
from .db import get_db, run_scope
from .models import MatchResult
from .pipeline import match_product
from .providers import SearchProvider, make_provider_chain
from .trace import record_task


@dataclass
class BatchResult:
    df: pd.DataFrame
    run_id: str
    provider_calls: dict[str, int]
    output_path: str | None


@dataclass(frozen=True)
class SearchRequest:
    product_name: str
    website: str | None
    country: str | None


@dataclass
class SearchItemResult:
    request: SearchRequest
    result: MatchResult | None = None
    error: str | None = None
    error_type: str | None = None


@dataclass
class SearchManyResult:
    items: list[SearchItemResult]
    run_id: str
    provider_calls: dict[str, int]


def _provider_call_counts(providers: list[SearchProvider]) -> dict[str, int]:
    return {provider.name: provider.calls_made() for provider in providers}


def _cell(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text.lower() or None


async def _run_row(
    providers: list[SearchProvider],
    semaphore: asyncio.Semaphore,
    product_name: str,
    website: str | None,
    country: str | None,
    *,
    db,
    run_id: str,
    row_index: int,
    web_col: str,
    country_col: str,
):
    async with semaphore:
        try:
            async with record_task(
                db,
                run_id=run_id,
                row_index=row_index,
                product_name=product_name,
                website=website or "",
                country=country or "",
            ) as recorder:
                if website is None:
                    raise ValueError(f"missing {web_col} value")
                if country is None:
                    raise ValueError(f"missing {country_col} value")
                result = await match_product(
                    product_name,
                    website,
                    country=country,
                    provider=providers,
                )
                recorder.complete(result, recorder.final_provider)
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"_error": str(exc), "_error_type": type(exc).__name__}


async def _execute_search_batch(
    requests: list[SearchRequest],
    *,
    concurrency: int = 16,
    serper_max_calls: int | None = None,
    provider: SearchProvider | list[SearchProvider] | None = None,
    progress: bool = False,
    job_config: dict[str, object],
    input_file: str | None = None,
    input_sku_col: str | None = None,
    output_file: str | None = None,
    web_col: str = "site_name",
    country_col: str = "country",
) -> SearchManyResult:
    """Run the one shared provider/concurrency/trace implementation for a batch."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    own_providers = provider is None
    if own_providers:
        spec = config.get("search", "provider", default="serper")
        provider_names = [spec] if isinstance(spec, str) else list(spec or [])
        providers: list[SearchProvider] = []
    elif isinstance(provider, SearchProvider):
        spec = None
        providers = [provider]
        provider_names = [provider.name]
    else:
        spec = None
        providers = list(provider)
        provider_names = [item.name for item in providers]
    if not own_providers and not providers:
        raise ValueError("provider chain must contain at least one provider")

    initial_calls = _provider_call_counts(providers)

    def calls() -> dict[str, int]:
        current = _provider_call_counts(providers)
        return {
            name: count - initial_calls.get(name, 0)
            for name, count in current.items()
        }

    db = get_db()
    websites = ",".join(
        dict.fromkeys(item.website for item in requests if item.website)
    ) or None
    countries = ",".join(
        dict.fromkeys(item.country for item in requests if item.country)
    ) or None
    try:
        async with run_scope(
            db=db,
            mode="batch",
            total_tasks=len(requests),
            job_config=job_config,
            provider_chain=",".join(str(name) for name in provider_names),
            provider_calls=calls,
            input_file=input_file,
            input_sku_col=input_sku_col,
            output_file=output_file,
            country=countries,
            website=websites,
            concurrency=concurrency,
            serper_max_calls=serper_max_calls,
        ) as run_id:
            if own_providers:
                providers.extend(
                    make_provider_chain(spec, serper_max_calls=serper_max_calls)
                )
                initial_calls.update(_provider_call_counts(providers))
            if not providers:
                raise ValueError("provider chain must contain at least one provider")
            semaphore = asyncio.Semaphore(concurrency)
            tasks = [
                _run_row(
                    providers,
                    semaphore,
                    item.product_name,
                    item.website,
                    item.country,
                    db=db,
                    run_id=run_id,
                    row_index=index,
                    web_col=web_col,
                    country_col=country_col,
                )
                for index, item in enumerate(requests)
            ]
            if not tasks:
                raw_results = []
            elif progress:
                raw_results = await tqdm_asyncio.gather(*tasks)
            else:
                raw_results = await asyncio.gather(*tasks)
            items = []
            for request, raw in zip(requests, raw_results):
                if isinstance(raw, dict) and "_error" in raw:
                    items.append(
                        SearchItemResult(
                            request=request,
                            error=raw["_error"],
                            error_type=raw.get("_error_type"),
                        )
                    )
                else:
                    items.append(SearchItemResult(request=request, result=raw))
            return SearchManyResult(items=items, run_id=run_id, provider_calls=calls())
    finally:
        if own_providers:
            for item in providers:
                await item.aclose()


async def match_products(
    requests: list[SearchRequest],
    *,
    concurrency: int = 16,
    serper_max_calls: int | None = None,
    provider: SearchProvider | list[SearchProvider] | None = None,
    progress: bool = False,
) -> SearchManyResult:
    """Run one recorded Search batch directly from in-memory requests."""
    return await _execute_search_batch(
        requests,
        concurrency=concurrency,
        serper_max_calls=serper_max_calls,
        provider=provider,
        progress=progress,
        job_config={"source": "in_memory", "concurrency": concurrency},
    )


async def match_product_batch(
    input_file: str,
    *,
    sku_col: str,
    web_col: str,
    country_col: str,
    output_file: str | None = None,
    concurrency: int = 16,
    serper_max_calls: int | None = None,
    provider: SearchProvider | list[SearchProvider] | None = None,
    progress: bool = True,
) -> BatchResult:
    """Match every spreadsheet row and optionally write the enriched workbook.

    Input and output paths are used exactly as supplied; no repository-relative
    directory prefix is added.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    input_path = os.fspath(input_file)
    output_path = os.fspath(output_file) if output_file is not None else None
    df = pd.read_excel(input_path)
    required_columns = [sku_col, web_col, country_col]
    missing_columns = [
        column for column in required_columns if column not in df.columns
    ]
    if missing_columns:
        raise KeyError(f"columns not found: {', '.join(missing_columns)}")

    product_names = [str(value) for value in df[sku_col].tolist()]
    websites = [_cell(value) for value in df[web_col].tolist()]
    countries = [_cell(value) for value in df[country_col].tolist()]
    job_config = {
        "input_file": input_path,
        "sku_col": sku_col,
        "web_col": web_col,
        "country_col": country_col,
        "output_file": output_path,
        "concurrency": concurrency,
        "serper_max_calls": serper_max_calls,
    }
    batch = await _execute_search_batch(
        [
            SearchRequest(product_name, website, country)
            for product_name, website, country in zip(
                product_names, websites, countries
            )
        ],
        concurrency=concurrency,
        serper_max_calls=serper_max_calls,
        provider=provider,
        progress=progress,
        job_config=job_config,
        input_file=input_path,
        input_sku_col=sku_col,
        output_file=output_path,
        web_col=web_col,
        country_col=country_col,
    )

    urls: list[str] = []
    verdicts: list[str] = []
    traces: list[str] = []
    reasons: list[str] = []
    for item in batch.items:
        if item.error is not None:
            urls.append("not found")
            verdicts.append("error")
            traces.append("{}")
            reasons.append(item.error)
            continue
        result = item.result
        assert result is not None
        urls.append(
            result.matched_candidate.url
            if result.matched_candidate is not None
            else "not found"
        )
        verdicts.append(result.verdict.value)
        traces.append(json.dumps(result.layer_trace.to_dict()))
        reasons.append(result.reason)

    df["url_search_1"] = urls
    df["match_verdict"] = verdicts
    df["match_layer_trace"] = traces
    df["match_reason"] = reasons

    if output_path is not None:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        await asyncio.to_thread(df.to_excel, output_path, index=False)

    return BatchResult(
        df=df,
        run_id=batch.run_id,
        provider_calls=batch.provider_calls,
        output_path=output_path,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match product URLs from an Excel file"
    )
    parser.add_argument("--input", required=True, help="Input .xlsx path")
    parser.add_argument("--sku-col", required=True, help="Product-name column")
    parser.add_argument(
        "--web-col",
        "--web_col",
        required=True,
        help="Column holding the target marketplace code",
    )
    parser.add_argument(
        "--country-col",
        "--country_col",
        required=True,
        help="Column holding the country code",
    )
    parser.add_argument("--output", help="Optional output .xlsx path")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--serper-max-calls", type=int)
    parser.add_argument("--no-progress", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = asyncio.run(
        match_product_batch(
            args.input,
            sku_col=args.sku_col,
            web_col=args.web_col,
            country_col=args.country_col,
            output_file=args.output,
            concurrency=args.concurrency,
            serper_max_calls=args.serper_max_calls,
            progress=not args.no_progress,
        )
    )
    if result.output_path is not None:
        print(f"saved -> {result.output_path}")
    calls = ", ".join(
        f"{name}={count}" for name, count in result.provider_calls.items()
    )
    print(f"run_id: {result.run_id}")
    print(f"search calls used: {calls}")


if __name__ == "__main__":
    main()
