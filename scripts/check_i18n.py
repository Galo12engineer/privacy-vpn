#!/usr/bin/env python3
"""CI check for i18n. Six checks:

1) TEMPLATES: every RUSSIAN _('...') string in web/templates/**.html must have an EN translation
   in web/i18n/en_by_ru.json. Otherwise at lang=en the string falls back to RU and the visitor
   reads Russian text (bug #7 from improvement-audit).

2) PYTHON (control-plane/app): user-facing strings must not reach a template bypassing the
   translator. Two cases are caught:
   a) tr("<russian>") with no dictionary entry → RU fallback;
   b) error="<russian>" / sub="<russian>" NOT wrapped in tr(...) → never translated at all.
   Case (b) was a real prod bug on 2026-07-16: the English /buy page showed Russian coin hints
   (the per-coin minimums and the "any amount" note), because the check looked at TEMPLATES ONLY
   and the Python strings went straight past it. An f-string (error=f"…{x}") is a FAIL too: it
   cannot be a translation key — it has to be tr("… {x} …").format(x=…).

3) PLACEHOLDERS: the EN translation must carry exactly the same `{names}` as the Russian key.

4) ORPHANS (added 2026-07-21): every dictionary key must be used somewhere. Checks 1-2 only catch
   MISSING keys, so dead weight piled up unnoticed: when this check landed, 277 of 950 entries
   (29%) were used by nobody. Among them were mocks from the old static demo with locations we
   never had, and a euro price list cancelled back in July. This is not cosmetics: a dead key with
   a cancelled price looks like a live translation, and sooner or later somebody "fixes" it
   instead of the real one. Adding a locale multiplies the cost — a translator gets paid for text
   nobody will ever see.

5) DO-NOT-TRANSLATE (added 2026-07-21): if a term from web/i18n/glossary.json appears in the
   Russian source, it must appear verbatim in the translation too. A translated "WireGuard" or
   "AllowedIPs" makes a guide impossible to follow — the reader hunts for a button that does not
   exist under that name. Only automation catches this: to the eye it reads as a fine translation.

6) PARITY (added 2026-08-10): every locale dictionary of a family must carry the SAME key set, and
   no entry may be empty or left in the source language. Checks 1-5 all hardcode `en_by_ru.json`,
   so a second locale would be born unguarded: `de_by_ru.json` missing a key is not an error at
   runtime — `translator()` does `.get(s, s)` and silently renders the RUSSIAN source on a German
   page. That is the exact class of bug that left the Russian coin hint on the English /buy in
   August (see CLAUDE.md), except a missing key hides it from checks 1-2 as well: those ask "is
   this key in EN?", and EN is fine.
   An EMPTY value is worse than a missing one — `.get` returns "" and the visitor gets a blank
   label instead of a readable fallback. A value still carrying Cyrillic in a non-RU locale means
   the key was copied but never translated; it looks translated in the diff, and only a machine
   sees it.
   Two filename families are recognised in web/i18n/:
     `<lang>_by_ru.json` — keyed by the Russian source of the templates (the live dictionary);
     `<lang>.json`       — flat key→string. EMPTY since 2026-08-10: `en.json`/`ru.json` (57 keys
                           each) were deleted along with `webui.i18n()`, which nothing called —
                           dead weight that reads as live translation, the very thing check 4
                           exists to prevent. The pattern stays so that a flat family, if one is
                           ever reintroduced, is guarded from its first commit rather than after
                           the first locale drifts.
   Both are checked. Neither has a privileged reference locale: the expected key set is the UNION
   over the family, so "en has a key de lacks" and the reverse are reported the same way. With a
   single locale in a family the check is inert by construction — it starts biting on the day the
   second one lands, which is the day it is needed.
   Checks 3 and 5 (placeholders, do-not-translate) iterate the whole `_by_ru` family for the same
   reason: hardcoding EN there would leave a new locale's `{placeholders}` unvalidated, and a
   placeholder typo is a 500 on that locale's page only.

Strings without Cyrillic (".onion", "PGP", numbers) need no translation — they render as is.
Exit 1 if anything is uncovered.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "web" / "templates"
PY_ROOT = ROOT / "control-plane" / "app"
I18N = ROOT / "web" / "i18n"
GLOSSARY = I18N / "glossary.json"

# The two dictionary families, told apart by filename. `glossary.json` matches neither (it is not a
# locale), which is deliberate: it is configuration, not translation.
_BY_RU_FILE = re.compile(r"^([a-z]{2})_by_ru\.json$")
_FLAT_FILE = re.compile(r"^([a-z]{2})\.json$")

_CALL = re.compile(r"""_\(\s*(['"])(.*?)\1\s*\)""", re.DOTALL)
_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")  # ru-ok: the character class this whole check is built on

# kwargs / context keys whose value travels into a template and is shown to the visitor.
# `entity` was added after the Russian fallback for the operator name leaked onto the EN /privacy.
USER_FACING_KW = {"error", "sub", "entity", "jurisdiction"}
# names of the translator functions
TR_NAMES = {"tr", "_"}

# The operator console is RU-only DELIBERATELY (admin/templates/*.html are entirely in Russian and
# have no EN dictionary, nor will they). The CLAUDE.md invariant is "RU on an EN CUSTOMER PAGE is a
# bug", not "there must be no Russian strings in Python". Without this exemption the check would
# demand that operator-side errors go through the customer translator — i.e. an EN translation for
# a string nobody will ever read in English.
#
# The exemption is PER kwarg, not per file. A file-level one (which is how it started) blinds the
# check on those modules forever: `admin_service` is an ordinary Python module, nothing stops
# `cabinet.py` from importing it, and any future customer-facing string there would go unchecked.
# What is lifted here is exactly `error=` in the two operator modules; `sub`/`entity`/`jurisdiction`,
# tr() calls and dictionary keys in them are checked as everywhere else.
ADMIN_ONLY_MODULES = {
    "control-plane/app/api/admin_ui.py",
    "control-plane/app/services/admin_service.py",
}
ADMIN_ONLY_KW = {"error"}


def extract(text: str) -> list[str]:
    return [m.group(2) for m in _CALL.finditer(text)]


def check_templates(en_by_ru: dict) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    # rglob: templates also live in subfolders (guides/)
    for tpl in sorted(TEMPLATES.rglob("*.html")):
        for raw in extract(tpl.read_text(encoding="utf-8")):
            key = raw.strip()
            if not key or not _CYRILLIC.search(key):
                continue  # empty / not Russian — no translation needed
            if key not in en_by_ru:
                missing.setdefault(tpl.name, []).append(key)
    return missing


def check_python(en_by_ru: dict) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(a) tr('<russian>') with no translation, (b) user-facing Russian NOT wrapped in tr()."""
    missing: dict[str, list[str]] = {}
    unwrapped: dict[str, list[str]] = {}
    for py in sorted(PY_ROOT.rglob("*.py")):
        rel = str(py.relative_to(ROOT))
        # operator-only modules: exempt the `error=` kwarg alone, everything else still checked
        watched = USER_FACING_KW - (ADMIN_ONLY_KW if rel in ADMIN_ONLY_MODULES else set())
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # (a) tr("<russian>") → a translation is required
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in TR_NAMES
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                key = node.args[0].value.strip()
                if key and _CYRILLIC.search(key) and key not in en_by_ru:
                    missing.setdefault(rel, []).append(key)
            # (b) error="<russian>" / sub="<russian>" bypassing the translator
            if isinstance(node, ast.keyword) and node.arg in watched:
                val = node.value
                if (
                    isinstance(val, ast.Constant)
                    and isinstance(val.value, str)
                    and _CYRILLIC.search(val.value)
                ):
                    unwrapped.setdefault(rel, []).append(val.value)
                elif isinstance(val, ast.JoinedStr) and any(
                    isinstance(v, ast.Constant) and isinstance(v.value, str)
                    and _CYRILLIC.search(v.value)
                    for v in val.values
                ):
                    unwrapped.setdefault(rel, []).append("f-string with Cyrillic (cannot be a translation key)")
            # (c) {"entity": "<russian>"} — a context-dict key (e.g. _legal_ctx), NOT a kwarg.
            # A separate branch is needed because (b) only catches ast.keyword: the real bug
            # `"entity": s.legal_entity or "<operator name>"` is an ast.Dict and would have
            # slipped past. The value can be a BoolOp (`x or "<russian>"`) — check every branch.
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if not (isinstance(k, ast.Constant) and k.value in watched):
                        continue
                    for cand in (v.values if isinstance(v, ast.BoolOp) else [v]):
                        if (
                            isinstance(cand, ast.Constant)
                            and isinstance(cand.value, str)
                            and _CYRILLIC.search(cand.value)
                        ):
                            unwrapped.setdefault(rel, []).append(f'{k.value}: {cand.value}')
    return missing, unwrapped


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def check_placeholders(lang: str, by_ru: dict) -> list[str]:
    """The translation must carry EXACTLY the same `{placeholders}` as the RU key.

    Templates and Python call `.format(...)` on the RUSSIAN key while the translated string is what
    gets substituted: an extra placeholder in the translation → KeyError right in the render, a
    missing one → a silently swallowed value. So a typo in the dictionary is a 500 on ONE locale's
    page, with RU perfectly healthy — exactly the kind of half-visible bug that has cost the most
    here. Runs per locale (item 6): a new locale's placeholders are nobody else's business.
    The check is purely textual and fires before the rollout.
    """
    bad = []
    for ru, translated in by_ru.items():
        want, got = set(_PLACEHOLDER.findall(ru)), set(_PLACEHOLDER.findall(translated))
        if want != got:
            bad.append(f"{lang}: {ru[:60]!r}: RU={sorted(want)} {lang.upper()}={sorted(got)}")
    return bad


def load_family(pattern: re.Pattern[str]) -> dict[str, dict]:
    """{lang: dictionary} for one filename family in web/i18n/, sorted by locale code."""
    found = {}
    for path in sorted(I18N.glob("*.json")):
        m = pattern.match(path.name)
        if m:
            found[m.group(1)] = json.loads(path.read_text(encoding="utf-8"))
    return found


def check_parity(family: str, dicts: dict[str, dict]) -> list[str]:
    """Every locale of a family must carry the same keys. See item 6 in the module docstring.

    The reference is the UNION over the family, not a designated "main" locale: whichever locale a
    key was added to, every other one owes a translation. A single-locale family produces nothing —
    the union is that locale's own key set.
    """
    if len(dicts) < 2:
        return []
    union: set[str] = set().union(*(set(d) for d in dicts.values()))
    bad = []
    for lang, d in dicts.items():
        for key in sorted(union - set(d)):
            here = sorted(other for other, od in dicts.items() if key in od)
            bad.append(f"{lang}{family}: missing {key[:70]!r} (present in {', '.join(here)})")
    return bad


def check_values(family: str, dicts: dict[str, dict]) -> list[str]:
    """No entry may be empty, and a non-RU locale may not leave the Russian source in place.

    Empty is checked in every locale (a blank label is a blank label). The Cyrillic rule applies to
    every locale except `ru`, where Russian values are the point. Nested values (glossary-style
    lists) are not part of these families and are skipped rather than guessed at.
    """
    bad = []
    for lang, d in dicts.items():
        for key, value in d.items():
            if not isinstance(value, str):
                continue
            if not value.strip():
                bad.append(f"{lang}{family}: empty translation for {key[:70]!r}")
            elif lang != "ru" and _CYRILLIC.search(value):
                bad.append(f"{lang}{family}: untranslated (Cyrillic) {key[:50]!r} -> {value[:50]!r}")
    return bad


def used_keys() -> set[str]:
    """Every translation key that is requested anywhere.

    Exactly the same two sources as checks 1-2 — templates and tr()/_() in Python — because those
    are all the consumers the dictionary has: the only reader is webui.translator. Keeping a wider
    list here would blind the orphan check forever.
    """
    keys: set[str] = set()
    for tpl in TEMPLATES.rglob("*.html"):
        keys |= {k.strip() for k in extract(tpl.read_text(encoding="utf-8")) if k.strip()}
    for py in PY_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in TR_NAMES
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                key = node.args[0].value.strip()
                if key:
                    keys.add(key)
    return keys


def check_orphans(en_by_ru: dict) -> list[str]:
    """Dictionary keys nobody requests. See item 4 in the module docstring."""
    used = used_keys()
    return [k for k in en_by_ru if k.strip() not in used]


def check_do_not_translate(lang: str, by_ru: dict) -> list[str]:
    """A glossary term that survived in RU must survive in EN too. See item 5 in the module docstring.

    The comparison is case-sensitive and substring-based: what matters is the exact form the reader
    sees in the client, not "roughly the same word".
    """
    if not GLOSSARY.exists():
        return []
    terms = json.loads(GLOSSARY.read_text(encoding="utf-8")).get("do_not_translate", [])
    bad = []
    for ru, translated in by_ru.items():
        for term in terms:
            if term in ru and term not in translated:
                bad.append(f"{lang}: {term!r} lost in translation: {ru[:70]!r} -> {translated[:70]!r}")
    return bad


def _report(title: str, data: dict[str, list[str]]) -> None:
    print(title)
    for name, keys in data.items():
        print(f"  {name}: {len(keys)}")
        for k in keys:
            print(f"    - {k}")


def main() -> int:
    by_ru_family = load_family(_BY_RU_FILE)
    flat_family = load_family(_FLAT_FILE)
    # EN stays the reference for checks 1-2 and 4: those compare against what the CODE requests, and
    # coverage only has to be proven once — parity then carries it to every other locale.
    en_by_ru = by_ru_family["en"]
    tpl_missing = check_templates(en_by_ru)
    py_missing, py_unwrapped = check_python(en_by_ru)
    orphans = check_orphans(en_by_ru)

    ph_bad: list[str] = []
    dnt_bad: list[str] = []
    for lang, d in by_ru_family.items():
        ph_bad += check_placeholders(lang, d)
        dnt_bad += check_do_not_translate(lang, d)

    parity_bad = check_parity("_by_ru.json", by_ru_family) + check_parity(".json", flat_family)
    values_bad = check_values("_by_ru.json", by_ru_family) + check_values(".json", flat_family)

    if parity_bad:
        print("i18n FAIL: locale dictionaries disagree on keys (the missing locale renders Russian):")
        for line in parity_bad:
            print(f"    - {line}")
    if values_bad:
        print("i18n FAIL: empty or untranslated dictionary entries:")
        for line in values_bad:
            print(f"    - {line}")

    if orphans:
        print(f"i18n FAIL: en_by_ru.json keys used by nobody ({len(orphans)}) — delete them:")
        for key in orphans:
            print(f"    - {key[:100]}")
    if dnt_bad:
        print("i18n FAIL: a glossary.json term got translated (the guide becomes impossible to follow):")
        for line in dnt_bad:
            print(f"    - {line}")

    if ph_bad:
        print("i18n FAIL: placeholders do not match the RU key (KeyError at render time):")
        for line in ph_bad:
            print(f"    - {line}")

    if tpl_missing:
        _report("i18n FAIL: Russian template strings with no EN translation in en_by_ru.json:", tpl_missing)
    if py_missing:
        _report("i18n FAIL: tr('<russian>') in Python with no EN translation:", py_missing)
    if py_unwrapped:
        _report(
            "i18n FAIL: user-facing Russian strings in Python WITHOUT tr(...) "
            "(they reach the EN page verbatim):",
            py_unwrapped,
        )
    if tpl_missing or py_missing or py_unwrapped or ph_bad or orphans or dnt_bad or parity_bad or values_bad:
        return 1

    langs = ", ".join(sorted(by_ru_family))
    print("i18n OK: templates and Python — every user-facing Russian string is translated and covered by en_by_ru.json")
    print(f"i18n OK: placeholders match in all {len(en_by_ru)} RU->* pairs, locales: {langs}")
    print(f"i18n OK: no orphans — all {len(en_by_ru)} keys are in use")
    print("i18n OK: no glossary.json term is translated anywhere")
    flat = f"*.json [{', '.join(sorted(flat_family))}]" if flat_family else "no flat *.json family"
    print(
        f"i18n OK: key parity — *_by_ru.json [{langs}] agree on {len(en_by_ru)} keys, {flat}; "
        "no empty or untranslated values"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
