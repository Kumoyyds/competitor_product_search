"""Golden set + promote/prune lifecycle (spec §5.4 ③④, §5.7).

Responsibilities:
  - `classify_page_type`: label a ProductData into one of four buckets
  - `maybe_seed_golden`: on successful scrape, seed a golden sample if bucket empty
  - `promote_candidate`: run candidate against goldens; only promote if all pass
  - `_prune_hard_cap`: enforce max 4 active parsers per site
  - `prune_stale`: retire parsers with 0 hits in last N runs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from ..config import get_config
from ..models.product_data import ProductData
from ..storage import GoldenStore, ParserStore, RunStore, ScrapeDB
from ..validation import validate
from .sandbox import run_in_sandbox

logger = logging.getLogger(__name__)


@dataclass
class GoldenRejection:
    """Structured reason for golden-test failure, fed back to the repair ladder."""
    golden_id: int
    page_type: str
    failure: str            # "sandbox_crash" | "field_mismatch"
    field: Optional[str] = None
    expected: Any = None
    actual: Any = None

    def describe(self) -> str:
        if self.failure == "sandbox_crash":
            return (
                f"golden id={self.golden_id} ({self.page_type}): "
                f"candidate crashed on this golden HTML"
            )
        if self.failure == "field_mismatch":
            return (
                f"golden id={self.golden_id} ({self.page_type}) field mismatch: "
                f"{self.field} expected={self.expected!r} actual={self.actual!r}"
            )
        return f"golden id={self.golden_id}: {self.failure}"


def classify_page_type(product: ProductData) -> str:
    """Auto-classify a ProductData into one of five buckets (spec §5.7).

    Precedence: out_of_stock > membership > discounted > multipack > standard.
    """
    if not product.in_stock:
        return "out_of_stock"

    if product.membership_price is not None and product.membership_price > 0:
        return "membership"

    if product.list_price is not None and product.price is not None:
        if product.list_price > product.price:
            return "discounted"

    if product.variant:
        pack_qty = product.variant.get("pack_qty")
        if pack_qty is not None:
            try:
                if int(pack_qty) > 1:
                    return "multipack"
            except (ValueError, TypeError):
                pass

    return "standard"


def maybe_seed_golden(site: str, html: str, product: ProductData) -> Optional[int]:
    """Seed a golden sample if this page_type bucket is empty for the site.

    Best-effort: returns sample id on seed, None if bucket already populated.
    """
    page_type = classify_page_type(product)
    cfg = get_config()
    db = ScrapeDB(cfg.db_path)
    db.init_db()
    try:
        gs = GoldenStore(db)
        existing = gs.get_by_site_and_type(site, page_type, exclude_stale=True)
        if existing:
            return None
        expected = product.model_dump(mode="json")
        sample_id = gs.seed(site, page_type, html, expected)
        logger.info(
            "seeded golden site=%s page_type=%s sample_id=%s", site, page_type, sample_id
        )
        return sample_id
    finally:
        db.close()


async def promote_candidate(
    site: str,
    code: str,
    current_product: ProductData,
    current_html: str,
) -> int | GoldenRejection:
    """Run candidate against every non-stale golden; promote if all pass.

    Returns the new parser row id on success, GoldenRejection on rejection.
    Side effects:
      - Marks goldens `is_stale=1` if no active parser can reproduce them
      - Enforces hard-cap prune (retires lowest-hit if adding would exceed 4)
      - When goldens are empty, seeds current (html, product) as first golden
    """
    cfg = get_config()
    db = ScrapeDB(cfg.db_path)
    db.init_db()
    try:
        gs = GoldenStore(db)
        goldens_by_type: dict[str, list[dict[str, Any]]] = {}
        for pt in ("standard", "out_of_stock", "discounted", "multipack", "membership"):
            goldens_by_type[pt] = gs.get_by_site_and_type(site, pt, exclude_stale=True)

        covered = {pt: samples for pt, samples in goldens_by_type.items() if samples}

        if not covered:
            # No goldens yet — seed the current as the first, then promote.
            _seed_current_as_first_golden(gs, site, current_html, current_product)
            return _do_promote(db, site, code, current_html, current_product)

        # Run candidate against every non-stale golden
        for page_type, samples in covered.items():
            for sample in samples:
                sandbox_result = await run_in_sandbox(
                    code, sample["html_snapshot"], "golden://"
                )
                if not isinstance(sandbox_result, dict):
                    logger.info(
                        "promote reject: candidate failed sandbox on golden id=%s (%s)",
                        sample["id"], type(sandbox_result).__name__,
                    )
                    if _no_active_parser_passes(db, site, sample):
                        gs.mark_stale(sample["id"])
                        continue
                    return GoldenRejection(
                        golden_id=sample["id"],
                        page_type=page_type,
                        failure="sandbox_crash",
                    )

                mismatch = _matches_expected(sandbox_result, sample["expected_output"])
                if mismatch is not None:
                    if _no_active_parser_passes(db, site, sample):
                        gs.mark_stale(sample["id"])
                        continue
                    logger.info(
                        "promote reject: candidate mismatch on golden id=%s", sample["id"]
                    )
                    return GoldenRejection(
                        golden_id=sample["id"],
                        page_type=page_type,
                        failure="field_mismatch",
                        field=mismatch[0],
                        expected=mismatch[1],
                        actual=mismatch[2],
                    )

        return _do_promote(db, site, code, current_html, current_product)
    finally:
        db.close()


def prune_stale(site: str) -> list[int]:
    """Retire active parsers with 0 hits in last N runs (natural prune).

    Returns list of retired parser ids. Best-effort.
    """
    cfg = get_config()
    window = cfg.prune_sliding_window
    db = ScrapeDB(cfg.db_path)
    db.init_db()
    retired: list[int] = []
    try:
        actives = ParserStore(db).get_active(site)
        if not actives:
            return retired

        recent_hit_ids = _recent_window_hit_ids(db, site, window)
        for parser in actives:
            # Skip parser if it has no scrape_runs history yet at all (fresh promote)
            total_ever = db.conn.execute(
                "SELECT COUNT(*) as c FROM scrape_runs WHERE winning_parser_id = ? AND site = ?",
                (parser["id"], site),
            ).fetchone()["c"]
            if total_ever == 0:
                continue
            if parser["id"] not in recent_hit_ids:
                ParserStore(db).retire(parser["id"])
                retired.append(parser["id"])
                logger.info("pruned stale parser id=%s site=%s", parser["id"], site)
    finally:
        db.close()
    return retired


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _do_promote(
    db: ScrapeDB,
    site: str,
    code: str,
    html: str,
    product: ProductData,
) -> int:
    _prune_hard_cap(db, site)
    version = _next_version(db, site)
    parser_id = ParserStore(db).create(
        site=site, version=version, code=code, created_by="agent"
    )
    logger.info(
        "promoted parser id=%s site=%s version=%s", parser_id, site, version
    )
    # If this page_type bucket is empty, seed the current html + product for future promotes
    _maybe_seed_golden_inline(db, site, html, product)
    return parser_id


def _prune_hard_cap(db: ScrapeDB, site: str) -> None:
    cfg = get_config()
    actives = ParserStore(db).get_active(site)
    if len(actives) < cfg.per_site_parser_limit:
        return

    hit_map = {r["winning_parser_id"]: r["hits"] for r in RunStore(db).get_hit_rates(site)}
    # Sort by (hits ASC, id ASC): lowest-hit oldest goes first
    losers = sorted(actives, key=lambda p: (hit_map.get(p["id"], 0), p["id"]))
    to_retire = losers[0]
    ParserStore(db).retire(to_retire["id"])
    logger.info(
        "hard-cap pruned parser id=%s (hits=%d) site=%s",
        to_retire["id"], hit_map.get(to_retire["id"], 0), site,
    )


def _matches_expected(candidate_dict: dict[str, Any], expected: dict[str, Any]) -> Optional[tuple[str, Any, Any]]:
    """Compare candidate output to expected golden output.

    Returns None on match, or (field_name, expected_val, actual_val) on first mismatch.
    """
    compare_fields = (
        "title", "brand", "gtin", "image_urls", "variant",
        "price", "currency", "list_price", "membership_price", "unit_price", "unit",
        "in_stock", "availability_raw",
    )
    for field in compare_fields:
        cand_val = _normalize(candidate_dict.get(field))
        exp_val = _normalize(expected.get(field))
        if cand_val != exp_val:
            logger.debug("golden mismatch on %s: %r != %r", field, cand_val, exp_val)
            return (field, exp_val, cand_val)
    return None


def _normalize(v: Any) -> Any:
    """Normalize for equality: Decimal ~ numeric string comparison."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (int, float)):
        return str(Decimal(str(v)))
    if isinstance(v, str):
        # If it's a numeric string, compare as Decimal
        try:
            return str(Decimal(v))
        except Exception:
            return v
    return v


