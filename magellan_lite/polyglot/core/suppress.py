"""Keeping the gate usable: suppression, accepted findings, and SARIF.

A gate that reports the same known finding on every commit gets switched off.
Three ways to say "I have seen this", in increasing scope:

* **inline** -- ``# magellan: ignore[rule]`` (or ``// magellan: ignore[rule]``)
  on the finding's line or the line above it; a bare ``ignore`` covers any rule
* **ignore file** -- ``.magellanignore``, one entry per line:
  ``rule``, ``rule path/glob``, or ``fingerprint:<id>``; ``#`` starts a comment
* **accepted** -- ``magellan accept`` records today's findings in
  ``.magellan-accepted.json`` so only *new* ones are reported afterwards. The
  file is meant to be committed and reviewed like any other change.

A fingerprint is built from the rule, the node it is about and the file -- not the
line number or the counts in the title -- so it survives edits elsewhere.

``to_sarif`` renders findings as SARIF 2.1.0 for code-scanning UIs and CI.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from magellan_lite.polyglot.core.findings import Finding

IGNORE_FILE = ".magellanignore"
ACCEPTED_FILE = ".magellan-accepted.json"
#: an ignore comment in any supported language: #, //, /* */, Fortran !, COBOL *>
_INLINE = re.compile(r"(?:#|//|/\*|!|\*>)\s*magellan:\s*ignore(?:\[([^\]]*)\])?", re.I)


def ignored_on_line(lines: list[str], lineno: int, rule: str) -> bool:
    """Does an ignore comment on ``lineno`` (or the line above) cover ``rule``?"""
    for ln in (lineno, lineno - 1):
        if 1 <= ln <= len(lines):
            m = _INLINE.search(lines[ln - 1])
            if m:
                rules = m.group(1)
                if rules is None or not rules.strip() or rule in {r.strip() for r in rules.split(",")}:
                    return True
    return False


def fingerprint(f: Finding) -> str:
    if f.identity:
        key = f"{f.rule}|{f.identity}"
    else:
        key = "|".join((f.rule, f.node_id or f.label, f.label, f.path))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass
class Suppressed:
    finding: Finding
    reason: str


@dataclass
class Suppression:
    kept: list[Finding] = field(default_factory=list)
    dropped: list[Suppressed] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.dropped:
            out[s.reason] = out.get(s.reason, 0) + 1
        return out


# --------------------------------------------------------------------------
def load_ignore(root: str | Path) -> list[tuple[str, str | None]]:
    """``[(rule or 'fingerprint:<id>', path glob or None)]``."""
    p = Path(root) / IGNORE_FILE
    if not p.exists():
        return []
    entries: list[tuple[str, str | None]] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        entries.append((parts[0], parts[1].strip() if len(parts) > 1 else None))
    return entries


def load_accepted(root: str | Path) -> dict[str, dict]:
    p = Path(root) / ACCEPTED_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {e["fingerprint"]: e for e in data.get("accepted", []) if "fingerprint" in e}


def save_accepted(root: str | Path, findings: list[Finding]) -> Path:
    p = Path(root) / ACCEPTED_FILE
    merged = load_accepted(root)
    for f in findings:
        merged[fingerprint(f)] = {"fingerprint": fingerprint(f), "rule": f.rule,
                                  "label": f.label, "path": f.path, "title": f.title}
    rows = sorted(merged.values(), key=lambda e: (e["rule"], e["path"], e["label"]))
    p.write_text(json.dumps({"version": 1, "accepted": rows}, indent=2) + "\n",
                 encoding="utf-8")
    return p


def _inline_ignored(f: Finding, root: Path, cache: dict[str, list[str]]) -> bool:
    if not f.path or not f.lineno:
        return False
    lines = cache.get(f.path)
    if lines is None:
        try:
            lines = (root / f.path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        cache[f.path] = lines
    return ignored_on_line(lines, f.lineno, f.rule)


def apply_suppressions(findings: list[Finding], root: str | Path,
                       accepted: bool = True) -> Suppression:
    root = Path(root)
    ignore = load_ignore(root)
    acc = load_accepted(root) if accepted else {}
    out = Suppression()
    cache: dict[str, list[str]] = {}
    for f in findings:
        fp = fingerprint(f)
        reason = None
        if _inline_ignored(f, root, cache):
            reason = "inline"
        else:
            for token, glob in ignore:
                if token.startswith("fingerprint:"):
                    hit = token.split(":", 1)[1] == fp
                else:
                    hit = token == f.rule and (glob is None or fnmatch.fnmatch(f.path, glob))
                if hit:
                    reason = "ignore file"
                    break
        if reason is None and fp in acc:
            reason = "accepted"
        if reason:
            out.dropped.append(Suppressed(f, reason))
        else:
            out.kept.append(f)
    return out


# --------------------------------------------------------------------------
_LEVEL = {"critical": "error", "high": "error", "medium": "warning",
          "low": "note", "info": "note"}


def to_sarif(findings: list[Finding], version: str = "0") -> dict:
    rules: dict[str, dict] = {}
    results = []
    for f in findings:
        rules.setdefault(f.rule, {
            "id": f.rule, "name": f.rule,
            "shortDescription": {"text": f.rule.replace("-", " ")},
            "defaultConfiguration": {"level": _LEVEL.get(f.severity, "warning")},
        })
        text = f.title + (f"\n{f.detail}" if f.detail else "")
        if f.evidence:
            text += "\n" + "\n".join(f"- {e}" for e in f.evidence[:10])
        if f.suggestion:
            text += f"\nSuggestion: {f.suggestion}"
        res: dict = {
            "ruleId": f.rule,
            "level": _LEVEL.get(f.severity, "warning"),
            "message": {"text": text},
            "partialFingerprints": {"magellan/v1": fingerprint(f)},
            "properties": {"severity": f.severity, "label": f.label},
        }
        if f.path:
            res["locations"] = [{"physicalLocation": {
                "artifactLocation": {"uri": f.path},
                "region": {"startLine": max(1, f.lineno or 1)}}}]
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {
            "name": "magellan", "version": version,
            "informationUri": "https://github.com/Scramblehub/Magellan",
            "rules": list(rules.values())}},
            "results": results}],
    }
