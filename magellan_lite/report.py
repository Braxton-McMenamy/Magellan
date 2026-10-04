"""What one check produces: the changes, the checklist, and a verdict. Shared contract."""

from __future__ import annotations

from dataclasses import dataclass, field

from magellan_lite.diff import Change
from magellan_lite.findings import Finding, blocking_rules

VERDICTS = ("ok", "review", "block")


@dataclass
class Report:
    root: str
    against: str
    changes: list[Change] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: the blast radius: unedited code the change can break -- the engine's next step
    affected: list[dict] = field(default_factory=list)
    #: files that did not parse, rules that crashed: reported, never hidden
    errors: list[str] = field(default_factory=list)
    #: findings silenced by a ``# magellan: ignore[rule-id]`` comment: counted, not listed
    suppressed: int = 0

    @property
    def verdict(self) -> str:
        """``block``: a blocking rule found something high or worse. ``review``: something
        medium or worse. ``ok``: nothing that needs a person."""
        blocking = blocking_rules()
        if any(f.rule in blocking and f.severity in ("critical", "high") for f in self.findings):
            return "block"
        if any(f.severity in ("critical", "high", "medium") for f in self.findings):
            return "review"
        return "ok"

    @property
    def files_changed(self) -> list[str]:
        return sorted({c.path for c in self.changes})

    def fails(self, fail_on: str) -> bool:
        """Should this report stop a commit? ``fail_on`` is a verdict, or ``never``."""
        if fail_on == "never":
            return False
        return VERDICTS.index(self.verdict) >= VERDICTS.index(fail_on)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "root": self.root,
            "against": self.against,
            "files_changed": self.files_changed,
            "changes": [c.to_dict() for c in self.changes],
            "findings": [f.to_dict() for f in self.findings],
            "affected": self.affected,
            "errors": self.errors,
            "suppressed": self.suppressed,
        }
