"""Repair Agent --- the ladder-based candidate parser generator (spec SS5.5, D8).

Called from HTMLScraper when the ordered parser list produces no valid output.
Shared budget of 4 attempts (config.repair_budget).

Ladder:
  Attempt 0: deepseek-v4-flash (Turn A only)
  Attempt 1: deepseek-v4-flash + source_absence check (Turn B)
  Attempt 2: deepseek-v4-pro (temperature-driven exploration)
  Attempt 3: deepseek-v4-pro with thinking mode (last-ditch)

Each attempt starts with a no_product_on_page check (Turn A) on attempt 0 only.
Turn A does not consume budget; Turn B (source_absence) only runs on attempt 1.
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
from ..storage import PhraseStore, ScrapeDB
from ..validation import validate
from .prompts import (
    no_product_prompt,
    parser_gen_prompt,
    source_absence_prompt,
)
from .sandbox import run_in_sandbox

if TYPE_CHECKING:
    from ..scrapers.html_scraper import HTMLScraper

logger = logging.getLogger(__name__)

# F5: Parser-generation temperature ramp. Attempt 0 stays low (deterministic
# best-guess); later attempts explore alternative strategies. Judgment prompts
# (no_product / source_absence) always run at 0.1 for stability.
# Length matches config.repair_budget (4). Attempts 2 and 3 use the pro model;
# attempt 3 additionally enables reasoning/thinking mode (see _make_llm).
_PARSER_GEN_TEMPERATURE_LADDER: list[float] = [0.1, 0.4, 0.7, 0.9]


@dataclass
class RepairContext:
    site: str
    url: str
    html: str
    attempt: int = 0
    error_history: list[list[str]] = field(default_factory=list)
    candidates_tried: list[str] = field(default_factory=list)


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


async def run_repair_ladder(
    scraper: "HTMLScraper",
    url: str,
    html: str,
    initial_errors: list[str],
) -> Union[ProductData, InvalidTargetResult, ScrapeFailed]:
    """Run the repair ladder. Returns the final outcome: product, invalid-target, or failure."""
    cfg = get_config()
    ctx = RepairContext(site=scraper.site, url=url, html=html)
    if initial_errors:
        ctx.error_history.append(initial_errors)

    for attempt in range(cfg.repair_budget):
        ctx.attempt = attempt
        model = cfg.repair_model_ladder[min(attempt, len(cfg.repair_model_ladder) - 1)]
        # The final attempt enables the pro model's reasoning/thinking mode
        # so the LLM can chain-of-thought through cases prior attempts couldn't
        # solve. Only meaningful when the last-attempt model actually supports
        # thinking (deepseek-v4-pro etc.).
        is_last_attempt = attempt == cfg.repair_budget - 1
        outcome = await _try_repair(ctx, model, is_last_attempt=is_last_attempt)

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
                errors=[outcome.reason] + [str(e) for e in ctx.error_history],
                snapshot=html[:2000],
            )

        if isinstance(outcome, CandidateSucceeded):
            return outcome.product

        # CandidateFailed — consume budget, continue
        ctx.error_history.append(outcome.errors)

    return ScrapeFailed(
        site=scraper.site, url=url,
        scraper_name=scraper.__class__.__name__,
        failed_stage="parser_broken",
        signature=(scraper.site, "repair_budget_exhausted", ""),
        errors=[str(e) for e in ctx.error_history],
        snapshot=html[:2000],
    )


async def _try_repair(
    ctx: RepairContext, model: str, is_last_attempt: bool = False
) -> RepairOutcome:
    # F5: judgments (no_product / source_absence) stay deterministic;
    # parser_gen uses a temperature ramp to force genuine exploration on retries.
    # Judgment prompts never enable thinking — they answer yes/no gates that
    # don't benefit from chain-of-thought.
    judgment_llm = _make_llm(model, temperature=0.1)
    if judgment_llm is None:
        return CandidateFailed(errors=["LLM not configured (DEEPSEEK_KEY missing)"])

    parser_gen_temp = _PARSER_GEN_TEMPERATURE_LADDER[
        min(ctx.attempt, len(_PARSER_GEN_TEMPERATURE_LADDER) - 1)
    ]
    # Enable reasoning/thinking mode only on the last repair-ladder attempt,
    # and only for the parser_gen call (Turn C) — that's where deep reasoning
    # actually helps.
    parser_gen_llm = _make_llm(
        model, temperature=parser_gen_temp, enable_thinking=is_last_attempt
    )
    if parser_gen_llm is None:
        return CandidateFailed(errors=["LLM not configured (DEEPSEEK_KEY missing)"])

    # F3: Turn A (no_product judgment) only on attempt 0 — repeating it
    # on retries wastes LLM calls on a question we already answered.
    if ctx.attempt == 0:
        verdict = await _ask_no_product(judgment_llm, ctx)
        if verdict and verdict.get("decision") == "no_product":
            return NoProductVerdict(phrase=verdict.get("phrase"))

    # Turn B: source_absence check (attempt 2 only, i.e. index 1)
    if ctx.attempt == 1:
        absent = await _ask_source_absence(judgment_llm, ctx)
        if absent and absent.get("decision") == "source_absent":
            return SourceAbsent(reason=absent.get("reason", "source_absent"))

    # Turn C: parser generation
    parser_source = await _gen_parser(parser_gen_llm, ctx)
    if not parser_source:
        return CandidateFailed(errors=["parser_gen returned nothing"])
    ctx.candidates_tried.append(parser_source)

    # Sandbox execution
    sandbox_result = await run_in_sandbox(parser_source, ctx.html, ctx.url)
    if not isinstance(sandbox_result, dict):
        # F1: preserve the full traceback (when the sandbox result carries one) so
        # the next attempt's prompt shows line numbers and the failing expression.
        return CandidateFailed(errors=[_format_sandbox_error(sandbox_result)])

    # Wrap + validate
    wrapped = dict(sandbox_result)
    wrapped.setdefault("url", ctx.url)
    wrapped["website"] = ctx.site
    wrapped["source_type"] = "html"
    wrapped["scraped_at"] = datetime.now(timezone.utc)
    wrapped["parser_version"] = f"agent_attempt_{ctx.attempt}"

    product, errors = validate(wrapped)
    if product is None:
        return CandidateFailed(errors=errors)

    # Golden test + promote (M9)
    from .golden import promote_candidate

    parser_id = await promote_candidate(
        site=ctx.site,
        code=parser_source,
        current_product=product,
        current_html=ctx.html,
    )
    if parser_id is None:
        return CandidateFailed(errors=["failed golden test"])

    # Rewrite parser_version to the promoted parser's version
    return CandidateSucceeded(
        product=product,
        parser_id=parser_id,
        parser_source=parser_source,
    )


def _make_llm(model: str, temperature: float = 0.1, enable_thinking: bool = False):
    """Build a DeepSeek LangChain client.

    Args:
      model: model id (e.g. "deepseek-v4-flash" or "deepseek-v4-pro")
      temperature: sampling temperature (0.1 for judgments, ramp for parser_gen)
      enable_thinking: when True, enables DeepSeek's reasoning/thinking mode via
        `reasoning_effort="high"` and `extra_body={"thinking": {"type": "enabled"}}`.
        Only used on the last repair-ladder attempt so the pro model can reason
        through hard cases (spec-driven parser generation on gnarly pages).
    """
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.error("langchain_openai not installed")
        return None

    cfg = get_config()
    if not cfg.deepseek_key:
        logger.warning("DEEPSEEK_KEY not set — repair ladder cannot invoke LLM")
        return None

    model_kwargs: dict[str, Any] = {"response_format": {"type": "json_object"}}
    if enable_thinking:
        model_kwargs["reasoning_effort"] = "high"
        model_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

    return ChatOpenAI(
        api_key=cfg.deepseek_key,
        base_url=cfg.deepseek_base_url,
        model=model,
        temperature=temperature,
        model_kwargs=model_kwargs,
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
    try:
        resp = await llm.ainvoke(source_absence_prompt(ctx.html, ctx.site, ctx.error_history))
        return _parse(resp)
    except Exception:
        logger.exception("source_absence prompt failed")
        return None


async def _gen_parser(llm, ctx: RepairContext) -> Optional[str]:
    try:
        resp = await llm.ainvoke(parser_gen_prompt(
            ctx.html, ctx.site, tier=ctx.attempt,
            prior_errors=ctx.error_history,
            prior_candidates=ctx.candidates_tried,
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
    try:
        cfg = get_config()
        db = ScrapeDB(cfg.db_path)
        db.init_db()
        PhraseStore(db).add(site, phrase, source="agent_backfill")
        db.close()
        logger.info("backfilled phrase for site=%s: %r", site, phrase[:80])
    except Exception:
        logger.exception("phrase backfill failed")
