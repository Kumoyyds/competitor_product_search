"""M23 verification: Argos runtime repair and site-aware page profiles.

Offline and read-only with respect to the project database. Uses the 16 human-
reviewed golden snapshots and the active Tesco/Argos parsers already stored in
``scraping.db``; no BrightData or LLM request is made.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from openpyxl import Workbook

from src.scraping.config import ScrapingConfig, set_config

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "scraping.db"
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
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def _open_readonly() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _expected_kind(expected: dict) -> str | None:
    if expected.get("membership_price") is not None:
        return "membership"
    if (
        expected.get("list_price") is not None
        and expected.get("price") is not None
        and float(expected["list_price"]) > float(expected["price"])
    ):
        return "discount"
    return None


async def verify_goldens() -> None:
    from src.scraping.repair.prepass import detect_promotion
    from src.scraping.repair.sandbox import run_in_sandbox
    from src.scraping.scrapers.html_scraper import _fast_path_sane
    from src.scraping.validation import validate

    section("M23.1 - 16 reviewed golden snapshots")
    conn = _open_readonly()
    try:
        goldens = conn.execute(
            "SELECT id, site, html_snapshot, expected_output "
            "FROM golden_samples WHERE is_stale = 0 ORDER BY site, id"
        ).fetchall()
        parser_rows = conn.execute(
            "SELECT site, code FROM parsers WHERE status = 'active' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    parsers = {row["site"]: row["code"] for row in parser_rows}
    check("golden corpus has 16 samples", len(goldens) == 16, str(len(goldens)))
    check(
        "active parsers exist for Argos and Tesco",
        set(parsers) >= {"argos", "tesco"},
        str(sorted(parsers)),
    )

    for row in goldens:
        sample_id = row["id"]
        site = row["site"]
        expected = json.loads(row["expected_output"])
        expected_kind = _expected_kind(expected)
        soup = BeautifulSoup(row["html_snapshot"], "lxml")
        trusted = {str(expected["price"])} if expected.get("price") is not None else set()
        signal = detect_promotion(soup, trusted, site=site)
        signal_summary = {
            key: None if signal is None else signal.get(key)
            for key in (
                "kind",
                "current_price",
                "reference_price",
                "regular_price",
                "member_price",
            )
        }

        check(
            f"g{sample_id} promotion kind",
            signal is not None and signal.get("kind") == expected_kind,
            f"expected={expected_kind!r} actual={None if signal is None else signal.get('kind')!r}",
        )
        if expected_kind == "discount":
            check(
                f"g{sample_id} anchored discount prices",
                signal.get("current_price") == str(expected["price"])
                and signal.get("reference_price") == str(expected["list_price"]),
                str(signal_summary),
            )
        elif expected_kind == "membership":
            check(
                f"g{sample_id} anchored membership prices",
                signal.get("regular_price") == str(expected["price"])
                and signal.get("member_price") == str(expected["membership_price"]),
                str(signal_summary),
            )
        else:
            check(
                f"g{sample_id} standard price is not contaminated",
                signal.get("current_price") in (None, str(expected.get("price"))),
                str(signal_summary),
            )

        raw = await run_in_sandbox(
            parsers[site], row["html_snapshot"], expected["url"]
        )
        product = None
        errors: list[str] = []
        if isinstance(raw, dict):
            wrapped = dict(raw)
            wrapped.update(
                url=expected["url"],
                website=site,
                source_type="html",
                scraped_at=expected["scraped_at"],
                parser_version="m23_verify",
            )
            product, errors = validate(wrapped)
        check(
            f"g{sample_id} active parser still passes both gates",
            product is not None,
            str(errors or raw) if product is None else "",
        )
        if product is not None:
            check(
                f"g{sample_id} fast path remains trusted",
                _fast_path_sane(raw, product, soup, site) is None,
            )

        fallback = detect_promotion(soup, site=site)
        fallback_kind = None if fallback is None else fallback.get("kind")
        fallback_ok = (
            fallback_kind == expected_kind
            or (expected_kind == "discount" and fallback_kind is None)
        )
        check(
            f"g{sample_id} unanchored fallback remains safe",
            fallback_ok,
            f"expected={expected_kind!r} actual={fallback_kind!r}",
        )


def verify_site_profiles_and_url() -> None:
    from src.scraping.coldstart import read_coldstart_input
    from src.scraping.exceptions import ColdStartInputError
    from src.scraping.repair.prepass import _URL_PID_PATTERNS
    from src.scraping.site_profile import (
        is_mandatory_page_type,
        page_type_available,
    )

    section("M23.2 - site profiles and Argos URL IDs")
    check(
        "Argos membership is unavailable",
        not page_type_available("argos", "membership"),
    )
    check(
        "undeclared site fails open",
        page_type_available("amazon", "membership"),
    )
    check(
        "Tesco out_of_stock remains mandatory",
        is_mandatory_page_type("tesco", "out_of_stock"),
    )
    check(
        "Argos out_of_stock is optional",
        not is_mandatory_page_type("argos", "out_of_stock"),
    )

    match = re.search(_URL_PID_PATTERNS["argos"], "/product/tuc143428469")
    check(
        "Argos alphanumeric product ID is extracted",
        match is not None and match.group(1) == "tuc143428469",
    )

    with tempfile.TemporaryDirectory(prefix="verify_m23_") as temp_dir:
        path = Path(temp_dir) / "argos_membership.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["page_type", "url"])
        sheet.append(["membership", "https://www.argos.co.uk/product/123"])
        workbook.save(path)
        workbook.close()
        try:
            read_coldstart_input(path, "argos")
            message = ""
        except ColdStartInputError as exc:
            message = str(exc)
        check(
            "Argos membership input fails before extraction",
            "unavailable" in message and "membership" in message,
            message if not ("unavailable" in message and "membership" in message) else "",
        )


class _FakeChatOpenAI:
    calls: list[dict] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.calls.append(kwargs)


def verify_thinking_budget() -> None:
    from src.scraping.providers import make_chat_client

    section("M23.3 - DeepSeek thinking output budget")
    set_config(ScrapingConfig(_env_file=None))
    _FakeChatOpenAI.calls.clear()
    with (
        patch("langchain_openai.ChatOpenAI", new=_FakeChatOpenAI),
        patch.object(
            ScrapingConfig, "api_key_for", return_value="offline-test-key"
        ),
    ):
        make_chat_client("deepseek-v4-flash", enable_thinking=False)
        make_chat_client("deepseek-v4-flash", enable_thinking=True)

    plain, thinking = _FakeChatOpenAI.calls
    check(
        "non-thinking DeepSeek cap remains 32768",
        plain.get("extra_body", {}).get("max_tokens") == 32768,
        str(plain.get("extra_body")),
    )
    check(
        "thinking DeepSeek cap is 65536",
        thinking.get("extra_body", {}).get("max_tokens") == 65536,
        str(thinking.get("extra_body")),
    )


async def run() -> None:
    await verify_goldens()
    verify_site_profiles_and_url()
    verify_thinking_budget()


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()

    print()
    print("=" * 74)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 74)
    for name, detail in FAILED:
        print(f"  FAILED: {name}  ({detail})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
