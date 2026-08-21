from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.scraping.storage.database import (
    CLEARABLE_TABLES,
    DEFAULT_CLEAR_TABLES,
    ScrapeDB,
)
from tests._support.db import fetchall, temp_scrape_db


def _count(db: ScrapeDB, table: str, site: str) -> int:
    return db.conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE site = ?", (site,)
    ).fetchone()[0]


def _seed(db: ScrapeDB) -> tuple[int, int]:
    parser_ids: list[int] = []
    for site in ("site_a", "site_b"):
        cursor = db.conn.execute(
            "INSERT INTO parsers (site, version, code, created_by) "
            "VALUES (?, 'v1', ?, 'initial')",
            (site, f"def parse_{site}(html, url): pass"),
        )
        parser_ids.append(cursor.lastrowid)

        for page_type in ("standard", "discounted"):
            db.conn.execute(
                "INSERT INTO golden_samples "
                "(site, page_type, html_snapshot, expected_output, created_by) "
                "VALUES (?, ?, ?, ?, 'coldstart')",
                (
                    site,
                    page_type,
                    f"<html>{site}-{page_type}</html>",
                    json.dumps({"title": f"{site}-{page_type}"}),
                ),
            )

        db.conn.execute(
            "INSERT INTO invalid_target_phrases (site, phrase) VALUES (?, ?)",
            (site, f"not found on {site}"),
        )

    db.conn.executemany(
        "INSERT INTO scrape_runs "
        "(url, host, site, scraper, outcome, path, winning_parser_id) "
        "VALUES (?, ?, ?, 'html_scraper', 'success', 'fast', ?)",
        (
            ("https://site_a.test/1", "site_a.test", "site_a", parser_ids[0]),
            ("https://site_a.test/2", "site_a.test", "site_a", None),
            ("https://site_b.test/1", "site_b.test", "site_b", parser_ids[1]),
        ),
    )
    run_a = db.conn.execute(
        "SELECT id FROM scrape_runs WHERE url = 'https://site_a.test/1'"
    ).fetchone()[0]
    run_b = db.conn.execute(
        "SELECT id FROM scrape_runs WHERE url = 'https://site_b.test/1'"
    ).fetchone()[0]
    escalation_ids: list[int] = []
    for signature in ("site_a|price|v1", "site_b|price|v1"):
        cursor = db.conn.execute(
            "INSERT INTO escalations (signature, reason) "
            "VALUES (?, 'parser_broken')",
            (signature,),
        )
        escalation_ids.append(cursor.lastrowid)
    db.conn.executemany(
        "UPDATE scrape_runs SET escalation_id = ? WHERE id = ?",
        ((escalation_ids[0], run_a), (escalation_ids[1], run_b)),
    )
    db.conn.executemany(
        "INSERT INTO results (url, site, scraped_at, product_data, run_id) "
        "VALUES (?, ?, '2026-08-21T00:00:00Z', '{}', ?)",
        (
            ("https://site_a.test/product", "site_a", run_a),
            ("https://site_b.test/product", "site_b", run_b),
        ),
    )
    db.conn.commit()
    return parser_ids[0], parser_ids[1]


@pytest.fixture
def seeded_db() -> Iterator[tuple[Path, ScrapeDB, int, int]]:
    with temp_scrape_db() as (path, db):
        parser_a, parser_b = _seed(db)
        yield path, db, parser_a, parser_b


def _schema_snapshot(db: ScrapeDB) -> dict[str, dict[str, list[tuple]]]:
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {
        row["name"]: {
            "columns": [tuple(item) for item in db.conn.execute(
                f"PRAGMA table_info({row['name']})"
            )],
            "indexes": [tuple(item) for item in db.conn.execute(
                f"PRAGMA index_list({row['name']})"
            )],
        }
        for row in tables
    }


def test_clearable_table_contract() -> None:
    assert CLEARABLE_TABLES == (
        "parsers",
        "golden_samples",
        "results",
        "escalations",
        "invalid_target_phrases",
        "scrape_runs",
    )
    assert DEFAULT_CLEAR_TABLES == ("parsers", "golden_samples")


