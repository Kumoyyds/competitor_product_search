from __future__ import annotations

import json
import sqlite3
from typing import Any


def _decode(row: sqlite3.Row, *columns: str) -> dict[str, Any]:
    result = dict(row)
    for column in columns:
        if result.get(column) is not None:
            result[column] = json.loads(result[column])
    return result


def list_batches(
    conn: sqlite3.Connection,
    *,
    limit: int,
    offset: int,
    operation: str | None = None,
    status: str | None = None,
    root_batch_id: str | None = None,
) -> dict[str, Any]:
    filters: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("operation", operation),
        ("status", status),
        ("root_batch_id", root_batch_id),
    ):
        if value is not None:
            filters.append(f"{column} = ?")
            values.append(value)
    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM batch_summary{where}", values
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM batch_summary{where} ORDER BY created_at DESC, batch_id DESC LIMIT ? OFFSET ?",
        [*values, limit, offset],
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_decode(row, "job_config") for row in rows],
    }


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM batch_summary WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        return None
    result = _decode(row, "job_config")
    counts = conn.execute(
        "SELECT status, COUNT(*) AS count FROM batch_items WHERE batch_id = ? "
        "AND status IN ('pending', 'running') GROUP BY status",
        (batch_id,),
    ).fetchall()
    result.update({"pending_count": 0, "running_count": 0})
    for count in counts:
        result[f"{count['status']}_count"] = count["count"]
    result["finished_count"] = result["valid_count"] + result["failure_count"]
    return result


def list_items(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    limit: int,
    offset: int,
    status: str | None = None,
) -> dict[str, Any]:
    where = "batch_id = ?"
    values: list[Any] = [batch_id]
    if status is not None:
        where += " AND status = ?"
        values.append(status)
    total = conn.execute(
        f"SELECT COUNT(*) FROM item_outcomes WHERE {where}", values
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM item_outcomes WHERE {where} "
        "ORDER BY row_index, item_id LIMIT ? OFFSET ?",
        [*values, limit, offset],
    ).fetchall()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            _decode(row, "input_image_urls", "product_data", "stage_trace", "failure_detail")
            for row in rows
        ],
    }
