from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.models import (
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
    )


def _valid_root(db_path, *, count: int = 1, title: str = "Same search title") -> str:
    db = OrchestratorDB(db_path)
    root = db.create_new_batch(vision_enabled=False, source_file=None, job_config={})
    for index in range(count):
        item = InputItem(title=f"Input {index}", country="uk", site_name="tesco")
        item_id = db.add_item(root, row_index=index, raw=item.model_dump(), item=item)
        product = product_data(title=f"Product {index}", url=f"https://example.test/{index}")
        db.record_valid(
            item_id,
            product,
            search_title=title,
            url=product.url,
            execution_path="new_input",
            matching_result=verified(),
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
        url=product.url,
        execution_path="new_input",
        matching_result=verified(),
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
            source_item_id=original.source_item_id,
            execution_path="stored_url",
        )
        latest = product_data(title="Latest", url="https://example.test/latest")
        db.record_valid(
            item_id,
            latest,
            search_title="Latest search title",
            url=latest.url,
            execution_path="stored_url",
            matching_result=None,
            source_valid_result_id=original.valid_result_id,
        )
        db.finish_batch(child)

        selected = db.rerun_sources(root)[0]
        assert selected.url == "https://example.test/latest"
        assert selected.search_title == "Latest search title"
    finally:
        db.close()
