"""Verification script for M12 -- End-to-end live scraping with real BrightData + DeepSeek.

Reads URLs from src/scraping/data/tesco_test.xlsx.xlsx, runs the full Router.scrape()
pipeline for each, and logs detailed per-URL scraping status including which
mechanisms triggered.

Run from repo root:
    python -m src.scraping.tests.verify_m12

Requires: BRIGHT_DATA_KEY in .env (refuses to run without it).
Optional: DEEPSEEK_KEY in .env (HTML scrapers will escalate without repair; API scrapers still work).

Cost: real BrightData API calls for all 22 URLs + potentially DeepSeek calls for repair.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import textwrap
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO

# ====================================================================
#  SETUP: temp DB + config  (MUST run at module level before any
#  scraping import, to avoid polluting production DB)
# ====================================================================
_DB_PATH = os.path.join(tempfile.gettempdir(), "verify_m12.db")
if os.path.exists(_DB_PATH):
    try:
        os.remove(_DB_PATH)
    except OSError:
        pass
os.environ["SCRAPING_DB_PATH"] = _DB_PATH

from dotenv import load_dotenv

load_dotenv()

# Compatibility: accept BRIGHT_UNLOCKER_KEY as an alias for BRIGHT_DATA_KEY
# (the .env in this repo uses BRIGHT_UNLOCKER_KEY, but scraping/config.py reads BRIGHT_DATA_KEY).
# This must happen BEFORE the first get_config() call — otherwise the singleton
# caches an empty key and downstream HTTP requests fail with "Illegal header value b'Bearer '".
if not os.environ.get("BRIGHT_DATA_KEY") and not os.environ.get("SCRAPING_BRIGHT_DATA_KEY"):
    _fallback = os.environ.get("BRIGHT_UNLOCKER_KEY", "")
    if _fallback:
        os.environ["BRIGHT_DATA_KEY"] = _fallback

# Force config reload so it picks up the temp DB path
import src.scraping.config as _cfg_mod

_cfg_mod._config = None
_cfg = _cfg_mod.get_config()

_DATA_DIR = Path(__file__).parent.parent / "data"
INPUT_XLSX = _DATA_DIR / "tesco_argos.xlsx"
OUTPUT_LOG = Path(__file__).parent / "verify_m12_v3_output.log"

HAS_BRIGHT_DATA = bool(_cfg.bright_data_key)
HAS_LLM = bool(_cfg.deepseek_key)

# Max in-flight URLs. BrightData Web Unlocker + DCA tolerate parallel calls;
# the bottleneck is per-URL latency (~60-100s each), so 4-way parallelism
# roughly quarters wall-clock time. Bump higher if you need faster and
# BrightData is happy.
CONCURRENCY = 4

# ---- Trackers (matching verify_mN pattern) ----
PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[str] = []


# ====================================================================
#  Utilities
# ====================================================================


def check(name: str, cond: bool, detail: str = "") -> None:
    """Record a pass/fail check. Matching verify_mN convention."""
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def skip(name: str, reason: str) -> None:
    """Record a skipped check."""
    SKIPPED.append(name)
    print(f"  [SKIP] {name}  ({reason})")


def section(title: str) -> None:
    """Print a section banner."""
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


class TeeWriter:
    """Writes to stdout AND a log file simultaneously."""

    def __init__(self, file_path: Path) -> None:
        self._file: TextIO = open(str(file_path), "w", encoding="utf-8")
        self._stdout: TextIO = sys.stdout

    def write(self, s: str) -> None:
        self._stdout.write(s)
        self._file.write(s)
        # Flush both streams so a `Get-Content -Wait` tail sees progress live
        self._stdout.flush()
        self._file.flush()

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def close(self) -> None:
        self._file.close()


_tee: Optional[TeeWriter] = None


def _print(*args, **kwargs) -> None:
    """Print to both stdout and log file."""
    if _tee is not None:
        kwargs["file"] = _tee
    print(*args, **kwargs)


# ====================================================================
#  Data structures
# ====================================================================


@dataclass
class PerURLReport:
    """Everything we learned about one URL's scrape."""

    # Input
    idx: int  # 1-based position in input file
    label: str
    url: str
    host: str

    # Resolution
    site: str = ""

    # Timing
    start_time: str = ""
    latency_ms: int = 0

    # Outcome
    outcome: str = ""  # success | invalid_target | scrape_failed | infra_error | unhandled_exception
    result_type: str = ""  # ProductData | InvalidTargetResult | ScrapeFailed | BrightDataInfraError | ...

    # DB path (from scrape_runs table)
    db_path: str = ""

    # Success fields
    title: str = ""
    price: str = ""  # e.g. "12.99 GBP"
    currency: str = ""
    brand: str = ""
    in_stock: str = ""
    image_count: int = 0
    gtin: str = ""
    parser_version: str = ""
    source_type: str = ""
    # Additional ProductData fields (all Optional in the schema; only displayed when populated)
    list_price: str = ""        # e.g. "80.99 GBP"
    unit_price: str = ""        # e.g. "3.50"
    unit: str = ""              # e.g. "kg"
    availability_raw: str = ""  # e.g. "In stock"
    variant: str = ""           # JSON-encoded dict, e.g. '{"size":"12x330ml"}'

    # Invalid-target fields
    invalid_reason: str = ""

    # Failure fields
    failed_stage: str = ""
    failed_scraper: str = ""
    errors: list[str] = field(default_factory=list)

    # Mechanisms triggered
    mechanisms: list[str] = field(default_factory=list)

    # Escalation
    escalation_reason: str = ""
    escalation_signature: str = ""

    # DB details
    attempts: int = 0
    model_used: str = ""


