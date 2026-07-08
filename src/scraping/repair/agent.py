"""Repair Agent — the ladder-based candidate parser generator (spec §5.5, D8).

Called from HTMLScraper when the ordered parser list produces no valid output.
Shared budget of 3 attempts (parse-exception + Gate1 + Gate2 + promote-fail all count).

Ladder:
  Attempt 1: deepseek-chat (flash)
  Attempt 2: deepseek-chat (flash) with prior error context
             + source_absence check (spec §5.5) — if source is absent, terminate
  Attempt 3: deepseek-reasoner (pro) with all prior errors

Each attempt starts with a no_product_on_page check (Turn A) — if true, terminate
ladder immediately (no budget consumed), record InvalidTargetResult, backfill phrase.
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
        outcome = await _try_repair(ctx, model)

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


async def _try_repair(ctx: RepairContext, model: str) -> RepairOutcome:
    llm = _make_llm(model)
    if llm is None:
        return CandidateFailed(errors=["LLM not configured (DEEPSEEK_KEY missing)"])

    # Turn A: no_product judgment
    verdict = await _ask_no_product(llm, ctx)
    if verdict and verdict.get("decision") == "no_product":
        return NoProductVerdict(phrase=verdict.get("phrase"))

    # Turn B: source_absence check (attempt 2 only, i.e. index 1)
    if ctx.attempt == 1:
        absent = await _ask_source_absence(llm, ctx)
        if absent and absent.get("decision") == "source_absent":
            return SourceAbsent(reason=absent.get("reason", "source_absent"))

    # Turn C: parser generation
    parser_source = await _gen_parser(llm, ctx)
    if not parser_source:
        return CandidateFailed(errors=["parser_gen returned nothing"])
    ctx.candidates_tried.append(parser_source)

    # Sandbox execution
    sandbox_result = await run_in_sandbox(parser_source, ctx.html, ctx.url)
    if not isinstance(sandbox_result, dict):
        return CandidateFailed(
            errors=[f"sandbox rejected: {type(sandbox_result).__name__}: "
                    f"{getattr(sandbox_result, 'reason', getattr(sandbox_result, 'message', sandbox_result))}"]
        )

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


def _make_llm(model: str):
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.error("langchain_openai not installed")
        return None

    cfg = get_config()
    if not cfg.deepseek_key:
        logger.warning("DEEPSEEK_KEY not set — repair ladder cannot invoke LLM")
        return None

    return ChatOpenAI(
        api_key=cfg.deepseek_key,
        base_url=cfg.deepseek_base_url,
        model=model,
        temperature=0.1,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


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
