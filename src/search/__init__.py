from typing import TYPE_CHECKING, Any

from .pipeline import match_product
from .models import (
    BaseAttributes,
    CandidateEval,
    FinalVerdict,
    LayerTrace,
    MatchResult,
    RawCandidate,
    Verdict,
)

if TYPE_CHECKING:
    from .batch import BatchResult


def __getattr__(name: str) -> Any:
    if name in {"match_product_batch", "BatchResult"}:
        from .batch import BatchResult, match_product_batch

        return {
            "match_product_batch": match_product_batch,
            "BatchResult": BatchResult,
        }[name]
    raise AttributeError(name)

__all__ = [
    "match_product",
    "match_product_batch",
    "BatchResult",
    "BaseAttributes",
    "CandidateEval",
    "FinalVerdict",
    "LayerTrace",
    "MatchResult",
    "RawCandidate",
    "Verdict",
]
