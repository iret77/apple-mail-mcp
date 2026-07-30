#!/usr/bin/env python3
"""Find or remove the code that must not travel upstream.

This fork carries a few pieces that describe its own distribution — the
`.mcpb` launcher, the build stamp that answers "which build is
answering me" when a bundle tracks a moving git ref. Upstream ships
through PyPI and has none of that; carrying it there would make the
maintainer own code for a distribution that does not exist.

The plan used to list these by line number, which goes stale on the
next edit. They are marked in the source instead:

    # fork-only:start
    ...
    # fork-only:end

    something()  # fork-only

    x = "plain"  # fork-only-replace: <what the fork version did>

Usage:
    scripts/fork-only.py list            # what is marked, and where
    scripts/fork-only.py strip <dir>     # write a stripped copy
    scripts/fork-only.py check           # markers balanced?

`strip` copies the tree and removes the marked regions, so an upstream
branch can be cut mechanically instead of from memory.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

START = "# fork-only:start"
END = "# fork-only:end"
LINE = "# fork-only"
REPLACE = "# fork-only-replace:"

SEARCH_DIRS = ("src", "tests", "scripts")
SUFFIXES = {".py", ".js", ".md", ".sh"}


def _files(root: Path):
    for d in SEARCH_DIRS:
        for p in sorted((root / d).rglob("*")):
            if p.name == Path(__file__).name:
                continue  # the tool describes the markers; skip itself
            if p.is_file() and p.suffix in SUFFIXES:
                yield p


def _regions(text: str) -> list[tuple[int, int]]:
    """Line index ranges (inclusive) covered by start/end markers."""
    out: list[tuple[int, int]] = []
    open_at: int | None = None
    for i, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith(START):
            if open_at is not None:
                raise SystemExit(f"nested {START} at line {i + 1}")
            open_at = i
        elif stripped.startswith(END):
            if open_at is None:
                raise SystemExit(f"unmatched {END} at line {i + 1}")
            out.append((open_at, i))
            open_at = None
    if open_at is not None:
        raise SystemExit(f"unclosed {START} at line {open_at + 1}")
    return out


def cmd_list(root: Path) -> int:
    total = 0
    for p in _files(root):
        text = p.read_text()
        if LINE not in text:
            continue
        lines = text.splitlines()
        for a, b in _regions(text):
            note = lines[a].strip()[len(START):].lstrip(" —-")
            print(f"{p.relative_to(root)}:{a + 1}-{b + 1}  block"
                  f"{('  ' + note) if note else ''}")
            total += b - a + 1
        for i, line in enumerate(lines):
            s = line.strip()
            if REPLACE in line:
                print(f"{p.relative_to(root)}:{i + 1}  replace  "
                      f"{line.split(REPLACE, 1)[1].strip()}")
                total += 1
            elif LINE in line and not s.startswith((START, END)) \
                    and REPLACE not in line and not s.startswith(LINE):
                print(f"{p.relative_to(root)}:{i + 1}  line")
                total += 1
    print(f"\n{total} marked line(s).")
    return 0


def cmd_check(root: Path) -> int:
    for p in _files(root):
        _regions(p.read_text())  # raises on imbalance
    print("markers balanced")
    return 0


def cmd_strip(root: Path, dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        root, dest,
        ignore=shutil.ignore_patterns(
            ".git", "mcpb", "dist", "__pycache__", ".venv", ".pytest_cache",
        ),
    )
    (dest / "UPSTREAM_PR_PLAN.md").unlink(missing_ok=True)
    shutil.rmtree(dest / "upstream-prs", ignore_errors=True)
    removed = 0
    for p in _files(dest):
        text = p.read_text()
        if LINE not in text:
            continue
        lines = text.splitlines(keepends=True)
        drop = set()
        for a, b in _regions(text):
            drop.update(range(a, b + 1))
        kept = []
        for i, line in enumerate(lines):
            if i in drop:
                removed += 1
                continue
            s = line.strip()
            if REPLACE in line:
                kept.append(line.split("#", 1)[0].rstrip() + "\n")
                continue
            if LINE in line and not s.startswith((START, END, LINE)):
                removed += 1
                continue
            kept.append(line)
        p.write_text("".join(kept))
    print(f"stripped {removed} line(s) into {dest}")
    return 0


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    args = sys.argv[1:]
    if not args or args[0] == "list":
        return cmd_list(root)
    if args[0] == "check":
        return cmd_check(root)
    if args[0] == "strip":
        if len(args) < 2:
            print("usage: fork-only.py strip <dir>", file=sys.stderr)
            return 2
        return cmd_strip(root, Path(args[1]).resolve())
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
