from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from src.models import InputItem, ProductData, ProductMatchResult

SCHEMA_VERSION = 1

_DDL = """
-- Top-level New Input and Rerun executions with lineage and aggregate status.
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, -- Human-readable identifier for one top-level execution
    root_batch_id TEXT NOT NULL, -- Initial New Input batch shared by the whole rerun lineage
    parent_batch_id TEXT REFERENCES batches(batch_id), -- Immediately requested parent batch for a rerun
    rerun_no INTEGER NOT NULL DEFAULT 0, -- Zero for New Input; monotonic rerun suffix within a root lineage
    operation TEXT NOT NULL CHECK(operation IN ('new_input', 'rerun')), -- User operation that created the batch
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'completed_with_failures', 'failed', 'interrupted')), -- Batch lifecycle state
    vision_enabled INTEGER NOT NULL DEFAULT 0, -- Boolean integer controlling optional image comparison
    source_file TEXT, -- Original xlsx/csv/json path when New Input came from a file
    job_config TEXT NOT NULL DEFAULT '{}', -- JSON snapshot of invocation settings
    total_items INTEGER NOT NULL DEFAULT 0, -- Number of rows/items registered in this batch
    valid_count INTEGER NOT NULL DEFAULT 0, -- Terminal Valid item count
    failure_count INTEGER NOT NULL DEFAULT 0, -- Terminal Failure item count
    created_at TEXT NOT NULL, -- UTC ISO timestamp when the batch was allocated
    finished_at TEXT, -- UTC ISO timestamp when the batch reached a terminal state
    error_message TEXT -- Batch-level fatal error, when present
);

-- One logical product execution inside a batch, including stage progress and fallback trace.
CREATE TABLE IF NOT EXISTS batch_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate item execution identifier
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, -- Owning batch
    logical_item_id TEXT NOT NULL, -- Stable product identity copied through rerun descendants
    source_item_id INTEGER REFERENCES batch_items(item_id), -- Prior item execution that supplied this rerun input
    row_index INTEGER NOT NULL, -- Zero-based source-row position retained across reruns
    input_title TEXT NOT NULL, -- Original user title, possibly blank for an invalid row
    country TEXT, -- Normalized country code or NULL for invalid input
    site_name TEXT, -- Normalized marketplace key or NULL for invalid input
    input_gtin TEXT, -- Optional user-provided GTIN preserved as text
    input_image_urls TEXT NOT NULL DEFAULT '[]', -- JSON array of original image URLs
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'valid', 'failed')), -- Per-item lifecycle state
    execution_path TEXT NOT NULL, -- new_input, stored_url, identity_revalidation, or fallback
    search_title TEXT, -- Search-selected title; NULL unless Search succeeded
    matched_url TEXT, -- Search-selected or stored URL used for the latest stage
    stage_trace TEXT NOT NULL DEFAULT '[]', -- JSON array of ordered stage outcomes including non-terminal failures
    created_at TEXT NOT NULL, -- UTC ISO timestamp when this item execution was created
    updated_at TEXT NOT NULL, -- UTC ISO timestamp of the latest state transition
    UNIQUE(batch_id, row_index)
);

-- Append-only terminal qualified product snapshots available to future reruns.
CREATE TABLE IF NOT EXISTS valid_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate qualified-result identifier
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, -- Denormalized owning batch for direct queries
    item_id INTEGER NOT NULL UNIQUE REFERENCES batch_items(item_id) ON DELETE CASCADE, -- Exactly one Valid terminal for the item
    logical_item_id TEXT NOT NULL, -- Stable product identity used for latest-result lineage lookup
    source_valid_result_id INTEGER REFERENCES valid_results(result_id), -- Prior Valid snapshot used by a rerun
    input_title TEXT NOT NULL, -- Original user title copied for self-contained result queries
    search_title TEXT, -- Search-selected title, or inherited title when no new Search ran
    url TEXT NOT NULL, -- Validated product URL stored for future reruns
    product_data TEXT NOT NULL, -- Complete qualified ProductData serialized as JSON
    matching_result TEXT, -- ProductMatchResult JSON; NULL when unchanged identity safely reused prior validation
    execution_path TEXT NOT NULL, -- Path that produced the result: new_input, stored_url, revalidated, or fallback
    created_at TEXT NOT NULL -- UTC ISO timestamp when the snapshot was committed
);

-- Append-only terminal row failures and business no-match outcomes.
CREATE TABLE IF NOT EXISTS failure_results (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate terminal-failure identifier
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, -- Denormalized owning batch for direct queries
    item_id INTEGER NOT NULL UNIQUE REFERENCES batch_items(item_id) ON DELETE CASCADE, -- Exactly one Failure terminal for the item
    logical_item_id TEXT NOT NULL, -- Stable product identity retained for audit
    operation TEXT NOT NULL CHECK(operation IN ('new_input', 'rerun')), -- User operation active when the terminal failure occurred
    fail_node TEXT NOT NULL CHECK(fail_node IN ('input', 'search', 'scraping', 'match', 'rerun')), -- Actual terminal workflow stage
    failure_kind TEXT NOT NULL, -- Structured business or technical failure category
    input_title TEXT NOT NULL, -- Original user title copied for self-contained failure queries
    search_title TEXT, -- Search-selected title when Search succeeded before the failure
    url TEXT, -- Candidate or stored URL involved in the terminal failure
    reasoning TEXT NOT NULL, -- Human-readable rule, model, validation, or exception explanation
    detail TEXT NOT NULL DEFAULT '{}', -- JSON diagnostics without changing the stable column contract
    created_at TEXT NOT NULL -- UTC ISO timestamp when the terminal failure was committed
);
"""

