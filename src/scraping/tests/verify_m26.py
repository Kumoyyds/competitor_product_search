"""M26 offline verification: subprocess and connection lifecycle hardening."""

from __future__ import annotations

import asyncio
import errno
import os
import tempfile
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.scraping.config import ScrapingConfig, set_config

from ._harness import FAILED, PASSED, SKIPPED, check, section, skip, run_main






def configure(db_path: Path, **overrides) -> ScrapingConfig:
    cfg = ScrapingConfig(db_path=db_path, _env_file=None, **overrides)
    set_config(cfg)
    return cfg


async def verify_sandbox_lifecycle(db_path: Path) -> None:
    from src.scraping.repair.sandbox import (
        SandboxTimeout,
        active_child_pids,
        run_in_sandbox,
    )

    section("M26.1 - cancellation and timeout reap child processes")
    configure(db_path, sandbox_max_concurrency=3)

    task = asyncio.create_task(
        run_in_sandbox(
            "def parse(html, url):\n    while True:\n        pass",
            "<html></html>",
            "https://sandbox.example/cancel",
            timeout=30,
        )
    )
    for _ in range(200):
        if active_child_pids():
            break
        await asyncio.sleep(0.01)
    spawned = active_child_pids()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("cancelled sandbox registered a child", len(spawned) == 1, str(spawned))
    check("cancelled sandbox leaves no owned child", not active_child_pids())
    gone = True
    for pid in spawned:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        else:
            gone = False
    check("cancelled child is waitpid-reaped before return", gone, str(spawned))

    result = await run_in_sandbox(
        "def parse(html, url):\n    while True:\n        pass",
        "",
        "https://sandbox.example/timeout",
        timeout=0.1,
    )
    check("timeout contract remains SandboxTimeout", isinstance(result, SandboxTimeout))
    check("timeout path leaves no owned child", not active_child_pids())


