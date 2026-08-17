from __future__ import annotations

import asyncio
import hashlib
import traceback as traceback_module
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

from .models import CandidateEval, FinalVerdict, MatchResult, Verdict

if TYPE_CHECKING:
    from .db import SearchDB


FAILURE_MATCHED = "matched"
FAILURE_NO_SEARCH_RESULTS = "no_search_results"
FAILURE_DOMAIN_MAP_MISSING = "domain_map_missing"
FAILURE_ALL_DOMAIN_FILTERED = "all_domain_filtered"
FAILURE_BRAND_MISMATCH = "brand_mismatch"
FAILURE_NUMERIC_MISMATCH = "numeric_mismatch"
FAILURE_LLM_NO_MATCH = "llm_no_match"
FAILURE_LLM_ERROR = "llm_error"
FAILURE_LLM_PARSE_ERROR = "llm_parse_error"
FAILURE_BUDGET_EXHAUSTED = "budget_exhausted"
FAILURE_PROVIDER_ERROR = "provider_error"
FAILURE_UNKNOWN_ERROR = "unknown_error"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AttemptRecord:
    attempt_no: int
    provider: str
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    duration_ms: int | None = None
    verdict: str = "error"
    reason: str = ""
    candidates_found: int = 0
    alive_after_domain: int = 0
    alive_after_base: int = 0
    budget_exhausted: bool = False
    query_variants: list[str] = field(default_factory=list)
    node_events: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_indices: dict[int, int] = field(default_factory=dict)
    skipped_nodes: set[str] = field(default_factory=set)
    _started_monotonic: float = 0.0
    _seq: int = 0


