"""co-change: a file that history says moves with the one you edited was left alone.

From the full Magellan (``magellan/rules/cochange.py``). The code says what depends on what;
version control says what people *found* they had to change together, which includes
everything a parser cannot see: a config schema and the code that reads it, both ends of a
wire format, a migration and the model it mirrors, generated files and their generator. Two
files edited in most of the same commits are coupled, whether or not any import connects them.

Mined from ``git log`` (the last 300 commits, merges and sweeping commits of more than 25
files left out); nothing is run. Confidence is one-way: if ``schema.py`` changed in 6 commits
and ``parser.py`` in 5 of them, editing ``schema.py`` alone is worth a look, even when
``parser.py`` also changes on its own all the time. Reported when the two shared at least 4
commits and at least 60% of the edited file's commits.

Only for a real project checked against a git revision (``magellan-lite check``, the editor):
with no repository behind the check -- code pasted into the website, a baseline directory --
it says nothing, and does not try.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations

from magellan_lite.findings import Finding, change_rule
from magellan_lite.languages import is_source
from magellan_lite.rules.common import baseline, changed_files, git, on_disk

MAX_COMMITS = 300
MAX_FILES = 25          # a commit touching more is a sweep (rename, formatter), not coupling
MIN_SUPPORT = 4
MIN_CONFIDENCE = 0.6

_cache: dict[tuple[str, str], "History"] = {}


class History:
    """For each file and each pair of files, how many commits touched them."""

    def __init__(self) -> None:
        self.files: Counter = Counter()
        self.pairs: Counter = Counter()

    def add(self, files: list[str]) -> None:
        if not files or len(files) > MAX_FILES:
            return
        self.files.update(files)
        self.pairs.update(combinations(sorted(files), 2))

    def partners(self, path: str) -> list[tuple[str, int, float]]:
        """``(other file, commits shared, share of path's commits)``, strongest first."""
        mine = self.files.get(path, 0)
        if not mine:
            return []
        out = []
        for (a, b), n in self.pairs.items():
            if path not in (a, b) or n < MIN_SUPPORT:
                continue
            conf = n / mine
            if conf >= MIN_CONFIDENCE:
                out.append((b if a == path else a, n, conf))
        out.sort(key=lambda r: (-r[2], -r[1], r[0]))
        return out


def parse_log(text: str) -> History:
    """``git log --name-only --pretty=format:%x1e%H`` output, as a History."""
    h = History()
    for block in text.split("\x1e"):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        h.add(sorted({ln for ln in lines[1:] if is_source(ln)}))
    return h


def mine(root, head: str) -> History | None:
    key = (str(root), head)
    if key not in _cache:
        out = git(root, "log", "--no-merges", "--name-only", "--relative", "-M",
                  f"-n{MAX_COMMITS}", "--pretty=format:%x1e%H", "--", ".", timeout=15)
        if out is None:
            return None
        if len(_cache) >= 8:
            _cache.clear()                          # a long-running process: keep it small
        _cache[key] = parse_log(out)
    return _cache[key]


def _linked(ctx, a: str, b: str) -> bool:
    """Does a call, read or import already connect the two files?"""
    for e in ctx.graph.edges:
        if e.path in (a, b):
            d = ctx.after_defs.get(e.dst)
            if d is not None and {e.path, d.path} == {a, b}:
                return True
    return False


@change_rule("co-change", "medium",
             fix="Check whether the other file needs the matching change; if it does not, "
                 "nothing to do.")
def co_change(ctx):
    root = on_disk(ctx)
    base = baseline(ctx) if root is not None else None
    if base is None or base[0] != "git":
        return
    a = ctx.after.files
    changed = changed_files(ctx, "")
    if not changed:
        return
    head = git(root, "rev-parse", "HEAD")
    history = mine(root, (head or "").strip()) if head else None
    if history is None:
        return
    seen: set[tuple[str, str]] = set()
    for path in sorted(changed):
        if path not in a:
            continue
        for other, together, conf in history.partners(path):
            pair = tuple(sorted((path, other)))
            if other in changed or other not in a or pair in seen:
                continue
            seen.add(pair)
            linked = _linked(ctx, path, other)
            total = history.files[path]
            yield Finding(
                "co-change", "medium" if conf >= 0.8 and not linked else "low",
                f"{path} usually changes together with {other}, which this change leaves "
                f"alone",
                path, 1,
                detail=(f"They changed together in {together} of the last {total} commits "
                        f"that touched {path} ({conf:.0%}). "
                        + ("No import or call connects them, so nothing else would point you "
                           "at it." if not linked else "They are also connected in the code.")),
                fix=f"Check whether {other} needs the matching change.")
