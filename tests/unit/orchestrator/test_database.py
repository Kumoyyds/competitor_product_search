from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from src.models import (
    DecisionNode,
    DecisionNodeRecord,
    DecisionSource,
    EvidenceStatus,
    InputItem,
    ProductMatchResult,
    ProductMatchVerdict,
)
from src.orchestrator.database import OrchestratorDB
from tests._support.factories import product_data


def verified() -> ProductMatchResult:
    return ProductMatchResult(
        verdict=ProductMatchVerdict.MATCH,
        decision_source=DecisionSource.LLM,
        reasoning="same",
        gtin_status=EvidenceStatus.UNKNOWN,
        variant_status=EvidenceStatus.UNKNOWN,
        trace=[
            DecisionNodeRecord(
                node=DecisionNode.LLM,
                status="match",
                terminal=True,
                detail={"reasoning": "same"},
            )
        ],
    )


def _valid_root(db_path, *, count: int = 1, title: str = "Same search title") -> str:
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    for index in range(count):
        item = InputItem(title=f"Input {index}", country="uk", site_name="tesco")
        item_id = db.add_item(root, row_index=index, raw=item.model_dump(), item=item)
        product = product_data(title=f"Product {index}", url=f"https://example.test/{index}")
        db.record_matching_result(
            item_id,
            execution_path="new_input",
            url=product.url,
            result=verified(),
        )
        db.record_valid(
            item_id,
            product,
            search_title=title,
            execution_path="new_input",
        )
    db.finish_batch(root)
    db.close()
    return root


