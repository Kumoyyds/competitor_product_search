"""Repair Agent --- the ladder-based candidate parser generator (spec SS5.5, D8).

Called from HTMLScraper when the ordered parser list produces no valid output.
Attempt count = len(cfg.repair_model_ladder) — edit config to add/remove nodes.
Temperature ladder lives in config.py (must match model ladder length).

Ladder (config-driven):
  Turn A (no_product): index==0 only
  Turn B (source_absence): index==1 with prior gate/missing-field evidence
  Turn C (parser_gen): every attempt, thinking mode on last only

Each attempt records a full trace (code, capture summary, errors) fed back to
the next attempt via AttemptRecord list — no index misalignment, works for any
node count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Union

from ..config import get_config
from ..exceptions import ScrapeFailed
from ..models.product_data import ProductData
from ..models.results import InvalidTargetResult
from ..providers import make_chat_client
from ..storage import PhraseStore, ScrapeDB
from ..validation import validate
from .golden import GoldenRejection, promote_candidate
from .prepass import PriceContext, build_price_aware_context
from .prompts import (
    no_product_prompt,
    parser_gen_prompt,
    source_absence_prompt,
)
from .sandbox import run_in_sandbox

if TYPE_CHECKING:
    from ..scrapers.html_scraper import HTMLScraper

logger = logging.getLogger(__name__)


@dataclass
class AttemptRecord:
    """Full trace of one repair attempt — code, output, capture, and why it failed.

    index/model/code/output/capture/errors all share the same index, so the
    prompt builder can align them without the old error/candidate misalignment.
    This list is inherently attempt-count-agnostic.
    """
    index: int
    model: str
    code: str
    output: Optional[dict[str, Any]] = None   # parsed dict if sandbox returned one
    capture: Optional[dict[str, Any]] = None   # {captured, missing_required, missing_optional}
    failure_stage: Optional[str] = None        # "sandbox" | "gate" | "golden" | None (=success)
    errors: list[str] = field(default_factory=list)


@dataclass
class RepairContext:
    site: str
    url: str
    html: str
    initial_errors: list[str] = field(default_factory=list)   # pre-repair, labelled separately
    attempts: list[AttemptRecord] = field(default_factory=list)
    price_context: Optional[PriceContext] = None               # computed once in _gen_parser


@dataclass
class NoProductVerdict:
    phrase: Optional[str]


@dataclass
class SourceAbsent:
    reason: str


@dataclass
class CandidateFailed:
    errors: list[str]


@dataclass
class CandidateSucceeded:
    product: ProductData
    parser_id: int
    parser_source: str


RepairOutcome = Union[NoProductVerdict, SourceAbsent, CandidateFailed, CandidateSucceeded]


def summarize_capture(output: dict[str, Any]) -> dict[str, Any]:
    """Compute which ProductData fields a parser output captured/missed.

    Uses the current gate2 semantics (every in-stock product needs price).
    Feeds into the next attempt's prompt so the LLM sees exactly what to fix.
    """
    required = ["title", "in_stock"]
    if output.get("in_stock") is True:
        if not output.get("price"):
            required.append("price")
    optional = [
        "brand", "gtin", "image_urls", "currency", "list_price",
        "membership_price", "availability_raw", "variant",
    ]
    present = lambda f: output.get(f) not in (None, [], "", {})

    return {
        "captured": [f for f in required + optional if present(f)],
        "missing_required": [f for f in required if not present(f)],
        "missing_optional": [f for f in optional if not present(f)],
    }


def _flatten_errors(
    initial_errors: list[str],
    attempts: list[AttemptRecord],
    prefix: Optional[list[str]] = None,
) -> list[str]:
    """Flatten initial_errors + per-attempt error lists into a flat list[str]."""
    out: list[str] = list(prefix or [])
    out.extend(str(e) for e in initial_errors)
    for a in attempts:
        out.extend(a.errors)
    return out


async def run_repair_ladder(
    scraper: "HTMLScraper",
    url: str,
    html: str,
    initial_errors: list[str],
) -> Union[ProductData, InvalidTargetResult, ScrapeFailed]:
    """Run the repair ladder. Returns the final outcome: product, invalid-target, or failure.

    Node count = len(cfg.repair_model_ladder).  Edit the config lists to add/remove
    nodes — Turn A/B/thinking adapt via semantic positions (first/second/last).
    """
    cfg = get_config()
    ladder = cfg.repair_model_ladder
    temps = cfg.repair_temperature_ladder
    assert len(temps) == len(ladder), (
        f"repair_temperature_ladder length ({len(temps)}) != "
        f"repair_model_ladder length ({len(ladder)})"
    )
    n = len(ladder)
    ctx = RepairContext(site=scraper.site, url=url, html=html)
    if initial_errors:
        ctx.initial_errors = initial_errors

    for i, model in enumerate(ladder):
        is_last = (i == n - 1)
        outcome = await _try_repair(ctx, index=i, model=model, is_last=is_last)

        if isinstance(outcome, NoProductVerdict):
            _backfill_phrase(scraper.site, outcome.phrase)
            return InvalidTargetResult(
                url=url, site=scraper.site,
                reason_signal=f"agent_no_product: {outcome.phrase or 'no phrase'}",
            )

        if isinstance(outcome, SourceAbsent):
            return ScrapeFailed(
                site=scraper.site, url=url,
                scraper_name=scraper.__class__.__name__,
                failed_stage="source_absent",
                signature=(scraper.site, "source_absent", ""),
                errors=_flatten_errors(ctx.initial_errors, ctx.attempts, prefix=[outcome.reason]),
                snapshot=html[:2000],
            )

        if isinstance(outcome, CandidateSucceeded):
            return outcome.product

        # CandidateFailed — record already in ctx.attempts, continue

    # Budget exhausted — escalate with last capture detail
    last_capture = ctx.attempts[-1].capture if ctx.attempts else None
    last_failure_stage = ctx.attempts[-1].failure_stage if ctx.attempts else None
    import json as _json
    return ScrapeFailed(
        site=scraper.site, url=url,
        scraper_name=scraper.__class__.__name__,
        failed_stage="parser_broken",
        signature=(scraper.site, "repair_budget_exhausted", ""),
        errors=_flatten_errors(ctx.initial_errors, ctx.attempts),
        snapshot=_json.dumps({
            "html_preview": html[:2000],
            "last_capture": last_capture,
            "last_failure_stage": last_failure_stage,
        }, default=str),
    )


async def _try_repair(
    ctx: RepairContext, index: int, model: str, is_last: bool
) -> RepairOutcome:
    """Run one ladder attempt.  index/model/is_last come from the config-driven loop.

    Turn A (no_product): index==0 only.
    Turn B (source_absence): index==1 when the prior parser ran but missed a
      required field.
    Turn C (parser_gen): every attempt; captures output + errors into an
      AttemptRecord appended to ctx.attempts — aligned by index, no mislabelling.
    """
    cfg = get_config()

    # Judgment LLM: deterministic answers for yes/no gates
    judgment_llm = _make_llm(model, temperature=0.1)
    if judgment_llm is None:
        return CandidateFailed(errors=["LLM not configured (QWEN_KEY missing)"])

    # Turn A — no_product judgment, first attempt only
    if index == 0:
        verdict = await _ask_no_product(judgment_llm, ctx)
        if verdict and verdict.get("decision") == "no_product":
            return NoProductVerdict(phrase=verdict.get("phrase"))

    # Turn B — source_absence, on the second attempt.
    # Requires evidence: the previous attempt must have produced a runnable
    # parser that still missed required fields. A sandbox crash means the
    # generated code was broken, which says nothing about whether the page
    # carries the data — asking then would invite a false source_absent.
    if index == 1 and _has_source_absence_evidence(ctx):
        absent = await _ask_source_absence(judgment_llm, ctx)
        if absent and absent.get("decision") == "source_absent":
            return SourceAbsent(reason=absent.get("reason", "source_absent"))

    # Turn C — parser generation (temperature from config, thinking on last)
    temp = cfg.repair_temperature_ladder[index]
    parser_gen_llm = _make_llm(
        model, temperature=temp, enable_thinking=is_last
    )
    if parser_gen_llm is None:
        return CandidateFailed(errors=["LLM not configured (QWEN_KEY missing)"])

    # Build role name for the prompt
    role = "last" if is_last else ("first" if index == 0 else "middle")

    parser_source = await _gen_parser(parser_gen_llm, ctx, role)
    if not parser_source:
        return CandidateFailed(errors=["parser_gen returned nothing"])

    # Sandbox execution
    sandbox_result = await run_in_sandbox(parser_source, ctx.html, ctx.url)

    # Build an AttemptRecord with everything we know so far
    rec = AttemptRecord(index=index, model=model, code=parser_source)

    if not isinstance(sandbox_result, dict):
        rec.failure_stage = "sandbox"
        rec.errors = [_format_sandbox_error(sandbox_result)]
        ctx.attempts.append(rec)
        return CandidateFailed(errors=rec.errors)

    rec.output = sandbox_result
    rec.capture = summarize_capture(sandbox_result)

    # Wrap + validate
    wrapped = dict(sandbox_result)
    wrapped.setdefault("url", ctx.url)
    wrapped["website"] = ctx.site
    wrapped["source_type"] = "html"
    wrapped["scraped_at"] = datetime.now(timezone.utc)
    wrapped["parser_version"] = f"agent_attempt_{index}"

    product, errors = validate(wrapped)
    if product is None:
        rec.failure_stage = "gate"
        rec.errors = errors
        ctx.attempts.append(rec)
        return CandidateFailed(errors=errors)

    # Golden test + promote (M9)
    result = await promote_candidate(
        site=ctx.site,
        code=parser_source,
        current_product=product,
        current_html=ctx.html,
    )
    if isinstance(result, GoldenRejection):
        rec.failure_stage = "golden"
        rec.errors = [result.describe()]
        ctx.attempts.append(rec)
        return CandidateFailed(errors=rec.errors)

    # Success — record attempt and return
    ctx.attempts.append(rec)
    return CandidateSucceeded(
        product=product,
        parser_id=result,             # result is int (parser id) when not GoldenRejection
        parser_source=parser_source,
    )


def _has_source_absence_evidence(ctx: RepairContext) -> bool:
    """Return whether the previous runnable parser missed required fields."""
    if not ctx.attempts:
        return False
    previous = ctx.attempts[-1]
    if previous.failure_stage != "gate":
        return False
    return bool((previous.capture or {}).get("missing_required"))


def _make_llm(model: str, temperature: float = 0.1, enable_thinking: bool = False):
    """Build the configured provider's LangChain client.

    Args:
      model: registered model id, optionally prefixed with ``provider/``
      temperature: sampling temperature (0.1 for judgments, ramp for parser_gen)
      enable_thinking: enable provider-specific thinking on the last ladder node
    """
    return make_chat_client(
        model=model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        purpose="repair ladder",
    )


def _format_sandbox_error(sandbox_result: Any) -> str:
    """F1: emit the type + full traceback when available, so the next attempt's
    prompt shows line numbers and the specific failing expression instead of a
    single opaque message."""
    type_name = type(sandbox_result).__name__
    message = getattr(sandbox_result, "message", None) or getattr(
        sandbox_result, "reason", str(sandbox_result)
    )
    traceback_text = getattr(sandbox_result, "traceback", None)
    if traceback_text:
        return f"sandbox rejected: {type_name}: {message}\nTraceback:\n{traceback_text}"
    return f"sandbox rejected: {type_name}: {message}"


async def _ask_no_product(llm, ctx: RepairContext) -> Optional[dict[str, Any]]:
    try:
        resp = await llm.ainvoke(no_product_prompt(ctx.html, ctx.site))
        return _parse(resp)
    except Exception:
        logger.exception("no_product prompt failed")
        return None


async def _ask_source_absence(llm, ctx: RepairContext) -> Optional[dict[str, Any]]:
    # Feed prior errors from initial_errors + all attempts so the LLM has full context
    prior_errors = [ctx.initial_errors] if ctx.initial_errors else []
    for a in ctx.attempts:
        prior_errors.append(a.errors)
    try:
        resp = await llm.ainvoke(source_absence_prompt(ctx.html, ctx.site, prior_errors))
        return _parse(resp)
    except Exception:
        logger.exception("source_absence prompt failed")
        return None


async def _gen_parser(llm, ctx: RepairContext, role: str) -> Optional[str]:
    """Invoke parser_gen_prompt with the attempt history (aligned by index).

    Builds the PriceContext once (cached on ctx) so every ladder attempt shares
    the same evidence bundle — no redundant DOM scans.
    """
    if ctx.price_context is None:
        ctx.price_context = build_price_aware_context(ctx.html, ctx.url)
    try:
        resp = await llm.ainvoke(parser_gen_prompt(
            ctx.price_context, ctx.site, role=role,
            initial_errors=ctx.initial_errors,
            attempts=ctx.attempts,
        ))
        parsed = _parse(resp)
        if parsed:
            return parsed.get("parser_code")
    except Exception:
        logger.exception("parser_gen prompt failed")
    return None


def _parse(resp) -> Optional[dict[str, Any]]:
    content = resp.content if hasattr(resp, "content") else str(resp)
    import json as _json

    try:
        return _json.loads(content)
    except (_json.JSONDecodeError, TypeError):
        try:
            start = content.index("{")
            end = content.rindex("}") + 1
            return _json.loads(content[start:end])
        except (ValueError, _json.JSONDecodeError):
            return None


def _backfill_phrase(site: str, phrase: Optional[str]) -> None:
    if not phrase:
        return
    db: ScrapeDB | None = None
    try:
        cfg = get_config()
        db = ScrapeDB(cfg.db_path)
        db.init_db()
        PhraseStore(db).add(site, phrase, source="agent_backfill")
        logger.info("backfilled phrase for site=%s: %r", site, phrase[:80])
    except Exception:
        logger.exception("phrase backfill failed")
    finally:
        if db is not None:
            db.close()
