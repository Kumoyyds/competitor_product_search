"""Validate the search pipeline on src/0_Data/tesco_algo.xlsx with a Serper budget cap.

Usage:
    python scripts/validate_search.py --sample 30 --budget 50

Stratified sample: half rows with a non-'not found' legacy url_search_1, half without.
Pre-pass: prints numeric extraction over every sampled product name (no Serper used).
End-to-end: runs the pipeline within the Serper budget, writes a report Excel
and prints a per-layer summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections import Counter

import pandas as pd

# allow `python scripts/validate_search.py` from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.search.layers.numeric import extract_numerics  # noqa: E402
from src.search.models import FinalVerdict  # noqa: E402
from src.search.pipeline import match_product  # noqa: E402
from src.search.providers import SerperProvider  # noqa: E402
from src.search.providers.base import BudgetExhausted  # noqa: E402


DEFAULT_INPUT = os.path.join("src", "0_Data", "tesco_algo.xlsx")
DEFAULT_REPORT = os.path.join("output", "validation_report.xlsx")


def stratified_sample(df: pd.DataFrame, n: int, seed: int = 42) -> pd.DataFrame:
    has_url = df[df["url_search_1"].astype(str).str.lower() != "not found"]
    no_url = df[df["url_search_1"].astype(str).str.lower() == "not found"]
    half = n // 2
    rng = random.Random(seed)
    h_idx = list(has_url.index)
    n_idx = list(no_url.index)
    rng.shuffle(h_idx)
    rng.shuffle(n_idx)
    pick = h_idx[:half] + n_idx[: n - half]
    out = df.loc[pick].reset_index(drop=True)
    return out


def print_numeric_prepass(rows: pd.DataFrame) -> None:
    print("\n=== Numeric extraction pre-pass (no Serper) ===")
    for _, r in rows.iterrows():
        name = str(r["item_sku_name_en_new"])
        attrs = extract_numerics(name)
        print(f"  {name}")
        print(f"    -> {attrs}")
    print()


async def _run_pipeline(rows: pd.DataFrame, budget: int, country: str) -> tuple[list[dict], int]:
    provider = SerperProvider(max_calls=budget)
    out: list[dict] = []
    budget_hit = False
    try:
        for _, r in rows.iterrows():
            name = str(r["item_sku_name_en_new"])
            web = str(r["web_1"]).lower()
            row = {
                "product_name": name,
                "legacy_url": r["url_search_1"],
                "new_verdict": "",
                "new_url": "",
                "layer_trace_json": "",
                "candidates_considered": 0,
                "reason": "",
            }
            if budget_hit:
                row["reason"] = "skipped: budget already exhausted"
                out.append(row)
                continue
            try:
                res = await match_product(name, web, country=country, provider=provider)
                row["new_verdict"] = res.verdict.value
                row["new_url"] = res.matched_candidate.url if res.matched_candidate else "not found"
                row["layer_trace_json"] = json.dumps(res.layer_trace.to_dict())
                row["candidates_considered"] = res.candidates_considered
                row["reason"] = res.reason
            except BudgetExhausted as e:
                budget_hit = True
                row["new_verdict"] = "budget_exhausted"
                row["reason"] = str(e)
            except Exception as e:
                row["new_verdict"] = "error"
                row["reason"] = f"{type(e).__name__}: {e}"
            out.append(row)
    finally:
        await provider.aclose()
    return out, provider.calls_made()


def summarize(rows: list[dict], calls_used: int) -> None:
    print("\n=== End-to-end summary ===")
    print(f"rows processed: {len(rows)}    serper calls used: {calls_used}")

    verdict_counts = Counter(r["new_verdict"] for r in rows)
    print(f"verdict mix: {dict(verdict_counts)}")

    layer_counts = {layer: Counter() for layer in ("domain", "brand", "numeric", "distinguishing")}
    for r in rows:
        if not r["layer_trace_json"]:
            continue
        trace = json.loads(r["layer_trace_json"])
        for layer, ctr in layer_counts.items():
            ctr[trace.get(layer)] += 1
    for layer, ctr in layer_counts.items():
        print(f"  {layer:14s}: {dict(ctr)}")

    legacy_has_url = sum(1 for r in rows if str(r["legacy_url"]).lower() != "not found")
    new_has_url = sum(
        1 for r in rows if r["new_verdict"] == FinalVerdict.MATCH.value
    )
    agree = sum(
        1
        for r in rows
        if (
            (str(r["legacy_url"]).lower() != "not found")
            == (r["new_verdict"] == FinalVerdict.MATCH.value)
        )
    )
    print(
        f"legacy-found: {legacy_has_url}   new-found: {new_has_url}   "
        f"agreement (match-vs-not): {agree}/{len(rows)}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_INPUT)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--budget", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--country", default="uk")
    args = ap.parse_args()

    df = pd.read_excel(args.input)
    print(f"loaded {len(df)} rows from {args.input}")

    sampled = stratified_sample(df, args.sample, seed=args.seed)
    print(f"sampled {len(sampled)} rows (stratified: ~half with legacy url, ~half without)")

    print_numeric_prepass(sampled)

    rows, calls_used = asyncio.run(
        _run_pipeline(sampled, budget=args.budget, country=args.country)
    )

    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    report_df = pd.DataFrame(rows)
    report_df.to_excel(args.report, index=False)
    print(f"\nreport saved -> {args.report}")

    summarize(rows, calls_used)


if __name__ == "__main__":
    main()
