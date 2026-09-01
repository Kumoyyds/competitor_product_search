#!/usr/bin/env python3
"""Generate storage-schema reference documents from the real SQLite DDL."""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


TAG = "gen-storage-docs"
MARKER_RE = re.compile(
    r"<!-- BEGIN GENERATED: (?P<id>[a-z0-9-]+) -->"
    r"(?P<body>.*?)"
    r"<!-- END GENERATED: (?P=id) -->",
    re.DOTALL,
)
KNOWN_BLOCKS = {
    "scraping-tables",
    "scraping-er",
    "scraping-migrations",
    "search-tables",
    "search-er",
    "search-migrations",
    "orchestrator-tables",
    "orchestrator-er",
    "orchestrator-migrations",
}
_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?(?P<kind>TABLE|VIEW|INDEX)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE,
)
_COLUMN_RE = re.compile(r"^\s*[`\"\[]?(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
_NON_COLUMN = {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}


def fail(message: str) -> None:
    print(f"[{TAG}] {message}", file=sys.stderr)
    raise SystemExit(1)


def warn(message: str) -> None:
    print(f"[{TAG}] warning: {message}", file=sys.stderr)


def repo_root(arg_root: Optional[str]) -> Path:
    if arg_root:
        return Path(arg_root).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail("not inside a git repository")
    return Path(result.stdout.strip()).resolve()


def run_git(root: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=root).returncode


def stage(root: Path, paths: Sequence[Path]) -> None:
    relative = [str(path.relative_to(root)) for path in paths]
    if run_git(root, "add", "--", *relative) != 0:
        fail(f"git add failed for {relative}")


def _literal_assignment(path: Path, name: str) -> Any:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        fail(f"cannot parse {path}: {exc}")

    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            try:
                return ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError) as exc:
                fail(f"{path}: {name} must be a literal value: {exc}")
    fail(f"{path}: missing module-level assignment {name}")


