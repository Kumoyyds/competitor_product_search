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

from src.models import DecisionNode, InputItem, ProductData, ProductMatchResult

SCHEMA_VERSION = 3

_DDL = """
-- Top-level New Input and Rerun executions with lineage and lifecycle status.
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY, -- Human-readable identifier for one top-level execution
    root_batch_id TEXT NOT NULL REFERENCES batches(batch_id), -- Initial New Input batch shared by the whole rerun lineage
    parent_batch_id TEXT REFERENCES batches(batch_id), -- Immediately requested parent batch for a rerun
    rerun_no INTEGER NOT NULL DEFAULT 0, -- Zero for New Input; monotonic rerun suffix within a root lineage
    operation TEXT NOT NULL CHECK(operation IN ('new_input', 'rerun')), -- User operation that created the batch
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'completed_with_failures', 'failed', 'interrupted')), -- Batch lifecycle state
    vision_enabled INTEGER NOT NULL DEFAULT 0, -- Boolean integer controlling optional image comparison
    source_file TEXT, -- Original xlsx/csv/json path when New Input came from a file
    job_config TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(job_config)), -- JSON snapshot of invocation settings not represented by typed columns
    created_at TEXT NOT NULL, -- UTC ISO timestamp when the batch was allocated
    finished_at TEXT, -- UTC ISO timestamp when the batch reached a terminal state
    error_message TEXT -- Batch-level fatal error, when present
);

-- One logical product execution inside a batch, including stage progress and fallback trace.
CREATE TABLE IF NOT EXISTS batch_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate item execution identifier
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, -- Owning batch
    logical_item_id TEXT NOT NULL, -- Stable product identity copied through rerun descendants
    source_valid_result_id INTEGER REFERENCES valid_results(result_id), -- Prior qualified snapshot that supplied this rerun input
    row_index INTEGER NOT NULL, -- Zero-based source-row position retained across reruns
    input_title TEXT NOT NULL, -- Original user title, possibly blank for an invalid row
    country TEXT, -- Normalized country code or NULL for invalid input
    site_name TEXT, -- Normalized marketplace key or NULL for invalid input
    input_gtin TEXT, -- Optional user-provided GTIN preserved as text
    input_image_urls TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(input_image_urls)), -- JSON array of original image URLs
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'valid', 'failed')), -- Per-item lifecycle state
    execution_path TEXT NOT NULL CHECK(execution_path IN ('new_input', 'stored_url', 'identity_revalidation', 'fallback')), -- Current or terminal pipeline path
    search_title TEXT, -- Search-selected title; NULL unless Search succeeded
    matched_url TEXT, -- Search-selected or stored URL used for the latest stage
    stage_trace TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(stage_trace)), -- JSON array of ordered stage outcomes including non-terminal failures
    created_at TEXT NOT NULL, -- UTC ISO timestamp when this item execution was created
    updated_at TEXT NOT NULL, -- UTC ISO timestamp of the latest state transition
    UNIQUE(batch_id, row_index)
);

-- Append-only terminal qualified product snapshots available to future reruns.
CREATE TABLE IF NOT EXISTS valid_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate qualified-result identifier
    item_id INTEGER NOT NULL UNIQUE REFERENCES batch_items(item_id) ON DELETE CASCADE, -- Exactly one Valid terminal for the item
    product_data TEXT NOT NULL CHECK(json_valid(product_data)), -- Complete qualified ProductData serialized as JSON
    created_at TEXT NOT NULL -- UTC ISO timestamp when the snapshot was committed
);

-- Append-only terminal row failures and business no-match outcomes.
CREATE TABLE IF NOT EXISTS failure_results (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate terminal-failure identifier
    item_id INTEGER NOT NULL UNIQUE REFERENCES batch_items(item_id) ON DELETE CASCADE, -- Exactly one Failure terminal for the item
    fail_node TEXT NOT NULL CHECK(fail_node IN ('input', 'search', 'scraping', 'match', 'rerun')), -- Actual terminal workflow stage
    failure_kind TEXT NOT NULL, -- Structured business or technical failure category
    reasoning TEXT NOT NULL, -- Human-readable rule, model, validation, or exception explanation
    detail TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail)), -- JSON diagnostics without changing the stable column contract
    created_at TEXT NOT NULL -- UTC ISO timestamp when the terminal failure was committed
);

-- Append-only record of every Matching invocation, including rule short-circuits and skipped decisions.
CREATE TABLE IF NOT EXISTS matching_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate matching-decision identifier
    item_id INTEGER NOT NULL REFERENCES batch_items(item_id) ON DELETE CASCADE, -- Item execution the decision belongs to
    attempt_no INTEGER NOT NULL, -- One-based Matching invocation counter within one item execution
    execution_path TEXT NOT NULL CHECK(execution_path IN ('new_input', 'stored_url', 'identity_revalidation', 'fallback')), -- Pipeline path that requested the decision
    url TEXT, -- Scraped product URL the decision was made against
    verdict TEXT NOT NULL CHECK(verdict IN ('match', 'no_match', 'error')), -- Business outcome, or error when Matching failed technically
    decision_source TEXT NOT NULL CHECK(decision_source IN ('gtin', 'variant_rule', 'llm', 'identity_guard', 'technical_error')), -- Node that settled the verdict
    gtin_status TEXT CHECK(gtin_status IS NULL OR gtin_status IN ('pass', 'conflict', 'unknown')), -- EvidenceStatus of the GTIN node, or NULL when that node did not run
    variant_status TEXT CHECK(variant_status IS NULL OR variant_status IN ('pass', 'conflict', 'unknown')), -- EvidenceStatus of the brand/numeric/multipack node, or NULL when that node did not run
    vision_status TEXT CHECK(vision_status IS NULL OR vision_status IN ('not_requested', 'not_available', 'success', 'failed')), -- VisionStatus of the image-comparison node, or NULL when that node did not run
    reasoning TEXT, -- Rule sentence or LLM one-sentence reasoning explaining the verdict
    decision_process TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(decision_process)), -- JSON ordered node trace with per-node status and detail
    created_at TEXT NOT NULL, -- UTC ISO timestamp when the decision was recorded
    UNIQUE(item_id, attempt_no)
);
"""

