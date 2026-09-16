from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

from src.matching import MatchingBatchError, verify_products
from src.models import InputItem, ProductData, ProductMatchVerdict
from src.scraping import InvalidTargetResult, scrape, validate_model_ladders
from src.scraping.storage.run_store import capture_run_ids
from src.search import FinalVerdict
from src.search.batch import SearchRequest, match_products

from .database import OrchestratorDB, RerunSource
from .input import load_input
from .models import BatchResult


@dataclass(slots=True)
class WorkItem:
    item_id: int
    item: InputItem


@dataclass(slots=True)
class ScrapeResult:
    work: WorkItem
    product: ProductData | None = None
    error: Exception | None = None
    invalid: InvalidTargetResult | None = None
    run_ids: tuple[int, ...] = ()


async def _scrape_many(
    works: Sequence[WorkItem],
    urls: Sequence[str],
    concurrency: int,
    *,
    progress: bool = False,
    desc: str = "scrape",
) -> list[ScrapeResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def one(work: WorkItem, url: str) -> ScrapeResult:
        async with semaphore:
            with capture_run_ids() as run_ids:
                try:
                    outcome = await scrape(url)
                    if isinstance(outcome, InvalidTargetResult):
                        return ScrapeResult(work=work, invalid=outcome, run_ids=tuple(run_ids))
                    return ScrapeResult(work=work, product=outcome, run_ids=tuple(run_ids))
                except Exception as exc:
                    return ScrapeResult(work=work, error=exc, run_ids=tuple(run_ids))

    tasks = [one(work, url) for work, url in zip(works, urls)]
    if not tasks:
        return []
    if progress:
        return await tqdm_asyncio.gather(*tasks, desc=desc)
    return await asyncio.gather(*tasks)


def _trace_error(value: Exception | InvalidTargetResult) -> dict[str, Any]:
    if isinstance(value, InvalidTargetResult):
        return {"type": "InvalidTargetResult", "reason": value.reason_signal}
    return {"type": type(value).__name__, "reason": str(value)}


def _record_matching_error(
    db: OrchestratorDB,
    item_id: int,
    error: Exception,
    *,
    execution_path: str,
    url: str | None,
) -> None:
    trace = [
        node.model_dump(mode="json")
        for node in getattr(error, "trace", [])
    ]
    db.record_matching_decision(
        item_id,
        execution_path=execution_path,
        url=url,
        verdict="error",
        decision_source="technical_error",
        reasoning=str(error),
        decision_process={"nodes": trace, "terminated_at": "technical_error"},
    )


async def _match_and_persist(
    db: OrchestratorDB,
    works: list[WorkItem],
    products: list[ProductData],
    *,
    search_titles: list[str | None],
    urls: list[str],
    execution_path: str,
    vision_enabled: bool,
    concurrency: int,
    progress: bool = False,
    desc: str = "match",
) -> None:
    """Persist terminal Match/No-Match outcomes for one full pipeline pass."""
    if not works:
        return
    partial: list[Any]
    errors: dict[int, Exception] = {}
    bar = tqdm(total=len(works), desc=desc) if progress else None
    try:
        try:
            partial = await verify_products(
                [(work.item, product) for work, product in zip(works, products)],
                vision_enabled=vision_enabled,
                concurrency=concurrency,
                on_progress=(bar.update if bar is not None else None),
            )
        except MatchingBatchError as exc:
            partial = exc.results
            errors = exc.errors
    finally:
        if bar is not None:
            bar.close()

    for index, (work, product, title, url) in enumerate(zip(works, products, search_titles, urls)):
        if index in errors:
            _record_matching_error(
                db,
                work.item_id,
                errors[index],
                execution_path=execution_path,
                url=url,
            )
            db.record_failure(
                work.item_id,
                fail_node="match",
                failure_kind="technical_error",
                reasoning=str(errors[index]),
                search_title=title,
                url=url,
            )
            continue
        result = partial[index]
        assert result is not None
        db.record_matching_result(
            work.item_id,
            execution_path=execution_path,
            url=url,
            result=result,
        )
        if result.verdict == ProductMatchVerdict.MATCH:
            db.record_valid(
                work.item_id,
                product,
                search_title=title,
                execution_path=execution_path,
            )
        else:
            db.record_failure(
                work.item_id,
                fail_node="match",
                failure_kind="no_match",
                reasoning=result.reasoning,
                search_title=title,
                url=url,
            )


async def _run_full_pipeline(
    db: OrchestratorDB,
    works: list[WorkItem],
    *,
    vision_enabled: bool,
    concurrency: int,
    execution_path: str,
    progress: bool = False,
) -> None:
    if not works:
        return
    search_batch = await match_products(
        [SearchRequest(work.item.title, work.item.site_name, work.item.country) for work in works],
        concurrency=concurrency,
        progress=progress,
        progress_desc=f"{execution_path} search",
    )
    scrape_works: list[WorkItem] = []
    search_titles: list[str] = []
    urls: list[str] = []
    for row_index, (work, search_item) in enumerate(zip(works, search_batch.items)):
        result = search_item.result
        search_status = (
            "error" if search_item.error else
            "success" if result is not None and result.verdict == FinalVerdict.MATCH
            and result.matched_candidate is not None else "no_match"
        )
        db.update_item(
            work.item_id,
            trace_event={
                "stage": "search", "status": search_status,
                "run_id": search_batch.run_id, "row_index": row_index,
            },
        )
        if search_item.error:
            db.record_failure(
                work.item_id,
                fail_node="search",
                failure_kind="technical_error",
                reasoning=search_item.error,
            )
            continue
        if result is None or result.verdict != FinalVerdict.MATCH or result.matched_candidate is None:
            db.record_failure(
                work.item_id,
                fail_node="search",
                failure_kind="no_match",
                reasoning=(result.reason if result is not None else "search returned no result") or "search returned no match",
            )
            continue
        title = result.matched_candidate.title
        url = result.matched_candidate.url
        db.update_item(
            work.item_id,
            status="running",
            execution_path=execution_path,
            search_title=title,
            matched_url=url,
        )
        scrape_works.append(work)
        search_titles.append(title)
        urls.append(url)

    scraped = await _scrape_many(
        scrape_works, urls, concurrency, progress=progress, desc=f"{execution_path} scrape"
    )
    match_works: list[WorkItem] = []
    match_products_data: list[ProductData] = []
    match_titles: list[str] = []
    match_urls: list[str] = []
    for outcome, title, url in zip(scraped, search_titles, urls):
        db.update_item(
            outcome.work.item_id,
            trace_event={
                "stage": "scraping",
                "status": "success" if outcome.product is not None else
                    "invalid_target" if outcome.invalid is not None else "error",
                "run_ids": list(outcome.run_ids),
            },
        )
        if outcome.product is None:
            failure = outcome.invalid or outcome.error or RuntimeError("scrape returned no result")
            db.record_failure(
                outcome.work.item_id,
                fail_node="scraping",
                failure_kind="invalid_target" if outcome.invalid else "technical_error",
                reasoning=_trace_error(failure)["reason"],
                search_title=title,
                url=url,
                detail=_trace_error(failure),
            )
            continue
        match_works.append(outcome.work)
        match_products_data.append(outcome.product)
        match_titles.append(title)
        match_urls.append(url)

    await _match_and_persist(
        db,
        match_works,
        match_products_data,
        search_titles=match_titles,
        urls=match_urls,
        execution_path=execution_path,
        vision_enabled=vision_enabled,
        concurrency=concurrency,
        progress=progress,
        desc=f"{execution_path} match",
    )


async def run_new_input(
    source: str | Path | Sequence[InputItem],
    *,
    vision_enabled: bool = False,
    concurrency: int = 8,
    progress: bool = False,
    db_path: str | Path | None = None,
) -> BatchResult:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    validate_model_ladders()
    parsed, source_file = load_input(source)
    db = OrchestratorDB(db_path)
    batch_id = db.create_new_batch(
        vision_enabled=vision_enabled,
        source_file=source_file,
        job_config={"concurrency": concurrency},
    )
    try:
        works: list[WorkItem] = []
        for row in parsed:
            item_id = db.add_item(batch_id, row_index=row.row_index, raw=row.raw, item=row.item)
            if row.item is None:
                db.record_failure(
                    item_id,
                    fail_node="input",
                    failure_kind="validation_error",
                    reasoning=row.error or "invalid input",
                )
            else:
                works.append(WorkItem(item_id, row.item))
        await _run_full_pipeline(
            db,
            works,
            vision_enabled=vision_enabled,
            concurrency=concurrency,
            execution_path="new_input",
            progress=progress,
        )
        return BatchResult(**db.finish_batch(batch_id))
    except BaseException as exc:
        db.finish_batch(
            batch_id,
            error_message=f"{type(exc).__name__}: {exc}",
            interrupted=isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)),
        )
        raise
    finally:
        db.close()


