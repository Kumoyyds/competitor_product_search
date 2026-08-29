from __future__ import annotations

import pytest

from src.scraping.repair.sandbox import SandboxTimeout, run_in_sandbox


@pytest.mark.slow
async def test_cpu_limit_and_wall_clock_limit_share_timeout_contract() -> None:
    result = await run_in_sandbox(
        "def parse(html, url):\n    while True: pass",
        "",
        "https://sandbox.example/timeout",
        timeout=2,
    )

    assert result == SandboxTimeout(timeout=2)
