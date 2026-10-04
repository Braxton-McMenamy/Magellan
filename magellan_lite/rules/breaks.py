"""Calls the change breaks: a signature they no longer fit, or a definition that is gone.

These need the whole picture -- the old and new version of what changed, and the call graph
of who uses it -- so they are change rules. Both block a commit: each one is a TypeError,
NameError or ImportError waiting for the code path to run.
"""

from __future__ import annotations

from magellan_lite.findings import Finding, change_rule


@change_rule("signature-break", "critical", blocking=True,
             fix="Update the call, or give the new parameter a default so existing calls "
                 "keep working.")
def signature_break(ctx):
    return list(_python_signature_breaks(ctx)) + _other_languages(ctx, "signature-break")


def _other_languages(ctx, rule_id: str) -> list[Finding]:
    """The same break in C, Java, Fortran, COBOL, ...: their frontends judge their call sites
    (languages.py)."""
    if all(p.endswith(".py") for p in set(ctx.before.files) | set(ctx.after.files)):
        return []
    from magellan_lite import languages
    return [f for f in languages.findings(ctx) if f.rule == rule_id]


def _python_signature_breaks(ctx):
    """A call that fit the old signature and does not fit the new one."""
    edited = {c.name for c in ctx.changes}
    seen = set()
    for c in ctx.changes:
        if c.kind != "signature" or not (c.before.params and c.after.params):
            continue
        for e in ctx.graph.callers(c.name):
            if e.kind != "calls" or e.unpacked or e.guess or (e.path, e.line) in seen:
                continue
            new = c.after.params.bound() if e.bound else c.after.params
            old = c.before.params.bound() if e.bound else c.before.params
            now = new.problems(e.positional, e.keywords)
            if not now or old.problems(e.positional, e.keywords):
                continue                        # fits the new one, or never fit the old one
            seen.add((e.path, e.line))
            untouched = ("It is in code this change did not touch, so nobody updated it: "
                         if e.src not in edited else "")
            yield Finding(
                "signature-break", "critical",
                f"{e.src} calls {c.after.short}() the old way: it now {now[0]}",
                e.path, e.line,
                detail=(f"{c.name} went from {c.before.signature} to {c.after.signature}. "
                        f"{untouched}the call raises TypeError when it runs."))


@change_rule("removed-still-referenced", "critical", blocking=True,
             fix="Restore it, or update the code that still uses it in the same change.")
def removed_still_referenced(ctx):
    return list(_python_removed(ctx)) + _other_languages(ctx, "removed-still-referenced")


def _python_removed(ctx):
    """Code that still calls, reads or imports something the change deleted or renamed."""
    seen = set()
    for c in ctx.changes:
        if c.kind not in ("removed", "renamed"):
            continue
        gone = c.before.name
        if gone in ctx.after_defs:
            continue
        what = (f"which this change renamed to {c.after.name}" if c.kind == "renamed"
                else "which this change deleted")
        uses = [(e.path, e.line, f"{e.src} still {e.kind} {gone}") for e in ctx.graph.callers(gone)]
        uses += [(i.path, i.line, f"{i.path} still imports {gone}")
                 for i in ctx.graph.imports if i.target == gone]
        for path, line, message in uses:
            if (path, line) in seen:
                continue
            seen.add((path, line))
            yield Finding(
                "removed-still-referenced", "critical", f"{message}, {what}", path, line,
                detail="It fails when that line runs: NameError or AttributeError for a call "
                       "or read, ImportError as soon as the module is imported.")