def test_default_clear_preserves_runs_and_other_site(seeded_db) -> None:
    path, db, parser_a, parser_b = seeded_db

    assert _count(db, "parsers", "site_a") == 1
    assert _count(db, "golden_samples", "site_a") == 2
    assert _count(db, "scrape_runs", "site_a") == 2
    assert fetchall(
        path,
        "SELECT winning_parser_id FROM scrape_runs "
        "WHERE site = 'site_a' AND winning_parser_id IS NOT NULL",
    ) == [(parser_a,)]

    assert db.clear_site("site_a") == {
        "scrape_runs_detached": 1,
        "parsers": 1,
        "golden_samples": 2,
    }

    assert _count(db, "parsers", "site_a") == 0
    assert _count(db, "golden_samples", "site_a") == 0
    assert _count(db, "scrape_runs", "site_a") == 2
    assert fetchall(
        path,
        "SELECT winning_parser_id FROM scrape_runs WHERE site = 'site_a'",
    ) == [(None,), (None,)]
    assert _count(db, "results", "site_a") == 1
    assert _count(db, "invalid_target_phrases", "site_a") == 1

    assert _count(db, "parsers", "site_b") == 1
    assert _count(db, "golden_samples", "site_b") == 2
    assert _count(db, "scrape_runs", "site_b") == 1
    assert fetchall(
        path,
        "SELECT winning_parser_id FROM scrape_runs WHERE site = 'site_b'",
    ) == [(parser_b,)]


def test_default_clear_is_idempotent_and_unknown_site_is_noop(seeded_db) -> None:
    _, db, _, _ = seeded_db
    assert any(db.clear_site("site_a").values())
    assert db.clear_site("site_a") == {
        "scrape_runs_detached": 0,
        "parsers": 0,
        "golden_samples": 0,
    }
    assert db.clear_site("missing") == {
        "scrape_runs_detached": 0,
        "parsers": 0,
        "golden_samples": 0,
    }


def test_default_clear_does_not_change_schema(seeded_db) -> None:
    _, db, _, _ = seeded_db
    before = _schema_snapshot(db)
    db.clear_site("site_a")
    assert _schema_snapshot(db) == before


def test_default_clear_works_with_foreign_keys_disabled(seeded_db) -> None:
    _, db, _, _ = seeded_db
    db.conn.execute("PRAGMA foreign_keys=OFF")

    counts = db.clear_site("site_a")

    assert counts["scrape_runs_detached"] == 1
    assert _count(db, "parsers", "site_a") == 0
    assert _count(db, "golden_samples", "site_a") == 0
    assert _count(db, "scrape_runs", "site_a") == 2


@pytest.mark.parametrize("table", ("results", "invalid_target_phrases"))
def test_new_site_column_tables_can_be_cleared_individually(
    seeded_db,
    table: str,
) -> None:
    _, db, _, _ = seeded_db

    assert db.clear_site("site_a", tables=[table]) == {table: 1}

    assert _count(db, table, "site_a") == 0
    assert _count(db, table, "site_b") == 1
    assert _count(db, "parsers", "site_a") == 1
    assert "scrape_runs_detached" not in db.clear_site("missing", tables=[table])


@pytest.mark.parametrize("foreign_keys", (True, False))
def test_clearing_runs_detaches_retained_results(
    seeded_db,
    foreign_keys: bool,
) -> None:
    _, db, _, _ = seeded_db
    db.conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")

    counts = db.clear_site("site_a", tables=["scrape_runs"])

    assert counts == {"results_detached": 1, "scrape_runs": 2}
    assert _count(db, "scrape_runs", "site_a") == 0
    assert _count(db, "results", "site_a") == 1
    assert db.conn.execute(
        "SELECT run_id FROM results WHERE site = 'site_a'"
    ).fetchone()[0] is None


def test_escalations_use_exact_signature_site_prefix(seeded_db) -> None:
    _, db, _, _ = seeded_db
    db.conn.executemany(
        "INSERT INTO escalations (signature, reason) VALUES (?, 'infra_failure')",
        (("site_a",), ("site_ab|infra_failure|",)),
    )
    db.conn.commit()

    assert db.clear_site("site_a", tables=["escalations"]) == {
        "scrape_runs_escalation_detached": 1,
        "escalations": 2,
    }

    signatures = [
        row[0]
        for row in db.conn.execute(
            "SELECT signature FROM escalations ORDER BY signature"
        )
    ]
    assert signatures == ["site_ab|infra_failure|", "site_b|price|v1"]


