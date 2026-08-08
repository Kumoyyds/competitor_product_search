"""M19 verification: cold-start review/repair loop and golden reuse.

Fully offline. BrightData, human input, and LLM calls are deterministic fakes;
the sandbox-backed stale-golden checks still execute real parser subprocesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from src.scraping import config as config_module
from src.scraping.config import ScrapingConfig


TMP_DIR = Path(tempfile.mkdtemp(prefix="verify_m19_"))
DB_PATH = TMP_DIR / "m19.db"
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
    print(); print("=" * 76); print(title); print("=" * 76)


def set_cfg(**overrides) -> ScrapingConfig:
    policy = {name: False for name in (
        "standard", "out_of_stock", "discounted", "multipack", "membership"
    )}
    values = {
        "db_path": DB_PATH,
        "cold_start_page_require_mandatory": policy,
        "golden_max_samples_per_page_type": 10,
        "cold_start_model_ladder": ["node-0", "node-1"],
        "cold_start_temperature_ladder": [0.1, 0.4],
        "cold_start_max_repair_rounds": 10,
        "qwen_key": "offline-key",
        "_env_file": None,
        **overrides,
    }
    cfg = ScrapingConfig(**values)
    config_module.set_config(cfg)
    return cfg


def reset_db() -> None:
    from src.scraping.storage import ScrapeDB

    db = ScrapeDB(DB_PATH); db.init_db()
    for table in ("parsers", "golden_samples", "scrape_runs"):
        db.conn.execute(f"DELETE FROM {table}")
    db.conn.commit(); db.close()


def product(url: str, *, title: str = "Offline Product", **changes):
    from src.scraping.models import ProductData

    values = {
        "url": url,
        "website": "tesco",
        "scraped_at": datetime.now(timezone.utc),
        "source_type": "html",
        "parser_version": "coldstart_v1",
        "title": title,
        "brand": "Brand",
        "gtin": "1234567890123",
        "image_urls": ["https://images.example/product.jpg"],
        "price": Decimal("12.34"),
        "currency": "GBP",
        "in_stock": True,
        "availability_raw": "In stock",
    }
    values.update(changes)
    return ProductData(**values)


class FakeScraper:
    site = "tesco"

    def __init__(self, unlocker=None):
        self.unlocker = unlocker

    def _get_unlocker(self):
        return self.unlocker


async def run_driver(rows, rounds, answers, *, ladder=None, initial_results=None):
    """Run only the cold-start driver; inject resolved HTML and review cases."""
    from src.scraping.coldstart import run_coldstart

    reset_db()
    ladder = ladder or ["node-0", "node-1"]
    set_cfg(
        cold_start_model_ladder=ladder,
        cold_start_temperature_ladder=[0.1] * len(ladder),
    )
    answer_iter = iter(answers)
    prompts: list[str] = []
    repair_calls: list[dict] = []
    round_index = 0

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answer_iter)

    async def fake_resolve(scraper, supplied_rows, force_fetch):
        return [(r.url, 200, f"<html>{r.url}</html>", "brightdata") for r in supplied_rows]

    # Successive return values let a test simulate a node that produced nothing
    # usable (truncated LLM reply) and check the ladder falls through.
    initial_iter = iter(initial_results or [])

    async def fake_initial(site, html, **kwargs):
        return next(initial_iter, "initial parser")

    async def fake_repair(**kwargs):
        repair_calls.append(kwargs)
        return "repaired parser"

    async def fake_cases(site, supplied_rows, fetched, parser_code):
        nonlocal round_index
        result = rounds[min(round_index, len(rounds) - 1)]
        round_index += 1
        return result

    with (
        patch("src.scraping.coldstart._pick_html_scraper", return_value=FakeScraper),
        patch("src.scraping.coldstart._resolve_html", new=fake_resolve),
        patch("src.scraping.coldstart._gen_initial_parser", new=fake_initial),
        patch("src.scraping.coldstart._gen_repaired_parser", new=fake_repair),
        patch("src.scraping.coldstart._run_review_cases", new=fake_cases),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = await run_coldstart("tesco", rows, input_fn=fake_input)
    return result, output.getvalue(), prompts, repair_calls


async def verify_driver() -> None:
    from src.scraping.coldstart import ColdStartRow, _ReviewCase, _result_exit_code
    from src.scraping.storage import ScrapeDB

    section("M19.1 - all-cases-pass persistence gate")
    rows = [ColdStartRow("standard", f"https://example/{i}", i + 2) for i in range(4)]
    good_cases = [_ReviewCase(r, "html", product(r.url)) for r in rows]

    result, output, _, _ = await run_driver(
        rows, [good_cases], ["y", "y", "n", "", "", "y", "n"]
    )
    db = ScrapeDB(DB_PATH); db.init_db()
    counts = (
        db.conn.execute("SELECT COUNT(*) FROM parsers").fetchone()[0],
        db.conn.execute("SELECT COUNT(*) FROM golden_samples").fetchone()[0],
    ); db.close()
    check("one rejection prevents parser and golden writes", counts == (0, 0), str(counts))
    check("rejection returns non-zero outcome", _result_exit_code(result) == 1)
    check("rejection summary identifies URL", "[REJECTED]" in output and rows[2].url in output)

    result, _, _, _ = await run_driver(rows, [good_cases], ["y"] * 4)
    check("all accepted cases seed one parser", result["parser_id"] is not None)
    check("all accepted cases seed four goldens", result["seeded_goldens"] == 4)

    crashed = [_ReviewCase(rows[0], "html", None, "sandbox_failed", "AttributeError: boom")] + good_cases[1:]
    result, output, _, _ = await run_driver(rows, [crashed], ["y", "y", "y", "n"])
    check("sandbox crash prevents persistence", result["parser_id"] is None)
    check("sandbox crash summary is explicit", "[PARSER CRASH]" in output and "AttributeError" in output)

    fetch_failed = [_ReviewCase(rows[0], "", None, "extraction_failed", "HTTP/status 0")] + good_cases[1:]
    result, output, _, _ = await run_driver(rows, [fetch_failed], ["y", "y", "y"])
    check("fetch failure does not block parser", result["parser_id"] is not None)
    check("fetch failure is reported separately", "[FETCH FAIL]" in output)

    section("M19.2 - correction rounds and review reuse")
    two_rows = rows[:2]
    first_round = [_ReviewCase(r, "html", product(r.url)) for r in two_rows]
    second_round = [
        _ReviewCase(two_rows[0], "html", product(two_rows[0].url)),
        _ReviewCase(two_rows[1], "html", product(two_rows[1].url, price=Decimal("2.50"))),
    ]
    result, _, prompts, repairs = await run_driver(
        two_rows,
        [first_round, second_round],
        ["y", "n", "price", "2.50", "总价在主购买框", "y", "y"],
    )
    check("bad initial parser can converge and seed", result["parser_id"] is not None)
    check("repair receives structured correct value", repairs[0]["feedbacks"][0].corrections[0].correct_value == "2.50")
    check("repair receives original human hint", repairs[0]["feedbacks"][0].hint == "总价在主购买框")
    accept_prompts = [p for p in prompts if "Accept?" in p]
    check("unchanged prior acceptance is not re-prompted", len(accept_prompts) == 3, str(len(accept_prompts)))

    result, _, prompts, repairs = await run_driver(
        two_rows[:1], [first_round[:1], first_round[:1]],
        ["n", "", "", "c", "y"], ladder=["only-node"]
    )
    check("single-node ladder can repair until convergence", result["parser_id"] is not None)
    check("single-node failure asks whether to continue", any("Continue" in p for p in prompts))
    check("single-node final rung is reused for repair", len(repairs) == 1)

    result, _, _, repairs = await run_driver(
        two_rows[:1],
        [first_round[:1], first_round[:1]],
        ["n", "", "", "y", "y"],
    )
    check("repair can converge after reaching the final ladder rung", result["parser_id"] is not None)
    check("repair node was consumed exactly once", len(repairs) == 1)

    # A node returning nothing usable (e.g. an output-limit truncation) must not
    # kill the run while ladder nodes remain.
    result, output, _, repairs = await run_driver(
        two_rows[:1],
        [[_ReviewCase(two_rows[0], "html", product(two_rows[0].url))]],
        ["y"],
        initial_results=[None],
    )
    check("empty node-0 reply falls through instead of aborting", result["parser_id"] is not None)
    check("fall-through regenerates instead of repairing empty code", not repairs)
    check("fall-through is reported", "falling through to the next repair round" in output)

    result, output, _, _ = await run_driver(
        two_rows[:1],
        [[_ReviewCase(two_rows[0], "html", product(two_rows[0].url))]],
        ["y"],
        ladder=["only-node"],
        initial_results=[None],
    )
    check("one empty reply on the final rung can recover", result["parser_id"] is not None)
    check("empty final-rung reply reports fall-through", "falling through" in output)

    result, _, _, _ = await run_driver(
        two_rows[:1], [[]], [], ladder=["only-node"], initial_results=[None, None]
    )
    check("two consecutive empty replies abort", result["parser_id"] is None)


async def verify_prompt_config_panel() -> None:
    from src.scraping.coldstart import (
        ColdStartRow,
        FieldCorrection,
        ReviewFeedback,
        _gen_initial_parser,
        _gen_repaired_parser,
        _print_product_panel,
        run_coldstart,
    )
    from src.scraping.repair.prepass import build_price_aware_context
    from src.scraping.repair.prompts import coldstart_repair_prompt

    section("M19.3 - ladder config, prompt feedback, and review panel")
    set_cfg(
        cold_start_model_ladder=["first-model", "last-model"],
        cold_start_temperature_ladder=[0.2, 0.6],
    )
    calls: list[dict] = []

    class Response:
        content = json.dumps({"parser_code": "def parse(html, url):\n    return {}"})

    class Client:
        async def ainvoke(self, prompt):
            return Response()

    def fake_client(**kwargs):
        calls.append(kwargs)
        return Client()

    feedback = ReviewFeedback(
        "https://example/member",
        "membership",
        (FieldCorrection("price", "2.50"),),
        "总价在 .pdp-price__amount",
    )
    with patch("src.scraping.coldstart.make_chat_client", side_effect=fake_client):
        await _gen_initial_parser("tesco", "<html><h1>P</h1><span>£2.50</span></html>")
        await _gen_repaired_parser(
            site="tesco", html="<html><h1>P</h1><span>£2.50</span></html>",
            current_code="old-code", feedbacks=[feedback], failures=["boom"],
            index=1, model="last-model", temperature=0.6, enable_thinking=True,
            role="last", resolved_ledger={"https://example/member": ["title"]},
            regressions={},
        )
    check("node zero uses cold-start ladder model", calls[0]["model"] == "first-model")
    check("last repair node enables thinking", calls[1]["model"] == "last-model" and calls[1]["enable_thinking"] is True)

    ctx = build_price_aware_context("<html><h1>P</h1><span>£2.50</span></html>", "https://example/p")
    messages = coldstart_repair_prompt(
        ctx, "tesco", "old-code", [feedback], ["sandbox boom"],
        role="last", attempt_index=1, model="last-model",
        resolved_ledger={"https://example/member": ["title"]}, regressions={},
    )
    prompt_text = json.dumps(messages, ensure_ascii=False)
    check("repair prompt contains prior parser source", "old-code" in prompt_text)
    check("repair prompt contains correction and hint", "2.50" in prompt_text and feedback.hint in prompt_text)

    panel = io.StringIO()
    long_title = "T" * 100
    with contextlib.redirect_stdout(panel):
        _print_product_panel(
            product("https://example/p", title=long_title, membership_price=None),
            "membership",
        )
    text = panel.getvalue()
    check("panel includes all newly reviewable fields", all(name in text for name in ("membership_price", "image_urls", "availability_raw")))
    check("panel summarizes lists and truncates long strings", "1 项" in text and "…" in text)
    check("panel flags missing bucket-critical field", "该桶关键字段为空" in text)

    set_cfg(
        cold_start_model_ladder=["a", "b"],
        cold_start_temperature_ladder=[0.1],
    )
    try:
        with patch("src.scraping.coldstart._pick_html_scraper", return_value=FakeScraper):
            await run_coldstart("tesco", [ColdStartRow("standard", "u", 2)])
        mismatch_raised = False
    except AssertionError as exc:
        mismatch_raised = "length" in str(exc)
    check("mismatched cold-start ladders fail fast", mismatch_raised)


async def verify_stale_and_cache() -> None:
    from src.scraping.coldstart import ColdStartRow, _resolve_html
    from src.scraping.repair.golden import _no_active_parser_passes, promote_candidate
    from src.scraping.storage import GoldenStore, ParserStore, ScrapeDB

    section("M19.4 - stale-golden control and HTML snapshot reuse")
    reset_db(); set_cfg()
    db = ScrapeDB(DB_PATH); db.init_db(); gs = GoldenStore(db); ps = ParserStore(db)
    expected_product = product("https://example/golden", title="Golden")
    sample_id = gs.seed(
        "tesco", "standard", "<html>golden</html>",
        expected_product.model_dump(mode="json"), created_by="coldstart",
    )
    sample = gs.get_by_site_and_type("tesco", "standard")[0]
    check("orphan golden is stale", await _no_active_parser_passes(db, "tesco", sample))

    matching = """
