"""The finding type shared by the risk and reachability rule sets."""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}


@dataclass
class Finding:
    rule: str
    severity: str
    title: str
    detail: str
    node_id: str = ""
    label: str = ""
    path: str = ""
    lineno: int = 0
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""
    #: what makes this the same finding across revisions, when the anchor node is not
    #: (a cycle is its member set, whichever member a change happened to touch)
    identity: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 99)

    def to_dict(self) -> dict:
        from magellan_lite.polyglot.core.wording import tidy
        out = dict(self.__dict__)
        for k in ("title", "detail", "suggestion"):
            out[k] = tidy(out[k])
        out["evidence"] = [tidy(e) for e in out["evidence"]]
        if not out["identity"]:
            del out["identity"]
        return out
