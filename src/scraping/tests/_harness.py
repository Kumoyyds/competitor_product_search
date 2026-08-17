"""Temporary shared runner for the legacy ``verify_mN`` scripts.

The scripts remain runnable during the staged pytest migration, but their
reporting and temporary resources now have one implementation. Delete this
module after the final legacy script has been migrated.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import os
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
SKIPPED: list[tuple[str, str]] = []
_TEMP_DIRS: list[tempfile.TemporaryDirectory[str]] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    rendered = str(detail) if detail else ""
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({rendered})" if rendered else ""))
    else:
        FAILED.append((name, rendered))
        print(f"  [FAIL] {name}  ({rendered})")


def section(title: str, width: int = 72) -> None:
    print()
    print("=" * width)
    print(title)
    print("=" * width)


def skip(name: str, reason: str = "") -> None:
    SKIPPED.append((name, reason))
    print(f"  [SKIP] {name}" + (f"  ({reason})" if reason else ""))


def use_temp_scrape_db(label: str) -> str:
    """Configure an auto-cleaned per-process scraping database."""
    directory = tempfile.TemporaryDirectory(prefix=f"{label}_")
    _TEMP_DIRS.append(directory)
    path = str(Path(directory.name) / "scraping.db")
    os.environ["SCRAPING_DB_PATH"] = path
    return path


def temp_workdir(label: str) -> Path:
    """Return an auto-cleaned working directory for a legacy script."""
    directory = tempfile.TemporaryDirectory(prefix=f"{label}_")
    _TEMP_DIRS.append(directory)
    return Path(directory.name)


def _cleanup() -> None:
    while _TEMP_DIRS:
        _TEMP_DIRS.pop().cleanup()


atexit.register(_cleanup)


def _invoke(verifier: Callable[[], Any]) -> None:
    result = verifier()
    if inspect.isawaitable(result):
        asyncio.run(result)


def run_main(
    *verifiers: Callable[[], Any],
    title: str | None = None,
    width: int = 72,
) -> int:
    """Run sync/async verification sections and print one standard summary."""
    PASSED.clear()
    FAILED.clear()
    SKIPPED.clear()
    if title:
        print(title)

    for verifier in verifiers:
        try:
            _invoke(verifier)
        except Exception:
            FAILED.append((verifier.__name__, "EXCEPTION"))
            print(f"  [EXCEPTION] {verifier.__name__}")
            traceback.print_exc()
    print()
    print("=" * width)
    summary = f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed"
    if SKIPPED:
        summary += f", {len(SKIPPED)} skipped"
    print(summary)
    print("=" * width)
    for name, detail in FAILED:
        print(f"  FAILED: {name}  ({detail})")
    for name, reason in SKIPPED:
        print(f"  SKIPPED: {name}" + (f"  ({reason})" if reason else ""))
    return 1 if FAILED else 0
