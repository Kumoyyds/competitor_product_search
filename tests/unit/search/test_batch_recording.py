import asyncio
import json
import sqlite3

import pytest

from src.search.batch import SearchRequest, _parse_args, match_product_batch, match_products
from src.search.db import SCHEMA_VERSION, SearchDB
from src.search.models import FinalVerdict
from src.search.pipeline import match_product
from tests._support.db import fetchall
from tests._support.factories import sku_workbook
from tests._support.providers import FakeSearchProvider


async def test_single_match_product_creates_single_run(tmp_path, monkeypatch):
    path = tmp_path / "single.db"
    db = SearchDB(str(path))
    provider = FakeSearchProvider()
    monkeypatch.setattr("src.search.pipeline.get_db", lambda: db)
    result = await match_product(
        "Unlisted product", "tesco", provider=provider, record=True
    )
    assert result.verdict == FinalVerdict.NO_MATCH
    assert provider.closed is False
    assert fetchall(
        path,
        "SELECT mode,total_tasks,status,matched_count,no_match_count,error_count,provider_calls FROM runs",
    ) == [("single", 1, "completed", 0, 1, 0, None)]
    run_id = fetchall(path, "SELECT run_id FROM runs")[0][0]
    assert fetchall(path, "SELECT run_id,row_index,verdict FROM tasks") == [
        (run_id, 0, "no_match")
    ]
    assert fetchall(path, "PRAGMA foreign_key_check") == []


async def test_single_provider_initialization_failure_is_recorded(tmp_path, monkeypatch):
    path = tmp_path / "single.db"
    db = SearchDB(str(path))

    monkeypatch.setattr("src.search.pipeline.get_db", lambda: db)

    def fail_provider_init(*_args, **_kwargs):
        raise RuntimeError("provider init failed")

    monkeypatch.setattr("src.search.pipeline.make_provider_chain", fail_provider_init)
    with pytest.raises(RuntimeError, match="provider init failed"):
        await match_product("product", "tesco", record=True)

    assert fetchall(path, "SELECT mode,status,error_count FROM runs") == [
        ("single", "failed", 1)
    ]
    assert fetchall(path, "SELECT status,error_type FROM tasks") == [
        ("error", "RuntimeError")
    ]


async def test_batch_returns_columns_writes_file_and_records_one_run(tmp_path, monkeypatch):
    input_path = sku_workbook(
        tmp_path,
        [
            {"sku": "one", "web": "tesco", "country": "uk"},
            {"sku": "two", "web": "tesco", "country": "uk"},
            {"sku": "three", "web": "tesco", "country": "uk"},
        ],
    )
    output_path = tmp_path / "nested" / "output.xlsx"
    db_path = tmp_path / "batch.db"
    db = SearchDB(str(db_path))
    provider = FakeSearchProvider()
    monkeypatch.setattr("src.search.batch.get_db", lambda: db)
    result = await match_product_batch(
        str(input_path),
        sku_col="sku",
        web_col="web",
        country_col="country",
        output_file=str(output_path),
        concurrency=2,
        provider=provider,
        progress=False,
    )
    assert list(result.df.columns[-4:]) == [
        "url_search_1",
        "match_verdict",
        "match_layer_trace",
        "match_reason",
    ]
    assert output_path.exists()
    assert result.output_path == str(output_path)
    assert result.provider_calls == {"fake": provider.calls}
    assert provider.closed is False
    assert fetchall(
        db_path,
        "SELECT run_id,mode,total_tasks,status,no_match_count,error_count FROM runs",
    ) == [(result.run_id, "batch", 3, "completed", 3, 0)]
    assert fetchall(db_path, "SELECT COUNT(*) FROM tasks") == [(3,)]
    assert fetchall(db_path, "PRAGMA foreign_key_check") == []