def parse(html, url):
    return {'title': 'Golden', 'brand': 'Brand', 'gtin': '1234567890123',
            'image_urls': ['https://images.example/product.jpg'], 'price': '12.34',
            'currency': 'GBP', 'in_stock': True, 'availability_raw': 'In stock'}
"""
    ps.create("tesco", "match", matching)
    check("matching active parser keeps golden live", not await _no_active_parser_passes(db, "tesco", sample))
    db.conn.execute("DELETE FROM parsers"); db.conn.commit()
    wrong = "def parse(html, url):\n    return {'title': 'Wrong', 'price': '12.34', 'currency': 'GBP', 'in_stock': True, 'image_urls': []}"
    ps.create("tesco", "wrong", wrong)
    check("all active parsers failing marks golden stale", await _no_active_parser_passes(db, "tesco", sample))
    db.close()

    result = await promote_candidate(
        "tesco", wrong, product("https://example/current", title="Wrong"), "<html>current</html>"
    )
    db = ScrapeDB(DB_PATH); db.init_db()
    stale = db.conn.execute("SELECT is_stale FROM golden_samples WHERE id = ?", (sample_id,)).fetchone()[0]
    db.close()
    check("promotion marks rotten golden stale instead of rejecting", isinstance(result, int) and stale == 1)

    reset_db(); set_cfg()
    db = ScrapeDB(DB_PATH); db.init_db(); gs = GoldenStore(db)
    cached_url = "https://example/cached"
    gs.seed("tesco", "standard", "<html>cached</html>", product(cached_url).model_dump(mode="json"), created_by="coldstart")
    before = db.conn.execute("SELECT COUNT(*) FROM golden_samples").fetchone()[0]
    db.close()

    class Unlocker:
        def __init__(self): self.calls = 0
        async def fetch(self, url):
            self.calls += 1
            return 200, "<html>fresh</html>"

    unlocker = Unlocker(); scraper = FakeScraper(unlocker)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        resolved = await _resolve_html(scraper, [ColdStartRow("standard", cached_url, 2)], False)
    check("non-stale golden snapshot avoids BrightData", resolved[0][3] == "goldset" and unlocker.calls == 0)
    check("golden source is printed", "[goldset]" in output.getvalue())

    db = ScrapeDB(DB_PATH); db.init_db(); db.conn.execute("UPDATE golden_samples SET is_stale = 1"); db.conn.commit(); db.close()
    resolved = await _resolve_html(scraper, [ColdStartRow("standard", cached_url, 2)], False)
    check("stale golden falls back to BrightData", resolved[0][3] == "brightdata" and unlocker.calls == 1)

    db = ScrapeDB(DB_PATH); db.init_db(); db.conn.execute("UPDATE golden_samples SET is_stale = 0"); db.conn.commit(); db.close()
    resolved = await _resolve_html(scraper, [ColdStartRow("standard", cached_url, 2)], True)
    db = ScrapeDB(DB_PATH); db.init_db(); after = db.conn.execute("SELECT COUNT(*) FROM golden_samples").fetchone()[0]; db.close()
    check("force-fetch bypasses reusable snapshot", resolved[0][3] == "brightdata" and unlocker.calls == 2)
    check("HTML resolution is read-only", before == after, f"before={before}, after={after}")


async def run() -> None:
    await verify_driver()
    await verify_prompt_config_panel()
    await verify_stale_and_cache()


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    print(); print("=" * 76)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 76)
    for name, detail in FAILED:
        print(f"  FAILED: {name}  ({detail})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
