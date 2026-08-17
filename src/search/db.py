from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TYPE_CHECKING

from . import config
from .trace import utc_now

if TYPE_CHECKING:
    from .trace import TaskRecorder


SCHEMA_VERSION = "2"
_DB_UNSET = object()


class SearchDB:
    """SQLite persistence for batch runs and task-level execution traces."""

    def __init__(
        self,
        path: str | None = None,
        *,
        store_candidates: bool | None = None,
        store_llm_payload: bool | None = None,
    ):
        self.path = path or config.get("db", "sqlite_path", default="search_db.sqlite")
        self.store_candidates = (
            bool(config.get("db", "store_candidates", default=True))
            if store_candidates is None
            else store_candidates
        )
        self.store_llm_payload = (
            bool(config.get("db", "store_llm_payload", default=True))
            if store_llm_payload is None
            else store_llm_payload
        )
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        # Unlike cache.py, every connection in this writer is explicitly closed:
        # sqlite3's context manager commits/rolls back but does not close it.
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        mode TEXT,
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

                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        row_index INTEGER NOT NULL,
                        product_name TEXT NOT NULL,
                        product_key TEXT NOT NULL,
                        brand_input TEXT,
                        website TEXT,
                        country TEXT,
                        status TEXT NOT NULL,
                        verdict TEXT NOT NULL,
                        failure_kind TEXT NOT NULL,
                        matched_url TEXT,
                        matched_title TEXT,
                        reason TEXT,
                        layer_trace TEXT,
                        candidates_considered INTEGER,
                        final_provider TEXT,
                        attempt_count INTEGER,
                        error_type TEXT,
                        error_message TEXT,
                        traceback TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        duration_ms INTEGER,
                        UNIQUE(run_id, row_index)
                    );

                    CREATE TABLE IF NOT EXISTS attempts (
                        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id INTEGER NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                        run_id TEXT NOT NULL,
                        attempt_no INTEGER NOT NULL,
                        provider TEXT NOT NULL,
                        verdict TEXT,
                        reason TEXT,
                        candidates_found INTEGER,
                        alive_after_domain INTEGER,
                        alive_after_base INTEGER,
                        budget_exhausted INTEGER,
                        query_variants TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        duration_ms INTEGER,
                        UNIQUE(task_id, attempt_no)
                    );

                    CREATE TABLE IF NOT EXISTS node_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                        task_id INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        node TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_kind TEXT,
                        error_message TEXT,
                        traceback TEXT,
                        detail TEXT,
                        candidates_in INTEGER,
                        candidates_out INTEGER,
                        started_at TEXT,
                        duration_ms INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS candidates (
                        candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                        task_id INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        rank INTEGER NOT NULL,
                        url TEXT,
                        title TEXT,
                        snippet TEXT,
                        host TEXT,
                        brands TEXT,
                        numerics TEXT,
                        v_domain TEXT,
                        v_brand TEXT,
                        v_numeric TEXT,
                        v_distinguishing TEXT,
                        alive INTEGER,
                        trace_depth INTEGER,
                        is_matched INTEGER,
                        llm_index INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS llm_calls (
                        call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        attempt_id INTEGER NOT NULL REFERENCES attempts(attempt_id) ON DELETE CASCADE,
                        task_id INTEGER NOT NULL,
                        run_id TEXT NOT NULL,
                        node TEXT,
                        model TEXT,
                        base_url TEXT,
                        temperature REAL,
                        timeout_s REAL,
                        prompt TEXT,
                        raw_response TEXT,
                        parsed_match_idx INTEGER,
                        parsed_reason TEXT,
                        status TEXT,
                        error_message TEXT,
                        prompt_tokens INTEGER,
                        completion_tokens INTEGER,
                        total_tokens INTEGER,
                        duration_ms INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id);
                    CREATE INDEX IF NOT EXISTS idx_tasks_verdict ON tasks(run_id, verdict);
                    CREATE INDEX IF NOT EXISTS idx_tasks_failure ON tasks(run_id, failure_kind);
                    CREATE INDEX IF NOT EXISTS idx_tasks_key ON tasks(product_key);
                    CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id);
                    CREATE INDEX IF NOT EXISTS idx_events_attempt ON node_events(attempt_id);
                    CREATE INDEX IF NOT EXISTS idx_events_err ON node_events(run_id, status, node);
                    CREATE INDEX IF NOT EXISTS idx_cand_attempt ON candidates(attempt_id);
                    CREATE INDEX IF NOT EXISTS idx_cand_url ON candidates(run_id, url);
                    CREATE INDEX IF NOT EXISTS idx_llm_attempt ON llm_calls(attempt_id);

                    CREATE VIEW IF NOT EXISTS v_errors AS
                    SELECT e.*, t.row_index, t.product_name, a.provider
                    FROM node_events e
                    JOIN tasks t ON t.task_id = e.task_id
                    JOIN attempts a ON a.attempt_id = e.attempt_id
                    WHERE e.status IN ('error', 'warning');

                    CREATE VIEW IF NOT EXISTS v_task_result AS
                    SELECT t.*, r.started_at AS run_started_at,
                           r.finished_at AS run_finished_at, r.status AS run_status,
                           r.input_file, r.output_file, r.provider_chain, r.llm_model
                    FROM tasks t JOIN runs r ON r.run_id = t.run_id;

                    CREATE VIEW IF NOT EXISTS v_funnel AS
                    SELECT run_id, node, COUNT(*) AS event_count,
                           SUM(candidates_in) AS candidates_in,
                           SUM(candidates_out) AS candidates_out,
                           AVG(duration_ms) AS avg_duration_ms
                    FROM node_events
                    WHERE status != 'warning'
                    GROUP BY run_id, node;

                    CREATE VIEW IF NOT EXISTS v_run_summary AS
                    SELECT r.*,
                           CAST((julianday(r.finished_at) - julianday(r.started_at))
                                * 86400000 AS INTEGER) AS duration_ms,
                           COALESCE((SELECT json_group_object(failure_kind, n)
                             FROM (SELECT failure_kind, COUNT(*) AS n
                                   FROM tasks t2 WHERE t2.run_id = r.run_id
                                   GROUP BY failure_kind)), '{}') AS failure_distribution
                    FROM runs r;
                    """
                )
                run_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                }
                if "mode" not in run_columns:
                    conn.execute("ALTER TABLE runs ADD COLUMN mode TEXT")
                conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (SCHEMA_VERSION,),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def start_run(self, **values: Any) -> None:
        columns = [
            "run_id", "started_at", "status", "mode", "input_file", "input_sku_col",
            "output_file", "country", "website", "provider_chain", "llm_model",
            "concurrency", "serper_max_calls", "total_tasks", "job_config",
            "pipeline_config", "git_commit",
        ]
        row = {**values, "started_at": values.get("started_at") or utc_now(), "status": "running"}
        row["job_config"] = self._json(row.get("job_config") or {})
        row["pipeline_config"] = self._json(row.get("pipeline_config") or {})
        with self._lock:
            conn = self._connect()
            try:
                placeholders = ",".join("?" for _ in columns)
                conn.execute(
                    f"INSERT INTO runs ({','.join(columns)}) VALUES ({placeholders})",
                    [row.get(column) for column in columns],
                )
                conn.commit()
            finally:
                conn.close()

    def flush_task(self, recorder: TaskRecorder) -> None:
        """Write one complete task and all child records in one short transaction."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN")
                cur = conn.execute(
                    """
                    INSERT INTO tasks (
                        run_id,row_index,product_name,product_key,brand_input,website,country,
                        status,verdict,failure_kind,matched_url,matched_title,reason,layer_trace,
                        candidates_considered,final_provider,attempt_count,error_type,error_message,
                        traceback,started_at,finished_at,duration_ms
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        recorder.run_id, recorder.row_index, recorder.product_name,
                        recorder.product_key, recorder.brand_input, recorder.website,
                        recorder.country, recorder.status, recorder.verdict,
                        recorder.failure_kind, recorder.matched_url, recorder.matched_title,
                        recorder.reason, self._json(recorder.layer_trace),
                        recorder.candidates_considered, recorder.final_provider,
                        len(recorder.attempts), recorder.error_type, recorder.error_message,
                        recorder.traceback, recorder.started_at, recorder.finished_at,
                        recorder.duration_ms,
                    ),
                )
                task_id = int(cur.lastrowid)
                for attempt in recorder.attempts:
                    attempt_cur = conn.execute(
                        """
                        INSERT INTO attempts (
                            task_id,run_id,attempt_no,provider,verdict,reason,candidates_found,
                            alive_after_domain,alive_after_base,budget_exhausted,query_variants,
                            started_at,finished_at,duration_ms
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            task_id, recorder.run_id, attempt.attempt_no, attempt.provider,
                            attempt.verdict, attempt.reason, attempt.candidates_found,
                            attempt.alive_after_domain, attempt.alive_after_base,
                            int(attempt.budget_exhausted), self._json(attempt.query_variants),
                            attempt.started_at, attempt.finished_at, attempt.duration_ms,
                        ),
                    )
                    attempt_id = int(attempt_cur.lastrowid)
                    conn.executemany(
                        """
                        INSERT INTO node_events (
                            attempt_id,task_id,run_id,seq,node,status,error_kind,error_message,
                            traceback,detail,candidates_in,candidates_out,started_at,duration_ms
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        [
                            (
                                attempt_id, task_id, recorder.run_id, event["seq"],
                                event["node"], event["status"], event["error_kind"],
                                event["error_message"], event["traceback"],
                                self._json(event["detail"]), event["candidates_in"],
                                event["candidates_out"], event["started_at"],
                                event["duration_ms"],
                            )
                            for event in attempt.node_events
                        ],
                    )
                    if self.store_candidates:
                        conn.executemany(
                            """
                            INSERT INTO candidates (
                                attempt_id,task_id,run_id,rank,url,title,snippet,host,brands,
                                numerics,v_domain,v_brand,v_numeric,v_distinguishing,alive,
                                trace_depth,is_matched,llm_index
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            [
                                (
                                    attempt_id, task_id, recorder.run_id, c["rank"], c["url"],
                                    c["title"], c["snippet"], c["host"], self._json(c["brands"]),
                                    self._json(c["numerics"]), c["v_domain"], c["v_brand"],
                                    c["v_numeric"], c["v_distinguishing"], c["alive"],
                                    c["trace_depth"], c["is_matched"], c["llm_index"],
                                )
                                for c in attempt.candidates
                            ],
                        )
                    for call in attempt.llm_calls:
                        conn.execute(
                            """
                            INSERT INTO llm_calls (
                                attempt_id,task_id,run_id,node,model,base_url,temperature,timeout_s,
                                prompt,raw_response,parsed_match_idx,parsed_reason,status,error_message,
                                prompt_tokens,completion_tokens,total_tokens,duration_ms
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                attempt_id, task_id, recorder.run_id, call.get("node"),
                                call.get("model"), call.get("base_url"), call.get("temperature"),
                                call.get("timeout_s"),
                                call.get("prompt") if self.store_llm_payload else None,
                                call.get("raw_response") if self.store_llm_payload else None,
                                call.get("parsed_match_idx"), call.get("parsed_reason"),
                                call.get("status"), call.get("error_message"),
                                call.get("prompt_tokens"), call.get("completion_tokens"),
                                call.get("total_tokens"), call.get("duration_ms"),
                            ),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        provider_calls: dict[str, int] | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._lock:
            conn = self._connect()
            try:
                counts = conn.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN verdict='match' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN verdict='no_match' THEN 1 ELSE 0 END),
                           SUM(CASE WHEN verdict='error' THEN 1 ELSE 0 END)
                    FROM tasks WHERE run_id=?
                    """,
                    (run_id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE runs SET finished_at=?, status=?, matched_count=?,
                        no_match_count=?, error_count=?, provider_calls=?, error_message=?
                    WHERE run_id=?
                    """,
                    (
                        utc_now(), status, counts[1] or 0, counts[2] or 0,
                        counts[3] or 0,
                        self._json(provider_calls) if provider_calls is not None else None,
                        error_message,
                        run_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()


_singleton: SearchDB | None = None


def get_db() -> SearchDB | None:
    global _singleton
    if not bool(config.get("db", "enabled", default=True)):
        return None
    if _singleton is None:
        _singleton = SearchDB()
    return _singleton


def git_commit() -> str | None:
    """Return the current short git commit without making tracing mandatory."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@asynccontextmanager
async def run_scope(
    *,
    mode: str,
    total_tasks: int,
    job_config: dict[str, Any],
    provider_chain: str,
    provider_calls: Callable[[], dict[str, int] | None] | None = None,
    db: SearchDB | None | object = _DB_UNSET,
    input_file: str | None = None,
    input_sku_col: str | None = None,
    output_file: str | None = None,
    country: str | None = None,
    website: str | None = None,
    concurrency: int | None = None,
    serper_max_calls: int | None = None,
) -> AsyncIterator[str]:
    """Create and finish one persisted run around an async operation.

    By default the configured singleton is resolved on entry. Passing ``db``
    lets callers resolve it once; explicitly passing ``None`` makes tracing a
    no-op while still yielding a stable run id.
    """
    if mode not in {"batch", "single"}:
        raise ValueError("mode must be 'batch' or 'single'")
    if db is _DB_UNSET:
        db = get_db()

    run_id = uuid.uuid4().hex
    if db is None:
        yield run_id
        return
    assert isinstance(db, SearchDB)

    await asyncio.to_thread(
        db.start_run,
        run_id=run_id,
        mode=mode,
        input_file=input_file,
        input_sku_col=input_sku_col,
        output_file=output_file,
        country=country,
        website=website,
        provider_chain=provider_chain,
        llm_model=config.get("llm", "model"),
        concurrency=concurrency,
        serper_max_calls=serper_max_calls,
        total_tasks=total_tasks,
        job_config=job_config,
        pipeline_config=config.load_config(),
        git_commit=git_commit(),
    )

    def calls_snapshot() -> dict[str, int] | None:
        if provider_calls is None:
            return None
        try:
            return provider_calls()
        except Exception:
            return None

    try:
        yield run_id
    except asyncio.CancelledError as exc:
        await asyncio.to_thread(
            db.finish_run,
            run_id,
            status="interrupted",
            provider_calls=calls_snapshot(),
            error_message=str(exc) or "run interrupted",
        )
        raise
    except BaseException as exc:
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        await asyncio.to_thread(
            db.finish_run,
            run_id,
            status=status,
            provider_calls=calls_snapshot(),
            error_message=str(exc),
        )
        raise
    else:
        await asyncio.to_thread(
            db.finish_run,
            run_id,
            status="completed",
            provider_calls=calls_snapshot(),
        )
