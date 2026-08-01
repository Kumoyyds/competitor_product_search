#!/usr/bin/env python3
"""Bidirectional sync between CLAUDE.md and AGENTS.md.

Codex reads AGENTS.md, Claude Code reads CLAUDE.md; both tools apply a
"nearest-directory-first" rule, so the two files in each directory must be
kept byte-identical. This script keeps them in sync in both directions.

Modes:
  --bootstrap   (default) copy every CLAUDE.md -> AGENTS.md, or the
                reverse for pairs that only have AGENTS.md. On any
                difference CLAUDE.md wins. Used once when the repo is
                set up, and for fresh checkouts that need repair.
  --pre-commit  called by the pre-commit hook (.githooks/pre-commit):
                copy whichever file of each pair changed since HEAD to
                the other, then `git add` both so the commit lands with
                them in sync. If BOTH changed and differ -- a real
                conflict -- abort the commit (exit 1) and let a human
                reconcile the two files.

Pure stdlib, so it runs anywhere Python 3 runs (macOS/Linux/Windows).
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".idea", ".vscode", ".claude",
}


def fail(msg: str) -> None:
    print(f"[sync-agent-docs] {msg}", file=sys.stderr)
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


def find_pairs(root: Path) -> List[Tuple[Path, Path]]:
    pairs = []
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        d = Path(dirpath)
        c, a = d / "CLAUDE.md", d / "AGENTS.md"
        if c.exists() or a.exists():
            pairs.append((c, a))
    return pairs


def run_git(root: Path, *args: str) -> int:
    return subprocess.run(["git", *args], cwd=root).returncode


def rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def tracked(path: Path, root: Path) -> bool:
    return run_git(root, "ls-files", "--error-unmatch", "--", rel(path, root)) == 0


def changed_since_head(path: Path, root: Path) -> bool:
    """True if the file differs from HEAD (including new or deleted)."""
    if not path.exists():
        # Deleted in worktree but present in HEAD -> changed.
        out = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel(path, root)}"],
            cwd=root, capture_output=True,
        )
        return out.returncode == 0
    if not tracked(path, root):
        return True  # untracked -> new
    return run_git(root, "diff", "--quiet", "HEAD", "--", rel(path, root)) != 0


def copy(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)


def stage(root: Path, *paths: Path) -> None:
    rels = [rel(p, root) for p in paths]
    if run_git(root, "add", "--", *rels) != 0:
        fail(f"git add failed for {rels}")


def sync_bootstrap(c: Path, a: Path, root: Path) -> None:
    if c.exists() and a.exists() and c.read_bytes() == a.read_bytes():
        return
    if c.exists():
        copy(c, a)
        print(f"  CLAUDE.md -> AGENTS.md: {rel(c, root) or '.'}")
    elif a.exists():
        copy(a, c)
        print(f"  AGENTS.md -> CLAUDE.md: {rel(a, root) or '.'}")


def sync_pre_commit(c: Path, a: Path, root: Path) -> None:
    c_exists, a_exists = c.exists(), a.exists()
    if c_exists and a_exists and c.read_bytes() == a.read_bytes():
        return
    where = rel(c, root) or "."

    c_changed = changed_since_head(c, root)
    a_changed = changed_since_head(a, root)

    if c_changed and not a_changed:
        if c_exists:
            copy(c, a)
            print(f"  synced CLAUDE.md -> AGENTS.md: {where}")
        else:
            copy(a, c)
            print(f"  recreated CLAUDE.md from AGENTS.md: {where}")
        stage(root, c, a)
    elif a_changed and not c_changed:
        if a_exists:
            copy(a, c)
            print(f"  synced AGENTS.md -> CLAUDE.md: {where}")
        else:
            copy(c, a)
            print(f"  recreated AGENTS.md from CLAUDE.md: {where}")
        stage(root, c, a)
    elif c_changed and a_changed:
        fail(
            f"conflict in {where}: both CLAUDE.md and AGENTS.md changed since "
            "HEAD and differ. Reconcile them to identical content, then stage "
            "and commit again."
        )
    else:
        # Neither changed since HEAD, but they differ (pre-existing drift
        # committed earlier). Converge toward CLAUDE.md.
        if c_exists:
            copy(c, a)
            print(f"  repaired drift CLAUDE.md -> AGENTS.md: {where}")
        else:
            copy(a, c)
            print(f"  repaired drift AGENTS.md -> CLAUDE.md: {where}")
        stage(root, c, a)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync CLAUDE.md <-> AGENTS.md")
    parser.add_argument("--root", help="git repo root (default: git rev-parse)")
    parser.add_argument(
        "--pre-commit", action="store_true",
        help="pre-commit hook mode: bidirectional sync, abort on conflict",
    )
    args = parser.parse_args()

    root = repo_root(args.root)
    pairs = find_pairs(root)
    if not pairs:
        print("[sync-agent-docs] no CLAUDE.md/AGENTS.md pairs found")
        return

    print(f"[sync-agent-docs] syncing agent docs in {root}")
    for c, a in pairs:
        if args.pre_commit:
            sync_pre_commit(c, a, root)
        else:
            sync_bootstrap(c, a, root)


if __name__ == "__main__":
    main()