def _statements(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines(keepends=True):
        current.append(line)
        candidate = "".join(current)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            current = []
    if "".join(current).strip():
        fail("DDL contains an incomplete SQL statement")
    return statements


def object_purposes(script: str) -> dict[tuple[str, str], str]:
    """Return comments immediately preceding each CREATE object."""
    purposes: dict[tuple[str, str], str] = {}
    pending: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            pending.append(stripped[2:].strip())
            continue
        match = _CREATE_RE.match(line)
        if match:
            key = (match.group("kind").lower(), match.group("name"))
            purposes[key] = " ".join(pending).strip()
            pending = []
            continue
        if stripped:
            pending = []
    return purposes


def _table_body(statement: str) -> str:
    start = statement.find("(")
    end = statement.rfind(")")
    if start < 0 or end <= start:
        fail("cannot locate CREATE TABLE column list")
    return statement[start + 1 : end]


def parse_column_comments(script: str) -> dict[tuple[str, str], str]:
    """Parse preceding and trailing ``--`` comments for table columns."""
    comments: dict[tuple[str, str], str] = {}
    for statement in _statements(script):
        match = _CREATE_RE.search(statement)
        if not match or match.group("kind").lower() != "table":
            continue
        table = match.group("name")
        pending: list[str] = []
        for line in _table_body(statement).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("--"):
                pending.append(stripped[2:].strip())
                continue
            sql_part, marker, trailing = line.partition("--")
            column_match = _COLUMN_RE.match(sql_part)
            if not column_match:
                pending = []
                continue
            column = column_match.group("name")
            if column.upper() in _NON_COLUMN:
                pending = []
                continue
            pieces = [*pending]
            if marker and trailing.strip():
                pieces.append(trailing.strip())
            comments[(table, column)] = " ".join(pieces).strip()
            pending = []
    return comments


def _strip_line_comments(sql: str) -> str:
    return "\n".join(line.partition("--")[0] for line in sql.splitlines())


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(body):
        char = body[i]
        if quote:
            if char == quote:
                if i + 1 < len(body) and body[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:i].strip())
            start = i + 1
        i += 1
    tail = body[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def parse_column_definitions(script: str) -> dict[tuple[str, str], str]:
    definitions: dict[tuple[str, str], str] = {}
    for statement in _statements(script):
        match = _CREATE_RE.search(statement)
        if not match or match.group("kind").lower() != "table":
            continue
        table = match.group("name")
        body = _strip_line_comments(_table_body(statement))
        for item in _split_top_level(body):
            column_match = _COLUMN_RE.match(item)
            if not column_match:
                continue
            column = column_match.group("name")
            if column.upper() in _NON_COLUMN:
                continue
            definitions[(table, column)] = " ".join(item.split())
    return definitions


@dataclass(frozen=True)
class ForeignKey:
    column: str
    target_table: str
    target_column: str
    on_delete: str


@dataclass(frozen=True)
class Index:
    name: str
    columns: tuple[str, ...]
    unique: bool
    origin: str
    purpose: str


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None
    pk: bool
    definition: str
    meaning: str


@dataclass(frozen=True)
class Table:
    name: str
    purpose: str
    columns: tuple[Column, ...]
    indexes: tuple[Index, ...]
    foreign_keys: tuple[ForeignKey, ...]


@dataclass(frozen=True)
class View:
    name: str
    purpose: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class Schema:
    tables: tuple[Table, ...]
    views: tuple[View, ...]


def introspect_schema(table_ddl: str, index_ddl: str = "", view_ddl: str = "") -> Schema:
    scripts = (table_ddl, index_ddl, view_ddl)
    purposes: dict[tuple[str, str], str] = {}
    for script in scripts:
        purposes.update(object_purposes(script))
    comments = parse_column_comments(table_ddl)
    definitions = parse_column_definitions(table_ddl)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for script in scripts:
            if script.strip():
                conn.executescript(script)

        table_names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
            )
        ]
        tables: list[Table] = []
        errors: list[str] = []
        for table_name in table_names:
            table_purpose = purposes.get(("table", table_name), "")
            if not table_purpose:
                errors.append(f"table {table_name} has no purpose comment")
            columns: list[Column] = []
            for row in conn.execute(f'PRAGMA table_info("{table_name}")'):
                key = (table_name, row["name"])
                meaning = comments.get(key, "")
                if not meaning:
                    errors.append(f"{table_name}.{row['name']} has no -- meaning comment")
                columns.append(
                    Column(
                        name=row["name"],
                        type=row["type"] or "ANY",
                        nullable=not bool(row["notnull"] or row["pk"]),
                        default=row["dflt_value"],
                        pk=bool(row["pk"]),
                        definition=definitions.get(key, ""),
                        meaning=meaning,
                    )
                )

            fks = tuple(
                ForeignKey(
                    column=row["from"],
                    target_table=row["table"],
                    target_column=row["to"],
                    on_delete=row["on_delete"],
                )
                for row in conn.execute(f'PRAGMA foreign_key_list("{table_name}")')
            )
            indexes: list[Index] = []
            for row in conn.execute(f'PRAGMA index_list("{table_name}")'):
                name = row["name"]
                origin = row["origin"]
                if origin == "pk":
                    continue
                index_purpose = purposes.get(("index", name), "")
                if origin == "c" and not index_purpose:
                    errors.append(f"index {name} has no purpose comment")
                if origin != "c":
                    index_purpose = index_purpose or "SQLite auto-index for a declared UNIQUE constraint."
                index_columns = tuple(
                    item["name"]
                    for item in conn.execute(f'PRAGMA index_info("{name}")')
                )
                indexes.append(
                    Index(name, index_columns, bool(row["unique"]), origin, index_purpose)
                )
            tables.append(
                Table(
                    table_name,
                    table_purpose,
                    tuple(columns),
                    tuple(indexes),
                    fks,
                )
            )

        views: list[View] = []
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY rowid"
        ):
            name = row["name"]
            purpose = purposes.get(("view", name), "")
            if not purpose:
                errors.append(f"view {name} has no purpose comment")
            columns = tuple(
                item["name"] for item in conn.execute(f'PRAGMA table_info("{name}")')
            )
            views.append(View(name, purpose, columns))
        if errors:
            fail("\n".join(errors))
        return Schema(tuple(tables), tuple(views))
    except sqlite3.Error as exc:
        fail(f"SQLite rejected extracted DDL: {exc}")
    finally:
        conn.close()


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _constraint_text(column: Column, table: Table) -> str:
    parts: list[str] = []
    upper = column.definition.upper()
    if column.pk:
        parts.append("PK")
    if "AUTOINCREMENT" in upper:
        parts.append("AUTOINCREMENT")
    for index in table.indexes:
        if not index.unique or column.name not in index.columns:
            continue
        if len(index.columns) == 1:
            parts.append("UNIQUE")
        else:
            parts.append("UNIQUE(" + ", ".join(index.columns) + ")")
    check = re.search(r"\bCHECK\s*(\(.+\))", column.definition, re.IGNORECASE)
    if check:
        parts.append("CHECK" + check.group(1))
    for fk in table.foreign_keys:
        if fk.column == column.name:
            action = f" ON DELETE {fk.on_delete}" if fk.on_delete != "NO ACTION" else ""
            parts.append(f"FK → {fk.target_table}.{fk.target_column}{action}")
    return "; ".join(dict.fromkeys(parts)) or "—"