_INDEX_DDL = """
-- Supports loading all item executions for a batch in source order.
CREATE INDEX IF NOT EXISTS idx_orch_items_batch ON batch_items(batch_id, row_index);
-- Supports locating one logical product throughout a rerun lineage.
CREATE INDEX IF NOT EXISTS idx_orch_items_logical ON batch_items(logical_item_id, item_id);
-- Supports allocating and querying reruns for one root batch.
CREATE INDEX IF NOT EXISTS idx_orch_batches_root ON batches(root_batch_id, rerun_no);
-- Supports latest Valid snapshot lookup for a logical product.
CREATE INDEX IF NOT EXISTS idx_orch_valid_logical ON valid_results(logical_item_id, result_id);
-- Supports batch result retrieval without joining through items.
CREATE INDEX IF NOT EXISTS idx_orch_valid_batch ON valid_results(batch_id, result_id);
-- Supports failure reporting by operation stage.
CREATE INDEX IF NOT EXISTS idx_orch_failure_node ON failure_results(batch_id, fail_node);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RerunSource:
    selected_item_id: int
    source_item_id: int
    logical_item_id: str
    row_index: int
    item: InputItem
    valid_result_id: int
    search_title: str | None
    url: str
    product: ProductData


class OrchestratorDB:
    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = Path(path or os.getenv("ORCHESTRATOR_DB_PATH", "orchestrator.db"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_db()

    def init_db(self) -> None:
        with self._lock:
            self.conn.executescript(_DDL)
            self.conn.executescript(_INDEX_DDL)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_new_batch(
        self,
        *,
        vision_enabled: bool,
        source_file: str | None,
        job_config: dict[str, Any],
    ) -> str:
        batch_id = f"b-{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock:
            self.conn.execute(
                "INSERT INTO batches "
                "(batch_id,root_batch_id,parent_batch_id,rerun_no,operation,status,"
                "vision_enabled,source_file,job_config,created_at) "
                "VALUES (?,?,NULL,0,'new_input','running',?,?,?,?)",
                (
                    batch_id,
                    batch_id,
                    int(vision_enabled),
                    source_file,
                    json.dumps(job_config, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            self.conn.commit()
        return batch_id

    def create_rerun_batch(
        self,
        parent_batch_id: str,
        *,
        vision_enabled: bool,
        job_config: dict[str, Any],
    ) -> str:
        now = utc_now()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                parent = self.conn.execute(
                    "SELECT root_batch_id FROM batches WHERE batch_id = ?",
                    (parent_batch_id,),
                ).fetchone()
                if parent is None:
                    raise KeyError(f"batch not found: {parent_batch_id}")
                root = parent["root_batch_id"]
                number = self.conn.execute(
                    "SELECT COALESCE(MAX(rerun_no), 0) + 1 FROM batches WHERE root_batch_id = ?",
                    (root,),
                ).fetchone()[0]
                batch_id = f"{root}-r{number}"
                self.conn.execute(
                    "INSERT INTO batches "
                    "(batch_id,root_batch_id,parent_batch_id,rerun_no,operation,status,"
                    "vision_enabled,job_config,created_at) "
                    "VALUES (?,?,?,?,'rerun','running',?,?,?)",
                    (
                        batch_id,
                        root,
                        parent_batch_id,
                        number,
                        int(vision_enabled),
                        json.dumps(job_config, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                self.conn.commit()
                return batch_id
            except BaseException:
                self.conn.rollback()
                raise

    def add_item(
        self,
        batch_id: str,
        *,
        row_index: int,
        raw: dict[str, Any],
        item: InputItem | None,
        logical_item_id: str | None = None,
        source_item_id: int | None = None,
        execution_path: str = "new_input",
    ) -> int:
        now = utc_now()
        title = item.title if item else str(raw.get("title") or "").strip()
        country = item.country if item else _optional_text(raw.get("country") or raw.get("region"))
        site = item.site_name if item else _optional_text(raw.get("site_name"))
        gtin = item.gtin if item else _optional_text(raw.get("gtin"))
        images = item.image_urls if item else []
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO batch_items "
                "(batch_id,logical_item_id,source_item_id,row_index,input_title,country,"
                "site_name,input_gtin,input_image_urls,status,execution_path,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    batch_id,
                    logical_item_id or uuid.uuid4().hex,
                    source_item_id,
                    row_index,
                    title,
                    country,
                    site,
                    gtin,
                    json.dumps(images, ensure_ascii=False),
                    execution_path,
                    now,
                    now,
                ),
            )
            self.conn.execute(
                "UPDATE batches SET total_items = total_items + 1 WHERE batch_id = ?",
                (batch_id,),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        execution_path: str | None = None,
        search_title: str | None = None,
        matched_url: str | None = None,
        trace_event: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT stage_trace FROM batch_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"item not found: {item_id}")
            trace = json.loads(row["stage_trace"])
            if trace_event is not None:
                trace.append({"at": utc_now(), **trace_event})
            assignments = ["stage_trace = ?", "updated_at = ?"]
            values: list[Any] = [json.dumps(trace, ensure_ascii=False), utc_now()]
            for column, value in (
                ("status", status),
                ("execution_path", execution_path),
                ("search_title", search_title),
                ("matched_url", matched_url),
            ):
                if value is not None:
                    assignments.append(f"{column} = ?")
                    values.append(value)
            values.append(item_id)
            self.conn.execute(
                f"UPDATE batch_items SET {', '.join(assignments)} WHERE item_id = ?",
                values,
            )
            self.conn.commit()

    def record_valid(
        self,
        item_id: int,
        product: ProductData,
        *,
        search_title: str | None,
        url: str,
        execution_path: str,
        matching_result: ProductMatchResult | None,
        source_valid_result_id: int | None = None,
    ) -> int:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                item = self._terminal_item(item_id)
                cur = self.conn.execute(
                    "INSERT INTO valid_results "
                    "(batch_id,item_id,logical_item_id,source_valid_result_id,input_title,"
                    "search_title,url,product_data,matching_result,execution_path,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["batch_id"],
                        item_id,
                        item["logical_item_id"],
                        source_valid_result_id,
                        item["input_title"],
                        search_title,
                        url,
                        product.model_dump_json(),
                        matching_result.model_dump_json() if matching_result else None,
                        execution_path,
                        utc_now(),
                    ),
                )
                self.conn.execute(
                    "UPDATE batch_items SET status='valid',execution_path=?,search_title=?,"
                    "matched_url=?,updated_at=? WHERE item_id=?",
                    (execution_path, search_title, url, utc_now(), item_id),
                )
                self.conn.commit()
                return int(cur.lastrowid)
            except BaseException:
                self.conn.rollback()
                raise

    def record_failure(
        self,
        item_id: int,
        *,
        fail_node: str,
        failure_kind: str,
        reasoning: str,
        search_title: str | None = None,
        url: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                item = self._terminal_item(item_id)
                operation = self.conn.execute(
                    "SELECT operation FROM batches WHERE batch_id = ?", (item["batch_id"],)
                ).fetchone()[0]
                cur = self.conn.execute(
                    "INSERT INTO failure_results "
                    "(batch_id,item_id,logical_item_id,operation,fail_node,failure_kind,"
                    "input_title,search_title,url,reasoning,detail,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["batch_id"], item_id, item["logical_item_id"], operation,
                        fail_node, failure_kind, item["input_title"], search_title, url,
                        reasoning, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                        utc_now(),
                    ),
                )
                self.conn.execute(
                    "UPDATE batch_items SET status='failed',search_title=?,matched_url=?,updated_at=? "
                    "WHERE item_id=?",
                    (search_title, url, utc_now(), item_id),
                )
                self.conn.commit()
                return int(cur.lastrowid)
            except BaseException:
                self.conn.rollback()
                raise

    def _terminal_item(self, item_id: int) -> sqlite3.Row:
        item = self.conn.execute(
            "SELECT * FROM batch_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise KeyError(f"item not found: {item_id}")
        if item["status"] in {"valid", "failed"}:
            raise RuntimeError(f"item {item_id} is already terminal")
        return item

    def finish_batch(self, batch_id: str, *, error_message: str | None = None) -> dict[str, Any]:
        with self._lock:
            valid = self.conn.execute(
                "SELECT COUNT(*) FROM valid_results WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            failed = self.conn.execute(
                "SELECT COUNT(*) FROM failure_results WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            total = self.conn.execute(
                "SELECT total_items FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            if error_message:
                status = "failed"
            elif failed:
                status = "completed_with_failures"
            else:
                status = "completed"
            self.conn.execute(
                "UPDATE batches SET status=?,valid_count=?,failure_count=?,finished_at=?,"
                "error_message=? WHERE batch_id=?",
                (status, valid, failed, utc_now(), error_message, batch_id),
            )
            self.conn.commit()
        return {"batch_id": batch_id, "status": status, "total": total, "valid": valid, "failed": failed}

    def rerun_sources(
        self, batch_id: str, search_titles: Sequence[str] | None = None
    ) -> list[RerunSource]:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"batch not found: {batch_id}")
        selected = self.conn.execute(
            "SELECT DISTINCT logical_item_id,item_id,row_index,input_title,country,site_name,"
            "input_gtin,input_image_urls FROM batch_items WHERE batch_id=? ORDER BY row_index",
            (batch_id,),
        ).fetchall()
        sources: list[RerunSource] = []
        for selected_row in selected:
            latest = self.conn.execute(
                "SELECT v.*,i.item_id AS source_item_id,i.country,i.site_name,i.input_gtin,"
                "i.input_image_urls,i.row_index FROM valid_results v "
                "JOIN batch_items i ON i.item_id=v.item_id "
                "JOIN batches b ON b.batch_id=v.batch_id "
                "WHERE b.root_batch_id=? AND v.logical_item_id=? "
                "ORDER BY v.result_id DESC LIMIT 1",
                (batch["root_batch_id"], selected_row["logical_item_id"]),
            ).fetchone()
            if latest is None:
                continue
            item = InputItem(
                title=latest["input_title"],
                country=latest["country"],
                site_name=latest["site_name"],
                gtin=latest["input_gtin"],
                image_urls=json.loads(latest["input_image_urls"]),
            )
            sources.append(
                RerunSource(
                    selected_item_id=selected_row["item_id"],
                    source_item_id=latest["source_item_id"],
                    logical_item_id=selected_row["logical_item_id"],
                    row_index=selected_row["row_index"],
                    item=item,
                    valid_result_id=latest["result_id"],
                    search_title=latest["search_title"],
                    url=latest["url"],
                    product=ProductData.model_validate_json(latest["product_data"]),
                )
            )
        if search_titles is not None:
            requested = {_title_key(title): title for title in search_titles}
            available = {_title_key(source.search_title): source for source in sources if source.search_title}
            missing = [original for key, original in requested.items() if key not in available]
            if missing:
                raise ValueError(f"search_title not found: {', '.join(missing)}")
            sources = [source for source in sources if source.search_title and _title_key(source.search_title) in requested]
        if not sources:
            raise ValueError(f"batch {batch_id} has no valid products to rerun")
        return sources


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _title_key(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()
