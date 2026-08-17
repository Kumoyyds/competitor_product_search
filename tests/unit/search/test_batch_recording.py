import asyncio
import json
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from src.search.batch import _parse_args, match_product_batch
from src.search.db import SCHEMA_VERSION, SearchDB
from src.search.models import FinalVerdict
from src.search.pipeline import match_product
from src.search.providers.base import SearchProvider


class FakeProvider(SearchProvider):
    name = "fake"

    def __init__(self):
        self.calls = 0
        self.closed = False

    async def search(self, query, k=10, country="uk"):
        self.calls += 1
        return []

    def calls_made(self):
        return self.calls

    async def aclose(self):
        self.closed = True


def _fetchall(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_single_match_product_creates_single_run(tmp_path):
    path = tmp_path / "single.sqlite"
    db = SearchDB(str(path))
    provider = FakeProvider()

    async def run():
        with patch("src.search.pipeline.get_db", return_value=db):
            return await match_product(
                "Unlisted product",
                "tesco",
                provider=provider,
                record=True,
            )

    result = asyncio.run(run())
    assert result.verdict == FinalVerdict.NO_MATCH
    assert provider.closed is False
    assert _fetchall(
        path,
        "SELECT mode,total_tasks,status,matched_count,no_match_count,error_count,provider_calls FROM runs",
    ) == [("single", 1, "completed", 0, 1, 0, None)]
    run_id = _fetchall(path, "SELECT run_id FROM runs")[0][0]
    assert _fetchall(path, "SELECT run_id,row_index,verdict FROM tasks") == [
        (run_id, 0, "no_match")
    ]
    assert _fetchall(path, "PRAGMA foreign_key_check") == []


def test_single_provider_initialization_failure_is_recorded(tmp_path):
    path = tmp_path / "single.sqlite"
    db = SearchDB(str(path))

    async def run():
        with (
            patch("src.search.pipeline.get_db", return_value=db),
            patch(
                "src.search.pipeline.make_provider_chain",
                side_effect=RuntimeError("provider init failed"),
            ),
        ):
            try:
                await match_product("product", "tesco", record=True)
            except RuntimeError:
                pass

    asyncio.run(run())
    assert _fetchall(path, "SELECT mode,status,error_count FROM runs") == [
        ("single", "failed", 1)
    ]
    assert _fetchall(path, "SELECT status,error_type FROM tasks") == [
        ("error", "RuntimeError")
    ]


def test_batch_returns_columns_writes_file_and_records_one_run(tmp_path):
    input_path = tmp_path / "input.xlsx"
    output_path = tmp_path / "nested" / "output.xlsx"
    db_path = tmp_path / "batch.sqlite"
    pd.DataFrame(
        {
            "sku": ["one", "two", "three"],
            "web": ["tesco", "tesco", "tesco"],
            "country": ["uk", "uk", "uk"],
        }
    ).to_excel(input_path, index=False)
    db = SearchDB(str(db_path))
    provider = FakeProvider()

    async def run():
        with patch("src.search.batch.get_db", return_value=db):
            return await match_product_batch(
                str(input_path),
                sku_col="sku",
                web_col="web",
                country_col="country",
                output_file=str(output_path),
                concurrency=2,
                provider=provider,
                progress=False,
            )

    result = asyncio.run(run())
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
    assert _fetchall(
        db_path,
        "SELECT run_id,mode,total_tasks,status,no_match_count,error_count FROM runs",
    ) == [(result.run_id, "batch", 3, "completed", 3, 0)]
    assert _fetchall(db_path, "SELECT COUNT(*) FROM tasks") == [(3,)]
    assert _fetchall(db_path, "PRAGMA foreign_key_check") == []


def test_batch_row_exception_becomes_error_without_failing_run(tmp_path):
    input_path = tmp_path / "input.xlsx"
    db_path = tmp_path / "batch.sqlite"
    pd.DataFrame(
        {
            "sku": ["good", "broken", "also good"],
            "web": ["tesco", "tesco", "tesco"],
            "country": ["uk", "uk", "uk"],
        }
    ).to_excel(input_path, index=False)
    db = SearchDB(str(db_path))
    provider = FakeProvider()
    real_match_product = match_product

    async def selective_match(product_name, *args, **kwargs):
        if product_name == "broken":
            raise RuntimeError("row exploded")
        return await real_match_product(product_name, *args, **kwargs)

    async def run():
        with (
            patch("src.search.batch.get_db", return_value=db),
            patch("src.search.batch.match_product", side_effect=selective_match),
        ):
            return await match_product_batch(
                str(input_path),
                sku_col="sku",
                web_col="web",
                country_col="country",
                provider=provider,
                progress=False,
            )

    result = asyncio.run(run())
    assert result.df.loc[1, "url_search_1"] == "not found"
    assert result.df.loc[1, "match_verdict"] == "error"
    assert result.df.loc[1, "match_reason"] == "row exploded"
    assert _fetchall(db_path, "SELECT status,error_count FROM runs") == [
        ("completed", 1)
    ]
    assert _fetchall(
        db_path, "SELECT status,verdict,error_type FROM tasks ORDER BY row_index"
    ) == [
        ("ok", "no_match", None),
        ("error", "error", "RuntimeError"),
        ("ok", "no_match", None),
    ]


def test_blank_country_cell_becomes_row_error_without_search_call(tmp_path):
    input_path = tmp_path / "input.xlsx"
    db_path = tmp_path / "batch.sqlite"
    pd.DataFrame(
        {
            "sku": ["one", "skipped", "three"],
            "web": ["tesco", "tesco", "tesco"],
            "country": ["uk", None, "uk"],
        }
    ).to_excel(input_path, index=False)
    db = SearchDB(str(db_path))
    provider = FakeProvider()

    async def run():
        with patch("src.search.batch.get_db", return_value=db):
            return await match_product_batch(
                str(input_path),
                sku_col="sku",
                web_col="web",
                country_col="country",
                provider=provider,
                progress=False,
            )

    result = asyncio.run(run())
    assert result.df["match_verdict"].tolist() == [
        "no_match",
        "error",
        "no_match",
    ]
    assert result.df.loc[1, "url_search_1"] == "not found"
    assert result.df.loc[1, "match_reason"] == "missing country value"
    assert provider.calls == 2
    assert _fetchall(db_path, "SELECT status,error_count FROM runs") == [
        ("completed", 1)
    ]
    assert _fetchall(
        db_path,
        "SELECT row_index,status,website,country FROM tasks ORDER BY row_index",
    ) == [
        (0, "ok", "tesco", "uk"),
        (1, "error", "tesco", ""),
        (2, "ok", "tesco", "uk"),
    ]


def test_missing_batch_column_raises_key_error(tmp_path):
    input_path = tmp_path / "input.xlsx"
    pd.DataFrame({"sku": ["one"], "country": ["uk"]}).to_excel(
        input_path, index=False
    )

    with pytest.raises(KeyError, match="columns not found: web"):
        asyncio.run(
            match_product_batch(
                str(input_path),
                sku_col="sku",
                web_col="web",
                country_col="country",
                provider=FakeProvider(),
                progress=False,
            )
        )


def test_mixed_row_targets_are_summarized_on_run(tmp_path):
    input_path = tmp_path / "input.xlsx"
    db_path = tmp_path / "batch.sqlite"
    pd.DataFrame(
        {
            "sku": ["one", "two", "three"],
            "web": [" Tesco ", "Amazon", "tesco"],
            "country": ["UK", "DE", "uk"],
        }
    ).to_excel(input_path, index=False)
    db = SearchDB(str(db_path))

    async def run():
        with patch("src.search.batch.get_db", return_value=db):
            return await match_product_batch(
                str(input_path),
                sku_col="sku",
                web_col="web",
                country_col="country",
                provider=FakeProvider(),
                progress=False,
            )

    asyncio.run(run())
    website, country, raw_job_config = _fetchall(
        db_path, "SELECT website,country,job_config FROM runs"
    )[0]
    assert (website, country) == ("tesco,amazon", "uk,de")
    job_config = json.loads(raw_job_config)
    assert job_config["web_col"] == "web"
    assert job_config["country_col"] == "country"
    assert "website" not in job_config
    assert "country" not in job_config


def test_batch_cli_accepts_underscore_column_aliases():
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
    with patch("sys.argv", argv):
        args = _parse_args()

    assert args.web_col == "web"
    assert args.country_col == "country"


def test_search_db_upgrades_v1_runs_table_in_place(tmp_path):
    path = tmp_path / "old.sqlite"
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
    columns = {row[1] for row in _fetchall(path, "PRAGMA table_info(runs)")}
    assert "mode" in columns
    assert _fetchall(path, "SELECT value FROM meta WHERE key='schema_version'") == [
        (SCHEMA_VERSION,)
    ]


def test_cancelled_batch_marks_run_interrupted_and_keeps_finished_tasks(tmp_path):
    input_path = tmp_path / "input.xlsx"
    db_path = tmp_path / "batch.sqlite"
    pd.DataFrame(
        {
            "sku": ["finished", "blocked"],
            "web": ["tesco", "tesco"],
            "country": ["uk", "uk"],
        }
    ).to_excel(input_path, index=False)
    db = SearchDB(str(db_path))
    provider = FakeProvider()

    async def run():
        first_finished = asyncio.Event()

        async def controlled_match(product_name, *args, **kwargs):
            if product_name == "blocked":
                await asyncio.Event().wait()
            result = await match_product(product_name, *args, **kwargs)
            first_finished.set()
            return result

        with (
            patch("src.search.batch.get_db", return_value=db),
            patch("src.search.batch.match_product", side_effect=controlled_match),
        ):
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
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    assert _fetchall(db_path, "SELECT status FROM runs") == [("interrupted",)]
    assert _fetchall(
        db_path, "SELECT product_name,status FROM tasks ORDER BY row_index"
    )[0] == ("finished", "ok")