def render_table(table: Table) -> str:
    lines = [
        f"### `{table.name}`",
        "",
        table.purpose,
        "",
        "| Column | Type | Nullable | Default | Key / constraints | Meaning |",
        "|---|---|---|---|---|---|",
    ]
    for column in table.columns:
        default = "—" if column.default is None else f"`{_cell(column.default)}`"
        lines.append(
            f"| `{column.name}` | `{column.type}` | "
            f"{'Yes' if column.nullable else 'No'} | {default} | "
            f"{_cell(_constraint_text(column, table))} | {_cell(column.meaning)} |"
        )
    if table.indexes:
        lines.extend(
            [
                "",
                "Indexes:",
                "",
                "| Name | Columns | Unique | Purpose |",
                "|---|---|---|---|",
            ]
        )
        for index in table.indexes:
            columns = ", ".join(f"`{column}`" for column in index.columns)
            lines.append(
                f"| `{index.name}` | {columns} | {'Yes' if index.unique else 'No'} | "
                f"{_cell(index.purpose)} |"
            )
    if table.foreign_keys:
        lines.extend(
            [
                "",
                "Declared foreign keys:",
                "",
                "| Column | References | On delete |",
                "|---|---|---|",
            ]
        )
        for fk in table.foreign_keys:
            lines.append(
                f"| `{fk.column}` | `{fk.target_table}.{fk.target_column}` | `{fk.on_delete}` |"
            )
    return "\n".join(lines)


def render_tables(schema: Schema) -> str:
    sections = [render_table(table) for table in schema.tables]
    if schema.views:
        lines = [
            "## Views",
            "",
            "View columns are derived from their query and therefore do not require per-column DDL comments.",
            "",
            "| View | Columns | Purpose |",
            "|---|---|---|",
        ]
        for view in schema.views:
            columns = ", ".join(f"`{column}`" for column in view.columns)
            lines.append(f"| `{view.name}` | {columns} | {_cell(view.purpose)} |")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def render_er(schema: Schema) -> str:
    lines = ["```mermaid", "erDiagram"]
    for table in schema.tables:
        pk_columns = [column for column in table.columns if column.pk]
        representative = pk_columns[0] if pk_columns else table.columns[0]
        lines.extend(
            [
                f"    {table.name} {{",
                f"        {representative.type.lower()} {representative.name}",
                "    }",
            ]
        )
    for table in schema.tables:
        nullable = {column.name: column.nullable for column in table.columns}
        for fk in table.foreign_keys:
            parent_cardinality = "|o" if nullable.get(fk.column, True) else "||"
            lines.append(
                f'    {fk.target_table} {parent_cardinality}--o{{ {table.name} : "{fk.column}"'
            )
    lines.append("```")
    return "\n".join(lines)


