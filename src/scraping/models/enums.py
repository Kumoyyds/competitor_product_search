from __future__ import annotations

from typing import Literal

SourceType = Literal["html", "api"]

Outcome = Literal["success", "escalated", "invalid_target"]

ScrapeRunPath = Literal[
    "fast",
    "retried",
    "agent_repaired",
    "backup_1",
    "backup_2",
    "escalated",
    "invalid_target",
]

EscalationReason = Literal[
    "parser_broken",
    "infra_failure",
    "api_malformed",
    "mass_invalid_target",
]

ParserStatus = Literal["active", "retired"]

PageType = Literal["standard", "out_of_stock", "discounted", "multipack", "membership"]