# ====================================================================
#  Mechanism inference
# ====================================================================


def _looks_like_rate_limit(msg: str) -> bool:
    """Detect rate-limit-shaped errors from BrightData."""
    low = msg.lower()
    return any(m in low for m in ("ratelimit", "rate limit", "429", "too many requests"))


def infer_mechanisms(report: PerURLReport) -> None:
    """Populate report.mechanisms from DB path, outcome, and exception info."""

    mech: list[str] = []

    # -- Path-based (authoritative, from scrape_runs) --
    if report.db_path == "fast":
        if report.source_type == "api":
            mech.append("fast path (API): field mapping passed gates directly")
        else:
            mech.append("fast path (parser): parser list succeeded on first usable parser")
    elif report.db_path == "agent_repaired":
        mech.append("agent repair ladder TRIGGERED: Turn C (parser_gen) succeeded -&gt; promoted")
        if report.model_used:
            mech.append(f"  repair model: {report.model_used}")
    elif report.db_path == "retried":
        mech.append("extraction retry TRIGGERED (D7): transient fetch failure -&gt; retried and succeeded")
    elif report.db_path == "fallback_scraper":
        mech.append("scraper fallback TRIGGERED: primary scraper failed -&gt; backup scraper succeeded")
    elif report.db_path == "invalid_target":
        mech.append(f"invalid-target detection TRIGGERED: {report.invalid_reason}")
    elif report.db_path == "escalated":
        mech.append("escalated: all scrapers exhausted -&gt; escalation written")
        if report.escalation_reason:
            mech.append(f"  escalation reason: {report.escalation_reason}")

    # -- Outcome-based supplements --
    if report.outcome == "infra_error":
        mech.append("INFRA ALERT: BrightDataInfraError -&gt; immediate raise, no fallback (D21)")
        if report.errors:
            err = report.errors[0]
            if _looks_like_rate_limit(err):
                mech.append("  rate-limit shaped error (HTTP 429 / quota)")

    if report.outcome == "scrape_failed":
        mech.append(f"terminal failure: stage={report.failed_stage}")
        if report.escalation_reason:
            mech.append(f"  escalation: {report.escalation_reason} ({report.escalation_signature})")
        if report.errors:
            # Show last error, truncated
            last_err = report.errors[-1][:200]
            mech.append(f"  last error: {last_err}")

    # -- Edge-case: no DB record --
    if not report.db_path and report.outcome in ("scrape_failed", "infra_error"):
        mech.append("no scrape_runs record (failure before DB write)")

    report.mechanisms = mech


# ====================================================================
#  Single-URL scrape
# ====================================================================


