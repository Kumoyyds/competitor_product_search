"""Cold start workflow (spec §5.9, D22).

New site with empty parsers table:
    1. User provides URL batch
    2. Fetch pages via HTMLScraper._get_unlocker()
    3. LLM generates first-version parser from a representative HTML
    4. Sandbox-execute parser against every URL's HTML
    5. Human confirms each result (y/n/q)
    6. Confirmed → seed parsers (created_by='initial') + golden_samples

Run:
    python -m src.scraping.coldstart --site tesco --urls-file urls.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import get_config
from .extraction import with_extraction_retry
from .models.product_data import ProductData
from .registry import get_scrapers
from .repair.prepass import build_price_aware_context
from .repair.prompts import initial_parser_gen_prompt
from .repair.sandbox import run_in_sandbox
from .storage import GoldenStore, ParserStore, ScrapeDB
from .validation import validate

logger = logging.getLogger(__name__)


async def run_coldstart(
    site: str,
    urls: list[str],
    input_fn=input,
) -> dict[str, Any]:
    """End-to-end cold start for a site.

    Args:
        site: site identifier (e.g. "tesco")
        urls: list of product URLs
        input_fn: injectable input function for testing (defaults to builtins.input)

    Returns:
        {"parser_id": int | None, "seeded_goldens": int, "accepted": int, "aborted": bool}
    """
    scraper_cls = _pick_html_scraper(site)
    scraper = scraper_cls()

    print(f"\n== Cold start for site '{site}' ==")
    print(f"Fetching {len(urls)} URLs via {scraper_cls.__name__}...")

    fetched = await _batch_fetch(scraper, urls)
    print(f"Fetched {len(fetched)} pages ({sum(1 for _, s, _ in fetched if s == 200)} OK).")

    representative_html = _pick_representative_html(fetched)
    if representative_html is None:
        print("No valid HTML fetched — aborting cold start.")
        return {"parser_id": None, "seeded_goldens": 0, "accepted": 0, "aborted": True}

    print("\nGenerating first-version parser via LLM...")
    parser_code = await _gen_initial_parser(site, representative_html)
    if not parser_code:
        print("LLM did not return usable parser code — aborting.")
        return {"parser_id": None, "seeded_goldens": 0, "accepted": 0, "aborted": True}

    print("\nRunning parser against all fetched HTMLs (sandbox)...")
    candidates = []
    for url, status, html in fetched:
        if status != 200 or not html:
            candidates.append((url, html, None, "extraction_failed"))
            continue
        result = await run_in_sandbox(parser_code, html, url)
        if isinstance(result, dict):
            wrapped = _wrap(result, site, url)
            product, errors = validate(wrapped)
            candidates.append((url, html, product, errors if errors else None))
        else:
            candidates.append((url, html, None, f"sandbox: {type(result).__name__}"))

    print(f"\n{len(candidates)} candidates for review.\n")

    accepted: list[tuple[str, str, ProductData]] = []
    for i, (url, html, product, error) in enumerate(candidates, 1):
        print(f"\n[{i}/{len(candidates)}] {url}")
        if product is None:
            print(f"  ERROR: {error} — marking skipped")
            continue
        print("  Extracted:")
        for field in ("title", "brand", "price", "currency", "list_price", "in_stock", "gtin"):
            val = getattr(product, field, None)
            print(f"    {field:12s} = {val}")

        ans = input_fn("  Accept? [y/N/q] ").strip().lower()
        if ans == "q":
            print("  Aborting review loop.")
            break
        if ans == "y":
            accepted.append((url, html, product))
            print("  → accepted")
        else:
            print("  → skipped")

    if not accepted:
        print("\nNo confirmations — nothing seeded.")
        return {"parser_id": None, "seeded_goldens": 0, "accepted": 0, "aborted": False}

    parser_id, seeded_count = _seed(site, parser_code, accepted)
    print(f"\n== Cold start complete ==")
    print(f"  parser_id     = {parser_id}")
    print(f"  goldens seeded = {seeded_count}")
    return {
        "parser_id": parser_id,
        "seeded_goldens": seeded_count,
        "accepted": len(accepted),
        "aborted": False,
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
    from langchain_openai import ChatOpenAI

    cfg = get_config()
    if not cfg.qwen_key:
        print("QWEN_KEY not set — cannot generate parser.")
        return None

    llm = ChatOpenAI(
        api_key=cfg.qwen_key,
        base_url=cfg.qwen_base_url,
        model="qwen-3.7-plus",
        temperature=0.1,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
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
    site: str, parser_code: str, accepted: list[tuple[str, str, ProductData]]
) -> tuple[int, int]:
    from .repair.golden import classify_page_type

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
        for url, html, product in accepted:
            page_type = classify_page_type(product)
            gs.seed(site, page_type, html, product.model_dump(mode="json"))
            seeded += 1

        return parser_id, seeded
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _read_urls(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Cold start for a new site.")
    parser.add_argument("--site", required=True, help="site identifier (e.g. tesco)")
    parser.add_argument("--urls-file", type=Path, required=True, help="text file with one URL per line")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    urls = _read_urls(args.urls_file)
    if not urls:
        print(f"No URLs in {args.urls_file}")
        return 1

    result = asyncio.run(run_coldstart(args.site, urls))
    return 0 if result.get("parser_id") is not None else 1


if __name__ == "__main__":
    sys.exit(main())