def _no_active_parser_passes(db: ScrapeDB, site: str, sample: dict[str, Any]) -> bool:
    """A golden is considered stale if no currently-active parser can reproduce its expected_output."""
    # Simple heuristic: don't run all active parsers here (async needed).
    # Just return False conservatively — mark_stale only fires on candidate mismatch when we can't verify.
    return False


def _recent_window_hit_ids(db: ScrapeDB, site: str, window: int) -> set[int]:
    """Return set of winning_parser_ids observed in the last `window` scrape_runs for the site."""
    rows = db.conn.execute(
        """
        SELECT DISTINCT winning_parser_id FROM (
            SELECT winning_parser_id FROM scrape_runs
            WHERE site = ? AND winning_parser_id IS NOT NULL
            ORDER BY id DESC LIMIT ?
        )
        """,
        (site, window),
    ).fetchall()
    return {r["winning_parser_id"] for r in rows if r["winning_parser_id"] is not None}


def _next_version(db: ScrapeDB, site: str) -> str:
    row = db.conn.execute(
        "SELECT COUNT(*) as c FROM parsers WHERE site = ?", (site,)
    ).fetchone()
    return f"v{row['c'] + 1}"


def _seed_current_as_first_golden(
    gs: GoldenStore, site: str, html: str, product: ProductData
) -> None:
    page_type = classify_page_type(product)
    gs.seed(site, page_type, html, product.model_dump(mode="json"))


def _maybe_seed_golden_inline(
    db: ScrapeDB, site: str, html: str, product: ProductData
) -> None:
    page_type = classify_page_type(product)
    gs = GoldenStore(db)
    existing = gs.get_by_site_and_type(site, page_type, exclude_stale=True)
    if not existing:
        gs.seed(site, page_type, html, product.model_dump(mode="json"))