async def scrape_one_url(
    url_info: dict[str, str],
    idx: int,
    total: int,
) -> PerURLReport:
    """Scrape a single URL through the full pipeline, returning a detailed report."""

    label = url_info["label"]
    url = url_info["url"]
    host = url_info["host"]

    report = PerURLReport(idx=idx, label=label, url=url, host=host)

    start = time.monotonic()
    report.start_time = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # -- Resolve site --
    from src.scraping.router import resolve_site

    try:
        report.site = resolve_site(url)
    except ValueError as e:
        report.outcome = "unhandled_exception"
        report.result_type = "ValueError"
        report.errors = [str(e)]
        report.latency_ms = int((time.monotonic() - start) * 1000)
        return report

    # -- Build scraper chain description --
    from src.scraping.registry import get_scrapers

    scraper_classes = get_scrapers(report.site)
    chain_parts: list[str] = []
    for cls in scraper_classes:
        order = getattr(cls, "_order", "?")
        kind = "HTML" if hasattr(cls, "_get_unlocker") else "API"
        chain_parts.append(f"{cls.__name__}({kind},o{order})")
    report.scraper_chain = " -&gt; ".join(chain_parts) if chain_parts else "(none registered)"

    # -- Run the scrape --
    from src.scraping import scrape, ProductData, InvalidTargetResult
    from src.scraping.exceptions import BrightDataInfraError, ScrapeFailed

    result = None
    exc = None

    try:
        result = await scrape(url)
    except BrightDataInfraError as e:
        exc = e
        report.outcome = "infra_error"
        report.result_type = "BrightDataInfraError"
        report.errors = [str(e)]
    except ScrapeFailed as e:
        exc = e
        report.outcome = "scrape_failed"
        report.result_type = "ScrapeFailed"
        report.failed_stage = e.failed_stage
        report.failed_scraper = e.scraper_name
        report.errors = list(e.errors)
    except Exception as e:
        exc = e
        report.outcome = "unhandled_exception"
        report.result_type = type(e).__name__
        report.errors = [str(e)]
        report.errors.append(traceback.format_exc()[-500:])

    # -- Process success / invalid_target --
    if result is not None:
        if isinstance(result, ProductData):
            report.outcome = "success"
            report.result_type = "ProductData"
            report.title = (result.title or "")[:150]
            if result.price is not None:
                report.price = f"{result.price} {result.currency or ''}".strip()
            report.currency = result.currency or ""
            report.brand = result.brand or ""
            report.in_stock = "True" if result.in_stock else "False"
            report.image_count = len(result.image_urls)
            report.gtin = result.gtin or ""
            report.parser_version = result.parser_version or ""
            report.source_type = result.source_type or ""
            # Additional ProductData fields (display when populated)
            if result.list_price is not None:
                report.list_price = f"{result.list_price} {result.currency or ''}".strip()
            if result.unit_price is not None:
                report.unit_price = str(result.unit_price)
            report.unit = result.unit or ""
            report.availability_raw = result.availability_raw or ""
            if result.variant:
                import json as _json
                report.variant = _json.dumps(result.variant, ensure_ascii=False)
        elif isinstance(result, InvalidTargetResult):
            report.outcome = "invalid_target"
            report.result_type = "InvalidTargetResult"
            report.invalid_reason = result.reason_signal

    report.latency_ms = int((time.monotonic() - start) * 1000)

    # -- Post-scrape: query scrape_runs and escalations --
    try:
        from src.scraping.config import get_config
        from src.scraping.storage import EscalationStore, ScrapeDB

        cfg = get_config()
        db = ScrapeDB(cfg.db_path)
        db.init_db()

        # Get the scrape_runs record for this URL
        run_row = db.conn.execute(
            "SELECT path, model_used, winning_parser_id, attempts "
            "FROM scrape_runs WHERE url = ? ORDER BY id DESC LIMIT 1",
            (url,),
        ).fetchone()
        if run_row:
            report.db_path = run_row["path"] or ""
            report.attempts = run_row["attempts"] or 1
            report.model_used = run_row["model_used"] or ""

        # Get new escalations involving our site
        esc_rows = EscalationStore(db).get_open()
        for esc in esc_rows:
            sig = esc.get("signature", "")
            if report.site and report.site in sig:
                report.escalation_reason = esc["reason"]
                report.escalation_signature = sig
                break

        db.close()
    except Exception:
        pass  # non-fatal: DB inspection failure shouldn't crash the report

    # -- Infer mechanisms --
    infer_mechanisms(report)

    return report


