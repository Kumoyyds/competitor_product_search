"""Shrink golden buckets to the configured cap (dry run by default).

Eviction priority is stale first, then oldest automatic samples, then oldest
human-confirmed cold-start samples. Use ``--apply`` for the hard delete.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..config import get_config
from ..models.enums import PAGE_TYPES
from ..storage import ScrapeDB


@dataclass(frozen=True)
class BucketPlan:
    site: str
    page_type: str
    before: int
    after: int
    active: int
    evict: tuple[dict, ...]
    under_coverage: bool


def build_prune_plan(db: ScrapeDB, site: str | None = None) -> list[BucketPlan]:
    cfg = get_config()
    if site:
        sites = [site]
    else:
        sites = [
            row["site"]
            for row in db.conn.execute(
                "SELECT DISTINCT site FROM golden_samples ORDER BY site"
            ).fetchall()
        ]

    plans: list[BucketPlan] = []
    for site_name in sites:
        for page_type in PAGE_TYPES:
            rows = [
                dict(row)
                for row in db.conn.execute(
                    "SELECT id, site, page_type, captured_at, is_stale, created_by, "
                    "length(CAST(html_snapshot AS BLOB)) AS snapshot_bytes "
                    "FROM golden_samples WHERE site = ? AND page_type = ?",
                    (site_name, page_type),
                ).fetchall()
            ]
            active = sum(1 for row in rows if not row["is_stale"])
            minimum = cfg.golden_min_for(page_type)
            under_coverage = active < minimum
            maximum = cfg.golden_max_for(page_type)
            delete_count = 0 if under_coverage else max(0, len(rows) - maximum)
            ranked = sorted(
                rows,
                key=lambda row: (
                    0 if row["is_stale"] else 1,
                    0 if row["created_by"] == "auto" else 1,
                    row["captured_at"],
                    row["id"],
                ),
            )
            evict = tuple(ranked[:delete_count])
            plans.append(
                BucketPlan(
                    site=site_name,
                    page_type=page_type,
                    before=len(rows),
                    after=len(rows) - len(evict),
                    active=active,
                    evict=evict,
                    under_coverage=under_coverage,
                )
            )
    return plans


def apply_prune_plan(db: ScrapeDB, plans: Iterable[BucketPlan]) -> int:
    ids = [row["id"] for plan in plans for row in plan.evict]
    if not ids:
        return 0
    db.conn.executemany(
        "DELETE FROM golden_samples WHERE id = ?", [(id_,) for id_ in ids]
    )
    db.conn.commit()
    return len(ids)


def _describe_evict(row: dict) -> str:
    if row["is_stale"]:
        label = "stale"
    else:
        label = row["created_by"]
    return f"id={row['id']} ({label}, {row['captured_at'][:10]})"


def print_plan(plans: list[BucketPlan], apply: bool) -> None:
    cfg = get_config()
    mode = "APPLY" if apply else "DRY RUN — nothing deleted"
    print(f"cap = {cfg.golden_max_samples_per_page_type} per page_type   ({mode})\n")
    for plan in plans:
        prefix = f"  {plan.site}/{plan.page_type:<12} {plan.before} -> {plan.after}"
        if plan.under_coverage:
            print(f"{prefix}   WARNING: mandatory page_type has no active golden")
        elif plan.evict:
            print(
                f"{prefix}   evict "
                + ", ".join(_describe_evict(row) for row in plan.evict)
            )
        else:
            suffix = (
                "ok"
                if plan.before or not cfg.is_mandatory_page_type(plan.page_type)
                else "WARNING: mandatory page_type has no active golden"
            )
            print(f"{prefix}   {suffix}")

    count = sum(len(plan.evict) for plan in plans)
    size = sum(row["snapshot_bytes"] or 0 for plan in plans for row in plan.evict)
    verb = "deleted" if apply else "would be deleted"
    print(f"\n{count} samples {verb} (~{size / (1024 * 1024):.1f} MB).")
    if not apply and count:
        print("Re-run with --apply to execute.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Shrink golden buckets to the configured cap."
    )
    parser.add_argument("--site", help="limit pruning to one site")
    parser.add_argument("--apply", action="store_true", help="hard-delete planned samples")
    parser.add_argument("--db", type=Path, help="override configured database path")
    args = parser.parse_args(argv)

    cfg = get_config()
    db = ScrapeDB(args.db or cfg.db_path)
    db.init_db()
    try:
        plans = build_prune_plan(db, args.site)
        if args.apply:
            apply_prune_plan(db, plans)
        print_plan(plans, args.apply)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
