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
    from .batch import BatchResult, SearchItemResult, SearchManyResult, SearchRequest


def __getattr__(name: str) -> Any:
    if name in {
        "match_product_batch",
        "match_products",
        "BatchResult",
        "SearchItemResult",
        "SearchManyResult",
        "SearchRequest",
    }:
        from .batch import (
            BatchResult,
            SearchItemResult,
            SearchManyResult,
            SearchRequest,
            match_product_batch,
            match_products,
        )

        return {
            "match_product_batch": match_product_batch,
            "match_products": match_products,
            "BatchResult": BatchResult,
            "SearchItemResult": SearchItemResult,
            "SearchManyResult": SearchManyResult,
            "SearchRequest": SearchRequest,
        }[name]
    raise AttributeError(name)

__all__ = [
    "match_product",
    "match_product_batch",
    "match_products",
    "BatchResult",
    "SearchItemResult",
    "SearchManyResult",
    "SearchRequest",
    "BaseAttributes",
    "CandidateEval",
    "FinalVerdict",
    "LayerTrace",
    "MatchResult",
    "RawCandidate",
    "Verdict",
]
