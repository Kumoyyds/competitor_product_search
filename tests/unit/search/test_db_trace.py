import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from src.search.db import SearchDB
from src.search.models import FinalVerdict, RawCandidate
from src.search.pipeline import match_product
from src.search.providers.base import SearchProvider
from src.search.trace import (
    AttemptRecord,
    FAILURE_ALL_DOMAIN_FILTERED,
    FAILURE_BRAND_MISMATCH,
    FAILURE_BUDGET_EXHAUSTED,
    FAILURE_DOMAIN_MAP_MISSING,
    FAILURE_LLM_ERROR,
    FAILURE_LLM_NO_MATCH,
    FAILURE_LLM_PARSE_ERROR,
    FAILURE_MATCHED,
    FAILURE_NO_SEARCH_RESULTS,
    FAILURE_NUMERIC_MISMATCH,
    FAILURE_PROVIDER_ERROR,
    FAILURE_UNKNOWN_ERROR,
    TaskRecorder,
    derive_failure_kind,
    reset_recorder,
    set_recorder,
)


class FakeProvider(SearchProvider):
    name = "fake"

    def __init__(self, results=None, error=None, name="fake"):
        self.results = results or []
        self.error = error
        self.name = name

    async def search(self, query, k=10, country="uk"):
        if self.error:
            raise self.error
        return list(self.results)


def _run_recorded(provider, llm=None):
    async def run():
        recorder = TaskRecorder(
            run_id="run-1",
            row_index=0,
            product_name="Magic Rock Saucery 4 X 330ML",
            website="tesco",
            country="uk",
        )
        token = set_recorder(recorder)
        try:
            if llm is None:
                result = await match_product(
                    recorder.product_name, recorder.website, provider=provider
                )
            else:
                with patch("src.search.layers.distinguishing._get_llm", return_value=llm):
                    result = await match_product(
                        recorder.product_name, recorder.website, provider=provider
                    )
            recorder.complete(result, recorder.final_provider)
            return recorder, result
        finally:
            reset_recorder(token)

    with patch("src.search.layers.base_match.get_cache", return_value=None):
        return asyncio.run(run())


def _candidate(domain=None, brand=None, numeric=None):
    return {
        "v_domain": domain,
        "v_brand": brand,
        "v_numeric": numeric,
    }


def _failure_recorder():
    recorder = TaskRecorder(
        run_id="r", row_index=0, product_name="x", website="tesco", country="uk"
    )
    recorder.status = "ok"
    recorder.verdict = FinalVerdict.NO_MATCH.value
    recorder.attempts = [AttemptRecord(attempt_no=1, provider="fake")]
    return recorder


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda r, a: None, FAILURE_NO_SEARCH_RESULTS),
        (
            lambda r, a: a.node_events.append({"error_kind": "DomainMapMissing"}),
            FAILURE_DOMAIN_MAP_MISSING,
        ),
        (
            lambda r, a: a.node_events.append({"error_kind": "LLMError"}),
            FAILURE_LLM_ERROR,
        ),
        (
            lambda r, a: a.node_events.append({"error_kind": "LLMParseError"}),
            FAILURE_LLM_PARSE_ERROR,
        ),
        (lambda r, a: setattr(a, "budget_exhausted", True), FAILURE_BUDGET_EXHAUSTED),
        (
            lambda r, a: (
                a.query_variants.append("q"),
                a.node_events.append({"node": "search", "status": "warning"}),
            ),
            FAILURE_PROVIDER_ERROR,
        ),
        (
            lambda r, a: a.candidates.append(_candidate(domain="fail")),
            FAILURE_ALL_DOMAIN_FILTERED,
        ),
        (
            lambda r, a: a.candidates.append(
                _candidate(domain="pass", brand="fail")
            ),
            FAILURE_BRAND_MISMATCH,
        ),
        (
            lambda r, a: a.candidates.append(
                _candidate(domain="pass", brand="pass", numeric="fail")
            ),
            FAILURE_NUMERIC_MISMATCH,
        ),
        (
            lambda r, a: (
                a.candidates.append(
                    _candidate(domain="pass", brand="pass", numeric="pass")
                ),
                a.llm_calls.append({"status": "ok"}),
            ),
            FAILURE_LLM_NO_MATCH,
        ),
        (
            lambda r, a: a.candidates.append(
                _candidate(domain="pass", brand="pass", numeric="pass")
            ),
            FAILURE_UNKNOWN_ERROR,
        ),
    ],
)
def test_derive_failure_kind(setup, expected):
    recorder = _failure_recorder()
    setup(recorder, recorder.attempts[0])
    assert derive_failure_kind(recorder) == expected


def test_derive_failure_kind_match_and_task_error():
    recorder = _failure_recorder()
    recorder.verdict = FinalVerdict.MATCH.value
    assert derive_failure_kind(recorder) == FAILURE_MATCHED
    recorder.status = "error"
    recorder.verdict = "error"
    assert derive_failure_kind(recorder) == FAILURE_UNKNOWN_ERROR


