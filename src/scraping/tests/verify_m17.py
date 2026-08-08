"""Verification for M17 — Excel cold-start input and golden bucket caps.

Fully offline: generated workbooks, local HTML, fixed parser code, and temp DBs.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from src.scraping import config as config_module
from src.scraping.config import ScrapingConfig


TMP_DIR = Path(tempfile.mkdtemp(prefix="verify_m17_"))
DB_PATH = TMP_DIR / "m17.db"
PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print(); print("=" * 72); print(title); print("=" * 72)


def set_test_config(**overrides) -> ScrapingConfig:
    values = {"db_path": DB_PATH, **overrides}
    cfg = ScrapingConfig(**values)
    config_module.set_config(cfg)
    return cfg


def make_xlsx(name: str, headers: list[str], rows: list[list[object]]) -> Path:
    path = TMP_DIR / name
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)
    wb.close()
    return path


def reset_site(site: str = "tesco") -> None:
    from src.scraping.storage import ScrapeDB

    db = ScrapeDB(DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM parsers WHERE site = ?", (site,))
    db.conn.execute("DELETE FROM golden_samples WHERE site = ?", (site,))
    db.conn.commit(); db.close()


def make_product(url: str, **kwargs):
    from src.scraping.models import ProductData

    values = {
        "url": url,
        "website": "tesco",
        "scraped_at": datetime.now(timezone.utc),
        "source_type": "html",
        "title": "Test Product",
        "price": Decimal("12.34"),
        "currency": "GBP",
        "image_urls": [],
        "in_stock": True,
    }
    values.update(kwargs)
    return ProductData(**values)


def verify_config_and_input() -> None:
    from pydantic import ValidationError

    from src.scraping.coldstart import read_coldstart_input
    from src.scraping.exceptions import ColdStartInputError

    section("M17.1 - config policy")
    cfg = set_test_config()
    check(
        "default mandatory page types",
        cfg.mandatory_page_types()
        == ("standard", "out_of_stock", "discounted", "membership"),
        str(cfg.mandatory_page_types()),
    )
    check("multipack optional", not cfg.is_mandatory_page_type("multipack"))
    check("mandatory minimum is 1", cfg.golden_min_for("standard") == 1)
    check("optional minimum is 0", cfg.golden_min_for("multipack") == 0)
    partial = ScrapingConfig(
        db_path=DB_PATH,
        cold_start_page_require_mandatory={"multipack": False},
    )
    check("missing policy key fails safe to mandatory", partial.is_mandatory_page_type("membership"))
    try:
        ScrapingConfig(db_path=DB_PATH, cold_start_page_require_mandatory={"stand": True})
        unknown_failed = False
    except ValidationError as exc:
        unknown_failed = "stand" in str(exc) and "standard" in str(exc)
    check("unknown policy key rejected", unknown_failed)
    try:
        ScrapingConfig(db_path=DB_PATH, golden_max_samples_per_page_type=0)
        zero_failed = False
    except ValidationError as exc:
        zero_failed = ">= 1" in str(exc)
    check("zero cap rejected", zero_failed)

    section("M17.2 - workbook validation")
    valid = make_xlsx(
        "valid.xlsx",
        [" URL ", "PAGE_TYPE", "host"],
        [
            ["https://example/1", "standard", "example"],
            ["https://example/2", "Out of Stock", "example"],
            ["https://example/3", "discounted", "example"],
            ["https://example/4", "membership", "example"],
            [None, None, None],
        ],
    )
    rows = read_coldstart_input(valid)
    check("valid workbook parsed", len(rows) == 4, str(rows))
    check("page type normalized", rows[1].page_type == "out_of_stock")
    check("sheet row retained", rows[1].row_no == 3)

    invalid = make_xlsx(
        "invalid.xlsx",
        ["page_type", "url"],
        [["stand", "https://example/1"], ["out", "https://example/2"], [None, "https://example/3"], ["standard", None]],
    )
    try:
        read_coldstart_input(invalid)
        invalid_message = ""
    except ColdStartInputError as exc:
        invalid_message = str(exc)
    check("all invalid rows reported", all(f"row {n}" in invalid_message for n in (2, 3, 4, 5)), invalid_message)
    check("legal values reported", "out_of_stock" in invalid_message and "multipack" in invalid_message)

    missing_header = make_xlsx("missing_header.xlsx", ["label", "url", "host"], [["standard", "x", "y"]])
    try:
        read_coldstart_input(missing_header)
        missing_message = ""
    except ColdStartInputError as exc:
        missing_message = str(exc)
    check("missing page_type header rejected", "missing required column(s): page_type" in missing_message)

    text_path = TMP_DIR / "urls.txt"
    text_path.write_text("https://example/1\n", encoding="utf-8")
    try:
        read_coldstart_input(text_path)
        extension_message = ""
    except ColdStartInputError as exc:
        extension_message = str(exc)
    check("text input rejected with Excel contract", ".xlsx/.xlsm" in extension_message)

    no_membership = make_xlsx(
        "no_membership.xlsx",
        ["page_type", "url"],
        [["standard", "a"], ["out_of_stock", "b"], ["discounted", "c"]],
    )
    try:
        read_coldstart_input(no_membership)
        coverage_message = ""
    except ColdStartInputError as exc:
        coverage_message = str(exc)
    check("missing mandatory type rejected", "membership" in coverage_message and "found:" in coverage_message)
    check("optional multipack may be omitted", len(rows) == 4)

    set_test_config(
        cold_start_page_require_mandatory={
            "standard": True,
            "out_of_stock": True,
            "discounted": True,
            "membership": False,
            "multipack": False,
        }
    )
    check("config can make membership optional", len(read_coldstart_input(no_membership)) == 3)
    set_test_config()


FIXED_PARSER = """
def parse(html, url):
    return {
        'title': 'Offline Product', 'price': '12.34', 'currency': 'GBP',
        'in_stock': True, 'image_urls': []
    }
