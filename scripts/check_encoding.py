#!/usr/bin/env python3
"""Reject commits / files that carry UTF-8 mojibake or a BOM.

Guards the repo's text files against the failure mode that corrupted
src/scraping/README.md: a UTF-8 file opened and re-saved as GBK/CP936
(Chinese-Windows default) mangles every non-ASCII character into garbage
and prepends a UTF-8 BOM.

Checks, per text file:
  1. No UTF-8 BOM (EF BB BF) at byte 0.
  2. Strict-valid UTF-8; a literal U+FFFD replacement char is a failure.
  3. No known GBK-garbled mojibake characters (see MOJIBAKE).

Marker code points are held as integers (built into characters with chr())
so this script's own source is pure ASCII and never triggers the check it
performs.

Modes:
  --all          scan every tracked file under ROOT (manual / CI use)
  --pre-commit   scan files staged for commit (called by .githooks/pre-commit)

Skips binary files (NUL byte in the first 8 KiB) and .log files (generated
tee artifacts - verify_m1_m3_output.log already carries a BOM).

Pure stdlib, runs anywhere Python 3 runs.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

TEXT_EXTENSIONS = {
    ".md", ".py", ".yaml", ".yml", ".txt", ".json", ".toml", ".ini", ".cfg", ".sh",
}
SKIP_EXTENSIONS = {".log"}
BOM = b"\xef\xbb\xbf"

# Code points produced when UTF-8 bytes are mis-decoded as GBK/CP936 and the
# result is re-encoded to UTF-8. Each maps to the original character(s) it
# came from, so the error message tells you what the file *should* contain.
MOJIBAKE = {
    0x9225: "em/en-dash lead (from em/en-dash)",
    0x922B: "arrow lead (from ->)",
    0x9239: "box-drawing lead (from box-drawing glyphs)",
    0x922E: "math lead (from >=)",
    0x9241: "check-mark lead (from check mark)",
    0x6EBE: "box-drawing byte (from box-drawing glyphs)",
    0x6522: "box-drawing byte (from box-drawing glyphs)",
    0x65BA: "box-drawing byte (from box-drawing glyphs)",
    0x63EC: "trailing byte after a dash",
    0x62AF: "trailing byte after an arrow",
    0x63D7: "trailing byte after a dash",
    0x63C7: "trailing byte after a dash",
    0x6402: "section sign (from section-sign + digit)",
}
MOJIBAKE_CHARS = {chr(cp): origin for cp, origin in MOJIBAKE.items()}


def fail(msg: str) -> None:
    print(f"[check-encoding] {msg}", file=sys.stderr)
    sys.exit(1)


def repo_root(arg_root: Optional[str]) -> Path:
    if arg_root:
        return Path(arg_root)
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    if out.returncode != 0:
        fail("not inside a git repository")
    return Path(out.stdout.strip())


def is_text_target(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in TEXT_EXTENSIONS and suffix not in SKIP_EXTENSIONS


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def check_file(path: Path) -> Tuple[bool, List[str]]:
    """Return (ok, reasons). reasons are empty when the file is clean."""
    data = path.read_bytes()
    if is_binary(data):
        return True, []
    reasons: List[str] = []
    if data.startswith(BOM):
        reasons.append("starts with a UTF-8 BOM (EF BB BF) - remove it")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        reasons.append(f"invalid UTF-8: {e}")
        return False, reasons
    if chr(0xFFFD) in text:
        reasons.append("contains the U+FFFD replacement char - file is corrupted")
    for ch, origin in MOJIBAKE_CHARS.items():
        if ch in text:
            reasons.append(
                f"contains mojibake char U+{ord(ch):04X} ({origin}) - likely GBK-garbled UTF-8"
            )
    return reasons == [], reasons


def collect_files(root: Path, staged_only: bool) -> List[Path]:
    cmd = ["git", "diff", "--cached", "--name-only", "-z"] if staged_only else ["git", "ls-files", "-z"]
    out = subprocess.run(cmd, cwd=root, capture_output=True)
    if out.returncode != 0:
        fail(f"{'git diff --cached' if staged_only else 'git ls-files'} failed")
    return [root / p.decode() for p in out.stdout.split(b"\x00") if p]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check text files for BOM / invalid UTF-8 / mojibake"
    )
    parser.add_argument("--root", help="git repo root (default: git rev-parse)")
    parser.add_argument(
        "--pre-commit", action="store_true",
        help="check staged files (called by .githooks/pre-commit)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="check all tracked text files (manual / CI use)",
    )
    args = parser.parse_args()
    if args.pre_commit and args.all:
        fail("--pre-commit and --all are mutually exclusive")

    root = repo_root(args.root)
    targets = [p for p in collect_files(root, args.pre_commit) if is_text_target(p) and p.exists()]

    problems: List[Tuple[str, List[str]]] = []
    for p in sorted(targets):
        ok, reasons = check_file(p)
        if not ok:
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            problems.append((str(rel), reasons))

    if problems:
        print(
            f"[check-encoding] {len(problems)} file(s) with encoding problems"
            f" (checked {len(targets)}):",
            file=sys.stderr,
        )
        for rel, reasons in problems:
            print(f"  {rel}", file=sys.stderr)
            for r in reasons:
                print(f"    - {r}", file=sys.stderr)
        sys.exit(1)

    print(f"[check-encoding] OK: {len(targets)} text file(s) clean")
    sys.exit(0)


if __name__ == "__main__":
    main()