def _validate_added_columns(schema: Schema, added: Any, source: Path) -> None:
    if not isinstance(added, dict):
        fail(f"{source}: _ADDED_COLUMNS must be a mapping")
    real = {table.name: {column.name for column in table.columns} for table in schema.tables}
    for table, columns in added.items():
        if table not in real or not isinstance(columns, dict):
            fail(f"{source}: invalid _ADDED_COLUMNS table {table!r}")
        missing = set(columns) - real[table]
        if missing:
            fail(
                f"{source}: _ADDED_COLUMNS entries absent from _DDL: "
                + ", ".join(f"{table}.{column}" for column in sorted(missing))
            )


def render_scraping_migrations(
    added: Mapping[str, Mapping[str, str]],
    renamed: Mapping[str, Mapping[str, str]],
    removed: Mapping[str, Sequence[str]],
) -> str:
    lines = [
        "`ScrapeDB.init_db()` creates the current schema, then `_ensure_columns()` applies these idempotent compatibility migrations inside `BEGIN IMMEDIATE`:",
        "",
        "| Operation | Table | Column(s) | Definition / result |",
        "|---|---|---|---|",
    ]
    for table, columns in added.items():
        for column, ddl in columns.items():
            lines.append(f"| Add if missing | `{table}` | `{column}` | `{_cell(ddl)}` |")
    for table, columns in renamed.items():
        for old, new in columns.items():
            lines.append(f"| Rename / merge | `{table}` | `{old}` → `{new}` | Preserve the new value, otherwise copy the old value |")
    for table, columns in removed.items():
        for column in columns:
            lines.append(f"| Drop if present | `{table}` | `{column}` | Removed from the current schema |")
    lines.extend(
        [
            "",
            "Existing rows are not backfilled for nullable correlation fields (`results.run_id`, `scrape_runs.escalation_id`, `signature`, or `error`) because their exact historical relationships cannot be reconstructed safely.",
        ]
    )
    return "\n".join(lines)


def render_search_migrations(version: Any) -> str:
    return "\n".join(
        [
            f"Current `SCHEMA_VERSION`: `{version}`.",
            "",
            "Each initialization probes `PRAGMA table_info(runs)` and adds `runs.mode TEXT` when opening a pre-v2 database. It then upserts `meta['schema_version']` to the current version. All other objects use idempotent `CREATE ... IF NOT EXISTS`; there is no general search migration framework yet.",
        ]
    )


def render_orchestrator_migrations(version: Any) -> str:
    return "\n".join(
        [
            f"Current `SCHEMA_VERSION`: `{version}`.",
            "",
            "This is the initial orchestrator schema. Initialization uses idempotent CREATE statements and sets SQLite `user_version`; there are no legacy orchestrator databases to migrate.",
        ]
    )


def build_blocks(root: Path) -> dict[str, str]:
    scraping_path = root / "src/scraping/storage/database.py"
    scraping_ddl = _literal_assignment(scraping_path, "_DDL")
    scraping_indexes = _literal_assignment(scraping_path, "_INDEX_DDL")
    scraping_schema = introspect_schema(scraping_ddl, scraping_indexes)
    added = _literal_assignment(scraping_path, "_ADDED_COLUMNS")
    renamed = _literal_assignment(scraping_path, "_RENAMED_COLUMNS")
    removed = _literal_assignment(scraping_path, "_REMOVED_COLUMNS")
    _validate_added_columns(scraping_schema, added, scraping_path)

    search_path = root / "src/search/db.py"
    search_ddl = _literal_assignment(search_path, "_DDL")
    search_indexes = _literal_assignment(search_path, "_INDEX_DDL")
    search_views = _literal_assignment(search_path, "_VIEW_DDL")
    search_schema = introspect_schema(search_ddl, search_indexes, search_views)
    version = _literal_assignment(search_path, "SCHEMA_VERSION")

    orchestrator_path = root / "src/orchestrator/database.py"
    orchestrator_ddl = _literal_assignment(orchestrator_path, "_DDL")
    orchestrator_indexes = _literal_assignment(orchestrator_path, "_INDEX_DDL")
    orchestrator_schema = introspect_schema(orchestrator_ddl, orchestrator_indexes)
    orchestrator_version = _literal_assignment(orchestrator_path, "SCHEMA_VERSION")

    return {
        "scraping-tables": "\n\n" + render_tables(scraping_schema) + "\n\n",
        "scraping-er": "\n\n" + render_er(scraping_schema) + "\n\n",
        "scraping-migrations": "\n\n" + render_scraping_migrations(added, renamed, removed) + "\n\n",
        "search-tables": "\n\n" + render_tables(search_schema) + "\n\n",
        "search-er": "\n\n" + render_er(search_schema) + "\n\n",
        "search-migrations": "\n\n" + render_search_migrations(version) + "\n\n",
        "orchestrator-tables": "\n\n" + render_tables(orchestrator_schema) + "\n\n",
        "orchestrator-er": "\n\n" + render_er(orchestrator_schema) + "\n\n",
        "orchestrator-migrations": "\n\n" + render_orchestrator_migrations(orchestrator_version) + "\n\n",
    }