# ====================================================================
#  Per-URL report printing
# ====================================================================


def print_url_report(report: PerURLReport) -> None:
    """Print a formatted report block for one URL."""

    label_tag = f"[{report.label}]"
    bar = "-" * 78

    _print(f"\n{bar}")
    _print(f"  [{report.idx:02d}/{_total_urls}] {label_tag}")
    _print(f"  URL:   {report.url}")
    _print(f"  Host:  {report.host}")
    _print(f"{bar}")

    _print(f"  Site resolved:  {report.site}")
    _print(f"  Scraper chain:  {report.scraper_chain}")
    _print(f"  Started:        {report.start_time}")
    _print(f"  Latency:        {report.latency_ms:,} ms  ({report.latency_ms / 1000:.1f}s)")

    # DB path
    if report.db_path:
        _print(f"  DB path:        {report.db_path}")
    if report.attempts > 1:
        _print(f"  Attempts:       {report.attempts}")

    # Outcome-specific details
    if report.outcome == "success":
        _print(f"  Outcome:        [OK] SUCCESS  ({report.result_type})")
        _print(f"  -- Extracted Fields --")
        _print(f"    title:         {report.title[:100]}")
        if report.price:
            _print(f"    price:         {report.price}")
        if report.list_price:
            _print(f"    list_price:    {report.list_price}")
        if report.currency and not report.price:
            _print(f"    currency:      {report.currency}")
        if report.brand:
            _print(f"    brand:         {report.brand}")
        _print(f"    in_stock:      {report.in_stock}")
        if report.availability_raw:
            _print(f"    availability:  {report.availability_raw}")
        if report.image_count:
            _print(f"    image_urls:    {report.image_count} images")
        if report.gtin:
            _print(f"    gtin:          {report.gtin}")
        if report.variant:
            _print(f"    variant:       {report.variant}")
        if report.unit_price:
            if report.unit:
                _print(f"    unit_price:    {report.unit_price} per {report.unit}")
            else:
                _print(f"    unit_price:    {report.unit_price}")
        if report.parser_version:
            _print(f"    parser_version: {report.parser_version}")
        if report.source_type:
            _print(f"    source_type:   {report.source_type}")

    elif report.outcome == "invalid_target":
        _print(f"  Outcome:        [X] INVALID TARGET  ({report.result_type})")
        _print(f"  Reason signal:  {report.invalid_reason}")

    elif report.outcome in ("scrape_failed", "infra_error", "unhandled_exception"):
        emoji = {"scrape_failed": "[FAIL] ESCALATED", "infra_error": "[!!] INFRA ERROR", "unhandled_exception": "[WARN] UNHANDLED"}
        _print(f"  Outcome:        {emoji.get(report.outcome, report.outcome)}  ({report.result_type})")
        if report.failed_stage:
            _print(f"  Failed stage:   {report.failed_stage}")
        if report.failed_scraper:
            _print(f"  Failed scraper: {report.failed_scraper}")
        if report.errors:
            _print(f"  Errors ({len(report.errors)}):")
            for e in report.errors[-3:]:
                for line in textwrap.wrap(
                    str(e)[:300],
                    width=74,
                    initial_indent="    ",
                    subsequent_indent="      ",
                ):
                    _print(line)

    # Mechanisms
    if report.mechanisms:
        _print(f"  -- Mechanisms Triggered --")
        for m in report.mechanisms:
            _print(f"    * {m}")

    # Escalation
    if report.escalation_reason:
        _print(f"  Escalation:     {report.escalation_reason}")
        if report.escalation_signature:
            _print(f"  Signature:      {report.escalation_signature}")

    _print(bar)


# ====================================================================
#  Summary reporting
# ====================================================================

_total_urls = 0