_INDEX_DDL = """
-- Supports locating one logical product throughout a rerun lineage.
CREATE INDEX IF NOT EXISTS idx_orch_items_logical ON batch_items(logical_item_id, item_id);
-- Supports allocating and querying reruns for one root batch.
CREATE INDEX IF NOT EXISTS idx_orch_batches_root ON batches(root_batch_id, rerun_no);
-- Supports failure breakdowns by terminal stage and category.
CREATE INDEX IF NOT EXISTS idx_orch_failure_node ON failure_results(fail_node, failure_kind);
"""

_VIEW_DDL = """
-- Batch lifecycle with terminal counts derived from item state instead of cached columns.
CREATE VIEW IF NOT EXISTS batch_summary AS
SELECT b.*,
       COUNT(i.item_id) AS total_items,
       COALESCE(SUM(i.status = 'valid'), 0) AS valid_count,
       COALESCE(SUM(i.status = 'failed'), 0) AS failure_count
FROM batches AS b
LEFT JOIN batch_items AS i ON i.batch_id = b.batch_id
GROUP BY b.batch_id;

-- One row per item with its mutually exclusive Valid or Failure terminal payload.
CREATE VIEW IF NOT EXISTS item_outcomes AS
SELECT i.*,
       v.result_id,
       v.product_data,
       f.failure_id,
       f.fail_node,
       f.failure_kind,
       f.reasoning AS failure_reasoning,
       f.detail AS failure_detail,
       COALESCE(v.created_at, f.created_at) AS terminal_at
FROM batch_items AS i
LEFT JOIN valid_results AS v ON v.item_id = i.item_id
LEFT JOIN failure_results AS f ON f.item_id = i.item_id;

-- Latest qualified snapshot for every stable logical product identity.
CREATE VIEW IF NOT EXISTS latest_valid_results AS
SELECT v.result_id,
       i.item_id,
       i.batch_id,
       b.root_batch_id,
       b.operation,
       b.rerun_no,
       i.logical_item_id,
       i.input_title,
       i.country,
       i.site_name,
       i.input_gtin,
       i.input_image_urls,
       i.search_title,
       i.matched_url AS url,
       i.execution_path,
       v.product_data,
       v.created_at
FROM valid_results AS v
JOIN batch_items AS i ON i.item_id = v.item_id
JOIN batches AS b ON b.batch_id = i.batch_id
WHERE v.result_id = (
    SELECT MAX(v2.result_id)
    FROM valid_results AS v2
    JOIN batch_items AS i2 ON i2.item_id = v2.item_id
    WHERE i2.logical_item_id = i.logical_item_id
);
"""

