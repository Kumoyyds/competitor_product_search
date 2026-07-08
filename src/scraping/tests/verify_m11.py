"""Verification for M11 — cold start CLI.

Uses REAL DeepSeek API. Uses local HTML samples (mocks BrightData fetch).

Covers:
  - End-to-end cold start seeds parser + goldens on user accept
  - 'q' aborts the review loop, nothing seeded
  - 'n' skips a URL, only accepted URLs become goldens
  - page_type classification during seeding
  - _pick_representative_html picks largest HTML
  - _read_urls parses a URL file

Cost: 1-2 DeepSeek calls per full run.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

_DB_PATH = os.path.join(tempfile.gettempdir(), "verify_m11.db")
if os.path.exists(_DB_PATH):
    os.remove(_DB_PATH)
os.environ["SCRAPING_DB_PATH"] = _DB_PATH

from dotenv import load_dotenv
load_dotenv()

from src.scraping import config as _config
_config._config = None
cfg = _config.get_config()

DATA_DIR = Path(__file__).parent.parent / "data"
HAS_LLM = bool(cfg.deepseek_key)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def skip(name: str, reason: str) -> None:
    SKIPPED.append(name)
    print(f"  [SKIP] {name}  ({reason})")


def section(title: str) -> None:
    print(); print("=" * 70); print(title); print("=" * 70)


async def run() -> None:
    from src.scraping.coldstart import (
        _pick_representative_html,
        _read_urls,
        run_coldstart,
    )
    from src.scraping.storage import GoldenStore, ParserStore, ScrapeDB
    from src.scraping.extraction import bright_data as bd_mod

    section("M11.1 - _read_urls parses text file")
    fd, tmp_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    tmp = Path(tmp_path)
    tmp.write_text(
        "# comment line\n"
        "https://www.argos.co.uk/product/1\n"
        "\n"
        "https://www.argos.co.uk/product/2\n",
        encoding="utf-8",
    )
    urls = _read_urls(tmp)
    check("comment stripped", "# comment line" not in urls[0])
    check("2 URLs read (blanks/comments skipped)", len(urls) == 2, str(urls))
    tmp.unlink()

    section("M11.2 - _pick_representative_html picks largest OK HTML")
    fetched = [
        ("http://a", 200, "x" * 100),
        ("http://b", 200, "y" * 500),  # largest 200 OK
        ("http://c", 404, ""),
        ("http://d", 200, "z" * 50),
    ]
    rep = _pick_representative_html(fetched)
    check("largest 200 OK returned", rep and len(rep) == 500, f"len={len(rep) if rep else 0}")

    fetched_all_bad = [("http://a", 404, ""), ("http://b", 500, "")]
    rep = _pick_representative_html(fetched_all_bad)
    check("None when no OK", rep is None)

    if not HAS_LLM:
        skip("M11.3-M11.5", "DEEPSEEK_KEY not set")
        return

    section("M11.3 - Cold start with 'y y' accepts both URLs")
    argos_html_1 = (DATA_DIR / "argos_response_1.html").read_text(encoding="utf-8")
    argos_html_2 = (DATA_DIR / "argos_response_2.html").read_text(encoding="utf-8")

    fake_fetch_map = {
        "https://www.argos.co.uk/product/3284476": (200, argos_html_1),
        "https://www.argos.co.uk/product/8747437": (200, argos_html_2),
    }

    async def _fake_fetch(self, url):
        return fake_fetch_map.get(url, (0, ""))

    # Mock BrightData Unlocker.fetch
    with patch.object(bd_mod.BrightDataUnlocker, "fetch", new=_fake_fetch):
        # Simulate user typing "y" for both URLs
        inputs = iter(["y", "y"])
        result = await run_coldstart(
            site="argos",
            urls=list(fake_fetch_map.keys()),
            input_fn=lambda prompt: next(inputs),
        )

    check("parser_id created", result["parser_id"] is not None, f"id={result['parser_id']}")
    check("2 goldens seeded", result["seeded_goldens"] == 2,
          f"seeded={result['seeded_goldens']}")
    check("accepted count = 2", result["accepted"] == 2, str(result["accepted"]))
    check("not aborted", not result["aborted"])

    db = ScrapeDB(_DB_PATH); db.init_db()
    parsers = ParserStore(db).get_active("argos")
    check("parser row in DB (created_by=initial)",
          any(p["created_by"] == "initial" for p in parsers),
          str([(p["id"], p["created_by"]) for p in parsers]))
    coverage = GoldenStore(db).get_page_type_coverage("argos")
    check("goldens present in coverage",
          sum(coverage.values()) == 2, str(coverage))
    db.close()

    section("M11.4 - Cold start with 'q' aborts review loop")
    # Clean DB
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM parsers WHERE site='argos'")
    db.conn.execute("DELETE FROM golden_samples WHERE site='argos'")
    db.conn.commit()
    db.close()

    with patch.object(bd_mod.BrightDataUnlocker, "fetch", new=_fake_fetch):
        inputs = iter(["q"])
        result = await run_coldstart(
            site="argos",
            urls=list(fake_fetch_map.keys()),
            input_fn=lambda prompt: next(inputs),
        )

    check("q aborts: no parser seeded", result["parser_id"] is None,
          f"id={result['parser_id']}")
    check("q aborts: no goldens seeded", result["seeded_goldens"] == 0,
          str(result["seeded_goldens"]))

    section("M11.5 - Cold start with 'n y': one accepted, one skipped")
    db = ScrapeDB(_DB_PATH); db.init_db()
    db.conn.execute("DELETE FROM parsers WHERE site='argos'")
    db.conn.execute("DELETE FROM golden_samples WHERE site='argos'")
    db.conn.commit()
    db.close()

    with patch.object(bd_mod.BrightDataUnlocker, "fetch", new=_fake_fetch):
        inputs = iter(["n", "y"])
        result = await run_coldstart(
            site="argos",
            urls=list(fake_fetch_map.keys()),
            input_fn=lambda prompt: next(inputs),
        )

    check("parser_id created", result["parser_id"] is not None)
    check("only 1 accepted", result["accepted"] == 1, str(result["accepted"]))
    check("only 1 golden seeded", result["seeded_goldens"] == 1,
          str(result["seeded_goldens"]))


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()
    finally:
        if os.path.exists(_DB_PATH):
            os.remove(_DB_PATH)

    print(); print("=" * 70)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    print("=" * 70)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