@pytest.mark.parametrize("foreign_keys", (True, False))
def test_clearing_escalations_detaches_retained_runs(
    seeded_db,
    foreign_keys: bool,
) -> None:
    _, db, _, _ = seeded_db
    db.conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")

    counts = db.clear_site("site_a", tables=["escalations"])

    assert counts == {
        "scrape_runs_escalation_detached": 1,
        "escalations": 1,
    }
    assert _count(db, "scrape_runs", "site_a") == 2
    assert db.conn.execute(
        "SELECT COUNT(*) FROM scrape_runs "
        "WHERE site = 'site_a' AND escalation_id IS NOT NULL"
    ).fetchone()[0] == 0


def test_clearing_runs_before_parsers_needs_no_detach(seeded_db) -> None:
    _, db, _, _ = seeded_db

    counts = db.clear_site("site_a", tables=["parsers", "scrape_runs"])

    assert counts == {
        "results_detached": 1,
        "scrape_runs": 2,
        "scrape_runs_detached": 0,
        "parsers": 1,
    }
    assert _count(db, "scrape_runs", "site_a") == 0
    assert _count(db, "parsers", "site_a") == 0
    assert _count(db, "scrape_runs", "site_b") == 1
    assert _count(db, "parsers", "site_b") == 1


def test_counts_omit_detach_when_parsers_are_not_selected(seeded_db) -> None:
    _, db, _, _ = seeded_db
    counts = db.clear_site(
        "site_a",
        tables=["golden_samples", "results", "invalid_target_phrases"],
    )
    assert counts == {
        "golden_samples": 2,
        "results": 1,
        "invalid_target_phrases": 1,
    }


def test_unknown_table_is_rejected_before_any_delete(seeded_db) -> None:
    _, db, _, _ = seeded_db

    with pytest.raises(ValueError, match="Valid tables"):
        db.clear_site("site_a", tables=["results", "results; DROP TABLE parsers"])
    with pytest.raises(ValueError, match="Unknown clearable table"):
        db.clear_site("site_a", tables="")

    assert _count(db, "results", "site_a") == 1
    assert _count(db, "parsers", "site_a") == 1


def test_empty_table_list_is_a_noop(seeded_db) -> None:
    _, db, _, _ = seeded_db
    before = {
        table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in CLEARABLE_TABLES
    }
    assert db.clear_site("site_a", tables=[]) == {}
    after = {
        table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in CLEARABLE_TABLES
    }
    assert after == before


def test_none_matches_explicit_default_tables() -> None:
    with temp_scrape_db() as (_, implicit_db):
        _seed(implicit_db)
        implicit_counts = implicit_db.clear_site("site_a", tables=None)
        implicit_remaining = {
            table: implicit_db.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in CLEARABLE_TABLES
        }

    with temp_scrape_db() as (_, explicit_db):
        _seed(explicit_db)
        explicit_counts = explicit_db.clear_site(
            "site_a", tables=DEFAULT_CLEAR_TABLES
        )
        explicit_remaining = {
            table: explicit_db.conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in CLEARABLE_TABLES
        }

    assert explicit_counts == implicit_counts
    assert explicit_remaining == implicit_remaining


def test_selected_deletes_and_detach_roll_back_together(seeded_db) -> None:
    _, db, parser_a, _ = seeded_db
    db.conn.execute(
        "CREATE TEMP TRIGGER abort_golden_delete "
        "BEFORE DELETE ON golden_samples "
        "BEGIN SELECT RAISE(ABORT, 'test rollback'); END"
    )

    with pytest.raises(Exception, match="test rollback"):
        db.clear_site("site_a", tables=["parsers", "golden_samples"])

    assert _count(db, "parsers", "site_a") == 1
    assert _count(db, "golden_samples", "site_a") == 2
    assert db.conn.execute(
        "SELECT winning_parser_id FROM scrape_runs "
        "WHERE site = 'site_a' AND winning_parser_id IS NOT NULL"
    ).fetchone()[0] == parser_a