_TRIGGER_DDL = """
CREATE TRIGGER IF NOT EXISTS trg_orch_valid_exclusive
BEFORE INSERT ON valid_results
WHEN EXISTS (SELECT 1 FROM failure_results WHERE item_id = NEW.item_id)
   OR (SELECT status FROM batch_items WHERE item_id = NEW.item_id) IN ('valid', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'item already has a terminal outcome');
END;

CREATE TRIGGER IF NOT EXISTS trg_orch_failure_exclusive
BEFORE INSERT ON failure_results
WHEN EXISTS (SELECT 1 FROM valid_results WHERE item_id = NEW.item_id)
   OR (SELECT status FROM batch_items WHERE item_id = NEW.item_id) IN ('valid', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'item already has a terminal outcome');
END;

CREATE TRIGGER IF NOT EXISTS trg_orch_item_terminal_immutable
BEFORE UPDATE ON batch_items
WHEN OLD.status IN ('valid', 'failed')
BEGIN
    SELECT RAISE(ABORT, 'terminal item is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_orch_item_terminal_requires_outcome
BEFORE UPDATE OF status ON batch_items
WHEN (NEW.status = 'valid' AND NOT EXISTS (
          SELECT 1 FROM valid_results WHERE item_id = NEW.item_id
     ))
  OR (NEW.status = 'failed' AND NOT EXISTS (
          SELECT 1 FROM failure_results WHERE item_id = NEW.item_id
     ))
BEGIN
    SELECT RAISE(ABORT, 'terminal item requires its outcome row');
END;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute complete SQL statements without sqlite3.executescript's implicit commit."""
    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        statement = "".join(pending)
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            pending = []
    if "".join(pending).strip():
        raise RuntimeError("incomplete orchestrator SQL script")


