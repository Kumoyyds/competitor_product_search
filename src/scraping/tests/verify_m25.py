"""M25 verification: evidence-gated repair source-absence pre-screen.

Fully offline. Covers the Turn B evidence matrix, one/two/three-node ladder
positions, source-absence short-circuit and fail-open behavior, run signature
persistence, and router fallback.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  ({detail})")


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def configure(db_path: Path, node_count: int = 2) -> None:
    from src.scraping.config import ScrapingConfig, set_config

    set_config(
        ScrapingConfig(
            db_path=db_path,
            repair_model_ladder=[f"offline-{index}" for index in range(node_count)],
            repair_temperature_ladder=[0.1] * node_count,
            _env_file=None,
        )
    )


def prior_attempt(
    failure_stage: str,
    missing_required: list[str] | None,
):
    from src.scraping.repair.agent import AttemptRecord

    capture = None
    if missing_required is not None:
        capture = {
            "captured": ["in_stock"],
            "missing_required": missing_required,
            "missing_optional": [],
        }
    return AttemptRecord(
        index=0,
        model="offline-0",
        code="def parse(html, url): return {}",
        capture=capture,
        failure_stage=failure_stage,
    )


async def run_attempt_one(previous, verdict: str):
    from src.scraping.repair.agent import RepairContext, _try_repair

    ctx = RepairContext(
        site="m25",
        url="https://m25.example/product",
        html="<html><h1>Fixture</h1></html>",
        attempts=[previous],
    )
    ask = AsyncMock(return_value={"decision": verdict, "reason": "offline fixture"})
    generate = AsyncMock(return_value=None)
    with (
        patch("src.scraping.repair.agent._make_llm", return_value=object()),
        patch("src.scraping.repair.agent._ask_source_absence", new=ask),
        patch("src.scraping.repair.agent._gen_parser", new=generate),
    ):
        outcome = await _try_repair(
            ctx, index=1, model="offline-1", is_last=True
        )
    return outcome, ask.await_count, generate.await_count


async def verify_evidence_matrix(db_path: Path) -> None:
    from src.scraping.repair.agent import CandidateFailed

    section("M25.1 - Turn B evidence matrix on the final two-node rung")
    configure(db_path, node_count=2)

    outcome, asks, generations = await run_attempt_one(
        prior_attempt("gate", ["price"]), "solvable"
    )
    check(
        "gate failure with missing required fields asks Turn B",
        asks == 1 and generations == 1 and isinstance(outcome, CandidateFailed),
        f"asks={asks}, parser_gen={generations}, outcome={type(outcome).__name__}",
    )

    _, asks, _ = await run_attempt_one(prior_attempt("sandbox", None), "solvable")
    check(
        "sandbox failure does not ask Turn B",
        asks == 0,
        f"asks={asks}",
    )

    _, asks, _ = await run_attempt_one(
        prior_attempt("golden", []), "solvable"
    )
    check(
        "golden rejection does not ask Turn B",
        asks == 0,
        f"asks={asks}",
    )

    _, asks, _ = await run_attempt_one(prior_attempt("gate", []), "solvable")
    check(
        "optional-only gate failure does not ask Turn B",
        asks == 0,
        f"asks={asks}",
    )


async def verify_short_circuit_and_fail_open(db_path: Path) -> None:
    from src.scraping.exceptions import ScrapeFailed
    from src.scraping.repair import agent as agent_mod
    from src.scraping.repair.agent import CandidateFailed, RepairContext, run_repair_ladder

    section("M25.2 - Source-absence short-circuit and fail-open behavior")
    configure(db_path, node_count=2)

    scraper = type("OfflineScraper", (), {"site": "m25"})()
    generate = AsyncMock(
        return_value="def parse(html, url): return {'in_stock': True}"
    )
    with (
        patch("src.scraping.repair.agent._make_llm", return_value=object()),
        patch(
            "src.scraping.repair.agent._ask_no_product",
            new=AsyncMock(return_value={"decision": "product"}),
        ),
        patch(
            "src.scraping.repair.agent._ask_source_absence",
            new=AsyncMock(
                return_value={
                    "decision": "source_absent",
                    "reason": "required price is absent",
                }
            ),
        ),
        patch("src.scraping.repair.agent._gen_parser", new=generate),
        patch(
            "src.scraping.repair.agent.run_in_sandbox",
            new=AsyncMock(return_value={"in_stock": True}),
        ),
    ):
        terminal = await run_repair_ladder(
            scraper=scraper,
            url="https://m25.example/missing-price",
            html="<html><h1>Fixture</h1></html>",
            initial_errors=["saved parser missed price"],
        )
    check(
        "source_absent stops before final parser generation",
        isinstance(terminal, ScrapeFailed)
        and terminal.failed_stage == "source_absent"
        and generate.await_count == 1,
        (
            f"stage={getattr(terminal, 'failed_stage', None)}, "
            f"parser_gen_calls={generate.await_count}"
        ),
    )

    outcome, asks, generations = await run_attempt_one(
        prior_attempt("gate", ["price"]), "solvable"
    )
    check(
        "solvable verdict continues to parser generation",
        asks == 1 and generations == 1 and isinstance(outcome, CandidateFailed),
        f"asks={asks}, parser_gen={generations}",
    )

    class ExplodingLLM:
        async def ainvoke(self, prompt):
            raise RuntimeError("offline judgment failure")

    ctx = RepairContext(
        site="m25",
        url="https://m25.example/llm-error",
        html="<html></html>",
        attempts=[prior_attempt("gate", ["price"])],
    )
    generate_after_error = AsyncMock(return_value=None)
    with (
        patch("src.scraping.repair.agent._make_llm", return_value=ExplodingLLM()),
        patch("src.scraping.repair.agent._gen_parser", new=generate_after_error),
        patch.object(agent_mod.logger, "exception"),
    ):
        outcome = await agent_mod._try_repair(
            ctx, index=1, model="offline-1", is_last=True
        )
    check(
        "source-absence LLM error fails open to parser generation",
        isinstance(outcome, CandidateFailed)
        and generate_after_error.await_count == 1,
        f"parser_gen={generate_after_error.await_count}",
    )


async def verify_ladder_positions(db_path: Path) -> None:
    from src.scraping.repair.agent import RepairContext, _try_repair

    section("M25.3 - One-node and three-node ladder positions")

    configure(db_path, node_count=1)
    ask_absence = AsyncMock(return_value={"decision": "source_absent"})
    with (
        patch("src.scraping.repair.agent._make_llm", return_value=object()),
        patch(
            "src.scraping.repair.agent._ask_no_product",
            new=AsyncMock(return_value={"decision": "product"}),
        ),
        patch("src.scraping.repair.agent._ask_source_absence", new=ask_absence),
        patch(
            "src.scraping.repair.agent._gen_parser",
            new=AsyncMock(return_value=None),
        ),
    ):
        await _try_repair(
            RepairContext("m25", "https://m25.example/one", "<html></html>"),
            index=0,
            model="offline-0",
            is_last=True,
        )
    check(
        "one-node ladder never asks Turn B",
        ask_absence.await_count == 0,
        f"asks={ask_absence.await_count}",
    )

    configure(db_path, node_count=3)
    ask_absence = AsyncMock(return_value={"decision": "solvable"})
    ctx = RepairContext(
        "m25",
        "https://m25.example/three",
        "<html></html>",
        attempts=[prior_attempt("gate", ["price"])],
    )
    with (
        patch("src.scraping.repair.agent._make_llm", return_value=object()),
        patch("src.scraping.repair.agent._ask_source_absence", new=ask_absence),
        patch(
            "src.scraping.repair.agent._gen_parser",
            new=AsyncMock(return_value=None),
        ),
    ):
        await _try_repair(ctx, index=1, model="offline-1", is_last=False)
        await _try_repair(ctx, index=2, model="offline-2", is_last=True)
    check(
        "three-node ladder asks Turn B only at index 1",
        ask_absence.await_count == 1,
        f"asks={ask_absence.await_count}",
    )


async def verify_observability_and_fallback(db_path: Path) -> None:
    from src.scraping import router as router_mod
    from src.scraping.exceptions import ScrapeFailed
    from src.scraping.models.results import InvalidTargetResult
    from src.scraping.scrapers.base import BaseScraper
    from src.scraping.storage import ScrapeDB

    section("M25.4 - Source-absence observability and router fallback")
    configure(db_path, node_count=2)

    class RecordingScraper(BaseScraper):
        site = "m25"
        source_type = "html"

        async def scrape(self, url: str):
            raise NotImplementedError

    failure = ScrapeFailed(
        site="m25",
        url="https://m25.example/recorded",
        scraper_name="RecordingScraper",
        failed_stage="source_absent",
        signature=("m25", "source_absent", ""),
        errors=["required price is absent"],
    )
    RecordingScraper()._record_failure(
        failure.url, "m25.example", failure, latency=12
    )
    db = ScrapeDB(db_path)
    db.init_db()
    row = dict(
        db.conn.execute(
            "SELECT outcome, signature FROM scrape_runs WHERE url = ?",
            (failure.url,),
        ).fetchone()
    )
    db.close()
    check(
        "source_absent is queryable in scrape_runs.signature",
        row == {"outcome": "escalated", "signature": "m25|source_absent|"},
        str(row),
    )

    calls: list[str] = []

    class SourceAbsentScraper:
        async def scrape(self, url: str):
            calls.append("primary")
            raise ScrapeFailed(
                site="m25",
                url=url,
                scraper_name="SourceAbsentScraper",
                failed_stage="source_absent",
                signature=("m25", "source_absent", ""),
            )

    class BackupScraper:
        async def scrape(self, url: str):
            calls.append("backup")
            return InvalidTargetResult(
                url=url, site="m25", reason_signal="offline backup fixture"
            )

    with (
        patch("src.scraping.router.resolve_site", return_value="m25"),
        patch(
            "src.scraping.router.get_scrapers",
            return_value=[SourceAbsentScraper, BackupScraper],
        ),
    ):
        result = await router_mod.scrape("https://m25.example/fallback")
    check(
        "router tries backup after source_absent",
        calls == ["primary", "backup"]
        and isinstance(result, InvalidTargetResult),
        f"calls={calls}, result={type(result).__name__}",
    )


async def run(db_path: Path) -> None:
    await verify_evidence_matrix(db_path)
    await verify_short_circuit_and_fail_open(db_path)
    await verify_ladder_positions(db_path)
    await verify_observability_and_fallback(db_path)


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="verify_m25_") as tmp:
            asyncio.run(run(Path(tmp) / "scraping.db"))
    except Exception:
        FAILED.append(("EXCEPTION", ""))
        traceback.print_exc()

    print()
    print("=" * 72)
    print(f"SUMMARY: {len(PASSED)} passed, {len(FAILED)} failed")
    print("=" * 72)
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name}  ({detail})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
