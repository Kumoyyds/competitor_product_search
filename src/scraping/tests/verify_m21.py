"""M21 verification: human-terminated cold-start repair ladder extension.

Fully offline. HTML resolution, review cases, human input, and LLM replies are
deterministic fakes; persistence uses a temporary SQLite database.
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


TMP_DIR = Path(tempfile.mkdtemp(prefix="verify_m21_"))
DB_PATH = TMP_DIR / "m21.db"
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


def set_cfg(*, ladder, temperatures, max_rounds=10) -> None:
    policy = {
        name: False
        for name in ("standard", "out_of_stock", "discounted", "multipack", "membership")
    }
    config_module.set_config(ScrapingConfig(
        db_path=DB_PATH,
        cold_start_page_require_mandatory=policy,
        golden_max_samples_per_page_type=10,
        cold_start_model_ladder=ladder,
        cold_start_temperature_ladder=temperatures,
        cold_start_max_repair_rounds=max_rounds,
        qwen_key="offline-key",
        _env_file=None,
    ))


def reset_db() -> None:
    from src.scraping.storage import ScrapeDB

    db = ScrapeDB(DB_PATH); db.init_db()
    for table in ("parsers", "golden_samples", "scrape_runs"):
        db.conn.execute(f"DELETE FROM {table}")
    db.conn.commit(); db.close()


def db_counts() -> tuple[int, int]:
    from src.scraping.storage import ScrapeDB

    db = ScrapeDB(DB_PATH); db.init_db()
    counts = (
        db.conn.execute("SELECT COUNT(*) FROM parsers").fetchone()[0],
        db.conn.execute("SELECT COUNT(*) FROM golden_samples").fetchone()[0],
    )
    db.close()
    return counts


def product(url: str, *, title="Product", price="12.34"):
    from src.scraping.models import ProductData

    return ProductData(
        url=url,
        website="tesco",
        scraped_at=datetime.now(timezone.utc),
        source_type="html",
        parser_version="coldstart_v1",
        title=title,
        brand="Brand",
        image_urls=["https://images.example/product.jpg"],
        price=Decimal(price),
        currency="GBP",
        in_stock=True,
        availability_raw="In stock",
    )


class FakeScraper:
    site = "tesco"


async def run_driver(
    rows,
    rounds,
    answers,
    *,
    ladder=("node-0", "node-1"),
    temperatures=(0.1, 0.7),
    max_rounds=10,
    llm_results=None,
):
    """Run the real driver/prompt generation with deterministic boundary fakes."""
    from src.scraping.coldstart import run_coldstart

    reset_db()
    set_cfg(ladder=list(ladder), temperatures=list(temperatures), max_rounds=max_rounds)
    answer_iter = iter(answers)
    llm_iter = iter(llm_results or [])
    input_prompts: list[str] = []
    client_calls: list[dict] = []
    llm_prompts: list[list[dict[str, str]]] = []
    case_round = 0

    def fake_input(prompt):
        input_prompts.append(prompt)
        return next(answer_iter)

    async def fake_resolve(scraper, supplied_rows, force_fetch):
        return [
            (row.url, 200, f"<html><h1>{row.url}</h1><span>£12.34</span></html>", "brightdata")
            for row in supplied_rows
        ]

    async def fake_cases(site, supplied_rows, fetched, parser_code):
        nonlocal case_round
        result = rounds[min(case_round, len(rounds) - 1)]
        case_round += 1
        return result

    class Response:
        def __init__(self, content): self.content = content

    class Client:
        async def ainvoke(self, prompt):
            llm_prompts.append(prompt)
            reply = next(llm_iter, f"parser-{len(llm_prompts)}")
            if reply is None:
                return Response("not valid json")
            return Response(json.dumps({"parser_code": reply}))

    def fake_client(**kwargs):
        client_calls.append(kwargs)
        return Client()

    with (
        patch("src.scraping.coldstart._pick_html_scraper", return_value=FakeScraper),
        patch("src.scraping.coldstart._resolve_html", new=fake_resolve),
        patch("src.scraping.coldstart._run_review_cases", new=fake_cases),
        patch("src.scraping.coldstart.make_chat_client", side_effect=fake_client),
    ):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = await run_coldstart("tesco", rows, input_fn=fake_input)
    return result, output.getvalue(), input_prompts, client_calls, llm_prompts


async def verify_repeating_ladder_and_sliding_context() -> None:
    from src.scraping.coldstart import ColdStartRow, _ReviewCase

    section("M21.1 - final rung repeats and feedback stays round-scoped")
    row = ColdStartRow("standard", "https://example/repeat", 2)
    bad = [_ReviewCase(row, "html", product(row.url))]
    rounds = [bad, bad, bad, bad, bad]
    answers: list[str] = []
    for index, action in enumerate(("c", "y", "c", "y"), 1):
        answers.extend(("n", "price", f"expected-{index}", f"hint-{index}", action))
    answers.append("y")
    result, _, _, calls, prompts = await run_driver(
        [row], rounds, answers, ladder=("warmup", "final"), temperatures=(0.2, 0.8)
    )

    check("five rounds converge on a two-rung ladder", result["parser_id"] is not None)
    check("successful extended run seeds its golden", result["seeded_goldens"] == 1)
    check("rounds three-plus repeat the final model", [c["model"] for c in calls] == ["warmup", "final", "final", "final", "final"], str([c["model"] for c in calls]))
    check("repeated final rung keeps its temperature", all(c["temperature"] == 0.8 for c in calls[1:]))
    check("thinking stays enabled on the final rung", all(c["enable_thinking"] is True for c in calls[1:]))

    repair_texts = [json.dumps(prompt, ensure_ascii=False) for prompt in prompts[1:]]
    check("last prompt contains only immediately prior correction", "expected-4" in repair_texts[-1] and "expected-1" not in repair_texts[-1])
    lengths = [len(text) for text in repair_texts]
    check("repair prompt length does not grow monotonically", not all(b > a for a, b in zip(lengths, lengths[1:])), str(lengths))
    check("prompt attempt header uses the true round index", "--- Attempt 4 (final) ---" in repair_texts[-1])


async def verify_exit_actions() -> None:
    from src.scraping.coldstart import ColdStartRow, _ReviewCase, _result_exit_code

    section("M21.2 - continue, quit, and partial-save actions")
    row_a = ColdStartRow("standard", "https://example/a", 2)
    row_b = ColdStartRow("standard", "https://example/b", 3)
    cases = [_ReviewCase(row_a, "html-a", product(row_a.url)), _ReviewCase(row_b, "html-b", product(row_b.url))]

    result, _, _, _, _ = await run_driver([row_a], [cases[:1]], ["q"])
    check("per-case q writes nothing", db_counts() == (0, 0))
    check("per-case q exits 1", _result_exit_code(result) == 1)

    result, _, _, _, _ = await run_driver([row_a], [cases[:1]], ["n", "price", "10", "", "q"])
    check("round-prompt q writes nothing", db_counts() == (0, 0))
    check("round-prompt q exits 1", _result_exit_code(result) == 1)

    result, _, _, _, _ = await run_driver(
        [row_a, row_b], [cases], ["y", "n", "price", "10", "", "s"]
    )
    check("s persists current parser and accepted goldens", db_counts() == (1, 1), str(db_counts()))
    check("s marks the result partial", result["partial"] is True)
    check("partial save exits 2", _result_exit_code(result) == 2)


async def verify_ledger_and_regression() -> None:
    from src.scraping.coldstart import ColdStartRow, _ReviewCase, _update_review_ledger

    section("M21.3 - resolved ledger and regression warning")
    a = ColdStartRow("standard", "https://example/ledger-a", 2)
    b = ColdStartRow("standard", "https://example/ledger-b", 3)
    c = ColdStartRow("standard", "https://example/ledger-c", 4)
    rounds = [
        [_ReviewCase(a, "a", product(a.url)), _ReviewCase(b, "b", product(b.url)), _ReviewCase(c, "c", product(c.url, title="bad-c1"))],
        [_ReviewCase(a, "a", product(a.url, price="10")), _ReviewCase(b, "b", product(b.url)), _ReviewCase(c, "c", product(c.url, title="bad-c2"))],
        [_ReviewCase(a, "a", product(a.url, price="11")), _ReviewCase(b, "b", product(b.url)), _ReviewCase(c, "c", product(c.url, title="C"))],
        [_ReviewCase(a, "a", product(a.url, price="10")), _ReviewCase(b, "b", product(b.url)), _ReviewCase(c, "c", product(c.url, title="C"))],
    ]
    answers = [
        "n", "price", "10", "hint-a", "y", "n", "title", "C", "hint-c1", "c",
        "y", "n", "title", "C", "hint-c2", "c",
        "n", "price", "10", "regressed-a", "y", "c",
        "y",
    ]
    result, output, _, _, prompts = await run_driver([a, b, c], rounds, answers)
    texts = [json.dumps(prompt, ensure_ascii=False) for prompt in prompts]

    check("ledger scenario converges", result["parser_id"] is not None)
    check("resolved ledger carries accepted url-field pair", "【历史已修复，保持现状勿回退】" in texts[2] and a.url in texts[2] and "price" in texts[2])
    check("stale correction text is absent from later prompt", "hint-a" not in texts[2])
    check("regression block names regressed url and field", "【回退警告 — 上一轮修复弄坏了已通过的字段】" in texts[3] and a.url in texts[3] and "price" in texts[3])
    check("console surfaces regression explicitly", "[REGRESSION]" in output and a.url in output)
    check("newly resolved field remains in compact ledger", "【历史已修复，保持现状勿回退】" in texts[3] and c.url in texts[3] and "title" in texts[3])

    resolved = {a.url: {"price"}}
    sandbox_regressions = _update_review_ledger(
        reported={}, resolved=resolved, previously_accepted={a.url}, accepted=[],
        sandbox_failed=[(a, "boom")], human_rejected=[],
    )
    check("sandbox crash marks a previously passing URL as regressed", sandbox_regressions[a.url] == {"price", "<sandbox>"})
    check("sandbox regression removes unverified fields from resolved ledger", not resolved[a.url])


async def verify_guards() -> None:
    from src.scraping.coldstart import ColdStartRow, _ReviewCase

    section("M21.4 - safety cap and unusable-reply guard")
    row = ColdStartRow("standard", "https://example/guard", 2)
    bad = [_ReviewCase(row, "html", product(row.url))]
    result, output, input_prompts, _, _ = await run_driver(
        [row], [bad], ["n", "price", "10", "", "q"], max_rounds=1
    )
    cap_prompts = [prompt for prompt in input_prompts if "保存当前结果" in prompt]
    check("max-round cap is reported", "safety cap reached (1 rounds)" in output)
    check("cap prompt offers only save or quit", len(cap_prompts) == 1 and "s=" in cap_prompts[0] and "q=" in cap_prompts[0] and "c=" not in cap_prompts[0])
    check("quitting at cap does not persist", result["parser_id"] is None and db_counts() == (0, 0))

    result, output, _, calls, _ = await run_driver(
        [row], [bad], [], ladder=("one",), temperatures=(0.3,), llm_results=[None, None]
    )
    check("two consecutive unusable replies abort", result["aborted"] is True and result["parser_id"] is None)
    check("unusable-reply guard stops after two calls", len(calls) == 2)
    check("unusable-reply abort is explicit", "twice consecutively" in output)

    result, _, _, _, prompts = await run_driver(
        [row], [bad, bad],
        ["n", "price", "10", "", "c", "y"],
        llm_results=["stable-parser", None, "recovered-parser"],
    )
    repair_texts = [json.dumps(prompt, ensure_ascii=False) for prompt in prompts[1:]]
    check("one unusable repair reply can recover on the next round", result["parser_id"] is not None)
    check("unusable repair reply retains current parser code", all("stable-parser" in text for text in repair_texts))


async def run() -> None:
    await verify_repeating_ladder_and_sliding_context()
    await verify_exit_actions()
    await verify_ledger_and_regression()
    await verify_guards()


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