def inject_generated_blocks(
    text: str, blocks: Mapping[str, str], source: str = "document"
) -> str:
    matches = list(MARKER_RE.finditer(text))
    unknown = sorted({match.group("id") for match in matches} - set(blocks))
    if unknown:
        fail(f"{source}: unknown generated block id(s): {', '.join(unknown)}")
    duplicates = sorted(
        block_id
        for block_id in {match.group("id") for match in matches}
        if sum(match.group("id") == block_id for match in matches) > 1
    )
    if duplicates:
        fail(f"{source}: duplicate generated block id(s): {', '.join(duplicates)}")

    def replace(match: re.Match[str]) -> str:
        block_id = match.group("id")
        return (
            f"<!-- BEGIN GENERATED: {block_id} -->"
            f"{blocks[block_id]}"
            f"<!-- END GENERATED: {block_id} -->"
        )

    return MARKER_RE.sub(replace, text)


def update_docs(root: Path, blocks: Mapping[str, str], check: bool, pre_commit: bool) -> int:
    targets = [
        root / "docs/scraping_storage.md",
        root / "docs/search_storage.md",
        root / "docs/orchestrator_storage.md",
    ]
    changed: list[Path] = []
    replacements: dict[Path, str] = {}
    seen_ids: set[str] = set()
    for path in targets:
        if not path.exists():
            fail(f"missing documentation target {path}")
        original = path.read_text(encoding="utf-8")
        seen_ids.update(match.group("id") for match in MARKER_RE.finditer(original))
        rendered = inject_generated_blocks(original, blocks, str(path.relative_to(root)))
        if rendered != original:
            changed.append(path)
            replacements[path] = rendered

    missing = sorted(KNOWN_BLOCKS - seen_ids)
    if missing:
        fail(f"generated block id(s) absent from storage docs: {', '.join(missing)}")
    if check:
        if changed:
            for path in changed:
                print(f"[{TAG}] stale: {path.relative_to(root)}", file=sys.stderr)
            return 1
        print(f"[{TAG}] OK: generated storage references are current")
        return 0

    for path in changed:
        path.write_text(replacements[path], encoding="utf-8")
        print(f"[{TAG}] rewrote {path.relative_to(root)}")
    if pre_commit and changed:
        stage(root, changed)
        print(f"[{TAG}] staged {len(changed)} generated documentation file(s)")
    if not changed:
        print(f"[{TAG}] no documentation changes")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="git repo root (default: git rev-parse)")
    parser.add_argument(
        "--pre-commit",
        action="store_true",
        help="rewrite and stage changed storage reference files",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when generated storage references are stale; write nothing",
    )
    args = parser.parse_args()
    if args.pre_commit and args.check:
        fail("--pre-commit and --check are mutually exclusive")
    root = repo_root(args.root)
    raise SystemExit(update_docs(root, build_blocks(root), args.check, args.pre_commit))


if __name__ == "__main__":
    main()