class TaskRecorder:
    """Task-local, in-memory trace accumulated across provider attempts."""

    def __init__(
        self,
        *,
        run_id: str,
        row_index: int,
        product_name: str,
        website: str,
        country: str,
        brand_input: str | None = None,
    ):
        import time

        self.run_id = run_id
        self.row_index = row_index
        self.product_name = product_name
        self.product_key = hashlib.md5(
            product_name.strip().lower().encode("utf-8")
        ).hexdigest()
        self.website = website
        self.country = country
        self.brand_input = brand_input
        self.status = "ok"
        self.verdict = "error"
        self.failure_kind = FAILURE_UNKNOWN_ERROR
        self.matched_url: str | None = None
        self.matched_title: str | None = None
        self.reason = ""
        self.layer_trace: dict[str, str | None] = {}
        self.candidates_considered = 0
        self.final_provider: str | None = None
        self.error_type: str | None = None
        self.error_message: str | None = None
        self.traceback: str | None = None
        self.started_at = utc_now()
        self.finished_at: str | None = None
        self.duration_ms: int | None = None
        self.attempts: list[AttemptRecord] = []
        self._current: AttemptRecord | None = None
        self._started_monotonic = time.monotonic()

    @property
    def current_attempt(self) -> AttemptRecord | None:
        return self._current

    def begin_attempt(self, provider: str) -> None:
        import time

        attempt = AttemptRecord(
            attempt_no=len(self.attempts) + 1,
            provider=provider,
            _started_monotonic=time.monotonic(),
        )
        self.attempts.append(attempt)
        self._current = attempt

    def set_query_variants(self, queries: list[str]) -> None:
        if self._current is not None:
            self._current.query_variants = list(queries)

    def record_node_event(
        self,
        *,
        node: str,
        status: str,
        started_at: str,
        duration_ms: int,
        candidates_in: int,
        candidates_out: int,
        error_kind: str | None = None,
        error_message: str | None = None,
        traceback: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        attempt = self._current
        if attempt is None:
            return
        attempt._seq += 1
        attempt.node_events.append(
            {
                "seq": attempt._seq,
                "node": node,
                "status": status,
                "error_kind": error_kind,
                "error_message": error_message,
                "traceback": traceback,
                "detail": detail or {},
                "candidates_in": candidates_in,
                "candidates_out": candidates_out,
                "started_at": started_at,
                "duration_ms": duration_ms,
            }
        )

    def record_skipped(self, nodes: list[str], candidates: int = 0) -> None:
        for node in nodes:
            attempt = self._current
            if attempt is None or node in attempt.skipped_nodes:
                continue
            attempt.skipped_nodes.add(node)
            self.record_node_event(
                node=node,
                status="skipped",
                started_at=utc_now(),
                duration_ms=0,
                candidates_in=candidates,
                candidates_out=candidates,
                detail={"reason": "short_circuit"},
            )

    def record_llm_call(self, payload: dict[str, Any], alive: list[CandidateEval]) -> None:
        attempt = self._current
        if attempt is None:
            return
        attempt.llm_calls.append(dict(payload))
        for llm_index, candidate in enumerate(alive):
            attempt.llm_indices[id(candidate)] = llm_index

    def end_attempt(self, state: dict[str, Any], result: MatchResult) -> None:
        import time

        attempt = self._current
        if attempt is None:
            return
        candidates: list[CandidateEval] = state.get("candidates", [])
        attempt.finished_at = utc_now()
        attempt.duration_ms = int((time.monotonic() - attempt._started_monotonic) * 1000)
        attempt.verdict = result.verdict.value
        attempt.reason = result.reason
        attempt.candidates_found = len(candidates)
        attempt.alive_after_domain = sum(
            1 for c in candidates if c.trace.domain == Verdict.PASS
        )
        attempt.alive_after_base = sum(
            1
            for c in candidates
            if c.trace.domain == Verdict.PASS
            and c.trace.brand != Verdict.FAIL
            and c.trace.numeric != Verdict.FAIL
        )
        attempt.budget_exhausted = bool(state.get("budget_exhausted"))
        matched_url = result.matched_candidate.url if result.matched_candidate else None
        attempt.candidates = [
            self._candidate_payload(i, candidate, matched_url, attempt)
            for i, candidate in enumerate(candidates)
        ]
        self._current = None

    def end_attempt_error(self, exc: BaseException) -> None:
        import time

        attempt = self._current
        if attempt is None:
            return
        attempt.finished_at = utc_now()
        attempt.duration_ms = int((time.monotonic() - attempt._started_monotonic) * 1000)
        attempt.verdict = "error"
        attempt.reason = str(exc)
        self._current = None

    @staticmethod
    def _candidate_payload(
        rank: int,
        candidate: CandidateEval,
        matched_url: str | None,
        attempt: AttemptRecord,
    ) -> dict[str, Any]:
        trace = candidate.trace.to_dict()
        try:
            host = urlparse(candidate.raw.url).netloc.lower()
        except Exception:
            host = ""
        return {
            "rank": rank,
            "url": candidate.raw.url,
            "title": candidate.raw.title,
            "snippet": candidate.raw.snippet,
            "host": host,
            "brands": list(candidate.base.brands),
            "numerics": dict(candidate.base.numerics),
            "v_domain": trace["domain"],
            "v_brand": trace["brand"],
            "v_numeric": trace["numeric"],
            "v_distinguishing": trace["distinguishing"],
            "alive": int(candidate.alive),
            "trace_depth": candidate.trace.depth(),
            "is_matched": int(candidate.raw.url == matched_url),
            "llm_index": attempt.llm_indices.get(id(candidate)),
        }

    def complete(self, result: MatchResult, final_provider: str | None) -> None:
        self.status = "ok"
        self.verdict = result.verdict.value
        self.matched_url = result.matched_candidate.url if result.matched_candidate else None
        self.matched_title = result.matched_candidate.title if result.matched_candidate else None
        self.reason = result.reason
        self.layer_trace = result.layer_trace.to_dict()
        self.candidates_considered = result.candidates_considered
        self.final_provider = final_provider
        self._finish()
        self.failure_kind = derive_failure_kind(self)

    def fail(self, exc: BaseException, tb: str | None = None) -> None:
        self.status = "error"
        self.verdict = "error"
        self.error_type = type(exc).__name__
        self.error_message = str(exc)
        self.traceback = tb or "".join(
            traceback_module.format_exception(type(exc), exc, exc.__traceback__)
        )
        self.reason = str(exc)
        self._finish()
        self.failure_kind = FAILURE_UNKNOWN_ERROR

    def _finish(self) -> None:
        import time

        self.finished_at = utc_now()
        self.duration_ms = int((time.monotonic() - self._started_monotonic) * 1000)


def derive_failure_kind(recorder: TaskRecorder) -> str:
    """Derive a queryable outcome from the complete task trace."""
    if recorder.status == "error" or recorder.verdict == "error":
        return FAILURE_UNKNOWN_ERROR
    if recorder.verdict == FinalVerdict.MATCH.value:
        return FAILURE_MATCHED

    events = [e for a in recorder.attempts for e in a.node_events]
    kinds = {e.get("error_kind") for e in events if e.get("error_kind")}
    llm_statuses = {
        call.get("status") for a in recorder.attempts for call in a.llm_calls
    }

    if "DomainMapMissing" in kinds:
        return FAILURE_DOMAIN_MAP_MISSING
    if "LLMParseError" in kinds or "parse_error" in llm_statuses:
        return FAILURE_LLM_PARSE_ERROR
    if "LLMError" in kinds or "error" in llm_statuses:
        return FAILURE_LLM_ERROR
    if "BudgetExhausted" in kinds or any(a.budget_exhausted for a in recorder.attempts):
        return FAILURE_BUDGET_EXHAUSTED

    for attempt in recorder.attempts:
        search_warnings = [
            e
            for e in attempt.node_events
            if e.get("node") == "search" and e.get("status") == "warning"
        ]
        if (
            attempt.candidates_found == 0
            and attempt.query_variants
            and len(search_warnings) >= len(attempt.query_variants)
        ):
            return FAILURE_PROVIDER_ERROR

    candidates = [c for a in recorder.attempts for c in a.candidates]
    if not candidates:
        return FAILURE_NO_SEARCH_RESULTS
    if not any(c["v_domain"] == Verdict.PASS.value for c in candidates):
        return FAILURE_ALL_DOMAIN_FILTERED

    base_survivors = [
        c
        for c in candidates
        if c["v_domain"] == Verdict.PASS.value
        and c["v_brand"] != Verdict.FAIL.value
        and c["v_numeric"] != Verdict.FAIL.value
    ]
    if not base_survivors:
        if any(c["v_numeric"] == Verdict.FAIL.value for c in candidates):
            return FAILURE_NUMERIC_MISMATCH
        return FAILURE_BRAND_MISMATCH

    if any(call.get("status") == "ok" for a in recorder.attempts for call in a.llm_calls):
        return FAILURE_LLM_NO_MATCH
    return FAILURE_UNKNOWN_ERROR


_CURRENT_RECORDER: ContextVar[TaskRecorder | None] = ContextVar(
    "search_task_recorder", default=None
)


def get_recorder() -> TaskRecorder | None:
    return _CURRENT_RECORDER.get()


def set_recorder(recorder: TaskRecorder) -> Token[TaskRecorder | None]:
    return _CURRENT_RECORDER.set(recorder)


def reset_recorder(token: Token[TaskRecorder | None]) -> None:
    _CURRENT_RECORDER.reset(token)


@asynccontextmanager
async def record_task(
    db: SearchDB | None,
    *,
    run_id: str,
    row_index: int,
    product_name: str,
    website: str,
    country: str,
    brand: str | None = None,
) -> AsyncIterator[TaskRecorder]:
    """Install a task-local recorder and atomically flush it on exit."""
    recorder = TaskRecorder(
        run_id=run_id,
        row_index=row_index,
        product_name=product_name,
        website=website,
        country=country,
        brand_input=brand,
    )
    token = set_recorder(recorder)
    try:
        yield recorder
    except BaseException as exc:
        recorder.fail(exc, traceback_module.format_exc())
        raise
    finally:
        reset_recorder(token)
        if db is not None:
            await asyncio.to_thread(db.flush_task, recorder)
