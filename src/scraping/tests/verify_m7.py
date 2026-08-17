"""Verification for M7 — sandbox runner.

Covers:
  - Valid parser returns dict
  - AST rejects forbidden imports (os, subprocess, urllib, ...)
  - AST rejects forbidden names (open, eval, exec, __import__)
  - AST rejects dunder attribute access (__globals__)
  - Sandbox catches ZeroDivisionError and reports type_name
  - Infinite loop hits timeout
  - Non-dict return values reported as TypeError
  - Windows setrlimit skipped without breaking

Runs offline; no LLM or network dependencies.
"""

from __future__ import annotations

import asyncio
import platform
import sys
import traceback

from src.scraping.repair import (
    SandboxException,
    SandboxTimeout,
    SandboxViolation,
    run_in_sandbox,
)

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
    print(); print("=" * 70); print(title); print("=" * 70)


async def run() -> None:
    section("M7.1 - Valid parser: returns dict")
    code = """
def parse(html, url):
    import re
    m = re.search(r'<h1[^>]*>([^<]+)</h1>', html)
    return {'title': m.group(1) if m else None}
"""
    r = await run_in_sandbox(code, "<h1>Hello</h1>", "http://x", timeout=5)
    check("valid parser returns dict", isinstance(r, dict) and r.get("title") == "Hello", str(r))

    section("M7.2 - AST rejects forbidden imports")
    for module in ("os", "subprocess", "urllib.request", "socket", "requests"):
        r = await run_in_sandbox(f"import {module}\ndef parse(html, url): return {{}}", "", "", timeout=5)
        check(f"import {module} rejected",
              isinstance(r, SandboxViolation) and module.split('.')[0] in r.reason,
              str(r))

    section("M7.3 - AST rejects forbidden names")
    for name_code, expected in [
        ("open('/tmp/x')", "open"),
        ("eval('1+1')", "eval"),
        ("exec('1+1')", "exec"),
        ("__import__('os')", "__import__"),
    ]:
        code = f"def parse(html, url):\n    {name_code}\n    return {{}}"
        r = await run_in_sandbox(code, "", "", timeout=5)
        check(f"forbidden name '{expected}' rejected",
              isinstance(r, SandboxViolation), str(r))

    section("M7.4 - AST rejects dunder attribute escape ladder")
    dunder_code = "def parse(html, url):\n    return {}.__class__.__mro__[0]"
    r = await run_in_sandbox(dunder_code, "", "", timeout=5)
    check("__class__.__mro__ rejected",
          isinstance(r, SandboxViolation), str(r))

    section("M7.5 - Runtime exception caught")
    r = await run_in_sandbox("def parse(html, url): return 1/0", "", "", timeout=5)
    check("ZeroDivisionError caught",
          isinstance(r, SandboxException) and "ZeroDivisionError" in r.type_name, str(r))

    section("M7.6 - Infinite loop hits timeout")
    r = await run_in_sandbox("def parse(html, url):\n    while True: pass", "", "", timeout=2)
    check("infinite loop -> SandboxTimeout",
          isinstance(r, SandboxTimeout) and r.timeout == 2, str(r))

    section("M7.7 - Non-dict return caught")
    r = await run_in_sandbox("def parse(html, url): return 42", "", "", timeout=5)
    check("non-dict return -> SandboxException (TypeError)",
          isinstance(r, SandboxException) and r.type_name == "TypeError", str(r))

    section("M7.8 - Whitelisted imports allowed")
    for module in ("bs4", "lxml", "re", "json"):
        code = f"def parse(html, url):\n    import {module}\n    return {{'ok': True}}"
        r = await run_in_sandbox(code, "", "", timeout=5)
        check(f"import {module} allowed",
              isinstance(r, dict) and r.get("ok") is True, str(r))

    section("M7.9 - Missing 'parse' function")
    r = await run_in_sandbox("x = 1", "", "", timeout=5)
    check("code without parse() detected",
          isinstance(r, SandboxException) and "parse" in r.message, str(r))

    section("M7.10 - Windows compatibility check")
    is_win = platform.system() == "Windows"
    check("platform detected", True, f"running on {platform.system()}")
    if is_win:
        r = await run_in_sandbox("def parse(html, url): return {'ok': True}", "", "", timeout=5)
        check("sandbox still works on Windows (no setrlimit)",
              isinstance(r, dict) and r.get("ok") is True, str(r))


def main() -> int:
    try:
        asyncio.run(run())
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()

    print(); print("=" * 70)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 70)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