def print_summary(reports: list[PerURLReport], total_latency_ms: int) -> None:
    """Print cross-tab summaries and mechanism tally."""

    total = len(reports)
    if total == 0:
        _print("\nNo reports to summarize.")
        return

    s_total = sum(1 for r in reports if r.outcome == "success")
    iv_total = sum(1 for r in reports if r.outcome == "invalid_target")
    f_total = sum(1 for r in reports if r.outcome == "scrape_failed")
    infra_total = sum(1 for r in reports if r.outcome == "infra_error")
    other_total = total - s_total - iv_total - f_total - infra_total

    # -- Overall --
    section("M12.4a -- Overall")
    _print(f"  Total URLs:          {total}")
    _print(f"  Success:             {s_total}  ({s_total * 100 / total:.0f}%)")
    _print(f"  Invalid Target:      {iv_total}  ({iv_total * 100 / total:.0f}%)")
    _print(f"  Scrape Failed:       {f_total}  ({f_total * 100 / total:.0f}%)")
    _print(f"  Infra Error:         {infra_total}  ({infra_total * 100 / total:.0f}%)")
    if other_total:
        _print(f"  Other:               {other_total}  ({other_total * 100 / total:.0f}%)")

    # -- By label --
    section("M12.4b -- By Label")
    by_label: dict[str, list[PerURLReport]] = defaultdict(list)
    for r in reports:
        by_label[r.label].append(r)

    header = f"  {'Label':<16} {'Total':>5}  {'OK':>5}  {'Invalid':>8}  {'Failed':>7}  {'Infra':>6}  {'Other':>6}"
    _print(header)
    _print(f"  {'-' * (len(header) - 2)}")
    for label in sorted(by_label):
        grp = by_label[label]
        n = len(grp)
        s = sum(1 for r in grp if r.outcome == "success")
        iv = sum(1 for r in grp if r.outcome == "invalid_target")
        f = sum(1 for r in grp if r.outcome == "scrape_failed")
        infra = sum(1 for r in grp if r.outcome == "infra_error")
        o = n - s - iv - f - infra
        _print(f"  {label:<16} {n:>5}  {s:>5}  {iv:>8}  {f:>7}  {infra:>6}  {o:>6}")
    _print(f"  {'-' * (len(header) - 2)}")
    _print(f"  {'TOTAL':<16} {total:>5}  {s_total:>5}  {iv_total:>8}  {f_total:>7}  {infra_total:>6}  {other_total:>6}")

    # -- By site --
    section("M12.4c -- By Site")
    by_site: dict[str, list[PerURLReport]] = defaultdict(list)
    for r in reports:
        by_site[r.site or "(unknown)"].append(r)

    header2 = f"  {'Site':<16} {'Total':>5}  {'OK':>5}  {'Invalid':>8}  {'Failed':>7}  {'Infra':>6}  {'Other':>6}"
    _print(header2)
    _print(f"  {'-' * (len(header2) - 2)}")
    for site in sorted(by_site):
        grp = by_site[site]
        n = len(grp)
        s = sum(1 for r in grp if r.outcome == "success")
        iv = sum(1 for r in grp if r.outcome == "invalid_target")
        f = sum(1 for r in grp if r.outcome == "scrape_failed")
        infra = sum(1 for r in grp if r.outcome == "infra_error")
        o = n - s - iv - f - infra
        _print(f"  {site:<16} {n:>5}  {s:>5}  {iv:>8}  {f:>7}  {infra:>6}  {o:>6}")

    # -- By path --
    section("M12.4d -- By Path (scrape_runs)")
    path_counter: Counter[str] = Counter(r.db_path or "(no run record)" for r in reports)
    for path, count in path_counter.most_common():
        _print(f"  {path:<35} {count:>4}")

    # -- Mechanism tally --
    section("M12.4e -- Mechanism Tally")
    mech_counter: Counter[str] = Counter()
    for r in reports:
        for m in r.mechanisms:
            # Normalize: strip leading indent and bullet prefixes
            normalized = m.lstrip().lstrip("* ")
            # Truncate at first colon or arrow to get the category
            category = normalized.split(":")[0].split("-&gt;")[0].strip()
            mech_counter[category] += 1
    if mech_counter:
        for mech, count in mech_counter.most_common():
            _print(f"  {mech:<60} {count:>3}")
    else:
        _print("  (no mechanisms detected)")

    # -- Escalations --
    escs = [r for r in reports if r.escalation_reason]
    if escs:
        section("M12.4f -- Escalations")
        esc_by_sig: dict[str, list[PerURLReport]] = defaultdict(list)
        for r in escs:
            esc_by_sig[r.escalation_signature or "unknown"].append(r)
        for sig, grp in sorted(esc_by_sig.items()):
            reason = grp[0].escalation_reason
            _print(f"  signature: {sig}")
            _print(f"  reason:    {reason}")
            _print(f"  affected:  {len(grp)} URL(s)")
            for r in grp:
                _print(f"    - [{r.label}] {r.url[:90]}")
            _print()

    # -- Timing --
    section("M12.4g -- Timing")
    latencies = [r.latency_ms for r in reports]
    latencies.sort()
    if latencies:
        _print(f"  Total wall time:    {total_latency_ms:,} ms  ({total_latency_ms / 1000:.1f} s)")
        _print(f"  Per-URL average:    {sum(latencies) // len(latencies):,} ms")
        _print(f"  Per-URL median:     {latencies[len(latencies) // 2]:,} ms")
        _print(f"  Per-URL p95:        {latencies[int(len(latencies) * 0.95)]:,} ms")
        _print(f"  Per-URL min:        {latencies[0]:,} ms")
        _print(f"  Per-URL max:        {latencies[-1]:,} ms")
        _print(f"  Per-URL sum:        {sum(latencies):,} ms")

    # -- Detail: URL-by-URL outcome table --
    section("M12.4h -- All URLs Quick View")
    _print(f"  {'#':>3}  {'label':<14} {'outcome':<18} {'site':<8} {'latency':>8}  {'db_path':<22}")
    _print(f"  {'-' * 80}")
    for r in sorted(reports, key=lambda x: x.idx):
        lat = f"{r.latency_ms / 1000:.1f}s"
        _print(f"  {r.idx:>3}  {r.label:<14} {r.outcome:<18} {r.site:<8} {lat:>8}  {r.db_path or '(none)':<22}")