def test_terminal_valid_and_failure_are_mutually_exclusive(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    product = product_data()
    db.record_valid(
        item_id,
        product,
        search_title="Search title",
        execution_path="new_input",
    )
    with pytest.raises(RuntimeError, match="already terminal"):
        db.record_failure(
            item_id,
            fail_node="match",
            failure_kind="no_match",
            reasoning="must not be inserted",
        )
    assert db.conn.execute("SELECT COUNT(*) FROM valid_results").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM failure_results").fetchone()[0] == 0
    db.close()


def test_finish_batch_never_completes_with_pending_items(tmp_path):
    db = OrchestratorDB(tmp_path / "orchestrator.db")
    try:
        batch_id = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
        item = InputItem(title="Pending", country="uk", site_name="tesco")
        db.add_item(batch_id, row_index=0, raw=item.model_dump(), item=item)
        assert db.finish_batch(batch_id)["status"] == "failed"
        row = db.get_batch(batch_id)
        assert row is not None and "1 item(s) did not finish" in row["error_message"]

        batch_id = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
        db.add_item(batch_id, row_index=0, raw=item.model_dump(), item=item)
        assert db.finish_batch(batch_id, error_message="")["status"] == "failed"

        batch_id = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
        db.add_item(batch_id, row_index=0, raw=item.model_dump(), item=item)
        assert db.finish_batch(batch_id, interrupted=True)["status"] == "interrupted"
    finally:
        db.close()


def test_concurrent_reruns_allocate_unique_monotonic_suffixes(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    root = _valid_root(db_path)

    def allocate(_index: int) -> str:
        db = OrchestratorDB(db_path)
        try:
            return db.create_rerun_batch(root, vision_enabled=False, job_config={})
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        allocated = list(pool.map(allocate, range(4)))
    assert set(allocated) == {f"{root}-r{number}" for number in range(1, 5)}


def test_title_filter_selects_all_duplicate_titles(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    root = _valid_root(db_path, count=2)
    db = OrchestratorDB(db_path)
    try:
        sources = db.rerun_sources(root, ["  SAME SEARCH TITLE  "])
        assert len(sources) == 2
    finally:
        db.close()


def test_rerun_source_uses_latest_valid_across_root_lineage(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    root = _valid_root(db_path)
    db = OrchestratorDB(db_path)
    try:
        original = db.rerun_sources(root)[0]
        child = db.create_rerun_batch(root, vision_enabled=False, job_config={})
        item_id = db.add_item(
            child,
            row_index=original.row_index,
            raw=original.item.model_dump(),
            item=original.item,
            logical_item_id=original.logical_item_id,
            source_valid_result_id=original.valid_result_id,
            execution_path="stored_url",
        )
        latest = product_data(title="Latest", url="https://example.test/latest")
        db.record_valid(
            item_id,
            latest,
            search_title="Latest search title",
            execution_path="stored_url",
        )
        db.finish_batch(child)

        selected = db.rerun_sources(root)[0]
        assert selected.url == "https://example.test/latest"
        assert selected.search_title == "Latest search title"
    finally:
        db.close()


def test_matching_decisions_increment_per_item_and_round_trip(tmp_path):
    db = OrchestratorDB(tmp_path / "orchestrator.db")
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input", country="uk", site_name="tesco")
    first = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    second = db.add_item(root, row_index=1, raw=item.model_dump(), item=item)

    assert db.record_matching_result(
        first,
        execution_path="new_input",
        url="https://example.test/one",
        result=verified(),
    ) > 0
    payload = {"nodes": [{"node": "llm", "status": "error"}], "terminated_at": "technical_error"}
    db.record_matching_decision(
        first,
        execution_path="fallback",
        url=None,
        verdict="error",
        decision_source="technical_error",
        reasoning="timeout",
        decision_process=payload,
    )
    db.record_matching_decision(
        second,
        execution_path="new_input",
        url=None,
        verdict="match",
        decision_source="identity_guard",
        reasoning="reused",
    )

    rows = db.conn.execute(
        "SELECT item_id,attempt_no,verdict,variant_status,vision_status,decision_process "
        "FROM matching_decisions "
        "ORDER BY decision_id"
    ).fetchall()
    assert [(row["item_id"], row["attempt_no"]) for row in rows] == [
        (first, 1),
        (first, 2),
        (second, 1),
    ]
    assert rows[0]["variant_status"] is None
    assert rows[0]["vision_status"] is None
    assert json.loads(rows[1]["decision_process"]) == payload
    assert db.conn.execute(
        "SELECT status FROM batch_items WHERE item_id = ?", (first,)
    ).fetchone()[0] == "pending"

    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "UPDATE matching_decisions SET attempt_no=1 "
            "WHERE item_id=? AND attempt_no=2",
            (first,),
        )
    db.conn.rollback()

    db.conn.execute("DELETE FROM batches WHERE batch_id = ?", (root,))
    db.conn.commit()
    assert db.conn.execute("SELECT COUNT(*) FROM matching_decisions").fetchone()[0] == 0
    db.close()


def test_summary_views_derive_terminal_counts_and_latest_snapshot(tmp_path):
    db = OrchestratorDB(tmp_path / "orchestrator.db")
    root = db.create_new_batch(
        vision_enabled=True,
        source_file=None,
        job_config={"concurrency": 4, "vision_enabled": True},
    )
    item = InputItem(title="Input", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)
    product = product_data(url="https://example.test/current")
    db.record_valid(
        item_id,
        product,
        search_title="Search title",
        execution_path="new_input",
    )
    db.finish_batch(root)

    summary = db.conn.execute(
        "SELECT total_items,valid_count,failure_count,job_config FROM batch_summary"
    ).fetchone()
    assert tuple(summary[:3]) == (1, 1, 0)
    assert json.loads(summary["job_config"]) == {"concurrency": 4}
    latest = db.conn.execute(
        "SELECT input_title,search_title,url,product_data FROM latest_valid_results"
    ).fetchone()
    assert tuple(latest[:3]) == (
        "Input",
        "Search title",
        "https://example.test/current",
    )
    assert json.loads(latest["product_data"])["url"] == latest["url"]
    db.close()


def test_database_constraints_reject_terminal_drift_and_invalid_json(tmp_path):
    db = OrchestratorDB(tmp_path / "orchestrator.db")
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    item = InputItem(title="Input", country="uk", site_name="tesco")
    item_id = db.add_item(root, row_index=0, raw=item.model_dump(), item=item)

    with pytest.raises(sqlite3.IntegrityError, match="requires its outcome"):
        db.conn.execute("UPDATE batch_items SET status='valid' WHERE item_id=?", (item_id,))
    db.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.conn.execute("UPDATE batch_items SET stage_trace='not-json' WHERE item_id=?", (item_id,))
    db.conn.rollback()

    product = product_data()
    db.record_valid(
        item_id,
        product,
        search_title="Search title",
        execution_path="new_input",
    )
    with pytest.raises(RuntimeError, match="already terminal"):
        db.update_item(item_id, trace_event={"stage": "late"})
    with pytest.raises(sqlite3.IntegrityError, match="already has a terminal outcome"):
        db.conn.execute(
            "INSERT INTO failure_results "
            "(item_id,fail_node,failure_kind,reasoning,detail,created_at) "
            "VALUES (?,'match','no_match','late','{}','now')",
            (item_id,),
        )
    db.conn.rollback()
    db.close()


def test_v2_migration_removes_denormalized_columns_and_backfills_decisions(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE batches (
            batch_id TEXT PRIMARY KEY, root_batch_id TEXT, parent_batch_id TEXT,
            rerun_no INTEGER, operation TEXT, status TEXT, vision_enabled INTEGER,
            source_file TEXT, job_config TEXT, total_items INTEGER, valid_count INTEGER,
            failure_count INTEGER, created_at TEXT, finished_at TEXT, error_message TEXT
        );
        CREATE TABLE batch_items (
            item_id INTEGER PRIMARY KEY, batch_id TEXT, logical_item_id TEXT,
            source_item_id INTEGER, row_index INTEGER, input_title TEXT, country TEXT,
            site_name TEXT, input_gtin TEXT, input_image_urls TEXT, status TEXT,
            execution_path TEXT, search_title TEXT, matched_url TEXT, stage_trace TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE valid_results (
            result_id INTEGER PRIMARY KEY, batch_id TEXT, item_id INTEGER,
            logical_item_id TEXT, source_valid_result_id INTEGER, input_title TEXT,
            search_title TEXT, url TEXT, product_data TEXT, matching_result TEXT,
            execution_path TEXT, created_at TEXT
        );
        CREATE TABLE failure_results (
            failure_id INTEGER PRIMARY KEY, batch_id TEXT, item_id INTEGER,
            logical_item_id TEXT, operation TEXT, fail_node TEXT, failure_kind TEXT,
            input_title TEXT, search_title TEXT, url TEXT, reasoning TEXT, detail TEXT,
            created_at TEXT
        );
        CREATE TABLE matching_decisions (
            decision_id INTEGER PRIMARY KEY, batch_id TEXT, item_id INTEGER,
            logical_item_id TEXT, attempt_no INTEGER, execution_path TEXT, url TEXT,
            verdict TEXT, decision_source TEXT, gtin_status TEXT, variant_status TEXT,
            vision_status TEXT, reasoning TEXT, decision_process TEXT, created_at TEXT
        );
        PRAGMA user_version=2;
        """
    )
    product = product_data(url="https://example.test/legacy")
    match_json = verified().model_dump_json()
    conn.execute(
        "INSERT INTO batches VALUES "
        "('root','root',NULL,0,'new_input','completed',1,NULL,?,1,1,0,'t','t',NULL)",
        (json.dumps({"concurrency": 4, "vision_enabled": True}),),
    )
    conn.execute(
        "INSERT INTO batch_items VALUES "
        "(1,'root','logical',NULL,0,'Input','uk','tesco',NULL,'[]','valid',"
        "'new_input','Search title',?,'[]','t','t')",
        (product.url,),
    )
    conn.execute(
        "INSERT INTO valid_results VALUES "
        "(10,'root',1,'logical',NULL,'Input','Search title',?,?,?,'new_input','t')",
        (product.url, product.model_dump_json(), match_json),
    )
    conn.commit()
    conn.close()

    db = OrchestratorDB(db_path)
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {
        row["name"] for row in db.conn.execute("PRAGMA table_info(valid_results)")
    } == {"result_id", "item_id", "product_data", "created_at"}
    config = json.loads(db.conn.execute("SELECT job_config FROM batches").fetchone()[0])
    assert config == {"concurrency": 4}
    decision = db.conn.execute(
        "SELECT verdict,decision_source,json_type(decision_process,'$.nodes') "
        "FROM matching_decisions"
    ).fetchone()
    assert tuple(decision) == ("match", "llm", "array")
    assert tuple(
        db.conn.execute(
            "SELECT total_items,valid_count,failure_count FROM batch_summary"
        ).fetchone()
    ) == (1, 1, 0)
    db.close()