async def test_typed_in_memory_batch_records_one_run(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    db = SearchDB(str(path))
    provider = FakeSearchProvider()
    monkeypatch.setattr("src.search.batch.get_db", lambda: db)
    result = await match_products(
        [
            SearchRequest("one", "tesco", "uk"),
            SearchRequest("two", "tesco", "uk"),
        ],
        provider=provider,
        concurrency=2,
    )
    assert len(result.items) == 2
    assert all(item.result.verdict == FinalVerdict.NO_MATCH for item in result.items)
    assert fetchall(path, "SELECT mode,total_tasks FROM runs") == [("batch", 2)]


async def test_batch_row_exception_becomes_error_without_failing_run(tmp_path, monkeypatch):
    input_path = sku_workbook(
        tmp_path,
        [
            {"sku": "good", "web": "tesco", "country": "uk"},
            {"sku": "broken", "web": "tesco", "country": "uk"},
            {"sku": "also good", "web": "tesco", "country": "uk"},
        ],
    )
    db_path = tmp_path / "batch.db"
    db = SearchDB(str(db_path))
    provider = FakeSearchProvider()
    real_match_product = match_product

    async def selective_match(product_name, *args, **kwargs):
        if product_name == "broken":
            raise RuntimeError("row exploded")
        return await real_match_product(product_name, *args, **kwargs)

    monkeypatch.setattr("src.search.batch.get_db", lambda: db)
    monkeypatch.setattr("src.search.batch.match_product", selective_match)
    result = await match_product_batch(
        str(input_path),
        sku_col="sku",
        web_col="web",
        country_col="country",
        provider=provider,
        progress=False,
    )
    assert result.df.loc[1, "url_search_1"] == "not found"
    assert result.df.loc[1, "match_verdict"] == "error"
    assert result.df.loc[1, "match_reason"] == "row exploded"
    assert fetchall(db_path, "SELECT status,error_count FROM runs") == [
        ("completed", 1)
    ]
    assert fetchall(
        db_path, "SELECT status,verdict,error_type FROM tasks ORDER BY row_index"
    ) == [
        ("ok", "no_match", None),
        ("error", "error", "RuntimeError"),
        ("ok", "no_match", None),
    ]


async def test_blank_country_cell_becomes_row_error_without_search_call(tmp_path, monkeypatch):
    input_path = sku_workbook(
        tmp_path,
        [
            {"sku": "one", "web": "tesco", "country": "uk"},
            {"sku": "skipped", "web": "tesco", "country": None},
            {"sku": "three", "web": "tesco", "country": "uk"},
        ],
    )
    db_path = tmp_path / "batch.db"
    db = SearchDB(str(db_path))
    provider = FakeSearchProvider()
    monkeypatch.setattr("src.search.batch.get_db", lambda: db)
    result = await match_product_batch(
        str(input_path),
        sku_col="sku",
        web_col="web",
        country_col="country",
        provider=provider,
        progress=False,
    )
    assert result.df["match_verdict"].tolist() == [
        "no_match",
        "error",
        "no_match",
    ]
    assert result.df.loc[1, "url_search_1"] == "not found"
    assert result.df.loc[1, "match_reason"] == "missing country value"
    assert provider.calls == 2
    assert fetchall(db_path, "SELECT status,error_count FROM runs") == [
        ("completed", 1)
    ]
    assert fetchall(
        db_path,
        "SELECT row_index,status,website,country FROM tasks ORDER BY row_index",
    ) == [
        (0, "ok", "tesco", "uk"),
        (1, "error", "tesco", ""),
        (2, "ok", "tesco", "uk"),
    ]


async def test_missing_batch_column_raises_key_error(tmp_path):
    input_path = sku_workbook(
        tmp_path, [{"sku": "one", "country": "uk"}]
    )

    with pytest.raises(KeyError, match="columns not found: web"):
        await match_product_batch(
            str(input_path),
            sku_col="sku",
            web_col="web",
            country_col="country",
            provider=FakeSearchProvider(),
            progress=False,
        )


async def test_mixed_row_targets_are_summarized_on_run(tmp_path, monkeypatch):
    input_path = sku_workbook(
        tmp_path,
        [
            {"sku": "one", "web": " Tesco ", "country": "UK"},
            {"sku": "two", "web": "Amazon", "country": "DE"},
            {"sku": "three", "web": "tesco", "country": "uk"},
        ],
    )
    db_path = tmp_path / "batch.db"
    db = SearchDB(str(db_path))
    monkeypatch.setattr("src.search.batch.get_db", lambda: db)
    await match_product_batch(
        str(input_path),
        sku_col="sku",
        web_col="web",
        country_col="country",
        provider=FakeSearchProvider(),
        progress=False,
    )
    website, country, raw_job_config = fetchall(
        db_path, "SELECT website,country,job_config FROM runs"
    )[0]
    assert (website, country) == ("tesco,amazon", "uk,de")
    job_config = json.loads(raw_job_config)
    assert job_config["web_col"] == "web"
    assert job_config["country_col"] == "country"
    assert "website" not in job_config
    assert "country" not in job_config


def test_batch_cli_accepts_underscore_column_aliases(monkeypatch):
    argv = [
        "batch",
        "--input",
        "input.xlsx",
        "--sku-col",
        "sku",
        "--web_col",
        "web",
        "--country_col",
        "country",
    ]
    monkeypatch.setattr("sys.argv", argv)
    args = _parse_args()

    assert args.web_col == "web"
    assert args.country_col == "country"


def test_search_db_upgrades_v1_runs_table_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                input_file TEXT,
                input_sku_col TEXT,
                output_file TEXT,
                country TEXT,
                website TEXT,
                provider_chain TEXT,
                llm_model TEXT,
                concurrency INTEGER,
                serper_max_calls INTEGER,
                total_tasks INTEGER,
                matched_count INTEGER,
                no_match_count INTEGER,
                error_count INTEGER,
                provider_calls TEXT,
                job_config TEXT,
                pipeline_config TEXT,
                git_commit TEXT,
                error_message TEXT
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta(key, value) VALUES ('schema_version', '1');
            """
        )
        conn.commit()
    finally:
        conn.close()

    SearchDB(str(path))
    columns = {row[1] for row in fetchall(path, "PRAGMA table_info(runs)")}
    assert "mode" in columns
    assert fetchall(path, "SELECT value FROM meta WHERE key='schema_version'") == [
        (SCHEMA_VERSION,)
    ]


async def test_cancelled_batch_marks_run_interrupted_and_keeps_finished_tasks(tmp_path, monkeypatch):
    input_path = sku_workbook(
        tmp_path,
        [
            {"sku": "finished", "web": "tesco", "country": "uk"},
            {"sku": "blocked", "web": "tesco", "country": "uk"},
        ],
    )
    db_path = tmp_path / "batch.db"
    db = SearchDB(str(db_path))
    provider = FakeSearchProvider()
    first_finished = asyncio.Event()

    async def controlled_match(product_name, *args, **kwargs):
        if product_name == "blocked":
            await asyncio.Event().wait()
        result = await match_product(product_name, *args, **kwargs)
        first_finished.set()
        return result

    monkeypatch.setattr("src.search.batch.get_db", lambda: db)
    monkeypatch.setattr("src.search.batch.match_product", controlled_match)
    task = asyncio.create_task(
        match_product_batch(
            str(input_path),
            sku_col="sku",
            web_col="web",
            country_col="country",
            concurrency=1,
            provider=provider,
            progress=False,
        )
    )
    await first_finished.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert fetchall(db_path, "SELECT status FROM runs") == [("interrupted",)]
    assert fetchall(
        db_path, "SELECT product_name,status FROM tasks ORDER BY row_index"
    )[0] == ("finished", "ok")
