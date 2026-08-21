from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from src.scraping.config import ScrapingConfig, get_config, set_config
from src.scraping.exceptions import ScrapeFailed
from src.scraping.models.product_data import ProductData
from src.scraping.repair.agent import CandidateSucceeded
from src.scraping.scrapers.api_scraper import DirectAPIScraper
from src.scraping.scrapers.base import BaseScraper
from src.scraping.scrapers.html_scraper import HTMLScraper
from src.scraping.storage import ParserStore, RunStore, ScrapeDB
from tests._support.db import temp_scrape_db


def _configure(db_path: Path) -> None:
    set_config(ScrapingConfig(db_path=db_path, _env_file=None))


@pytest.fixture(autouse=True)
def _restore_config() -> Iterator[None]:
    previous = get_config()
    try:
        yield
    finally:
        set_config(previous)


def _product(url: str, site: str, source_type: str = "html") -> ProductData:
    return ProductData(
        url=url,
        website=site,
        scraped_at=datetime.now(timezone.utc),
        source_type=source_type,
        parser_version="agent_attempt_0" if source_type == "html" else None,
        title="Fixture product",
        price=Decimal("12.34"),
        currency="GBP",
        in_stock=True,
        image_urls=["https://images.example/product.jpg"],
    )


def _rows(db: ScrapeDB, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    return [dict(row) for row in db.conn.execute(sql, params)]


class _HTMLProbe(HTMLScraper):
    site = "correlation_html"

    def _get_unlocker(self):
        class _Unlocker:
            async def fetch(self, url: str) -> tuple[int, str]:
                return 200, "<html><body><h1>Fixture product</h1></body></html>"

        return _Unlocker()

    def _on_success(self, html: str, product: ProductData) -> None:
        return None


class _APIProbe(DirectAPIScraper):
    site = "correlation_api"

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        return {"url": url, "title": "Fixture product"}

    def _map_fields(
        self,
        json_data: dict[str, Any],
        url: str,
    ) -> dict[str, Any]:
        return _product(url, self.site, "api").model_dump()


def test_html_fast_path_links_result_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    with temp_scrape_db() as (path, db):
        _configure(path)
        parser_id = ParserStore(db).create(
            _HTMLProbe.site,
            "v1",
            "def parse(html, url): return {}",
        )
        product = _product("https://fixture.test/html-fast", _HTMLProbe.site)

        async def _run_parsers(html: str, url: str):
            return product.model_dump(), parser_id, "v1", []

        monkeypatch.setattr(_HTMLProbe, "_run_parsers", staticmethod(_run_parsers))
        monkeypatch.setattr(
            "src.scraping.scrapers.html_scraper.detect_invalid_page",
            lambda *args, **kwargs: None,
        )

        result = asyncio.run(_HTMLProbe().scrape(product.url))
        joined = _rows(
            db,
            "SELECT r.run_id, s.id, s.winning_parser_id "
            "FROM results r JOIN scrape_runs s ON s.id = r.run_id",
        )

        assert result.url == product.url
        assert len(joined) == 1
        assert joined[0]["run_id"] == joined[0]["id"]
        assert joined[0]["winning_parser_id"] == parser_id


def test_html_repair_links_result_and_winning_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with temp_scrape_db() as (path, db):
        _configure(path)
        parser_id = ParserStore(db).create(
            _HTMLProbe.site,
            "v2",
            "def parse(html, url): return {}",
            created_by="agent",
        )
        product = _product("https://fixture.test/html-repair", _HTMLProbe.site)

        async def _no_parsers(html: str, url: str):
            return None

        monkeypatch.setattr(_HTMLProbe, "_run_parsers", staticmethod(_no_parsers))
        monkeypatch.setattr(
            "src.scraping.scrapers.html_scraper.detect_invalid_page",
            lambda *args, **kwargs: None,
        )

        async def _repaired(**kwargs):
            return CandidateSucceeded(
                product,
                parser_id,
                "def parse(html, url): return {}",
                "fixture-repair-model",
            )

        monkeypatch.setattr(
            "src.scraping.repair.agent.run_repair_ladder",
            _repaired,
        )

        result = asyncio.run(_HTMLProbe().scrape(product.url))
        joined = _rows(
            db,
            "SELECT r.run_id, s.id, s.path, s.winning_parser_id, s.repair_model "
            "FROM results r JOIN scrape_runs s ON s.id = r.run_id",
        )

        assert result.url == product.url
        assert len(joined) == 1
        assert joined[0]["run_id"] == joined[0]["id"]
        assert joined[0]["path"] == "agent_repaired"
        assert joined[0]["winning_parser_id"] == parser_id
        assert joined[0]["repair_model"] == "fixture-repair-model"


def test_repair_ladder_preserves_candidate_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.scraping.repair import agent

    with temp_scrape_db() as (path, _):
        _configure(path)
        product = _product(
            "https://fixture.test/repair-contract",
            _HTMLProbe.site,
        )
        candidate = CandidateSucceeded(
            product=product,
            parser_id=42,
            parser_source="def parse(html, url): return {}",
            repair_model="fixture-repair-model",
        )

        async def _candidate(*args, **kwargs):
            return candidate

        monkeypatch.setattr(agent, "_try_repair", _candidate)
        outcome = asyncio.run(
            agent.run_repair_ladder(
                scraper=_HTMLProbe(),
                url=product.url,
                html="<html><h1>Fixture product</h1></html>",
                initial_errors=["offline fixture"],
            )
        )

        assert outcome is candidate
        assert outcome.parser_id == 42
        assert outcome.repair_model == "fixture-repair-model"


def test_api_success_links_result_to_run() -> None:
    with temp_scrape_db() as (path, db):
        _configure(path)
        url = "https://fixture.test/api"

        result = asyncio.run(_APIProbe().scrape(url))
        joined = _rows(
            db,
            "SELECT r.run_id, s.id, s.scraper, s.outcome, s.path, "
            "s.winning_parser_id, s.repair_model "
            "FROM results r JOIN scrape_runs s ON s.id = r.run_id",
        )

        assert result.url == url
        assert len(joined) == 1
        assert joined[0]["run_id"] == joined[0]["id"]
        assert joined[0]["scraper"] == "_APIProbe"
        assert joined[0]["outcome"] == "success"
        assert joined[0]["path"] == "fast"
        assert joined[0]["winning_parser_id"] is None
        assert joined[0]["repair_model"] is None


def test_repeated_successes_each_write_a_run_and_result() -> None:
    with temp_scrape_db() as (path, db):
        _configure(path)
        url = "https://fixture.test/repeated"

        asyncio.run(_APIProbe().scrape(url))
        asyncio.run(_APIProbe().scrape(url))
        joined = _rows(
            db,
            "SELECT r.id AS result_id, r.run_id, s.id AS run_id_joined "
            "FROM results r JOIN scrape_runs s ON s.id = r.run_id "
            "WHERE r.url = ? ORDER BY r.id",
            (url,),
        )

        assert len(joined) == 2
        assert len({row["run_id"] for row in joined}) == 2
        assert all(row["run_id"] == row["run_id_joined"] for row in joined)


def test_router_exhaustion_attaches_ticket_to_every_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.scraping import router

    with temp_scrape_db() as (path, db):
        _configure(path)
        site = "correlation_router"
        url = "https://correlation-router.test/product"

        class _Failing(BaseScraper):
            site = ""
            source_type = "api"

            async def scrape(self, target: str):
                failure = ScrapeFailed(
                    site=self.site,
                    url=target,
                    scraper_name=self.__class__.__name__,
                    failed_stage="api_malformed",
                    signature=(self.site, "gate_validation", ""),
                    errors=["offline fixture"],
                )
                self._record_failure(target, "correlation-router.test", failure)
                raise failure

        class _FirstFailure(_Failing):
            site = "correlation_router"

        class _SecondFailure(_Failing):
            site = "correlation_router"

        monkeypatch.setitem(router.HOST_TO_SITE, "correlation-router.test", site)
        monkeypatch.setattr(
            router,
            "get_scrapers",
            lambda requested_site: [_FirstFailure, _SecondFailure],
        )

        with pytest.raises(ScrapeFailed) as raised:
            asyncio.run(router.scrape(url))

        runs = _rows(
            db,
            "SELECT id, escalation_id FROM scrape_runs WHERE url = ? ORDER BY id",
            (url,),
        )
        escalations = _rows(db, "SELECT id FROM escalations")

        assert len(runs) == 2
        assert len(escalations) == 1
        assert {row["escalation_id"] for row in runs} == {escalations[0]["id"]}
        assert raised.value.run_id == runs[-1]["id"]
        assert len(RunStore(db).get_by_escalation(escalations[0]["id"])) == 2


def test_mass_invalid_target_attaches_ticket_to_tripping_run() -> None:
    with temp_scrape_db() as (path, db):
        _configure(path)
        scraper = _HTMLProbe()
        run_ids = [
            scraper._record_run(
                f"https://fixture.test/invalid/{index}",
                "fixture.test",
                "invalid_target",
                "invalid_target",
            )
            for index in range(5)
        ]
        tripping_run_id = run_ids[-1]
        assert tripping_run_id is not None

        scraper._check_mass_invalid_target(
            "fixture.test",
            "https://fixture.test/invalid/4",
            tripping_run_id,
        )

        runs = _rows(
            db,
            "SELECT id, escalation_id FROM scrape_runs ORDER BY id",
        )
        escalations = _rows(
            db,
            "SELECT id, reason FROM escalations WHERE reason = 'mass_invalid_target'",
        )

        assert len(escalations) == 1
        assert runs[-1]["escalation_id"] == escalations[0]["id"]
        assert all(row["escalation_id"] is None for row in runs[:-1])


def test_legacy_database_migrates_columns_and_indexes_idempotently(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            host TEXT NOT NULL,
            site TEXT NOT NULL,
            scraper TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            path TEXT NOT NULL,
            winning_parser_id INTEGER,
            attempts INTEGER NOT NULL DEFAULT 1,
            model_used TEXT,
            latency_ms INTEGER,
            cost REAL,
            signature TEXT,
            error TEXT
        );
        CREATE TABLE results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            site TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            product_data TEXT NOT NULL
        );
        INSERT INTO scrape_runs
            (url, host, site, scraper, scraped_at, outcome, path, model_used, cost)
        VALUES
            ('https://legacy.test/product', 'legacy.test', 'legacy',
             'LegacyScraper', '2026-08-21T00:00:00Z', 'success', 'fast',
             'legacy-repair-model', 1.25);
        INSERT INTO results (url, site, scraped_at, product_data)
        VALUES
            ('https://legacy.test/product', 'legacy',
             '2026-08-21T00:00:00Z', '{}');
        """
    )
    conn.close()

    db = ScrapeDB(db_path)
    db.init_db()
    db.init_db()

    result_columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(results)")
    }
    run_columns = {
        row["name"] for row in db.conn.execute("PRAGMA table_info(scrape_runs)")
    }
    result_indexes = {
        row["name"] for row in db.conn.execute("PRAGMA index_list(results)")
    }
    run_indexes = {
        row["name"] for row in db.conn.execute("PRAGMA index_list(scrape_runs)")
    }
    result_foreign_keys = [
        dict(row) for row in db.conn.execute("PRAGMA foreign_key_list(results)")
    ]
    run_foreign_keys = [
        dict(row) for row in db.conn.execute("PRAGMA foreign_key_list(scrape_runs)")
    ]

    assert "run_id" in result_columns
    assert "escalation_id" in run_columns
    assert "repair_model" in run_columns
    assert "model_used" not in run_columns
    assert "cost" not in run_columns
    assert "idx_results_run" in result_indexes
    assert "idx_scrape_runs_escalation" in run_indexes
    assert any(
        key["from"] == "run_id"
        and key["table"] == "scrape_runs"
        and key["on_delete"] == "SET NULL"
        for key in result_foreign_keys
    )
    assert any(
        key["from"] == "escalation_id"
        and key["table"] == "escalations"
        and key["on_delete"] == "SET NULL"
        for key in run_foreign_keys
    )
    migrated_run = db.conn.execute(
        "SELECT repair_model FROM scrape_runs WHERE id = 1"
    ).fetchone()
    assert migrated_run["repair_model"] == "legacy-repair-model"
    assert db.conn.execute("SELECT run_id FROM results").fetchone()[0] is None
    assert db.conn.execute(
        "SELECT escalation_id FROM scrape_runs"
    ).fetchone()[0] is None
    db.close()
