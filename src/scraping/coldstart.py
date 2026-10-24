"""Cold start workflow (spec §5.9, D22).

New site with empty parsers table:
    1. User provides an Excel sheet declaring page_type + URL
    2. Fetch pages via HTMLScraper._get_unlocker()
    3. Generate from representative pages with real URLs for every page type
    4. Sandbox every snapshot and compare against persistent human expectations
    5. Retry regressions automatically; review retained base and typed corrections
    6. Rescore historic code/result pairs against latest truth within a total cap
    7. Seed best parser with only its own fully matching confirmed goldens

Run:
    python -m src.scraping.coldstart --site tesco --input tesco.xlsx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openpyxl import load_workbook

from src.common.llm_client import UnknownModelError

from .config import get_config
from .exceptions import ColdStartInputError
from .extraction import with_extraction_retry
from .models.enums import PAGE_TYPES
from .models.product_data import ProductData
from .providers import make_chat_client, validate_model_ladders
from .registry import get_scrapers
from .coldstart_ledger import (
    ExpectationLedger, RoundRecord, changes, diff_case, parse_correction,
)
from .repair.evidence import locate_value_snippets
from .repair.golden import classify_page_type
from .repair.prepass import build_price_aware_context
from .repair.prompts import coldstart_repair_prompt, initial_parser_gen_prompt
from .repair.sandbox import run_in_sandbox
from .site_profile import (
    golden_min_for as site_golden_min_for,
    is_mandatory_page_type as site_page_type_mandatory,
    page_type_available,
)
from .storage import GoldenStore, ParserStore, ScrapeDB
from .validation import validate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColdStartRow:
    page_type: str
    url: str
    row_no: int


@dataclass(frozen=True)
class FieldCorrection:
    field: str
    correct_value: Any


@dataclass(frozen=True)
class ReviewFeedback:
    url: str
    page_type: str
    corrections: tuple[FieldCorrection, ...]
    hint: str
    unresolved: bool = False


@dataclass(frozen=True)
class _ReviewCase:
    row: ColdStartRow
    html: str
    product: Optional[ProductData]
    failure_kind: Optional[str] = None
    failure_detail: Optional[str] = None


@dataclass(frozen=True)
class _Candidate:
    code: str
    cases: tuple[_ReviewCase, ...]

    def results(self) -> dict[str, Any]:
        return {case.row.url: case.product for case in self.cases
                if case.failure_kind != "extraction_failed"}


def _select_best(versions, base, ledger):
    # Stable ties prefer the current base, then the earliest retained version.
    ordered = ([base] if base is not None else []) + list(versions)
    return max(ordered, key=lambda version: ledger.score(version.results()))


def _accepted_cases(candidate, ledger, existing_coverage, cfg):
    counts = Counter(existing_coverage)
    accepted = []
    for case in candidate.cases:
        row = case.row
        if counts[row.page_type] >= cfg.golden_max_for(row.page_type):
            continue
        if ledger.matches(row.url, case.product):
            accepted.append((row, case.html, case.product))
            counts[row.page_type] += 1
    return accepted


def _pending_cases(candidate, ledger, existing_coverage, cfg):
    accepted = _accepted_cases(candidate, ledger, existing_coverage, cfg)
    counts = Counter(existing_coverage)
    counts.update(row.page_type for row, _, _ in accepted)
    return [case for case in candidate.cases
            if case.failure_kind != "extraction_failed"
            and counts[case.row.page_type] < cfg.golden_max_for(case.row.page_type)
            and not ledger.matches(case.row.url, case.product)]


async def run_coldstart(
    site: str, rows: list[ColdStartRow], input_fn=input, force_fetch: bool = False,
) -> dict[str, Any]:
    """Run bounded generate/review rounds and save the best version under latest truth."""
    # A URL is the identity of a snapshot/expectation. Deduplicate identical
    # declarations before fetching; conflicting declarations cannot share truth.
    unique = {}
    for row in rows:
        previous = unique.get(row.url)
        if previous is not None and previous.page_type != row.page_type:
            raise ColdStartInputError(f"Conflicting page types for URL: {row.url}")
        unique.setdefault(row.url, row)
    rows = list(unique.values())
    scraper_cls = _pick_html_scraper(site)
    scraper = scraper_cls()
    cfg = get_config()
    ladder, temperatures = cfg.cold_start_model_ladder, cfg.cold_start_temperature_ladder
    assert ladder, "cold_start_model_ladder must contain at least one model"
    assert len(temperatures) == len(ladder), "cold-start model/temperature ladder length mismatch"
    print(f"\n== Cold start for site '{site}' ==")
    print(f"Resolving {len(rows)} URLs via golden cache / {scraper_cls.__name__}...")
    fetched = await _resolve_html(scraper, rows, force_fetch=force_fetch)
    pages = _pick_representative_pages(fetched, rows)
    if not pages:
        print("No valid HTML fetched — aborting cold start.")
        return _empty_result(aborted=True)
    existing = _get_existing_coverage(site)
    ledger = ExpectationLedger()
    records: list[RoundRecord] = []
    versions: list[_Candidate] = []
    base = best = None
    merge_record = None
    auto_streak = consecutive_empty = 0
    partial = False
    max_rounds = cfg.cold_start_max_repair_rounds

    # Every generation attempt consumes one slot, including empty responses and
    # automatic merge retries. There is no retry path outside this bounded loop.
    for attempt in range(max_rounds):
        rung = min(attempt, len(ladder) - 1)
        model, temperature = ladder[rung], temperatures[rung]
        thinking = attempt >= len(ladder) - 1
        at_cap = attempt + 1 >= max_rounds
        prior_base = base
        print(f"\nGenerating parser via {model} (round {attempt + 1}/{max_rounds})...")
        if base is None:
            code = await _gen_initial_parser(site, pages, model=model,
                                            temperature=temperature, enable_thinking=thinking)
        else:
            code = await _gen_repaired_parser(
                site=site, pages=pages, current_code=base.code, ledger=ledger,
                cases=base.cases, records=records, merge_record=merge_record,
                index=attempt, model=model, temperature=temperature,
                enable_thinking=thinking, role="last" if thinking else "middle")
        if not code:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                print("LLM returned no usable parser code twice consecutively — aborting.")
                return _failed_result(existing, site=site, aborted=True)
            print("LLM returned no usable parser code.")
            if not at_cap:
                continue
        else:
            consecutive_empty = 0
            print("\nRunning parser against all resolved HTMLs (sandbox)...")
            candidate = _Candidate(code, tuple(await _run_review_cases(site, rows, fetched, code)))
            versions.append(candidate)
            fixed, broke = (changes(base.results(), candidate.results(), ledger)
                            if base is not None else (set(), set()))
            adopted = base is None or (not broke and
                ledger.score(candidate.results()) >= ledger.score(base.results()))
            if adopted:
                base = candidate
            elif broke and auto_streak < 2:
                record = RoundRecord.create(attempt + 1, code, base.code,
                                            candidate.results(), ledger, fixed, broke, False, "auto")
                records.append(record)
                merge_record = record
                auto_streak += 1
                print(f"Regression candidate retained for merge; retrying automatically ({auto_streak}/2).")
                if not at_cap:
                    continue
                # At the safety cap, offer saving without an extra generation or
                # asking the human to inspect an automatically rejected candidate.
            elif broke:
                base = _select_best(versions, base, ledger)

            auto_rejected_at_cap = bool(broke and not adopted and auto_streak <= 2
                                        and records and records[-1].round == attempt + 1
                                        and records[-1].reviewer == "auto")
            if not auto_rejected_at_cap:
                counts = Counter(existing)
                for i, case in enumerate(base.cases, 1):
                    row = case.row
                    if counts[row.page_type] >= cfg.golden_max_for(row.page_type):
                        print(f"  {row.url}: bucket '{row.page_type}' full — skipping (spare)")
                        continue
                    print(f"\n[{i}/{len(base.cases)}] {row.url}\n  declared page_type = {row.page_type}")
                    if case.product is None:
                        label = "FETCH ERROR (excluded)" if case.failure_kind == "extraction_failed" else "PARSER ERROR"
                        print(f"  {label}: {case.failure_detail}")
                        continue
                    product = case.product
                    classified = classify_page_type(product)
                    if classified != row.page_type:
                        print(f"  !! MISMATCH: extracted fields look like '{classified}', not declared '{row.page_type}'")
                    _print_product_panel(product, row.page_type)
                    if ledger.matches(row.url, product):
                        print("  → matches confirmed expectation; accepted automatically")
                    else:
                        exp = ledger.cases.get(row.url)
                        if exp and exp.status == "confirmed":
                            for name, (expected, got) in diff_case(product, exp).items():
                                print(f"  !! {name}: expected {_format_display_value(expected)}, got {_format_display_value(got)}")
                        answer = input_fn("  Accept? [y/N/q] ").strip().lower()
                        if answer == "q":
                            print("Parser was not saved.")
                            return _failed_result(existing, site=site, aborted=True,
                                                  accepted=len(_accepted_cases(base, ledger, existing, cfg)))
                        if answer == "y":
                            ledger.review(row.url, row.page_type, product, accepted=True)
                        else:
                            feedback = _collect_feedback(row, input_fn, case.html)
                            ledger.review(row.url, row.page_type, product, accepted=False,
                                          corrections={c.field: c.correct_value for c in feedback.corrections},
                                          hint=feedback.hint, unresolved=feedback.unresolved)
                    if ledger.matches(row.url, product):
                        counts[row.page_type] += 1
                        if not page_type_available(site, classified):
                            print(f"  !! SITE PROFILE WARNING: accepted '{classified}' is declared unavailable")
                # Truth can change after every answer: choose from all retained
                # immutable code/result pairs using the latest ledger each time.
                best = _select_best(versions, base, ledger)
                fixed, broke = (changes(prior_base.results(), candidate.results(), ledger)
                                if prior_base is not None else (set(), set()))
                records.append(RoundRecord.create(
                    attempt + 1, code, prior_base.code if prior_base else "",
                    candidate.results(), ledger, fixed, broke, base is candidate, "human"))
                auto_streak = 0
                merge_record = records[-1] if broke else None
                _print_failure_summary(base.cases, ledger, records)
                if not _pending_cases(base, ledger, existing, cfg):
                    # Saving always uses outputs belonging to the chosen code.
                    break
        if versions:
            best = _select_best(versions, base, ledger)
        if at_cap:
            print(f"\nCold-start safety cap reached ({max_rounds} rounds).")
        if best is None:
            return _failed_result(existing, site=site, aborted=True)
        action = input_fn(
            "Save best results or quit? [s=保存最佳结果并退出 / q=放弃退出] " if at_cap else
            "\nContinue 纠错? [c=继续修复 / s=保存最佳结果并退出 / q=放弃退出] "
        ).strip().lower()
        if action == "s":
            partial = True
            break
        if not at_cap and action in {"c", "y"}:
            continue
        print("Parser was not saved.")
        return _failed_result(existing, site=site, aborted=True,
                              accepted=len(_accepted_cases(best, ledger, existing, cfg)))
    else:
        return _failed_result(existing, site=site, aborted=True)

    best = _select_best(versions, base, ledger)
    accepted = _accepted_cases(best, ledger, existing, cfg)
    parser_id, seeded_count = _seed(site, best.code, accepted)
    final_coverage = Counter(existing)
    final_coverage.update(row.page_type for row, _, _ in accepted)
    coverage_shortfall = _coverage_shortfall(dict(final_coverage), site)
    if coverage_shortfall:
        print("\nWARNING: mandatory golden coverage is still incomplete: " + ", ".join(coverage_shortfall))
    print(f"\n== Cold start {'partial result saved' if partial else 'complete'} ==")
    print(f"  parser_id = {parser_id}\n  goldens seeded = {seeded_count}")
    return dict(parser_id=parser_id, seeded_goldens=seeded_count, accepted=len(accepted),
                aborted=False, partial=partial, coverage_shortfall=coverage_shortfall)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pick_html_scraper(site: str):
    from .scrapers.html_scraper import HTMLScraper

    for cls in get_scrapers(site):
        if issubclass(cls, HTMLScraper):
            return cls
    raise ValueError(f"No HTMLScraper registered for site: {site}")


def _empty_result(aborted: bool) -> dict[str, Any]:
    return {
        "parser_id": None,
        "seeded_goldens": 0,
        "accepted": 0,
        "aborted": aborted,
        "partial": False,
        "coverage_shortfall": [],
    }


def _failed_result(
    existing_coverage: dict[str, int],
    *,
    site: str = "",
    aborted: bool,
    accepted: int = 0,
) -> dict[str, Any]:
    return {
        **_empty_result(aborted=aborted),
        "accepted": accepted,
        "coverage_shortfall": _coverage_shortfall(existing_coverage, site),
    }


def _get_existing_coverage(site: str) -> dict[str, int]:
    cfg = get_config()
    db = ScrapeDB(cfg.db_path)
    db.init_db()
    try:
        return GoldenStore(db).get_page_type_coverage(site)
    finally:
        db.close()


def _coverage_shortfall(
    coverage: dict[str, int], site: str = ""
) -> list[str]:
    return [
        page_type
        for page_type in PAGE_TYPES
        if site_page_type_mandatory(site, page_type)
        and coverage.get(page_type, 0) < site_golden_min_for(site, page_type)
    ]


async def _batch_fetch(scraper, urls: list[str]) -> list[tuple[str, int, str]]:
    """Fetch all URLs; returns list of (url, status_code, html)."""
    if not urls:
        return []
    cfg = get_config()
    unlocker = scraper._get_unlocker()
    semaphore = asyncio.Semaphore(min(cfg.per_site_concurrency, len(urls)))

    async def _one(url: str) -> tuple[str, int, str]:
        async with semaphore:
            try:
                status, html = await with_extraction_retry(unlocker.fetch, url)
                return (url, status, html)
            except Exception as e:
                logger.warning("cold start fetch failed for %s: %s", url, e)
                return (url, 0, "")

    results = await asyncio.gather(*[_one(u) for u in urls])
    return list(results)


async def _resolve_html(
    scraper,
    rows: list[ColdStartRow],
    force_fetch: bool,
) -> list[tuple[str, int, str, str]]:
    """Resolve HTML from non-stale goldens first, then fetch cache misses."""
    cached: dict[str, str] = {}
    if not force_fetch:
        cfg = get_config()
        db = ScrapeDB(cfg.db_path)
        db.init_db()
        try:
            gs = GoldenStore(db)
            samples = [
                sample
                for page_type in PAGE_TYPES
                for sample in gs.get_by_site_and_type(
                    scraper.site, page_type, exclude_stale=True
                )
            ]
            for sample in sorted(samples, key=lambda item: item["id"], reverse=True):
                url = sample["expected_output"].get("url")
                html = sample.get("html_snapshot")
                if url and html and url not in cached:
                    cached[url] = html
        finally:
            db.close()

    missing_urls = list(dict.fromkeys(row.url for row in rows if row.url not in cached))
    fetched = await _batch_fetch(scraper, missing_urls) if missing_urls else []
    fetched_by_url = {url: (status, html) for url, status, html in fetched}

    resolved: list[tuple[str, int, str, str]] = []
    for row in rows:
        if row.url in cached:
            item = (row.url, 200, cached[row.url], "goldset")
        else:
            status, html = fetched_by_url.get(row.url, (0, ""))
            item = (row.url, status, html, "brightdata")
        resolved.append(item)
        print(f"  [{item[3]}] {row.url}")
    return resolved


def _pick_representative_pages(fetched, rows) -> list[tuple[str, str, str]]:
    """Largest successful snapshot per declared bucket, retaining the real URL."""
    chosen = {}
    for row, item in zip(rows, fetched):
        url, status, html = item[:3]
        if status == 200 and html:
            previous = chosen.get(row.page_type)
            if previous is None or len(html) > len(previous[2]):
                chosen[row.page_type] = (row.page_type, url, html)
    return list(chosen.values())


def _page_contexts(pages, *, repair=False):
    budget = max(6000, 24000 // len(pages)) // (2 if repair else 1)
    return [(kind, url, build_price_aware_context(html, url, budget=budget))
            for kind, url, html in pages]


def _print_prompt_size(prompt):
    size = sum(len(message["content"]) for message in prompt)
    print(f"  prompt ≈ {size / 1000:.1f}k chars ({size} chars)")


async def _gen_initial_parser(site: str, pages, *, model=None, temperature=None,
                              enable_thinking=None) -> Optional[str]:
    cfg = get_config()
    llm = make_chat_client(
        model=model if model is not None else cfg.cold_start_model_ladder[0],
        temperature=temperature if temperature is not None else cfg.cold_start_temperature_ladder[0],
        enable_thinking=enable_thinking if enable_thinking is not None else len(cfg.cold_start_model_ladder) == 1,
        purpose="cold start")
    if llm is None:
        print("LLM provider key not set — cannot generate parser.")
        return None
    try:
        prompt = initial_parser_gen_prompt(_page_contexts(pages), site)
        _print_prompt_size(prompt)
        resp = await llm.ainvoke(prompt)
    except Exception as exc:
        _log_llm_failure("initial parser gen", exc)
        return None
    return _extract_parser_code(resp.content if hasattr(resp, "content") else str(resp))


async def _gen_repaired_parser(*, site, pages, current_code, ledger, cases, records,
                               merge_record, index, model, temperature,
                               enable_thinking, role) -> Optional[str]:
    llm = make_chat_client(model=model, temperature=temperature,
                           enable_thinking=enable_thinking, purpose="cold start repair")
    if llm is None:
        print("LLM provider key not set — cannot repair parser.")
        return None
    try:
        prompt = coldstart_repair_prompt(
            _page_contexts(pages, repair=True), site, current_code, ledger, cases, records,
            role=role, attempt_index=index, model=model, merge_record=merge_record)
        _print_prompt_size(prompt)
        resp = await llm.ainvoke(prompt)
    except Exception as exc:
        _log_llm_failure("cold start parser repair", exc)
        return None
    return _extract_parser_code(resp.content if hasattr(resp, "content") else str(resp))


def _log_llm_failure(stage: str, exc: Exception) -> None:
    """Log an LLM call failure, calling out output truncation explicitly.

    A truncated reply arrives as ``LengthFinishReasonError``: the provider hit
    the output cap mid-JSON and the OpenAI SDK discards the partial content, so
    there is nothing to salvage — the ladder must move on to the next node.
    """
    if type(exc).__name__ == "LengthFinishReasonError":
        logger.error(
            "%s hit the model output limit — the reply was truncated before the "
            "JSON closed. Raise the provider's max_output_tokens in providers.py "
            "if this repeats.",
            stage,
        )
        print(f"{stage} exceeded the model output limit (truncated reply).")
        return
    logger.exception("%s failed: %s", stage, exc)


def _extract_parser_code(content: str) -> Optional[str]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            parsed = json.loads(content[start:end])
        except Exception:
            return None
    code = parsed.get("parser_code") if isinstance(parsed, dict) else None
    return code if isinstance(code, str) and code.strip() else None


async def _run_review_cases(
    site: str,
    rows: list[ColdStartRow],
    fetched: list[tuple[str, int, str, str]],
    parser_code: str,
) -> list[_ReviewCase]:
    cases: list[_ReviewCase] = []
    for row, (url, status, html, _) in zip(rows, fetched):
        if status != 200 or not html:
            cases.append(
                _ReviewCase(
                    row=row,
                    html=html,
                    product=None,
                    failure_kind="extraction_failed",
                    failure_detail=f"HTTP/status {status}",
                )
            )
            continue

        result = await run_in_sandbox(parser_code, html, url)
        if not isinstance(result, dict):
            cases.append(
                _ReviewCase(
                    row=row,
                    html=html,
                    product=None,
                    failure_kind="sandbox_failed",
                    failure_detail=_describe_sandbox_failure(result),
                )
            )
            continue

        product, errors = validate(_wrap(result, site, url))
        if product is None:
            cases.append(
                _ReviewCase(
                    row=row,
                    html=html,
                    product=None,
                    failure_kind="sandbox_failed",
                    failure_detail="gate: " + "; ".join(errors),
                )
            )
            continue
        cases.append(_ReviewCase(row=row, html=html, product=product))
    return cases


def _describe_sandbox_failure(result: Any) -> str:
    if hasattr(result, "type_name"):
        message = getattr(result, "message", "")
        return f"{result.type_name}: {message}".rstrip(": ")
    if hasattr(result, "reason"):
        return f"{type(result).__name__}: {result.reason}"
    if hasattr(result, "timeout"):
        return f"{type(result).__name__}: {result.timeout}s"
    return type(result).__name__


_TRACING_FIELDS = frozenset(
    {"url", "website", "scraped_at", "source_type", "parser_version", "raw"}
)

_PAGE_TYPE_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "standard": ("price",),
    "membership": ("membership_price",),
    "discounted": ("list_price", "price"),
    "out_of_stock": ("in_stock", "image_urls"),
    "multipack": ("variant",),
}


def _display_fields() -> list[str]:
    """Return reviewable ProductData fields in model declaration order."""
    return [
        name for name in ProductData.model_fields
        if name not in _TRACING_FIELDS
    ]


def _print_product_panel(product: ProductData, page_type: str) -> None:
    print("  Extracted:")
    key_fields = set(_PAGE_TYPE_KEY_FIELDS.get(page_type, ()))
    for field in _display_fields():
        value = getattr(product, field)
        rendered = _format_display_value(value)
        warning = ""
        if field in key_fields and value in (None, "", [], {}):
            warning = "  !! 该桶关键字段为空"
        print(f"    {field:18s} = {rendered}{warning}")


def _format_display_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        if not value:
            return "0 项"
        return f"{len(value)} 项 ({_truncate(str(value[0]), 80)})"
    if isinstance(value, str):
        return _truncate(value, 80)
    return str(value)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _collect_feedback(row: ColdStartRow, input_fn, html: str) -> ReviewFeedback:
    fields = _display_fields()
    print("  Which fields are wrong?")
    print("    " + "  ".join(f"{i}.{name}" for i, name in enumerate(fields, 1)))
    raw = input_fn("  (comma-separated, Enter to skip) > ").strip()
    selected = []
    for token in (part.strip() for part in raw.split(",")):
        if not token:
            continue
        if token.isdigit() and 1 <= int(token) <= len(fields):
            name = fields[int(token) - 1]
        elif token in fields:
            name = token
        else:
            print(f"  note: ignored unknown field selection {token!r}")
            continue
        if name not in selected:
            selected.append(name)
    corrections = []
    unresolved = False
    for name in selected:
        while True:
            try:
                value = parse_correction(name, input_fn(f"  correct {name}? > "))
                break
            except ValueError as exc:
                print(f"  Invalid value: {exc}")
        if name in {"price", "list_price", "membership_price"} and value is not None:
            found = locate_value_snippets(html, row.url, value, keywords=())
            if not found:
                keep = input_fn(f"  !! {value} 不在抓取到的 HTML 中（可能是地区/登录差异），仍然保留？[y/N] ").strip().lower()
                if keep != "y":
                    unresolved = True
                    continue
        corrections.append(FieldCorrection(name, value))
    hint = input_fn("  Any hint? (Enter to skip) > ").strip()
    return ReviewFeedback(row.url, row.page_type, tuple(corrections), hint, unresolved)


def _print_failure_summary(cases, ledger, records) -> None:
    for case in cases:
        if case.failure_kind == "extraction_failed":
            print(f"  [FETCH FAIL] {case.row.url}: {case.failure_detail} (excluded)")
            continue
        exp = ledger.cases.get(case.row.url)
        if case.product is None:
            print(f"  [PARSER CRASH] {case.row.url}: {case.failure_detail}")
        if ledger.matches(case.row.url, case.product):
            continue
        if exp is None or exp.status != "confirmed":
            print(f"  [REJECTED] {case.row.url}: unspecified / unconfirmed")
        else:
            for name, (expected, got) in diff_case(case.product, exp).items():
                print(f"  [REJECTED] {case.row.url} {name}: expected {_format_display_value(expected)}, got {_format_display_value(got)}")
    if records:
        for url, name in sorted(records[-1].broke):
            print(f"  [REGRESSION] {url}: {name}")


def _wrap(raw: dict[str, Any], site: str, url: str) -> dict[str, Any]:
    wrapped = dict(raw)
    wrapped.setdefault("url", url)
    wrapped["website"] = site
    wrapped["source_type"] = "html"
    wrapped["scraped_at"] = datetime.now(timezone.utc)
    wrapped["parser_version"] = "coldstart_v1"
    return wrapped


def _seed(
    site: str,
    parser_code: str,
    accepted: list[tuple[ColdStartRow, str, ProductData]],
) -> tuple[int, int]:
    cfg = get_config()
    db = ScrapeDB(cfg.db_path)
    db.init_db()
    try:
        version = f"cs_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        parser_id = ParserStore(db).create(
            site=site, version=version, code=parser_code, created_by="initial"
        )

        gs = GoldenStore(db)
        seeded = 0
        for row, html, product in accepted:
            gs.seed(
                site,
                row.page_type,
                html,
                product.model_dump(mode="json"),
                created_by="coldstart",
            )
            seeded += 1

        return parser_id, seeded
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def read_coldstart_input(path: Path, site: str = "") -> list[ColdStartRow]:
    """Read and validate a cold-start workbook against site-aware policy.

    The empty-site default preserves the pre-M23 global-policy behavior for
    callers that do not yet have a site identifier.
    """
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ColdStartInputError(
            f"expected an Excel file (.xlsx/.xlsm), got '{path.name}'\n"
            "  cold start input is a sheet with columns: page_type, url"
        )

    try:
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ColdStartInputError(f"could not read Excel file '{path}': {exc}") from exc

    try:
        sheet = workbook.active
        header_values = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), ())
        normalized_headers = [
            str(value).strip().lower() if value is not None else ""
            for value in header_values
        ]
        header_map = {
            header: index
            for index, header in enumerate(normalized_headers)
            if header and header not in normalized_headers[:index]
        }
        missing = [name for name in ("page_type", "url") if name not in header_map]
        if missing:
            found = (
                ", ".join(header for header in normalized_headers if header)
                or "(empty)"
            )
            raise ColdStartInputError(
                f"missing required column(s): {', '.join(missing)}\n"
                f"  found header: {found}\n"
                "  the sheet's first row must contain: page_type, url"
            )

        parsed: list[ColdStartRow] = []
        problems: list[str] = []
        for row_no, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
            page_index = header_map["page_type"]
            url_index = header_map["url"]
            page_value = values[page_index] if page_index < len(values) else None
            url_value = values[url_index] if url_index < len(values) else None
            raw_page_type = "" if page_value is None else str(page_value).strip()
            url = "" if url_value is None else str(url_value).strip()
            if not raw_page_type and not url:
                continue

            page_type = re.sub(r"[\s-]+", "_", raw_page_type.lower())
            row_problems = []
            if page_type not in PAGE_TYPES:
                row_problems.append(f"page_type={raw_page_type!r}")
            elif not page_type_available(site, page_type):
                row_problems.append(
                    f"page_type={page_type!r} is unavailable for site {site!r}"
                )
            if not url:
                row_problems.append("url='' (empty)")
            if row_problems:
                problems.append(f"  row {row_no}: " + ", ".join(row_problems))
                continue
            parsed.append(ColdStartRow(page_type=page_type, url=url, row_no=row_no))

        if problems:
            raise ColdStartInputError(
                f"invalid row value(s) in {path.name}:\n"
                + "\n".join(problems)
                + f"\nLegal page_type values: {', '.join(PAGE_TYPES)}"
            )
    finally:
        workbook.close()

    cfg = get_config()
    counts = Counter(row.page_type for row in parsed)
    mandatory = tuple(
        pt for pt in PAGE_TYPES if site_page_type_mandatory(site, pt)
    )
    missing_types = [pt for pt in mandatory if counts[pt] == 0]
    if missing_types:
        optional = [pt for pt in PAGE_TYPES if not site_page_type_mandatory(site, pt)]
        found = (
            ", ".join(f"{pt} x{counts[pt]}" for pt in PAGE_TYPES if counts[pt])
            or "none"
        )
        raise ColdStartInputError(
            f"{path.name} is missing required page_type(s): {', '.join(missing_types)}\n"
            "  cold start needs >=1 URL for each of: "
            f"{', '.join(mandatory)}\n"
            f"  optional (may be omitted): {', '.join(optional) if optional else 'none'}\n"
            f"  found: {found}\n"
            "  (edit sites.yaml for a declared site, or the global fallback "
            "in config.py)"
        )

    for page_type in PAGE_TYPES:
        maximum = cfg.golden_max_for(page_type)
        if counts[page_type] > maximum:
            print(
                f"note: {counts[page_type]} rows for '{page_type}'; at most {maximum} "
                "goldens are seeded per page_type — extras are spares\n"
                "      (used only if an earlier one fails extraction or is rejected during review)"
            )
    return parsed


def _result_exit_code(result: dict[str, Any]) -> int:
    if result.get("partial"):
        return 2
    if result.get("aborted") or result.get("parser_id") is None:
        return 1
    if result.get("coverage_shortfall"):
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cold start for a new site.")
    parser.add_argument("--site", required=True, help="site identifier (e.g. tesco)")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input",
        dest="input_path",
        type=Path,
        help="Excel workbook with page_type and url columns",
    )
    input_group.add_argument(
        "--urls-file",
        dest="input_path",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--force-fetch",
        action="store_true",
        help="ignore reusable golden HTML snapshots and fetch every URL",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    try:
        validate_model_ladders()
    except UnknownModelError as exc:
        print(f"UnknownModelError: {exc}")
        return 1

    try:
        rows = read_coldstart_input(args.input_path, args.site)
    except ColdStartInputError as exc:
        print(f"ColdStartInputError: {exc}")
        return 1
    if not rows:
        print(f"No data rows in {args.input_path}")
        return 1

    result = asyncio.run(
        run_coldstart(args.site, rows, force_fetch=args.force_fetch)
    )
    return _result_exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