def test_short_path_records_five_primary_node_events():
    recorder, result = _run_recorded(FakeProvider())
    assert result.verdict == FinalVerdict.NO_MATCH
    primary = [event for event in recorder.attempts[0].node_events if event["status"] != "warning"]
    assert [(event["node"], event["status"]) for event in primary] == [
        ("search", "ok"),
        ("domain_filter", "skipped"),
        ("base_match", "skipped"),
        ("distinguishing", "skipped"),
        ("aggregate", "ok"),
    ]
    assert recorder.failure_kind == FAILURE_NO_SEARCH_RESULTS


def test_domain_filter_records_product_page_reject_counts():
    candidate = RawCandidate(
        title="Tesco search results",
        url="https://www.tesco.com/shop/en-GB/search?q=saucery",
    )

    recorder, result = _run_recorded(FakeProvider([candidate]))

    assert result.verdict == FinalVerdict.NO_MATCH
    event = next(
        item
        for item in recorder.attempts[0].node_events
        if item["node"] == "domain_filter" and item["status"] == "ok"
    )
    assert event["detail"]["domain_rejects"] == {
        "host": 0,
        "not_product_page": 1,
    }


def test_llm_error_is_not_business_no_match():
    candidate = RawCandidate(
        title="Magic Rock Saucery 4 X 330ML",
        url="https://www.tesco.com/products/1",
    )
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    recorder, result = _run_recorded(FakeProvider([candidate]), llm=llm)
    assert result.verdict == FinalVerdict.NO_MATCH
    assert recorder.failure_kind == FAILURE_LLM_ERROR
    assert recorder.attempts[0].llm_calls[0]["status"] == "error"


def test_llm_parse_error_is_structured():
    candidate = RawCandidate(
        title="Magic Rock Saucery 4 X 330ML",
        url="https://www.tesco.com/products/1",
    )
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(
        return_value=type("Msg", (), {"content": "definitely not JSON"})()
    )
    recorder, result = _run_recorded(FakeProvider([candidate]), llm=llm)
    assert result.verdict == FinalVerdict.NO_MATCH
    assert recorder.failure_kind == FAILURE_LLM_PARSE_ERROR
    assert recorder.attempts[0].llm_calls[0]["status"] == "parse_error"


def test_provider_chain_records_distinct_attempts():
    candidate = RawCandidate(
        title="Magic Rock Saucery 4 X 330ML",
        url="https://www.tesco.com/products/1",
    )
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(
        return_value=type(
            "Msg", (), {"content": '{"match_idx": 0, "reason": "same SKU"}'}
        )()
    )
    providers = [
        FakeProvider(name="first"),
        FakeProvider([candidate], name="second"),
    ]
    recorder, result = _run_recorded(providers, llm=llm)
    assert result.verdict == FinalVerdict.MATCH
    assert [attempt.provider for attempt in recorder.attempts] == ["first", "second"]
    assert recorder.final_provider == "second"
    assert result.reason.endswith("(via second)")


def test_search_db_idempotent_and_flushes_full_task(tmp_path):
    path = tmp_path / "search.sqlite"
    db = SearchDB(str(path))
    SearchDB(str(path))
    db.start_run(
        run_id="run-1",
        input_file="input.xlsx",
        input_sku_col="sku",
        output_file="output.xlsx",
        country="uk",
        website="tesco",
        provider_chain="fake",
        llm_model="fake-model",
        concurrency=1,
        serper_max_calls=None,
        total_tasks=1,
        job_config={"web": "tesco"},
        pipeline_config={"search": {"provider": "fake"}},
        git_commit=None,
    )

    candidate = RawCandidate(
        title="Magic Rock Saucery 4 X 330ML",
        url="https://www.tesco.com/products/1",
    )
    llm = AsyncMock()
    llm.ainvoke = AsyncMock(
        return_value=type(
            "Msg",
            (),
            {
                "content": '{"match_idx": 0, "reason": "same SKU"}',
                "usage_metadata": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )()
    )
    recorder, _ = _run_recorded(FakeProvider([candidate]), llm=llm)
    db.flush_task(recorder)
    db.finish_run("run-1", status="completed", provider_calls={"fake": 1})

    conn = sqlite3.connect(path)
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "runs",
                "tasks",
                "attempts",
                "node_events",
                "candidates",
                "llm_calls",
                "meta",
            )
        }
        assert counts == {
            "runs": 1,
            "tasks": 1,
            "attempts": 1,
            "node_events": 5,
            "candidates": 1,
            "llm_calls": 1,
            "meta": 1,
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT failure_kind, final_provider FROM tasks"
        ).fetchone() == (FAILURE_MATCHED, "fake")
        assert conn.execute(
            "SELECT status, prompt_tokens, completion_tokens, total_tokens FROM llm_calls"
        ).fetchone() == ("ok", 10, 5, 15)
        assert conn.execute(
            "SELECT failure_distribution FROM v_run_summary"
        ).fetchone()[0] == '{"matched":1}'
        assert conn.execute("SELECT COUNT(*) FROM v_funnel").fetchone()[0] == 5
        assert conn.execute(
            "SELECT duration_ms IS NOT NULL FROM v_run_summary"
        ).fetchone()[0] == 1
    finally:
        conn.close()
