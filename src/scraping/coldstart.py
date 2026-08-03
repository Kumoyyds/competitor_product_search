"""Cold start workflow (spec §5.9, D22).

New site with empty parsers table:
    1. User provides an Excel sheet declaring page_type + URL
    2. Fetch pages via HTMLScraper._get_unlocker()
    3. LLM generates first-version parser from a representative HTML
    4. Sandbox-execute parser against every URL's HTML
    5. Human confirms each result (y/n/q)
    6. Confirmed → seed parsers (created_by='initial') + golden_samples

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

from .config import get_config
from .exceptions import ColdStartInputError
from .extraction import with_extraction_retry
from .models.enums import PAGE_TYPES
from .models.product_data import ProductData
from .providers import make_chat_client
from .registry import get_scrapers
from .repair.golden import classify_page_type
from .repair.prepass import build_price_aware_context
from .repair.prompts import initial_parser_gen_prompt
from .repair.sandbox import run_in_sandbox
from .storage import GoldenStore, ParserStore, ScrapeDB
from .validation import validate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ColdStartRow:
    page_type: str
    url: str
    row_no: int


async def run_coldstart(
    site: str,
    rows: list[ColdStartRow],
    input_fn=input,
) -> dict[str, Any]:
    """End-to-end cold start for a site.

    Args:
        site: site identifier (e.g. "tesco")
        rows: validated page_type + URL declarations
        input_fn: injectable input function for testing (defaults to builtins.input)

    Returns:
        Cold-start result including coverage_shortfall.
    """
    scraper_cls = _pick_html_scraper(site)
    scraper = scraper_cls()

    print(f"\n== Cold start for site '{site}' ==")
    print(f"Fetching {len(rows)} URLs via {scraper_cls.__name__}...")

    fetched = await _batch_fetch(scraper, [row.url for row in rows])
    print(f"Fetched {len(fetched)} pages ({sum(1 for _, s, _ in fetched if s == 200)} OK).")

    representative_html = _pick_representative_html(fetched)
    if representative_html is None:
        print("No valid HTML fetched — aborting cold start.")
        return _empty_result(aborted=True)

    print("\nGenerating first-version parser via LLM...")
    parser_code = await _gen_initial_parser(site, representative_html)
    if not parser_code:
        print("LLM did not return usable parser code — aborting.")
        return _empty_result(aborted=True)

    print("\nRunning parser against all fetched HTMLs (sandbox)...")
    candidates = []
    for row, (url, status, html) in zip(rows, fetched):
        if status != 200 or not html:
            candidates.append((row, html, None, "extraction_failed"))
            continue
        result = await run_in_sandbox(parser_code, html, url)
        if isinstance(result, dict):
            wrapped = _wrap(result, site, url)
            product, errors = validate(wrapped)
            candidates.append((row, html, product, errors if errors else None))
        else:
            candidates.append((row, html, None, f"sandbox: {type(result).__name__}"))

    print(f"\n{len(candidates)} candidates for review.\n")

    cfg = get_config()
    existing_coverage = _get_existing_coverage(site)
    accepted_by_type: Counter[str] = Counter()
    accepted: list[tuple[ColdStartRow, str, ProductData]] = []
    aborted = False
    for i, (row, html, product, error) in enumerate(candidates, 1):
        current_count = existing_coverage.get(row.page_type, 0) + accepted_by_type[row.page_type]
        if current_count >= cfg.golden_max_for(row.page_type):
            print(
                f"\n[{i}/{len(candidates)}] {row.url}\n"
                f"  bucket '{row.page_type}' already has {current_count} goldens — "
                "skipping (spare)"
            )
            continue

        print(f"\n[{i}/{len(candidates)}] {row.url}")
        print(f"  declared page_type = {row.page_type}")
        if product is None:
            print(f"  ERROR: {error} — marking skipped")
            continue
        classified = classify_page_type(product)
        if classified != row.page_type:
            print(
                f"  !! MISMATCH: extracted fields look like '{classified}', "
                f"not declared '{row.page_type}'"
            )
        print("  Extracted:")
        for field in ("title", "brand", "price", "currency", "list_price", "in_stock", "gtin"):
            val = getattr(product, field, None)
            print(f"    {field:12s} = {val}")

        ans = input_fn("  Accept? [y/N/q] ").strip().lower()
        if ans == "q":
            print("  Aborting review loop.")
            aborted = True
            break
        if ans == "y":
            accepted.append((row, html, product))
            accepted_by_type[row.page_type] += 1
            print("  → accepted")
        else:
            print("  → skipped")

    if not accepted:
        print("\nNo confirmations — nothing seeded.")
        return {
            **_empty_result(aborted=aborted),
            "coverage_shortfall": _coverage_shortfall(existing_coverage),
        }

    parser_id, seeded_count = _seed(site, parser_code, accepted)
    final_coverage = dict(existing_coverage)
    for page_type, count in accepted_by_type.items():
        final_coverage[page_type] = final_coverage.get(page_type, 0) + count
    coverage_shortfall = _coverage_shortfall(final_coverage)
    if coverage_shortfall:
        print("\nWARNING: mandatory golden coverage is still incomplete:")
        for page_type in coverage_shortfall:
            print(
                f"  - {page_type}: needs >= {cfg.golden_min_for(page_type)}, "
                f"has {final_coverage.get(page_type, 0)}"
            )
    print(f"\n== Cold start complete ==")
    print(f"  parser_id     = {parser_id}")
    print(f"  goldens seeded = {seeded_count}")
    return {
        "parser_id": parser_id,
        "seeded_goldens": seeded_count,
        "accepted": len(accepted),
        "aborted": aborted,
        "coverage_shortfall": coverage_shortfall,
    }


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
        "coverage_shortfall": [],
    }


def _get_existing_coverage(site: str) -> dict[str, int]:
    cfg = get_config()
    db = ScrapeDB(cfg.db_path)
    db.init_db()
    try:
        return GoldenStore(db).get_page_type_coverage(site)
    finally:
        db.close()


def _coverage_shortfall(coverage: dict[str, int]) -> list[str]:
    cfg = get_config()
    return [
        page_type
        for page_type in cfg.mandatory_page_types()
        if coverage.get(page_type, 0) < cfg.golden_min_for(page_type)
    ]


async def _batch_fetch(scraper, urls: list[str]) -> list[tuple[str, int, str]]:
    """Fetch all URLs; returns list of (url, status_code, html)."""
    unlocker = scraper._get_unlocker()

    async def _one(url: str) -> tuple[str, int, str]:
        try:
            status, html = await with_extraction_retry(unlocker.fetch, url)
            return (url, status, html)
        except Exception as e:
            logger.warning("cold start fetch failed for %s: %s", url, e)
            return (url, 0, "")

    results = await asyncio.gather(*[_one(u) for u in urls])
    return list(results)


def _pick_representative_html(
    fetched: list[tuple[str, int, str]]
) -> Optional[str]:
    """Pick the largest 200 HTML as representative for LLM parser generation."""
    ok = [(url, html) for url, status, html in fetched if status == 200 and html]
    if not ok:
        return None
    return max(ok, key=lambda t: len(t[1]))[1]


async def _gen_initial_parser(site: str, html: str) -> Optional[str]:
    cfg = get_config()
    llm = make_chat_client(
        model=cfg.repair_model_ladder[0],
        temperature=0.1,
        purpose="cold start",
    )
    if llm is None:
        print("LLM provider key not set — cannot generate parser.")
        return None
    try:
        price_ctx = build_price_aware_context(html, f"https://dummy/{site}")
        resp = await llm.ainvoke(initial_parser_gen_prompt(price_ctx, site))
    except Exception as e:
        logger.exception("initial parser gen failed: %s", e)
        return None

    content = resp.content if hasattr(resp, "content") else str(resp)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            parsed = json.loads(content[start:end])
        except Exception:
            return None
    return parsed.get("parser_code")


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

def read_coldstart_input(path: Path) -> list[ColdStartRow]:
    """Read and validate a cold-start Excel workbook."""
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
    missing_types = [pt for pt in cfg.mandatory_page_types() if counts[pt] == 0]
    if missing_types:
        optional = [pt for pt in PAGE_TYPES if not cfg.is_mandatory_page_type(pt)]
        found = (
            ", ".join(f"{pt} x{counts[pt]}" for pt in PAGE_TYPES if counts[pt])
            or "none"
        )
        raise ColdStartInputError(
            f"{path.name} is missing required page_type(s): {', '.join(missing_types)}\n"
            "  cold start needs >=1 URL for each of: "
            f"{', '.join(cfg.mandatory_page_types())}\n"
            f"  optional (may be omitted): {', '.join(optional) if optional else 'none'}\n"
            f"  found: {found}\n"
            "  (edit cold_start_page_require_mandatory in config.py to change what is required)"
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
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    try:
        rows = read_coldstart_input(args.input_path)
    except ColdStartInputError as exc:
        print(f"ColdStartInputError: {exc}")
        return 1
    if not rows:
        print(f"No data rows in {args.input_path}")
        return 1

    result = asyncio.run(run_coldstart(args.site, rows))
    return _result_exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
