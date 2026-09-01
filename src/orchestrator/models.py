from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BatchResult:
    batch_id: str
    status: str
    total: int
    valid: int
    failed: int

    @property
    def exit_code(self) -> int:
        if self.status == "completed":
            return 0
        if self.status == "completed_with_failures":
            return 2
        return 1