"""


async def run_coldstart_with_answers(rows, answers):
    from src.scraping.coldstart import run_coldstart
    from src.scraping.extraction import bright_data as bd_mod

    sample_html = "<html><body><h1>Offline Product</h1><span>£12.34</span></body></html>"

    async def fake_fetch(self, url):
        return 200, sample_html

    async def fake_gen(site, html, **_kwargs):
        return FIXED_PARSER

    prompts: list[str] = []
    answer_iter = iter(answers)

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answer_iter)

    with (
        patch.object(bd_mod.BrightDataUnlocker, "fetch", new=fake_fetch),
        patch("src.scraping.coldstart._gen_initial_parser", new=fake_gen),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = await run_coldstart("tesco", rows, input_fn=fake_input)
    return result, output.getvalue(), prompts


async def verify_coldstart_and_runtime() -> None:
    from src.scraping.coldstart import (
        ColdStartRow,
        _gen_initial_parser,
        _result_exit_code,
    )
    from src.scraping.repair.golden import maybe_seed_golden
    from src.scraping.storage import GoldenStore, ScrapeDB

    section("M17.3 - cold-start caps, declared buckets, and shortfalls")
    captured_llm_args = {}

    class FakeLLMResponse:
        content = json.dumps({"parser_code": FIXED_PARSER})

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured_llm_args.update(kwargs)

        async def ainvoke(self, prompt):
            return FakeLLMResponse()

    set_test_config(
        qwen_key="offline-test-key",
        cold_start_model_ladder=["configured-coldstart-model"],
        cold_start_temperature_ladder=[0.1],
    )
    with patch("langchain_openai.ChatOpenAI", new=FakeChatOpenAI):
        generated = await _gen_initial_parser(
            "tesco", "<html><h1>Product</h1><span>£12.34</span></html>"
        )
    check(
        "cold start uses configured first cold-start model",
        generated == FIXED_PARSER
        and captured_llm_args.get("model") == "configured-coldstart-model",
        str(captured_llm_args.get("model")),
    )

    set_test_config(golden_max_samples_per_page_type=3)
    reset_site()
    cap_rows = [ColdStartRow("standard", f"https://example/standard/{i}", i + 2) for i in range(5)]
    result, output, prompts = await run_coldstart_with_answers(cap_rows, ["y", "y", "y"])
    check("cap seeds only three goldens", result["seeded_goldens"] == 3, str(result))
    check("spares skipped without prompting", len(prompts) == 3, str(len(prompts)))
    check("spare message emitted", output.count("skipping (spare)") == 2)

    reset_site()
    mismatch_rows = [ColdStartRow("discounted", "https://example/discounted/1", 2)]
    result, output, _ = await run_coldstart_with_answers(mismatch_rows, ["y"])
    db = ScrapeDB(DB_PATH); db.init_db()
    discounted = GoldenStore(db).get_by_site_and_type("tesco", "discounted")
    db.close()
    check("declared bucket wins", len(discounted) == 1)
    check("mismatch warning shown", "MISMATCH" in output and "standard" in output)
    check("cold-start provenance stored", discounted[0]["created_by"] == "coldstart")

    reset_site()
    required_rows = [
        ColdStartRow("standard", "https://example/s", 2),
        ColdStartRow("discounted", "https://example/d", 3),
        ColdStartRow("out_of_stock", "https://example/o", 4),
        ColdStartRow("membership", "https://example/m", 5),
    ]
    result, output, _ = await run_coldstart_with_answers(
        required_rows,
        ["y", "y", "y", "n", "", "", "n"],
    )
    check(
        "blocked persistence leaves all mandatory buckets short",
        result["coverage_shortfall"]
        == ["standard", "out_of_stock", "discounted", "membership"],
        str(result),
    )
    check("rejection blocks all seeding", result["seeded_goldens"] == 0)
    check("rejection returns exit code 1", _result_exit_code(result) == 1)
    check("failure block emitted", "not saved" in output.lower() and "membership" in output)

    section("M17.4 - runtime cap and URL dedup")
    reset_site()
    ids = [
        maybe_seed_golden("tesco", f"<html>{i}</html>", make_product(f"https://example/{i}"))
        for i in range(1, 5)
    ]
    check("three distinct runtime URLs seeded", all(id_ is not None for id_ in ids[:3]), str(ids))
    check("fourth URL blocked by cap", ids[3] is None)
    reset_site()
    first = maybe_seed_golden("tesco", "<html>one</html>", make_product("https://example/same"))
    repeat = maybe_seed_golden("tesco", "<html>two</html>", make_product("https://example/same"))
    check("repeat URL blocked before cap", first is not None and repeat is None)


def verify_prune_and_schema_guard() -> None:
    from src.scraping.scripts.prune_goldens import apply_prune_plan, build_prune_plan
    from src.scraping.storage import GoldenStore, ScrapeDB

    section("M17.5 - prune ordering and schema guard")
    set_test_config(golden_max_samples_per_page_type=2)
    reset_site()
    db = ScrapeDB(DB_PATH); db.init_db(); gs = GoldenStore(db)
    sample_ids = [
        gs.seed("tesco", "standard", "stale", {"url": "u1"}, created_by="auto"),
        gs.seed("tesco", "standard", "old-auto", {"url": "u2"}, created_by="auto"),
        gs.seed("tesco", "standard", "new-auto", {"url": "u3"}, created_by="auto"),
        gs.seed("tesco", "standard", "human", {"url": "u4"}, created_by="coldstart"),
    ]
    db.conn.execute("UPDATE golden_samples SET is_stale = 1 WHERE id = ?", (sample_ids[0],))
    for day, sample_id in enumerate(sample_ids, 1):
        db.conn.execute(
            "UPDATE golden_samples SET captured_at = ? WHERE id = ?",
            (f"2026-06-0{day}T00:00:00Z", sample_id),
        )
    db.conn.commit()
    plans = build_prune_plan(db, "tesco")
    standard_plan = next(plan for plan in plans if plan.page_type == "standard")
    check("dry run chooses stale then oldest auto", [r["id"] for r in standard_plan.evict] == sample_ids[:2])
    before = db.conn.execute("SELECT COUNT(*) FROM golden_samples").fetchone()[0]
    check("dry run deletes nothing", before == 4)
    deleted = apply_prune_plan(db, plans)
    survivors = {
        row["id"] for row in db.conn.execute("SELECT id FROM golden_samples").fetchall()
    }
    check("apply deletes planned rows", deleted == 2)
    check("human and newest auto survive", survivors == {sample_ids[2], sample_ids[3]}, str(survivors))
    db.close()

    legacy = TMP_DIR / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE golden_samples (id INTEGER PRIMARY KEY, site TEXT, page_type TEXT, "
        "html_snapshot TEXT, expected_output TEXT, captured_at TEXT, is_stale INTEGER)"
    )
    conn.execute(
        "INSERT INTO golden_samples VALUES (1, 'tesco', 'standard', 'html', '{}', '2026-01-01', 0)"
    )
    conn.commit(); conn.close()
    legacy_db = ScrapeDB(legacy)
    legacy_db.init_db()
    columns = [
        row["name"]
        for row in legacy_db.conn.execute("PRAGMA table_info(golden_samples)")
    ]
    check("schema guard adds provenance column", "created_by" in columns)
    created_by = legacy_db.conn.execute(
        "SELECT created_by FROM golden_samples WHERE id = 1"
    ).fetchone()[0]
    check("legacy rows backfilled as auto", created_by == "auto")
    legacy_db.init_db()
    columns_after = [
        row["name"]
        for row in legacy_db.conn.execute("PRAGMA table_info(golden_samples)")
    ]
    created_by_after = legacy_db.conn.execute(
        "SELECT created_by FROM golden_samples WHERE id = 1"
    ).fetchone()[0]
    check(
        "schema guard is idempotent",
        columns_after.count("created_by") == 1 and created_by_after == "auto",
    )
    legacy_plans = build_prune_plan(legacy_db, "tesco")
    standard_legacy_plan = next(
        plan for plan in legacy_plans if plan.page_type == "standard"
    )
    check(
        "prune works after automatic schema repair",
        standard_legacy_plan.before == 1,
    )
    legacy_db.close()


async def run() -> None:
    verify_config_and_input()
    await verify_coldstart_and_runtime()
    verify_prune_and_schema_guard()


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()
    print(); print("=" * 72)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 72)
    for name, detail in FAILED:
        print(f"  FAILED: {name}  ({detail})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
