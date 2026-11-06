#!/usr/bin/env python3
"""CI check for opsec-leaking Cyrillic.

CLAUDE.md draws the line like this: Russian in the i18n dictionaries is DELIBERATE (the site
publicly offers RU), Russian in comments and logs is an OPSEC LEAK, and Russian on an EN page is a
bug. check_i18n.py enforces the third one. This file enforces the second, which until now was
enforced by nobody: check_i18n.py only looks at tr()-strings and templates, so `web/static/**`,
shell scripts and literals inside `log.*()` were invisible to automation. That is not theoretical
— `/static/style.css` served 49 lines of Russian comments to anyone who asked for it until
2026-07-27, and `infra/backups/backup-anonvm.sh` was still writing Russian into the anonvm journal
on 2026-08-04, months after the cleanup that was believed to be complete.

The check is deliberately split into three strictness levels instead of one blanket rule, because a
blanket rule would either drown in false positives or have to exempt whole files — and a file-level
exemption blinds the check on that file forever (the same trap check_i18n.py documents for its
per-kwarg admin exemption).

  STRICT   — no Cyrillic at all, `ru-ok` is NOT honoured. For surfaces an outsider can read:
             web/static/** goes verbatim to every visitor, and node-agent/** lands on the exit
             nodes, where seizure hands over the whole tree (CLAUDE.md rule 4).
  MARKED   — Cyrillic only on a line carrying a `ru-ok` marker. For infra/ and scripts/, where
             operator-facing Telegram texts legitimately live next to code that must stay English.
  TARGETED — only comments and `log.*()` literals are checked. For the application Python, where
             ordinary Russian string literals are legitimate (tr() keys, the RU-only admin console,
             the Telegram bot) and are already covered by check_i18n.py.

Marker syntax (MARKED only):
    ...code...                     # ru-ok: <reason>      one line
    # ru-ok:begin <reason>  ...  # ru-ok:end               a block, for multi-line strings whose
                                                          content cannot carry a comment
A reason is required: `ru-ok` on its own is rejected. The point of the marker is to record WHY the
Russian is intentional, so the next reader does not have to re-derive it.

Run: control-plane/.venv/bin/python scripts/check_no_cyrillic.py     (exit 1 = something leaked)
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CYRILLIC = re.compile("[Ѐ-ӿ]")  # ru-ok: the pattern itself, escaped so it is not literal
MARKER = re.compile(r"ru-ok(?::begin|:end)?\s*:?\s*(?P<reason>.*)")
MARKER_ANY = re.compile(r"ru-ok\b")
MARKER_BEGIN = re.compile(r"ru-ok:begin\b(?P<reason>.*)")
MARKER_END = re.compile(r"ru-ok:end\b")

# Files that are documentation rather than code. CLAUDE.md keeps docs/ and the operator runbooks in
# Russian on purpose; they are prose for the operator, not strings an outsider parses out of a
# binary. Reviewing them is part of the pre-publication pass, not of this gate.
DOC_SUFFIXES = {".md", ".markdown", ".txt"}

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
# Nothing here is text we can meaningfully scan.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".age", ".gz", ".tgz",
                 ".zip", ".pyc", ".woff", ".woff2", ".ttf"}

STRICT_ROOTS = [
    ("web/static", "served verbatim to every visitor, including over .onion"),
    ("node-agent", "ships to the exit nodes — seizure hands over the whole tree (rule 4)"),
]
MARKED_ROOTS = [
    ("infra", "runs on anonvm and the operator's Mac; only Telegram texts may be Russian"),
    ("scripts", "dev tooling that gets published together with the repo"),
]
TARGETED_ROOTS = [
    ("control-plane/app", "app code: comments and logs only — tr() keys stay Russian"),
    ("support-bot", "bot code: comments and logs only — operator replies stay Russian"),
]

LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _iter_files(root: Path):
    if not root.exists():
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIXES or p.suffix.lower() in DOC_SUFFIXES:
            continue
        yield p


def _read(p: Path) -> list[str] | None:
    try:
        return p.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable — nothing to check


def check_strict(rel_root: str, why: str, findings: list[str]) -> None:
    for p in _iter_files(ROOT / rel_root):
        lines = _read(p)
        if lines is None:
            continue
        for n, line in enumerate(lines, 1):
            if CYRILLIC.search(line):
                rel = p.relative_to(ROOT)
                note = " (a ru-ok marker does NOT apply here)" if MARKER_ANY.search(line) else ""
                findings.append(f"{rel}:{n}: Cyrillic in a STRICT surface — {why}{note}\n"
                                f"    {line.strip()[:110]}")


def check_marked(rel_root: str, why: str, findings: list[str]) -> None:
    for p in _iter_files(ROOT / rel_root):
        lines = _read(p)
        if lines is None:
            continue
        rel = p.relative_to(ROOT)
        in_block = False
        for n, line in enumerate(lines, 1):
            begin = MARKER_BEGIN.search(line)
            if begin:
                if not begin.group("reason").strip():
                    findings.append(f"{rel}:{n}: `ru-ok:begin` without a reason — say why the "
                                    f"Russian is intentional")
                in_block = True
                continue
            if MARKER_END.search(line):
                in_block = False
                continue
            if not CYRILLIC.search(line):
                continue
            if in_block:
                continue
            m = MARKER.search(line)
            if not m:
                findings.append(f"{rel}:{n}: unmarked Cyrillic — {why}\n"
                                f"    {line.strip()[:110]}\n"
                                f"    (if it is deliberate, append `# ru-ok: <reason>`)")
            elif not m.group("reason").strip():
                findings.append(f"{rel}:{n}: `ru-ok` without a reason — say why it is intentional\n"
                                f"    {line.strip()[:110]}")


def _log_call_strings(tree: ast.AST):
    """Yield (lineno, text) for every string that reaches a log.*() call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in LOG_METHODS:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield (getattr(sub, "lineno", node.lineno), sub.value)


