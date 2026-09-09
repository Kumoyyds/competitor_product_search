"""Structured result and evidence models for attribute-level verification."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ProductMatchVerdict(StrEnum):
    MATCH = "match"
    NO_MATCH = "no_match"


class EvidenceStatus(StrEnum):
    PASS = "pass"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class DecisionSource(StrEnum):
    GTIN = "gtin"
    VARIANT_RULE = "variant_rule"
    LLM = "llm"


class VisionStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    NOT_AVAILABLE = "not_available"
    SUCCESS = "success"
    FAILED = "failed"


class DecisionNode(StrEnum):
    GTIN = "gtin"
    VARIANT_RULE = "variant_rule"
    VISION = "vision"
    LLM = "llm"
    IDENTITY_GUARD = "identity_guard"


class DecisionNodeRecord(BaseModel):
    node: DecisionNode
    status: str
    terminal: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class ProductMatchResult(BaseModel):
    verdict: ProductMatchVerdict
    decision_source: DecisionSource
    reasoning: str
    gtin_status: EvidenceStatus
    variant_status: EvidenceStatus
    vision_status: VisionStatus = VisionStatus.NOT_REQUESTED
    vision_comment: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    trace: list[DecisionNodeRecord] = Field(default_factory=list)
