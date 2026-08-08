"""Sandbox runner for candidate parser code (spec §5.6, D14).

Two entry points in one file:
  - Parent (called from HTMLScraper / repair agent):
      `await run_in_sandbox(code, html, url, timeout) -> dict | Sandbox<Kind>`
  - Child (subprocess entry):
      `python -m src.scraping.repair.sandbox`  reads JSON from stdin, prints JSON to stdout.

Design decisions:
  - D14: stdlib only (subprocess + timeout + AST scan + setrlimit on POSIX)
  - AST whitelist: bs4, lxml, re, json (from cfg.sandbox_import_whitelist)
  - Windows fallback: setrlimit unavailable, timeout is only isolation. Documented Phase 0 compromise.
  - The sandbox only checks "can this code run safely and return a dict?"; correctness (against golden)
    is a separate concern handled by src/scraping/repair/golden.py.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import platform
import sys
import traceback
from dataclasses import dataclass
from typing import Any, Optional

from ..config import get_config

logger = logging.getLogger(__name__)

_FORBIDDEN_NAMES = {
    "open", "eval", "exec", "compile", "__import__",
    "input", "exit", "quit", "breakpoint",
}
_FORBIDDEN_ATTR_ROOTS = {
    "os", "sys", "socket", "subprocess", "shutil", "pathlib",
    "urllib", "http", "requests", "builtins", "importlib",
    "ctypes", "resource", "signal",
}
_FORBIDDEN_DUNDER_ATTRS = {
    "__globals__", "__code__", "__class__", "__subclasses__",
    "__mro__", "__builtins__", "__bases__", "__dict__",
    "__getattribute__", "__reduce__",
}


@dataclass(frozen=True)
class SandboxViolation:
    reason: str


@dataclass(frozen=True)
class SandboxTimeout:
    timeout: float


@dataclass(frozen=True)
class SandboxException:
    type_name: str
    message: str
    traceback: str


def _ast_scan(code: str, whitelist: set[str]) -> Optional[str]:
    """Return reason string on rejection, None on pass."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"syntax_error: {e}"

    for node in ast.walk(tree):
        # Import checks
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in whitelist:
                    return f"forbidden_import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                return "forbidden_import: relative import"
            root = node.module.split(".")[0]
            if root not in whitelist:
                return f"forbidden_import: {node.module}"

        # Bare-name calls (open, eval, exec, __import__)
        if isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            return f"forbidden_name: {node.id}"

        # Attribute access — catches os.system, sys.exit even without imports
        if isinstance(node, ast.Attribute):
            root = _attr_root(node)
            if root and root in _FORBIDDEN_ATTR_ROOTS:
                return f"forbidden_attr_root: {root}"
            if node.attr in _FORBIDDEN_DUNDER_ATTRS:
                return f"forbidden_dunder: {node.attr}"

    return None


def _attr_root(node: ast.Attribute) -> Optional[str]:
    """Return the leftmost Name in an Attribute chain (e.g. os.path.join -> 'os')."""
    while isinstance(node, ast.Attribute):
        node = node.value  # type: ignore[assignment]
    if isinstance(node, ast.Name):
        return node.id
    return None


async def run_in_sandbox(
    code: str,
    html: str,
    url: str,
    timeout: Optional[float] = None,
) -> dict[str, Any] | SandboxViolation | SandboxTimeout | SandboxException:
    """Run candidate parser code in an isolated subprocess.

    Returns:
        dict — parser's return value if the run succeeded and produced JSON-serializable dict
        SandboxViolation — AST scan rejected the code (never spawned subprocess)
        SandboxTimeout — subprocess exceeded timeout
        SandboxException — parser raised or produced invalid output
    """
    cfg = get_config()
    if timeout is None:
        timeout = float(cfg.sandbox_timeout)

    whitelist = set(cfg.sandbox_import_whitelist)
    violation = _ast_scan(code, whitelist)
    if violation:
        return SandboxViolation(reason=violation)

    payload = json.dumps({"code": code, "html": html, "url": url})

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.scraping.repair.sandbox",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=payload.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return SandboxTimeout(timeout=timeout)

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if not stdout:
        return SandboxException(
            type_name="SandboxInfra",
            message="child produced no stdout",
            traceback=stderr,
        )

    try:
        result = json.loads(stdout.splitlines()[-1])
    except json.JSONDecodeError as e:
        return SandboxException(
            type_name="SandboxInfra",
            message=f"child stdout not JSON: {e}",
            traceback=stdout[:2000],
        )

    if result.get("ok"):
        value = result.get("value")
        if isinstance(value, dict):
            return value
        return SandboxException(
            type_name="TypeError",
            message=f"parse() must return dict, got {type(value).__name__}",
            traceback="",
        )
    else:
        return SandboxException(
            type_name=result.get("error_type", "Exception"),
            message=result.get("error_message", "unknown"),
            traceback=result.get("traceback", ""),
        )


def _run_child() -> None:
    """Subprocess entry point. Reads JSON payload from stdin, prints JSON to stdout."""
    # Apply POSIX resource limits when available; silently skip on Windows.
    if platform.system() != "Windows":
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        except (ImportError, ValueError, OSError):
            pass

    try:
        payload = json.loads(sys.stdin.read())
        code = payload["code"]
        html = payload["html"]
        url = payload["url"]
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error_type": "SandboxInfra",
            "error_message": f"cannot read payload: {e}",
            "traceback": traceback.format_exc(),
        }))
        return

    namespace: dict[str, Any] = {}
    try:
        exec(compile(code, "<candidate>", "exec"), namespace)
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }))
        return

    parse_fn = namespace.get("parse")
    if not callable(parse_fn):
        print(json.dumps({
            "ok": False,
            "error_type": "AttributeError",
            "error_message": "candidate code has no callable 'parse'",
            "traceback": "",
        }))
        return

    try:
        result = parse_fn(html, url)
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }))
        return

    try:
        serialized = json.dumps({"ok": True, "value": result}, default=str)
    except (TypeError, ValueError) as e:
        print(json.dumps({
            "ok": False,
            "error_type": "TypeError",
            "error_message": f"parse() result not JSON serializable: {e}",
            "traceback": "",
        }))
        return

    print(serialized)


if __name__ == "__main__":
    _run_child()