def check_targeted(rel_root: str, why: str, findings: list[str]) -> None:
    for p in _iter_files(ROOT / rel_root):
        if p.suffix != ".py":
            continue
        src = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT)
        lines = src.splitlines()

        def marked(lineno: int) -> bool:
            return bool(lineno <= len(lines) and MARKER_ANY.search(lines[lineno - 1]))

        # comments — tokenize is the only way to tell a real `#` from one inside a string
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT and CYRILLIC.search(tok.string):
                    if not MARKER_ANY.search(tok.string):
                        findings.append(f"{rel}:{tok.start[0]}: Russian comment — {why}\n"
                                        f"    {tok.string.strip()[:110]}")
        except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
            findings.append(f"{rel}: could not tokenize ({exc}) — check skipped, fix the file")
            continue

        # log.*() literals
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            findings.append(f"{rel}: could not parse ({exc}) — check skipped, fix the file")
            continue
        for lineno, text in _log_call_strings(tree):
            if CYRILLIC.search(text) and not marked(lineno):
                findings.append(f"{rel}:{lineno}: Russian inside a log.*() call — the journal is "
                                f"read by whoever holds the box\n    {text.strip()[:110]}")


def main() -> int:
    findings: list[str] = []
    for rel, why in STRICT_ROOTS:
        check_strict(rel, why, findings)
    for rel, why in MARKED_ROOTS:
        check_marked(rel, why, findings)
    for rel, why in TARGETED_ROOTS:
        check_targeted(rel, why, findings)

    if findings:
        print(f"cyrillic FAIL: {len(findings)} finding(s)\n")
        for f in findings:
            print(f"  {f}")
        print("\nRule (CLAUDE.md): RU in the i18n dictionaries is deliberate; RU in comments and "
              "logs is an opsec leak; RU on an EN page is a bug.")
        return 1

    checked = ", ".join(r for r, _ in STRICT_ROOTS + MARKED_ROOTS + TARGETED_ROOTS)
    print(f"cyrillic OK: no leaking Russian in {checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
