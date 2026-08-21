"""M24 verification: API price normalization and failed-run observability.

Fully offline. Exercises all terminal raise sites in the API and HTML routes,
the historical-database migration, run dedup semantics, and the Amazon
equal-price regression.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ._harness import FAILED, PASSED, SKIPPED, check, section, skip, run_main






def configure(db_path: Path) -> None:
    from src.scraping.config import ScrapingConfig, set_config

    set_config(ScrapingConfig(db_path=db_path, _env_file=None))


def rows(db_path: Path, table: str = "scrape_runs") -> list[dict]:
    from src.scraping.storage import ScrapeDB

    db = ScrapeDB(db_path)
    db.init_db()
    result = [dict(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
    db.close()
    return result


def verify_normalizer_and_amazon() -> None:
    from src.scraping.scrapers.price_fields import normalize_price_fields
    from src.scraping.scrapers.sites.amazon_uk import AmazonUKScraper
    from src.scraping.validation import validate

    section("M24.1 - API price-contract normalizer")

    normalized, notes = normalize_price_fields(
        {"price": Decimal("19.99"), "list_price": Decimal("19.99")}
    )
    check(
        "equal list_price is dropped",
        normalized["list_price"] is None and "== price" in notes[0],
        str(notes),
    )

    normalized, notes = normalize_price_fields(
        {"price": Decimal("19.99"), "list_price": Decimal("29.99")}
    )
    check(
        "higher list_price is retained",
        normalized["list_price"] == Decimal("29.99") and not notes,
        str((normalized, notes)),
    )

    normalized, notes = normalize_price_fields(
        {
            "price": None,
            "list_price": Decimal("29.99"),
            "membership_price": Decimal("9.99"),
        }
    )
    check(
        "price=None preserves positive qualifier signals",
        normalized["list_price"] == Decimal("29.99")
        and normalized["membership_price"] == Decimal("9.99")
        and not notes,
        str((normalized, notes)),
    )

    normalized, notes = normalize_price_fields(
        {"price": Decimal("10"), "membership_price": Decimal("10")}
    )
    check(
        "membership_price equal to price is dropped",
        normalized["membership_price"] is None and "== price" in notes[0],
        str(notes),
    )

    normalized, notes = normalize_price_fields(
        {"price": None, "membership_price": Decimal("0")}
    )
    check(
        "non-positive membership_price is always dropped",
        normalized["membership_price"] is None and "<= 0" in notes[0],
        str(notes),
    )

    payload = {
        "url": "https://www.amazon.co.uk/dp/B0772VMN9Y",
        "title": "LIVIVO Heated Electric Over Blanket",
        "final_price": "19.99",
        "initial_price": "19.99",
        "currency": "GBP",
        "is_available": True,
        "availability": "In stock",
        "images": ["https://images.example/livivo.jpg"],
    }
    scraper = object.__new__(AmazonUKScraper)
    mapped = scraper._map_fields(payload, payload["url"])
    normalized, notes = normalize_price_fields(mapped)
    product, errors = validate(normalized)
    check(
        "Amazon initial_price == final_price passes both gates",
        product is not None
        and not errors
        and product.list_price is None
        and notes,
        str(errors or notes),
    )


async def verify_api_failures(db_path: Path) -> None:
    from src.scraping.exceptions import BrightDataInfraError, ScrapeFailed
    from src.scraping.scrapers.api_scraper import DirectAPIScraper

    section("M24.2 - API terminal paths write escalated runs")
    configure(db_path)

    class FakeAPI(DirectAPIScraper):
        site = "m24_api"
        mode = "malformed"

        async def _fetch_json(self, url: str) -> dict:
            if self.mode == "infra":
                raise BrightDataInfraError("offline infra fixture")
            if self.mode == "fetch":
                raise RuntimeError("offline fetch fixture")
            return {"fixture": "raw API payload", "price": "bad"}

        def _map_fields(self, json_data: dict, url: str) -> dict:
            return {
                "url": url,
                "website": self.site,
                "source_type": "api",
                "title": "",
                "in_stock": True,
                "price": None,
            }

    with patch(
        "src.scraping.repair.json_healer.heal_json",
        new=AsyncMock(return_value=None),
    ):
        for mode, expected_rule in (
            ("infra", "api_infra"),
            ("fetch", "api_fetch"),
            ("malformed", "gate_validation"),
        ):
            scraper = FakeAPI()
            scraper.mode = mode
            try:
                await scraper.scrape(f"https://api.example/{mode}")
            except ScrapeFailed:
                pass
            else:
                check(f"API {mode} raises ScrapeFailed", False)
                continue

            matching = [
                row
                for row in rows(db_path)
                if row["url"] == f"https://api.example/{mode}"
            ]
            check(
                f"API {mode} writes one diagnosable failure row",
                len(matching) == 1
                and matching[0]["outcome"] == "escalated"
                and matching[0]["path"] == "escalated"
                and expected_rule in (matching[0]["signature"] or "")
                and bool(matching[0]["error"]),
                str(matching),
            )


async def verify_html_failures(db_path: Path) -> None:
    from src.scraping.exceptions import BrightDataInfraError, ScrapeFailed
    from src.scraping.scrapers.html_scraper import HTMLScraper

    section("M24.3 - HTML terminal paths write escalated runs")
    configure(db_path)

    class FakeHTML(HTMLScraper):
        site = "m24_html"

        def _get_unlocker(self):
            class DummyUnlocker:
                async def fetch(self, url: str):
                    return 200, "<html><h1>Product</h1></html>"

            return DummyUnlocker()

        async def _run_parsers(self, html: str, url: str):
            return None

    async def raise_infra(*args, **kwargs):
        raise BrightDataInfraError("offline HTML infra fixture")

    async def raise_fetch(*args, **kwargs):
        raise RuntimeError("offline HTML fetch fixture")

    terminal = ScrapeFailed(
        site="m24_html",
        url="https://html.example/repair",
        scraper_name="FakeHTML",
        failed_stage="parser_broken",
        signature=("m24_html", "repair_budget_exhausted", "v2"),
        errors=["offline repair terminal fixture"],
    )

    async def return_terminal(*args, **kwargs):
        return terminal

    cases = (
        ("infra", raise_infra, "extraction_infra"),
        ("fetch", raise_fetch, "extraction"),
    )
    for name, extraction, expected_rule in cases:
        with patch(
            "src.scraping.scrapers.html_scraper.with_extraction_retry",
            new=extraction,
        ):
            try:
                await FakeHTML().scrape(f"https://html.example/{name}")
            except ScrapeFailed:
                pass
        matching = [
            row
            for row in rows(db_path)
            if row["url"] == f"https://html.example/{name}"
        ]
        check(
            f"HTML {name} writes one diagnosable failure row",
            len(matching) == 1
            and matching[0]["outcome"] == "escalated"
            and expected_rule in (matching[0]["signature"] or "")
            and bool(matching[0]["error"]),
            str(matching),
        )

    with (
        patch(
            "src.scraping.scrapers.html_scraper.with_extraction_retry",
            new=AsyncMock(return_value=(200, "<html><h1>Product</h1></html>")),
        ),
        patch(
            "src.scraping.scrapers.html_scraper.detect_invalid_page",
            return_value=None,
        ),
        patch(
            "src.scraping.repair.agent.run_repair_ladder",
            new=return_terminal,
        ),
    ):
        try:
            await FakeHTML().scrape("https://html.example/repair")
        except ScrapeFailed:
            pass

    matching = [
        row
        for row in rows(db_path)
        if row["url"] == "https://html.example/repair"
    ]
    check(
        "HTML repair terminal writes one diagnosable failure row",
        len(matching) == 1
        and matching[0]["outcome"] == "escalated"
        and "repair_budget_exhausted" in (matching[0]["signature"] or "")
        and "offline repair" in (matching[0]["error"] or ""),
        str(matching),
    )


def verify_execution_log_and_counts(db_path: Path) -> None:
    from src.scraping.scrapers.base import BaseScraper
    from src.scraping.storage import RunStore, ScrapeDB

    section("M24.4 - every execution remains observable")
    configure(db_path)

    class ProbeScraper(BaseScraper):
        site = "m24_dedup"
        source_type = "api"

        async def scrape(self, url: str):
            raise NotImplementedError

    probe = ProbeScraper()
    failure_url = "https://dedup.example/failure"
    success_url = "https://dedup.example/success"
    for _ in range(2):
        probe._record_run(
            failure_url,
            "dedup.example",
            "escalated",
            "escalated",
            signature="m24_dedup|fixture|",
            error="fixture failure",
        )
        probe._record_run(success_url, "dedup.example", "success", "fast")

    recorded = rows(db_path)
    failure_rows = [row for row in recorded if row["url"] == failure_url]
    success_rows = [row for row in recorded if row["url"] == success_url]
    check("two same-window failures produce two rows", len(failure_rows) == 2)
    check("two same-window successes produce two rows", len(success_rows) == 2)

    recovery_url = "https://dedup.example/recovery"
    probe._record_run(
        recovery_url,
        "dedup.example",
        "escalated",
        "escalated",
        signature="m24_dedup|temporary_failure|",
        error="temporary failure",
    )
    probe._record_run(recovery_url, "dedup.example", "success", "fast")
    probe._record_run(recovery_url, "dedup.example", "success", "fast")
    recovery_rows = [row for row in rows(db_path) if row["url"] == recovery_url]
    check(
        "a prior failure does not suppress the recovered success",
        len(recovery_rows) == 3
        and [row["outcome"] for row in recovery_rows]
        == ["escalated", "success", "success"],
        str(recovery_rows),
    )

    db = ScrapeDB(db_path)
    db.init_db()
    total = RunStore(db).count_total_runs("m24_dedup")
    db.close()
    check(
        "count_total_runs includes escalated attempts",
        total == 7,
        f"expected 7 (3 escalated + 4 success), got {total}",
    )


def verify_migration(tmp_dir: Path) -> None:
    from src.scraping.storage import ScrapeDB

    section("M24.5 - historical scrape_runs migration")
    db_path = tmp_dir / "old-schema.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            host TEXT NOT NULL,
            site TEXT NOT NULL,
            scraper TEXT NOT NULL,
            scraped_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            outcome TEXT NOT NULL,
            path TEXT NOT NULL,
            winning_parser_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 1,
            model_used TEXT,
            latency_ms INTEGER,
            cost REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO scrape_runs "
        "(url, host, site, scraper, outcome, path, model_used, cost) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://old.example/item",
            "old.example",
            "old",
            "Old",
            "success",
            "agent_repaired",
            "legacy-repair-model",
            1.25,
        ),
    )
    conn.commit()
    conn.close()

    db = ScrapeDB(db_path)
    db.init_db()
    columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(scrape_runs)")
    }
    surviving = db.conn.execute(
        "SELECT COUNT(*) AS count FROM scrape_runs WHERE url = ?",
        ("https://old.example/item",),
    ).fetchone()["count"]
    migrated_model = db.conn.execute(
        "SELECT repair_model FROM scrape_runs WHERE url = ?",
        ("https://old.example/item",),
    ).fetchone()["repair_model"]
    indexes = {
        row["name"] for row in db.conn.execute("PRAGMA index_list(scrape_runs)")
    }
    db.close()
    check(
        "migration updates run schema without data loss",
        {"signature", "error", "repair_model"} <= columns
        and "model_used" not in columns
        and "cost" not in columns
        and surviving == 1
        and migrated_model == "legacy-repair-model",
        str(columns),
    )
    check(
        "site/outcome query index exists",
        "idx_scrape_runs_site_outcome" in indexes,
        str(indexes),
    )


async def verify_snapshot_and_exception(db_path: Path) -> None:
    from src.scraping import router
    from src.scraping.exceptions import ScrapeFailed
    from src.scraping.scrapers.api_scraper import DirectAPIScraper

    section("M24.6 - API payload escalation preview and traceback detail")
    configure(db_path)

    class MalformedAPI(DirectAPIScraper):
        site = "m24_snapshot"

        async def _fetch_json(self, url: str) -> dict:
            return {"raw_marker": "persist-me", "nested": {"value": 7}}

        def _map_fields(self, json_data: dict, url: str) -> dict:
            return {
                "url": url,
                "website": self.site,
                "scraped_at": datetime.now(timezone.utc),
                "source_type": "api",
                "title": "Malformed fixture",
                "in_stock": True,
                "price": None,
            }

    caught: ScrapeFailed | None = None
    with (
        patch.object(router, "resolve_site", return_value="m24_snapshot"),
        patch.object(router, "get_scrapers", return_value=[MalformedAPI]),
        patch(
            "src.scraping.repair.json_healer.heal_json",
            new=AsyncMock(return_value=None),
        ),
    ):
        try:
            await router.scrape("https://snapshot.example/item")
        except ScrapeFailed as exc:
            caught = exc

    escalation_rows = rows(db_path, "escalations")
    snapshot = json.loads(escalation_rows[-1]["snapshot"])
    check(
        "api_malformed escalation retains raw payload preview",
        snapshot.get("snapshot_preview") is not None
        and "persist-me" in snapshot["snapshot_preview"],
        str(snapshot),
    )
    check(
        "ScrapeFailed text includes first underlying error",
        caught is not None
        and "price must be a positive" in str(caught),
        str(caught),
    )


async def run_all(tmp_dir: Path) -> None:
    verify_normalizer_and_amazon()
    await verify_api_failures(tmp_dir / "api-failures.db")
    await verify_html_failures(tmp_dir / "html-failures.db")
    verify_execution_log_and_counts(tmp_dir / "execution-log.db")
    verify_migration(tmp_dir)
    await verify_snapshot_and_exception(tmp_dir / "snapshot.db")


def main() -> int:
    async def run_in_temp_dir() -> None:
        with tempfile.TemporaryDirectory(prefix="verify_m24_") as tmp:
            await run_all(Path(tmp))

    return run_main(run_in_temp_dir)


if __name__ == "__main__":
    sys.exit(main())
