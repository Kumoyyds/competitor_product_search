from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.gen_storage_docs import (
    inject_generated_blocks,
    introspect_schema,
    parse_column_comments,
    render_table,
)


ROOT = Path(__file__).resolve().parents[2]


def test_column_comments_support_trailing_preceding_and_both():
    ddl = """
-- Test records.
CREATE TABLE records (
    id INTEGER PRIMARY KEY, -- trailing
    -- preceding
    name TEXT NOT NULL,
    -- first part
    value TEXT -- second part
);
"""

    comments = parse_column_comments(ddl)

    assert comments[("records", "id")] == "trailing"
    assert comments[("records", "name")] == "preceding"
    assert comments[("records", "value")] == "first part second part"


def test_missing_column_comment_is_rejected():
    ddl = """
-- Test records.
CREATE TABLE records (
    id INTEGER PRIMARY KEY
);
"""

    with pytest.raises(SystemExit):
        introspect_schema(ddl)


def test_pragma_structure_and_purposes_render_together():
    ddl = """
-- Parent records.
CREATE TABLE parents (
    id INTEGER PRIMARY KEY -- Parent identifier
);
-- Child records.
CREATE TABLE children (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- Child identifier
    parent_id INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE, -- Parent link
    code TEXT NOT NULL UNIQUE -- Unique code
);
"""
    indexes = """
-- Speeds parent lookup.
CREATE INDEX idx_children_parent ON children(parent_id);
"""

    schema = introspect_schema(ddl, indexes)
    child = next(table for table in schema.tables if table.name == "children")
    rendered = render_table(child)

    assert child.purpose == "Child records."
    assert "`parent_id` | `INTEGER` | No" in rendered
    assert "FK → parents.id ON DELETE CASCADE" in rendered
    assert "`code` | `TEXT` | No | — | UNIQUE" in rendered
    assert "Speeds parent lookup." in rendered


def test_injection_is_idempotent():
    source = (
        "before <!-- BEGIN GENERATED: search-er -->old"
        "<!-- END GENERATED: search-er --> after"
    )
    blocks = {"search-er": "\nnew\n"}

    once = inject_generated_blocks(source, blocks)
    twice = inject_generated_blocks(once, blocks)

    assert once == twice
    assert "before <!-- BEGIN" in once
    assert "<!-- END GENERATED: search-er --> after" in once


def test_real_docs_are_fresh():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/gen_storage_docs.py"),
            "--check",
            "--root",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