@dataclass(frozen=True, slots=True)
class RerunSource:
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
            version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"orchestrator database schema v{version} is newer than supported "
                    f"v{SCHEMA_VERSION}"
                )
            tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "batches" in tables and version < SCHEMA_VERSION:
                self._migrate_to_v3(tables)
            else:
                self.conn.executescript(_DDL)
                self.conn.executescript(_INDEX_DDL)
                self.conn.executescript(_VIEW_DDL)
                self.conn.executescript(_TRIGGER_DDL)
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.conn.commit()

    def _migrate_to_v3(self, tables: set[str]) -> None:
        """Rebuild v1/v2 tables without denormalized result columns."""
        required = {"batches", "batch_items", "valid_results", "failure_results"}
        missing = required - tables
        if missing:
            raise RuntimeError(
                "cannot migrate incomplete orchestrator schema; missing: "
                + ", ".join(sorted(missing))
            )

        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for view in ("batch_summary", "item_outcomes", "latest_valid_results"):
                self.conn.execute(f'DROP VIEW IF EXISTS "{view}"')
            for table in required:
                self.conn.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_legacy"')
            has_decisions = "matching_decisions" in tables
            if has_decisions:
                self.conn.execute(
                    'ALTER TABLE "matching_decisions" RENAME TO "matching_decisions_legacy"'
                )

            _execute_script(self.conn, _DDL)
            self.conn.execute(
                "INSERT INTO batches "
                "(batch_id,root_batch_id,parent_batch_id,rerun_no,operation,status,"
                "vision_enabled,source_file,job_config,created_at,finished_at,error_message) "
                "SELECT batch_id,root_batch_id,parent_batch_id,rerun_no,operation,status,"
                "vision_enabled,source_file,"
                "CASE WHEN json_valid(job_config) THEN json_remove(job_config,'$.vision_enabled') "
                "ELSE '{}' END,created_at,finished_at,error_message FROM batches_legacy"
            )
            self.conn.execute(
                "INSERT INTO batch_items "
                "(item_id,batch_id,logical_item_id,source_valid_result_id,row_index,input_title,"
                "country,site_name,input_gtin,input_image_urls,status,execution_path,search_title,"
                "matched_url,stage_trace,created_at,updated_at) "
                "SELECT i.item_id,i.batch_id,i.logical_item_id,"
                "COALESCE((SELECT v.result_id FROM valid_results_legacy AS v "
                "          WHERE v.item_id=i.source_item_id),"
                "         (SELECT v.source_valid_result_id FROM valid_results_legacy AS v "
                "          WHERE v.item_id=i.item_id)),"
                "i.row_index,i.input_title,i.country,i.site_name,i.input_gtin,"
                "CASE WHEN json_valid(i.input_image_urls) THEN i.input_image_urls ELSE '[]' END,"
                "i.status,CASE WHEN i.execution_path='revalidated' "
                "THEN 'identity_revalidation' ELSE i.execution_path END,"
                "i.search_title,i.matched_url,"
                "CASE WHEN json_valid(i.stage_trace) THEN i.stage_trace ELSE '[]' END,"
                "i.created_at,i.updated_at FROM batch_items_legacy AS i"
            )
            self.conn.execute(
                "INSERT INTO valid_results (result_id,item_id,product_data,created_at) "
                "SELECT result_id,item_id,product_data,created_at FROM valid_results_legacy"
            )
            self.conn.execute(
                "INSERT INTO failure_results "
                "(failure_id,item_id,fail_node,failure_kind,reasoning,detail,created_at) "
                "SELECT failure_id,item_id,fail_node,failure_kind,reasoning,"
                "CASE WHEN json_valid(detail) THEN detail ELSE '{}' END,created_at "
                "FROM failure_results_legacy"
            )
            if has_decisions:
                self.conn.execute(
                    "INSERT INTO matching_decisions "
                    "(decision_id,item_id,attempt_no,execution_path,url,verdict,decision_source,"
                    "gtin_status,variant_status,vision_status,reasoning,decision_process,created_at) "
                    "SELECT decision_id,item_id,attempt_no,"
                    "CASE WHEN execution_path='revalidated' THEN 'identity_revalidation' "
                    "ELSE execution_path END,url,verdict,decision_source,gtin_status,variant_status,"
                    "vision_status,reasoning,CASE WHEN json_valid(decision_process) "
                    "THEN decision_process ELSE '{}' END,created_at "
                    "FROM matching_decisions_legacy"
                )
            self._backfill_legacy_decisions()

            if has_decisions:
                self.conn.execute("DROP TABLE matching_decisions_legacy")
            self.conn.execute("DROP TABLE failure_results_legacy")
            self.conn.execute("DROP TABLE valid_results_legacy")
            self.conn.execute("DROP TABLE batch_items_legacy")
            self.conn.execute("DROP TABLE batches_legacy")
            _execute_script(self.conn, _INDEX_DDL)
            _execute_script(self.conn, _VIEW_DDL)
            _execute_script(self.conn, _TRIGGER_DDL)
            violations = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"orchestrator v3 migration produced foreign-key violations: {violations}"
                )
            inconsistent = self.conn.execute(
                "SELECT COUNT(*) FROM item_outcomes "
                "WHERE (status='valid' AND (result_id IS NULL OR failure_id IS NOT NULL)) "
                "OR (status='failed' AND (failure_id IS NULL OR result_id IS NOT NULL)) "
                "OR (status IN ('pending','running') "
                "    AND (result_id IS NOT NULL OR failure_id IS NOT NULL))"
            ).fetchone()[0]
            if inconsistent:
                raise RuntimeError(
                    f"orchestrator v3 migration found {inconsistent} inconsistent terminal item(s)"
                )
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _backfill_legacy_decisions(self) -> None:
        """Recover v1 decision history from the terminal JSON payloads where possible."""
        self.conn.execute(
            "INSERT INTO matching_decisions "
            "(item_id,attempt_no,execution_path,url,verdict,decision_source,gtin_status,"
            "variant_status,vision_status,reasoning,decision_process,created_at) "
            "SELECT v.item_id,1,"
            "CASE WHEN v.execution_path='revalidated' THEN 'identity_revalidation' "
            "ELSE v.execution_path END,v.url,"
            "json_extract(v.matching_result,'$.verdict'),"
            "json_extract(v.matching_result,'$.decision_source'),"
            "json_extract(v.matching_result,'$.gtin_status'),"
            "json_extract(v.matching_result,'$.variant_status'),"
            "json_extract(v.matching_result,'$.vision_status'),"
            "json_extract(v.matching_result,'$.reasoning'),"
            "json_object('nodes',json(COALESCE(json_extract(v.matching_result,'$.trace'),'[]')) ,"
            "            'terminated_at',json_extract(v.matching_result,'$.decision_source')) ,"
            "v.created_at FROM valid_results_legacy AS v "
            "WHERE v.matching_result IS NOT NULL AND json_valid(v.matching_result) "
            "AND json_extract(v.matching_result,'$.verdict') IN ('match','no_match') "
            "AND NOT EXISTS (SELECT 1 FROM matching_decisions AS d WHERE d.item_id=v.item_id)"
        )
        self.conn.execute(
            "INSERT INTO matching_decisions "
            "(item_id,attempt_no,execution_path,url,verdict,decision_source,gtin_status,"
            "variant_status,vision_status,reasoning,decision_process,created_at) "
            "SELECT f.item_id,1,i.execution_path,f.url,"
            "json_extract(f.detail,'$.verdict'),json_extract(f.detail,'$.decision_source'),"
            "json_extract(f.detail,'$.gtin_status'),json_extract(f.detail,'$.variant_status'),"
            "json_extract(f.detail,'$.vision_status'),json_extract(f.detail,'$.reasoning'),"
            "json_object('nodes',json(COALESCE(json_extract(f.detail,'$.trace'),'[]')) ,"
            "            'terminated_at',json_extract(f.detail,'$.decision_source')) ,"
            "f.created_at FROM failure_results_legacy AS f "
            "JOIN batch_items AS i ON i.item_id=f.item_id "
            "WHERE json_valid(f.detail) "
            "AND json_extract(f.detail,'$.verdict') IN ('match','no_match') "
            "AND NOT EXISTS (SELECT 1 FROM matching_decisions AS d WHERE d.item_id=f.item_id)"
        )
        self.conn.execute(
            "INSERT INTO matching_decisions "
            "(item_id,attempt_no,execution_path,url,verdict,decision_source,reasoning,"
            "decision_process,created_at) "
            "SELECT v.item_id,1,'stored_url',v.url,'match','identity_guard',"
            "'Stored-URL rescrape reproduced the prior qualified identity; Matching was skipped.',"
            "json_object('nodes',json_array(json_object('node','identity_guard','status','reused',"
            "'terminal',json('true'),'detail',json_object('source_valid_result_id',"
            "i.source_valid_result_id))),'terminated_at','identity_guard'),v.created_at "
            "FROM valid_results_legacy AS v JOIN batch_items AS i ON i.item_id=v.item_id "
            "WHERE v.matching_result IS NULL AND i.execution_path='stored_url' "
            "AND NOT EXISTS (SELECT 1 FROM matching_decisions AS d WHERE d.item_id=v.item_id)"
        )

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
            stored_config = {
                key: value
                for key, value in job_config.items()
                if key != "vision_enabled"
            }
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
                    json.dumps(stored_config, ensure_ascii=False, sort_keys=True),
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
                stored_config = {
                    key: value for key, value in job_config.items() if key != "vision_enabled"
                }
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
                        json.dumps(stored_config, ensure_ascii=False, sort_keys=True),
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
        source_valid_result_id: int | None = None,
        execution_path: str = "new_input",
    ) -> int:
        now = utc_now()
        title = item.title if item else str(raw.get("title") or "").strip()
        country = (
            item.country
            if item
            else _optional_text(raw.get("country") or raw.get("region"))
        )
        site = item.site_name if item else _optional_text(raw.get("site_name"))
        gtin = item.gtin if item else _optional_text(raw.get("gtin"))
        images = item.image_urls if item else []
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO batch_items "
                "(batch_id,logical_item_id,source_valid_result_id,row_index,input_title,country,"
                "site_name,input_gtin,input_image_urls,status,execution_path,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    batch_id,
                    logical_item_id or uuid.uuid4().hex,
                    source_valid_result_id,
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
                "SELECT status,stage_trace FROM batch_items WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"item not found: {item_id}")
            if row["status"] in {"valid", "failed"}:
                raise RuntimeError(f"item {item_id} is already terminal")
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
        execution_path: str,
    ) -> int:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self._assert_item_nonterminal(item_id)
                cur = self.conn.execute(
                    "INSERT INTO valid_results (item_id,product_data,created_at) VALUES (?,?,?)",
                    (
                        item_id,
                        product.model_dump_json(),
                        utc_now(),
                    ),
                )
                self.conn.execute(
                    "UPDATE batch_items SET status='valid',execution_path=?,search_title=?,"
                    "matched_url=?,updated_at=? WHERE item_id=?",
                    (execution_path, search_title, product.url, utc_now(), item_id),
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
                self._assert_item_nonterminal(item_id)
                cur = self.conn.execute(
                    "INSERT INTO failure_results "
                    "(item_id,fail_node,failure_kind,reasoning,detail,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        item_id,
                        fail_node,
                        failure_kind,
                        reasoning,
                        json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
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

    def record_matching_decision(
        self,
        item_id: int,
        *,
        execution_path: str,
        url: str | None,
        verdict: str,
        decision_source: str,
        reasoning: str | None,
        gtin_status: str | None = None,
        variant_status: str | None = None,
        vision_status: str | None = None,
        decision_process: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                exists = self.conn.execute(
                    "SELECT 1 FROM batch_items WHERE item_id = ?", (item_id,)
                ).fetchone()
                if exists is None:
                    raise KeyError(f"item not found: {item_id}")
                attempt_no = self.conn.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 "
                    "FROM matching_decisions WHERE item_id = ?",
                    (item_id,),
                ).fetchone()[0]
                cur = self.conn.execute(
                    "INSERT INTO matching_decisions "
                    "(item_id,attempt_no,execution_path,url,verdict,"
                    "decision_source,gtin_status,variant_status,vision_status,reasoning,"
                    "decision_process,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        attempt_no,
                        execution_path,
                        url,
                        verdict,
                        decision_source,
                        gtin_status,
                        variant_status,
                        vision_status,
                        reasoning,
                        json.dumps(
                            decision_process or {}, ensure_ascii=False, sort_keys=True
                        ),
                        utc_now(),
                    ),
                )
                self.conn.commit()
                return int(cur.lastrowid)
            except BaseException:
                self.conn.rollback()
                raise

    def record_matching_result(
        self,
        item_id: int,
        *,
        execution_path: str,
        url: str | None,
        result: ProductMatchResult,
    ) -> int:
        nodes = [node.model_dump(mode="json") for node in result.trace]
        node_names = {node.node for node in result.trace}
        terminal = next(
            (node.node.value for node in reversed(result.trace) if node.terminal),
            result.decision_source.value,
        )
        return self.record_matching_decision(
            item_id,
            execution_path=execution_path,
            url=url,
            verdict=result.verdict.value,
            decision_source=result.decision_source.value,
            reasoning=result.reasoning,
            gtin_status=(
                result.gtin_status.value if DecisionNode.GTIN in node_names else None
            ),
            variant_status=(
                result.variant_status.value
                if DecisionNode.VARIANT_RULE in node_names
                else None
            ),
            vision_status=(
                result.vision_status.value if DecisionNode.VISION in node_names else None
            ),
            decision_process={"nodes": nodes, "terminated_at": terminal},
        )

    def _assert_item_nonterminal(self, item_id: int) -> None:
        item = self.conn.execute(
            "SELECT * FROM batch_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if item is None:
            raise KeyError(f"item not found: {item_id}")
        if item["status"] in {"valid", "failed"}:
            raise RuntimeError(f"item {item_id} is already terminal")

    def finish_batch(
        self,
        batch_id: str,
        *,
        error_message: str | None = None,
        interrupted: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            valid = self.conn.execute(
                "SELECT COUNT(*) FROM valid_results AS v "
                "JOIN batch_items AS i ON i.item_id=v.item_id WHERE i.batch_id = ?",
                (batch_id,),
            ).fetchone()[0]
            failed = self.conn.execute(
                "SELECT COUNT(*) FROM failure_results AS f "
                "JOIN batch_items AS i ON i.item_id=f.item_id WHERE i.batch_id = ?",
                (batch_id,),
            ).fetchone()[0]
            total = self.conn.execute(
                "SELECT COUNT(*) FROM batch_items WHERE batch_id = ?", (batch_id,)
            ).fetchone()[0]
            if interrupted:
                status = "interrupted"
            elif error_message is not None:
                status = "failed"
            elif valid + failed < total:
                status = "failed"
                error_message = f"{total - valid - failed} item(s) did not finish"
            elif failed:
                status = "completed_with_failures"
            else:
                status = "completed"
            self.conn.execute(
                "UPDATE batches SET status=?,finished_at=?,error_message=? WHERE batch_id=?",
                (status, utc_now(), error_message, batch_id),
            )
            self.conn.commit()
        return {
            "batch_id": batch_id,
            "status": status,
            "total": total,
            "valid": valid,
            "failed": failed,
        }

    def rerun_sources(
        self, batch_id: str, search_titles: Sequence[str] | None = None
    ) -> list[RerunSource]:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"batch not found: {batch_id}")
        selected = self.conn.execute(
            "SELECT DISTINCT logical_item_id,row_index,input_title,country,site_name,"
            "input_gtin,input_image_urls FROM batch_items WHERE batch_id=? ORDER BY row_index",
            (batch_id,),
        ).fetchall()
        sources: list[RerunSource] = []
        for selected_row in selected:
            latest = self.conn.execute(
                "SELECT v.result_id,v.product_data,i.item_id,i.input_title,i.country,i.site_name,"
                "i.input_gtin,i.input_image_urls,i.row_index,i.search_title,i.matched_url AS url "
                "FROM valid_results v "
                "JOIN batch_items i ON i.item_id=v.item_id "
                "JOIN batches b ON b.batch_id=i.batch_id "
                "WHERE b.root_batch_id=? AND i.logical_item_id=? "
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
            available = {
                _title_key(source.search_title): source
                for source in sources
                if source.search_title
            }
            missing = [original for key, original in requested.items() if key not in available]
            if missing:
                raise ValueError(f"search_title not found: {', '.join(missing)}")
            sources = [
                source
                for source in sources
                if source.search_title and _title_key(source.search_title) in requested
            ]
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