# ====================================================================
#  Data loading
# ====================================================================


def load_urls(xlsx_path: Path) -> list[dict[str, str]]:
    """Read the URL xlsx. Expects columns: label, url, host."""
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    ws = wb.active
    rows: list[dict[str, str]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is not None and row[1] is not None:
            label = str(row[0]).strip()
            url = str(row[1]).strip()
            host = str(row[2]).strip() if len(row) > 2 and row[2] is not None else ""
            if not host:
                from urllib.parse import urlparse

                host = (urlparse(url).hostname or "")
            rows.append({"label": label, "url": url, "host": host})
    wb.close()
    return rows


# ====================================================================
#  Main run
# ====================================================================


async def run() -> int:
    """Main entry point. Returns exit code (0 = all good, 1 = failures)."""

    global _total_urls

    # -- M12.1: Load URLs --
    section("M12.1 -- Load input URLs")
    urls = load_urls(INPUT_XLSX)
    check(f"URLs loaded from {INPUT_XLSX.name}", len(urls) > 0, f"loaded {len(urls)} URLs")
    if not urls:
        return 1
    _total_urls = len(urls)

    label_counts = Counter(u["label"] for u in urls)
    host_counts = Counter(u["host"] for u in urls)
    _print(f"  Labels: {dict(label_counts)}")
    _print(f"  Hosts:  {dict(host_counts)}")

    # -- M12.2: API key checks --
    section("M12.2 -- API key checks")
    from src.scraping.config import get_config

    cfg = get_config()

    # BRIGHT_UNLOCKER_KEY -> BRIGHT_DATA_KEY aliasing is done at module load time (see top of file).
    bright_key = cfg.bright_data_key
    if not bright_key:
        check("BrightData key set", False,
              "MISSING -- set BRIGHT_DATA_KEY, BRIGHT_UNLOCKER_KEY, or SCRAPING_BRIGHT_DATA_KEY in .env")
        _print()
        _print("  +==============================================================+")
        _print("  |  REFUSING TO RUN: BrightData key is required.               |")
        _print("  |  This test makes REAL API calls that cost money.            |")
        _print("  |  Accepted env var names:                                    |")
        _print("  |    BRIGHT_DATA_KEY, BRIGHT_UNLOCKER_KEY, SCRAPING_BRIGHT_DATA_KEY")
        _print("  +==============================================================+")
        return 1
    check("BrightData key present", True, f"key starts with {bright_key[:6]}...")

    if cfg.deepseek_key:
        check("DEEPSEEK_KEY present", True, "repair ladder available for HTML scrapers")
    else:
        check("DEEPSEEK_KEY present", False, "MISSING -- HTML scraper repair will escalate (API scrapers unaffected)")

    # -- M12.3: Run all URLs concurrently --
    concurrency = min(CONCURRENCY, len(urls))
    section(f"M12.3 -- Scrape {len(urls)} URLs (concurrency={concurrency})")

    # Suppress noisy library loggers so captured logs stay clean
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    overall_start = time.monotonic()
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(url_info: dict[str, str], idx: int) -> PerURLReport:
        async with sem:
            return await scrape_one_url(url_info, idx=idx, total=len(urls))

    tasks = [_bounded(u, i + 1) for i, u in enumerate(urls)]

    # Stream reports as they complete (not in input order)
    reports: list[PerURLReport] = []
    for coro in asyncio.as_completed(tasks):
        report = await coro
        reports.append(report)
        print_url_report(report)

    # Restore input order for the summary tables
    reports.sort(key=lambda r: r.idx)

    overall_ms = int((time.monotonic() - overall_start) * 1000)

    # -- M12.4: Summaries --
    print_summary(reports, overall_ms)

    # -- M12.5: Label-based checks --
    section("M12.5 -- Label-based checks")
    normal_urls = [r for r in reports if r.label == "normal"]
    normal_ok = [r for r in normal_urls if r.outcome == "success"]
    check(
        f"Normal URLs succeeded ({len(normal_ok)}/{len(normal_urls)})",
        len(normal_ok) == len(normal_urls),
        f"unexpected failures: {[r.url for r in normal_urls if r.outcome != 'success']}",
    )

    error_urls = [r for r in reports if r.label == "error"]
    error_invalid = [r for r in error_urls if r.outcome == "invalid_target"]
    check(
        f"Error URLs detected as invalid_target ({len(error_invalid)}/{len(error_urls)})",
        len(error_invalid) == len(error_urls),
        f"not detected: {[r.url for r in error_urls if r.outcome != 'invalid_target']}",
    )

    discount_urls = [r for r in reports if r.label == "discount"]
    discount_ok = [r for r in discount_urls if r.outcome == "success"]
    if discount_urls:
        check(
            f"Discount URLs succeeded ({len(discount_ok)}/{len(discount_urls)})",
            len(discount_ok) == len(discount_urls),
            f"failed: {[r.url for r in discount_urls if r.outcome != 'success']}",
        )

    unavail_urls = [r for r in reports if r.label == "unavailable"]
    if unavail_urls:
        r = unavail_urls[0]
        handled = r.outcome in ("invalid_target", "scrape_failed")
        check(
            f"Unavailable URL handled ({r.outcome})",
            handled,
            f"url={r.url}",
        )

    return 0 if not FAILED else 1


# ====================================================================
#  Entry point
# ====================================================================


def main() -> int:
    """Parse argv, run, clean up, report exit code."""
    global _tee

    _tee = TeeWriter(OUTPUT_LOG)

    try:
        _print(f"verify_m12: End-to-End Live Scraping Verification")
        _print(f"Started:    {datetime.now(timezone.utc).isoformat()}")
        _print(f"Log file:   {OUTPUT_LOG}")
        _print(f"Temp DB:    {_DB_PATH}")

        exit_code = asyncio.run(run())

    except KeyboardInterrupt:
        _print("\n\nInterrupted by user.")
        exit_code = 1
    except Exception:
        FAILED.append(("UNHANDLED_EXCEPTION", ""))
        traceback.print_exc(file=_tee)
        exit_code = 1
    finally:
        # Clean up temp DB
        if os.path.exists(_DB_PATH):
            try:
                os.remove(_DB_PATH)
                for suffix in ("-wal", "-shm"):
                    p = _DB_PATH + suffix
                    if os.path.exists(p):
                        os.remove(p)
            except OSError:
                pass

    _print()
    _print("=" * 70)
    _print(f"FINAL: {len(PASSED)} checks passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    _print(f"Finished: {datetime.now(timezone.utc).isoformat()}")
    _print(f"Output log: {OUTPUT_LOG}")
    _print("=" * 70)

    if FAILED:
        for name, detail in FAILED:
            _print(f"  FAILED: {name}  ({detail})")

    _tee.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
