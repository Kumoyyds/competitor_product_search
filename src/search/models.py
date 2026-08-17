from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class FinalVerdict(str, Enum):
    MATCH = "match"
    NO_MATCH = "no_match"


@dataclass
class LayerTrace:
    domain: Verdict | None = None
    brand: Verdict | None = None
    numeric: Verdict | None = None
    distinguishing: Verdict | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "domain": self.domain.value if self.domain else None,
            "brand": self.brand.value if self.brand else None,
            "numeric": self.numeric.value if self.numeric else None,
            "distinguishing": self.distinguishing.value if self.distinguishing else None,
        }

    def depth(self) -> int:
        order = (self.domain, self.brand, self.numeric, self.distinguishing)
        d = 0
        for i, v in enumerate(order, start=1):
            if v is not None:
                d = i
        return d


@dataclass
class BaseAttributes:
    brands: list[str] = field(default_factory=list)
    numerics: dict[str, float] = field(default_factory=dict)


@dataclass
class RawCandidate:
    title: str
    url: str
    snippet: str = ""


@dataclass
class CandidateEval:
    raw: RawCandidate
    base: BaseAttributes = field(default_factory=BaseAttributes)
    trace: LayerTrace = field(default_factory=LayerTrace)
    alive: bool = True


@dataclass
class MatchResult:
    verdict: FinalVerdict
    matched_candidate: RawCandidate | None
    layer_trace: LayerTrace
    candidates_considered: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "matched_url": self.matched_candidate.url if self.matched_candidate else None,
            "matched_title": self.matched_candidate.title if self.matched_candidate else None,
            "layer_trace": self.layer_trace.to_dict(),
            "candidates_considered": self.candidates_considered,
            "reason": self.reason,
        }