def _identity(product: ProductData) -> str:
    value = {
        "title": " ".join(product.title.split()).casefold(),
        "brand": " ".join((product.brand or "").split()).casefold(),
        "gtin": "".join((product.gtin or "").split()),
        "variant": product.variant or {},
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


async def rerun(
    batch_id: str,
    *,
    search_titles: Sequence[str] | None = None,
    vision_enabled: bool | None = None,
    concurrency: int = 8,
    progress: bool = False,
    db_path: str | Path | None = None,
) -> BatchResult:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    validate_model_ladders()
    db = OrchestratorDB(db_path)
    try:
        sources = db.rerun_sources(batch_id, search_titles)
        parent = db.get_batch(batch_id)
        assert parent is not None
        active_vision = bool(parent["vision_enabled"]) if vision_enabled is None else vision_enabled
        child_id = db.create_rerun_batch(
            batch_id,
            vision_enabled=active_vision,
            job_config={
                "concurrency": concurrency,
                "search_titles": list(search_titles) if search_titles is not None else None,
            },
        )
        work_by_id: dict[int, tuple[WorkItem, RerunSource]] = {}
        works: list[WorkItem] = []
        for source in sources:
            item_id = db.add_item(
                child_id,
                row_index=source.row_index,
                raw=source.item.model_dump(),
                item=source.item,
                logical_item_id=source.logical_item_id,
                source_valid_result_id=source.valid_result_id,
                execution_path="stored_url",
            )
            work = WorkItem(item_id, source.item)
            works.append(work)
            work_by_id[item_id] = (work, source)

        direct = await _scrape_many(
            works,
            [source.url for source in sources],
            concurrency,
            progress=progress,
            desc="stored_url scrape",
        )
        revalidate_works: list[WorkItem] = []
        revalidate_products: list[ProductData] = []
        revalidate_titles: list[str | None] = []
        revalidate_urls: list[str] = []
        fallback: list[WorkItem] = []
        for outcome in direct:
            work, source = work_by_id[outcome.work.item_id]
            failure = (
                outcome.invalid or outcome.error or RuntimeError("scrape returned no result")
                if outcome.product is None else None
            )
            db.update_item(
                work.item_id,
                trace_event={
                    "stage": "stored_url_scraping",
                    "status": "success" if outcome.product is not None else
                        "invalid_target" if outcome.invalid is not None else "failed",
                    "run_ids": list(outcome.run_ids),
                    **(_trace_error(failure) if failure is not None else {}),
                },
            )
            if outcome.product is None:
                db.update_item(
                    work.item_id,
                    execution_path="fallback",
                )
                fallback.append(work)
            elif _identity(outcome.product) == _identity(source.product):
                identity_reason = (
                    "Stored-URL rescrape reproduced the prior qualified identity; "
                    "Matching was skipped."
                )
                db.record_matching_decision(
                    work.item_id,
                    execution_path="stored_url",
                    url=source.url,
                    verdict="match",
                    decision_source="identity_guard",
                    reasoning=identity_reason,
                    decision_process={
                        "nodes": [
                            {
                                "node": "identity_guard",
                                "status": "reused",
                                "terminal": True,
                                "detail": {
                                    "source_valid_result_id": source.valid_result_id
                                },
                            }
                        ],
                        "terminated_at": "identity_guard",
                    },
                )
                db.record_valid(
                    work.item_id,
                    outcome.product,
                    search_title=source.search_title,
                    execution_path="stored_url",
                )
            else:
                db.update_item(
                    work.item_id,
                    execution_path="identity_revalidation",
                    trace_event={"stage": "identity_guard", "status": "changed"},
                )
                revalidate_works.append(work)
                revalidate_products.append(outcome.product)
                revalidate_titles.append(source.search_title)
                revalidate_urls.append(source.url)

        if revalidate_works:
            bar = tqdm(total=len(revalidate_works), desc="identity_revalidation") if progress else None
            try:
                try:
                    results = await verify_products(
                        [(work.item, product) for work, product in zip(revalidate_works, revalidate_products)],
                        vision_enabled=active_vision,
                        concurrency=concurrency,
                        on_progress=(bar.update if bar is not None else None),
                    )
                    partial: list[Any] = results
                    errors: dict[int, Exception] = {}
                except MatchingBatchError as exc:
                    partial, errors = exc.results, exc.errors
            finally:
                if bar is not None:
                    bar.close()
            for index, (work, product, title, url) in enumerate(
                zip(revalidate_works, revalidate_products, revalidate_titles, revalidate_urls)
            ):
                if index in errors:
                    _record_matching_error(
                        db,
                        work.item_id,
                        errors[index],
                        execution_path="identity_revalidation",
                        url=url,
                    )
                    db.record_failure(
                        work.item_id, fail_node="match", failure_kind="technical_error",
                        reasoning=str(errors[index]), search_title=title, url=url,
                    )
                elif partial[index].verdict == ProductMatchVerdict.MATCH:
                    db.record_matching_result(
                        work.item_id,
                        execution_path="identity_revalidation",
                        url=url,
                        result=partial[index],
                    )
                    db.record_valid(
                        work.item_id, product, search_title=title,
                        execution_path="identity_revalidation",
                    )
                else:
                    db.record_matching_result(
                        work.item_id,
                        execution_path="identity_revalidation",
                        url=url,
                        result=partial[index],
                    )
                    db.update_item(
                        work.item_id,
                        execution_path="fallback",
                        trace_event={"stage": "identity_revalidation", "status": "no_match"},
                    )
                    fallback.append(work)

        await _run_full_pipeline(
            db,
            fallback,
            vision_enabled=active_vision,
            concurrency=concurrency,
            execution_path="fallback",
            progress=progress,
        )
        return BatchResult(**db.finish_batch(child_id))
    except BaseException as exc:
        # No child exists for preflight selection failures; those intentionally leave no rows.
        if "child_id" in locals():
            db.finish_batch(
                child_id,
                error_message=f"{type(exc).__name__}: {exc}",
                interrupted=isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)),
            )
        raise
    finally:
        db.close()
