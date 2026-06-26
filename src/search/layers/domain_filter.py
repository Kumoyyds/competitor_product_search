from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from .. import config
from ..models import CandidateEval, Verdict


def _host_matches(host: str, target: str) -> bool:
    host = host.lower().lstrip(".")
    target = target.lower().lstrip(".")
    return host == target or host.endswith("." + target)


async def domain_filter_node(state: dict[str, Any]) -> dict[str, Any]:
    website: str = state["website"]
    target = config.domain_for(website)
    candidates: list[CandidateEval] = state.get("candidates", [])

    if not target:
        for c in candidates:
            c.trace.domain = Verdict.FAIL
            c.alive = False
        return {**state, "candidates": candidates}

    for c in candidates:
        try:
            host = urlparse(c.raw.url).netloc
        except Exception:
            host = ""
        if host and _host_matches(host, target):
            c.trace.domain = Verdict.PASS
        else:
            c.trace.domain = Verdict.FAIL
            c.alive = False

    return {**state, "candidates": candidates}