async def verify_spawn_retry(db_path: Path) -> None:
    from src.scraping.exceptions import SandboxSpawnError
    from src.scraping.repair import sandbox

    section("M26.2 - transient spawn exhaustion retries with a typed failure")
    configure(
        db_path,
        sandbox_spawn_retries=2,
        sandbox_spawn_retry_interval=0,
    )
    real_spawn = asyncio.create_subprocess_exec
    attempts = 0

    async def flaky_spawn(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise BlockingIOError(errno.EAGAIN, "offline process-table fixture")
        return await real_spawn(*args, **kwargs)

    with patch.object(sandbox.asyncio, "create_subprocess_exec", new=flaky_spawn):
        result = await sandbox.run_in_sandbox(
            "def parse(html, url):\n    return {'ok': True}", "", "retry://ok"
        )
    check("EAGAIN succeeds after configured retries", result == {"ok": True})
    check("spawn retry count is initial plus two retries", attempts == 3, str(attempts))

    exhausted = AsyncMock(
        side_effect=BlockingIOError(errno.EAGAIN, "offline exhausted fixture")
    )
    caught: BaseException | None = None
    with patch.object(sandbox.asyncio, "create_subprocess_exec", new=exhausted):
        try:
            await sandbox.run_in_sandbox(
                "def parse(html, url):\n    return {}", "", "retry://failed"
            )
        except SandboxSpawnError as exc:
            caught = exc
    check("exhausted spawn raises SandboxSpawnError", caught is not None, str(caught))
    check("exhausted spawn attempts exactly retries + 1", exhausted.await_count == 3)
    check("failed spawn never enters live-child registry", not sandbox.active_child_pids())


async def verify_sandbox_gate(db_path: Path) -> None:
    from src.scraping.repair.sandbox import active_child_pids, run_in_sandbox

    section("M26.3 - sandbox subprocess concurrency is bounded")
    limit = 3
    configure(db_path, sandbox_max_concurrency=limit)
    code = (
        "def parse(html, url):\n"
        "    total = 0\n"
        "    for i in range(5000000):\n"
        "        total += i\n"
        "    return {'total': total}\n"
    )
    tasks = [
        asyncio.create_task(run_in_sandbox(code, "", f"gate://{index}", timeout=5))
        for index in range(12)
    ]
    peak = 0
    while any(not task.done() for task in tasks):
        peak = max(peak, len(active_child_pids()))
        await asyncio.sleep(0.005)
    results = await asyncio.gather(*tasks)
    check("all gated sandbox calls complete", all(isinstance(item, dict) for item in results))
    check("live subprocess peak does not exceed limit", 0 < peak <= limit, str(peak))
    check("gated batch leaves no child", not active_child_pids())


def _rows(db_path: Path, table: str) -> list[dict]:
    from src.scraping.storage import ScrapeDB

    db = ScrapeDB(db_path)
    db.init_db()
    try:
        return [dict(row) for row in db.conn.execute(f"SELECT * FROM {table}")]
    finally:
        db.close()


async def verify_html_and_router_fallback(db_path: Path) -> None:
    from src.scraping import router
    from src.scraping.exceptions import ScrapeFailed
    from src.scraping.scrapers.html_scraper import HTMLScraper

    section("M26.4 - sandbox spawn failures become observable router fallbacks")
    configure(db_path)

    class BrokenHTML(HTMLScraper):
        site = "m26_html"

        def _get_unlocker(self):
            class DummyUnlocker:
                async def fetch(self, target: str):
                    return 200, "<html><h1>Product</h1></html>"

            return DummyUnlocker()

        async def _run_parsers(self, html: str, url: str):
            raise BlockingIOError(errno.EAGAIN, "offline parser spawn fixture")

    url = "https://m26.example/product"
    caught: ScrapeFailed | None = None
    with (
        patch(
            "src.scraping.scrapers.html_scraper.with_extraction_retry",
            new=AsyncMock(return_value=(200, "<html><h1>Product</h1></html>")),
        ),
        patch("src.scraping.scrapers.html_scraper.detect_invalid_page", return_value=None),
    ):
        try:
            await BrokenHTML().scrape(url)
        except ScrapeFailed as exc:
            caught = exc
    runs = [row for row in _rows(db_path, "scrape_runs") if row["url"] == url]
    check(
        "raw parser-list OSError becomes sandbox_spawn ScrapeFailed",
        caught is not None
        and caught.failed_stage == "parser_list"
        and caught.signature[1] == "sandbox_spawn",
        str(caught),
    )
    check(
        "sandbox spawn failure writes an escalated scrape_run",
        len(runs) == 1
        and runs[0]["outcome"] == "escalated"
        and "sandbox_spawn" in (runs[0]["signature"] or ""),
        str(runs),
    )

    calls: list[str] = []

    class First:
        async def scrape(self, target: str):
            calls.append("first")
            raise ScrapeFailed(
                site="m26_router",
                url=target,
                scraper_name="First",
                failed_stage="parser_list",
                signature=("m26_router", "sandbox_spawn", ""),
            )

    class Second:
        async def scrape(self, target: str):
            calls.append("second")
            return "fallback-ok"

    with (
        patch.object(router, "resolve_site", return_value="m26_router"),
        patch.object(router, "get_scrapers", return_value=[First, Second]),
    ):
        result = await router.scrape("https://router.example/success")
    check("router tries the backup after sandbox spawn failure", calls == ["first", "second"])
    check("backup scraper can recover the request", result == "fallback-ok")

    class AlsoFails:
        async def scrape(self, target: str):
            raise ScrapeFailed(
                site="m26_router",
                url=target,
                scraper_name="AlsoFails",
                failed_stage="api_malformed",
                signature=("m26_router", "api_malformed", ""),
            )

    with (
        patch.object(router, "resolve_site", return_value="m26_router"),
        patch.object(router, "get_scrapers", return_value=[First, AlsoFails]),
    ):
        try:
            await router.scrape("https://router.example/exhausted")
        except ScrapeFailed:
            pass
    escalations = _rows(db_path, "escalations")
    check(
        "all-exhausted fallback writes aggregate escalation",
        any(row["signature"] == "m26_router|api_malformed|" for row in escalations),
        str(escalations),
    )


def verify_db_finally(db_path: Path) -> None:
    from src.scraping.scrapers.html_scraper import HTMLScraper
    from src.scraping.storage import ParserStore, ScrapeDB

    section("M26.5 - database connections close on exceptional paths")
    configure(db_path)

    class Probe(HTMLScraper):
        site = "m26_db"

        def _get_unlocker(self):
            raise NotImplementedError

    closed: list[ScrapeDB] = []
    real_close = ScrapeDB.close

    def close_spy(db: ScrapeDB) -> None:
        closed.append(db)
        real_close(db)

    async def exercise() -> None:
        with (
            patch.object(
                ParserStore,
                "get_active_ordered_by_hits",
                side_effect=RuntimeError("offline DB read fixture"),
            ),
            patch.object(ScrapeDB, "close", new=close_spy),
        ):
            try:
                await Probe()._run_parsers("<html></html>", "db://fixture")
            except RuntimeError:
                pass

    asyncio.run(exercise())
    check("parser-store exception still closes ScrapeDB", len(closed) == 1, str(len(closed)))


class _FakeSyncRoot:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeAsyncRoot:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeChatOpenAI:
    calls = 0

    def __init__(self, **kwargs) -> None:
        type(self).calls += 1
        self.kwargs = kwargs
        self.root_client = _FakeSyncRoot()
        self.root_async_client = _FakeAsyncRoot()


def verify_client_cache(db_path: Path) -> None:
    from src.scraping.providers import (
        _close_chat_clients_at_exit,
        close_chat_clients,
        make_chat_client,
        reset_chat_clients,
    )

    section("M26.6 - LLM clients reuse and close connection pools")
    configure(db_path, qwen_key="offline-key")
    reset_chat_clients()
    _FakeChatOpenAI.calls = 0
    with patch("langchain_openai.ChatOpenAI", new=_FakeChatOpenAI):
        first = make_chat_client("qwen3.7-plus", purpose="first use")
        same = make_chat_client("qwen3.7-plus", purpose="different purpose")
        different = make_chat_client("qwen3.7-plus", temperature=0.4)
    check("same effective LLM parameters reuse one client", first is same)
    check("different LLM parameters use another client", first is not different)
    check("only two clients were constructed", _FakeChatOpenAI.calls == 2)
    roots = [
        first.root_client,
        first.root_async_client,
        different.root_client,
        different.root_async_client,
    ]
    close_chat_clients()
    check("cached sync and async HTTP roots are closed", all(root.closed for root in roots))

    with patch("langchain_openai.ChatOpenAI", new=_FakeChatOpenAI):
        shutdown_client = make_chat_client("qwen3.7-plus", purpose="atexit fixture")
    _close_chat_clients_at_exit()
    check(
        "atexit still closes loop-independent synchronous HTTP roots",
        shutdown_client.root_client.closed,
    )
    check(
        "atexit skips async HTTP roots whose owning loop may be closed",
        not shutdown_client.root_async_client.closed,
    )


async def verify_coldstart_limit(db_path: Path) -> None:
    from src.scraping.coldstart import _batch_fetch

    section("M26.7 - cold-start fetch fan-out respects site concurrency")
    limit = 3
    configure(db_path, per_site_concurrency=limit)
    active = 0
    peak = 0

    class Unlocker:
        async def fetch(self, url: str):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.02)
                return 200, f"<html>{url}</html>"
            finally:
                active -= 1

    class Scraper:
        def _get_unlocker(self):
            return Unlocker()

    urls = [f"https://cold.example/{index}" for index in range(12)]
    results = await _batch_fetch(Scraper(), urls)
    check("cold-start returns every requested URL", [row[0] for row in results] == urls)
    check("cold-start network peak respects configured limit", peak == limit, str(peak))


def verify_config_guards() -> None:
    section("M26.8 - resource limits reject deadlocking values")
    rejected = 0
    for values in (
        {"sandbox_max_concurrency": 0},
        {"sandbox_spawn_retries": -1},
        {"sandbox_spawn_retry_interval": -0.1},
        {"per_site_concurrency": 0},
    ):
        try:
            ScrapingConfig(_env_file=None, **values)
        except ValueError:
            rejected += 1
    check("all invalid resource-limit settings are rejected", rejected == 4, str(rejected))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="verify_m26_") as temp_dir:
        db_path = Path(temp_dir) / "m26.db"
        return run_main(
            lambda: verify_sandbox_lifecycle(db_path),
            lambda: verify_spawn_retry(db_path),
            lambda: verify_sandbox_gate(db_path),
            lambda: verify_html_and_router_fallback(db_path),
            lambda: verify_db_finally(db_path),
            lambda: verify_client_cache(db_path),
            lambda: verify_coldstart_limit(db_path),
            verify_config_guards,
        )


if __name__ == "__main__":
    raise SystemExit(main())
