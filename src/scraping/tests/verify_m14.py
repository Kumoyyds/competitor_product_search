"""Verification for M14 — price-aware pre-pass + prompt rewrite + membership golden bucket.

Covers:
  Tier 1 — pre-pass + anchoring (offline, no Qwen, no DB)
  Tier 2 — prompt rendering (offline, no Qwen)
  Tier 3 — end-to-end parser-gen (gated on QWEN_KEY, best-effort)

See tests/README.md for the full inventory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).parent.parent / "data" / "html_sample"

from ._harness import FAILED, PASSED, SKIPPED, check, section, skip, run_main








def load_fixture(name: str) -> str:
    path = DATA_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8")


def find_evidence(evidence_list, **kwargs) -> Optional[Any]:
    """Find a PriceEvidence item matching all given kwargs."""
    for ev in evidence_list:
        match = True
        for k, v in kwargs.items():
            ev_val = getattr(ev, k, None)
            if isinstance(v, str) and isinstance(ev_val, str):
                if v not in ev_val:
                    match = False
                    break
            elif ev_val != v:
                match = False
                break
        if match:
            return ev
    return None


# ---------------------------------------------------------------------------
# Tier 1 — pre-pass + anchoring (offline)
# ---------------------------------------------------------------------------


def run_tier1() -> None:
    from src.scraping.repair.prepass import build_price_aware_context

    section("M14.1 - argos_frame_discount: DOM list_price + cross-sell deletion")

    html = load_fixture("argos_frame_discount.html")
    url = "https://www.argos.co.uk/product/3284476"
    ctx = build_price_aware_context(html, url)

    check("url_product_id extracted", ctx.url_product_id == "3284476",
          f"got={ctx.url_product_id}")
    check("canonical_title is non-empty", bool(ctx.canonical_title),
          ctx.canonical_title[:60])

    # JSON-LD source should have current price 52.20
    jl_price = find_evidence(ctx.price_evidence, source="json_ld", value="52.20")
    check("json_ld evidence has price=52.20", jl_price is not None,
          f"found={jl_price is not None}")

    # DOM source should have list_price 58.00 with struck-through
    dom_58 = find_evidence(ctx.price_evidence, source="dom", value="58.00")
    check("dom evidence has list_price 58.00", dom_58 is not None)
    if dom_58:
        check("dom 58.00 struck_through", dom_58.struck_through is True,
              f"struck={dom_58.struck_through}")
        check("dom 58.00 anchor is inside_main or ambiguous",
              dom_58.anchor_relation in ("inside_main", "ambiguous"),
              f"anchor={dom_58.anchor_relation}")
        check("dom 58.00 css_hint includes price-was",
              "price-was" in (dom_58.css_hint or "").lower(),
              dom_58.css_hint[:60])

    # Cross-sell prices should be deleted (alternatives bundle prices 700, 370, etc.)
    bundle_prices = [ev for ev in ctx.price_evidence
                     if ev.value in {"700", "370", "470", "270", "250", "440"}]
    check("cross-sell bundle prices are deleted", len(bundle_prices) == 0,
          f"found={len(bundle_prices)} values={[e.value for e in bundle_prices]}")

    # Unit price evidence should be empty for this fixture (no /kg prices)
    check("no unit_price_evidence for frame discount",
          len(ctx.unit_price_evidence) == 0,
          f"count={len(ctx.unit_price_evidence)}")

    section("M14.2 - tesco_net_discount: JSON-LD priceSpecification + validForMemberTier")

    html = load_fixture("tesco_net_discount.html")
    url = "https://www.tesco.com/groceries/en-GB/products/325098267"
    ctx = build_price_aware_context(html, url)

    # offers.price = 129.99 (regular)
    jl_129 = find_evidence(ctx.price_evidence, source="json_ld", value="129.99")
    check("json_ld offers.price=129.99 (regular)", jl_129 is not None)

    # priceSpecification.price = 99.99 with validForMemberTier
    jl_99 = find_evidence(ctx.price_evidence, source="json_ld", value="99.99")
    check("json_ld priceSpecification.price=99.99", jl_99 is not None)
    if jl_99:
        check("99.99 valid_for_member_tier=True", jl_99.valid_for_member_tier is True,
              f"member_tier={jl_99.valid_for_member_tier}")
        check("99.99 source_path includes priceSpecification",
              "priceSpecification" in (jl_99.source_path or ""),
              jl_99.source_path)

    # No basket guide-price £0.00
    zero_ev = find_evidence(ctx.price_evidence, value="0.00")
    check("no basket guide-price £0.00 in evidence", zero_ev is None,
          f"found={zero_ev is not None}")

    # url_product_id
    check("url_product_id extracted", ctx.url_product_id == "325098267",
          f"got={ctx.url_product_id}")

    section("M14.3 - tesco_pc_membership: meta description regular price + Clubcard in JSON-LD")

    html = load_fixture("tesco_pc_membership.html")
    url = "https://www.tesco.com/groceries/en-GB/products/312841117"
    ctx = build_price_aware_context(html, url)

    # JSON-LD offers.price = 2 (Clubcard price)
    jl_price2 = find_evidence(ctx.price_evidence, source="json_ld", value="2")
    check("json_ld offers.price=2", jl_price2 is not None)
    if jl_price2:
        check("json_ld price=2 NOT valid_for_member_tier (no priceSpecification in this fixture)",
              jl_price2.valid_for_member_tier is False,
              f"member_tier={jl_price2.valid_for_member_tier}")

    # Regular price 2.25 — from meta description or DOM (either source)
    ev_225 = find_evidence(ctx.price_evidence, value="2.25")
    check("regular price 2.25 found in evidence", ev_225 is not None,
          f"found={ev_225 is not None}")
    if ev_225:
        check("2.25 source is dom or meta",
              ev_225.source in ("dom", "meta"), f"source={ev_225.source}")

    section("M14.4 - tesco_pc_membership_bd: identical structure (clean-£ copy)")

    html = load_fixture("tesco_pc_membership_bd.html")
    ctx = build_price_aware_context(html, url)

    jl_price2bd = find_evidence(ctx.price_evidence, source="json_ld", value="2")
    check("bd: json_ld offers.price=2", jl_price2bd is not None)
    ev_225bd = find_evidence(ctx.price_evidence, value="2.25")
    check("bd: regular price 2.25 found", ev_225bd is not None)

    section("M14.5 - tesco_cloth_normal: single price, no basket decoy")

    html = load_fixture("tesco_cloth_normal.html")
    url = "https://www.tesco.com/groceries/en-GB/products/318411571"
    ctx = build_price_aware_context(html, url)

    jl_195 = find_evidence(ctx.price_evidence, source="json_ld", value="19.5")
    check("json_ld price=19.5", jl_195 is not None)

    zero_ev = find_evidence(ctx.price_evidence, value="0.00")
    check("no basket guide-price £0.00", zero_ev is None)

    section("M14.6 - tesco_snack_unavailable: OOS + unit price routing")

    html = load_fixture("tesco_snack_unavailable.html")
    url = "https://www.tesco.com/groceries/en-GB/products/321825466"
    ctx = build_price_aware_context(html, url)

    jl_125 = find_evidence(ctx.price_evidence, source="json_ld", value="1.25")
    check("json_ld price=1.25 (OOS)", jl_125 is not None)

    # Unit price 12.50/kg routed to unit_price_evidence
    unit_ev = find_evidence(ctx.unit_price_evidence, value="12.50")
    check("unit price 12.50/kg routed to unit_price_evidence",
          unit_ev is not None,
          f"unit_evidence_count={len(ctx.unit_price_evidence)}")

    section("M14.7 - argos_game_normal: single price, no spurious from empty <li>")

    html = load_fixture("argos_game_normal.html")
    url = "https://www.argos.co.uk/product/3011234"
    ctx = build_price_aware_context(html, url)

    jl_6999 = find_evidence(ctx.price_evidence, source="json_ld", value="69.99")
    check("json_ld price=69.99", jl_6999 is not None)

    # Empty <li></li> should not produce spurious evidence
    spurious = [ev for ev in ctx.price_evidence if not ev.value.strip()]
    check("empty <li></li> produces no spurious evidence", len(spurious) == 0)


# ---------------------------------------------------------------------------
# Tier 2 — prompt rendering (offline)
# ---------------------------------------------------------------------------


def run_tier2() -> None:
    from src.scraping.repair.prepass import build_price_aware_context
    from src.scraping.repair.prompts import parser_gen_prompt

    section("M14.2 - parser_gen_prompt renders PriceContext correctly")

    html = load_fixture("tesco_net_discount.html")
    url = "https://www.tesco.com/groceries/en-GB/products/325098267"
    ctx = build_price_aware_context(html, url)

    messages = parser_gen_prompt(ctx, site="tesco", role="first",
                                 initial_errors=[], attempts=[])
    user_content = messages[1]["content"]
    system_content = messages[0]["content"]

    # Should contain PriceContext sections
    check("renders [JSON-LD BLOCKS]", "[JSON-LD BLOCKS]" in user_content)
    check("renders [PRICE EVIDENCE", "[PRICE EVIDENCE" in user_content)
    check("renders [HEAD EXCERPT]", "[HEAD EXCERPT]" in user_content)
    check("renders [MAIN EXCERPT", "[MAIN EXCERPT" in user_content)
    check("renders CANONICAL TITLE", "CANONICAL TITLE" in user_content)

    # Should contain evidence details
    check("renders 129.99 in evidence", "129.99" in user_content)
    check("renders 99.99 in evidence", "99.99" in user_content)
    check("renders valid_for_member_tier", "valid_for_member_tier" in user_content)
    check("renders anchor=inside_main", "anchor=inside_main" in user_content)

    # System message should NOT contain old PREFER JSON-LD instruction
    check("system msg does NOT contain old PREFER JSON-LD over DOM",
          "PREFER JSON-LD-based extraction over DOM" not in system_content)

    # System message SHOULD contain new evidence-driven rules
    check("system msg contains validForMemberTier guidance",
          "validForMemberTier" in system_content)
    check("system msg contains recall failure warning",
          "RECALL FAILURE" in system_content)

    section("M14.2b - parser_gen_prompt with old _excerpt fallback unaffected")

    # no_product_prompt / source_absence_prompt still use _excerpt (not PriceContext)
    from src.scraping.repair.prompts import no_product_prompt
    np_messages = no_product_prompt(html, site="tesco")
    check("no_product_prompt still accepts raw html", len(np_messages) == 2)
    check("no_product_prompt uses _excerpt", "[JSON-LD blocks from page]" in np_messages[1]["content"])


# ---------------------------------------------------------------------------
# Tier 3 — end-to-end parser-gen (gated on QWEN_KEY, best-effort)
# ---------------------------------------------------------------------------


async def _single_repair(html: str, url: str, site: str) -> Optional[dict]:
    """Run a single _try_repair and return the parsed output dict (best-effort)."""
    from src.scraping.repair.agent import RepairContext, _try_repair, CandidateSucceeded

    ctx = RepairContext(site=site, url=url, html=html)
    outcome = await _try_repair(ctx, index=0, model="qwen-3.7-plus", is_last=True)
    if isinstance(outcome, CandidateSucceeded):
        return outcome.product.model_dump(mode="json")
    return None


async def run_tier3() -> None:
    """End-to-end parser-gen (best-effort — LLM output varies, so reports, doesn't assert)."""
    from src.scraping.repair.prepass import build_price_aware_context
    from src.scraping.repair.golden import _normalize

    section("M14.3 - end-to-end parser-gen per fixture (best-effort)")

    # Check QWEN_KEY
    from src.scraping.config import get_config
    cfg = get_config()
    if not cfg.qwen_key:
        skip("All Tier 3 checks", "QWEN_KEY not set")
        return

    fixtures = [
        ("argos_frame_discount.html", "https://www.argos.co.uk/product/3284476", "argos",
         {"price": "52.20", "list_price": "58.00", "membership_price": None, "in_stock": True,
          "page_type": "discounted"}),
        ("argos_game_normal.html", "https://www.argos.co.uk/product/3011234", "argos",
         {"price": "69.99", "list_price": None, "membership_price": None, "in_stock": True,
          "page_type": "standard"}),
        ("tesco_net_discount.html", "https://www.tesco.com/groceries/en-GB/products/325098267", "tesco",
         {"price": "129.99", "list_price": None, "membership_price": "99.99", "in_stock": True,
          "page_type": "membership"}),
        ("tesco_pc_membership.html", "https://www.tesco.com/groceries/en-GB/products/312841117", "tesco",
         {"price": "2.25", "list_price": None, "membership_price": "2.00", "in_stock": True,
          "page_type": "membership"}),
        ("tesco_pc_membership_bd.html", "https://www.tesco.com/groceries/en-GB/products/312841117", "tesco",
         {"price": "2.25", "list_price": None, "membership_price": "2.00", "in_stock": True,
          "page_type": "membership"}),
        ("tesco_cloth_normal.html", "https://www.tesco.com/groceries/en-GB/products/318411571", "tesco",
         {"price": "19.50", "list_price": None, "membership_price": None, "in_stock": True,
          "page_type": "standard"}),
        ("tesco_snack_unavailable.html", "https://www.tesco.com/groceries/en-GB/products/321825466", "tesco",
         {"price": "1.25", "list_price": None, "membership_price": None, "in_stock": False,
          "page_type": "out_of_stock"}),
    ]

    for fixture_name, url, site, expected in fixtures:
        label = fixture_name.replace(".html", "")
        html = load_fixture(fixture_name)
        try:
            result = await _single_repair(html, url, site)
        except Exception as e:
            print(f"  [INFO] {label}: repair raised {type(e).__name__}: {e}")
            continue

        if result is None:
            print(f"  [INFO] {label}: repair did not produce a product (attempt may have failed)")
            continue

        # Report extracted values (best-effort — don't assert exact values, consistent with M8/M11)
        r_price = _normalize(result.get("price"))
        r_list = _normalize(result.get("list_price"))
        r_member = _normalize(result.get("membership_price"))
        r_stock = result.get("in_stock")
        e_price = _normalize(expected["price"]) if expected["price"] else None
        e_list = _normalize(expected["list_price"]) if expected["list_price"] else None
        e_member = _normalize(expected["membership_price"]) if expected["membership_price"] else None

        price_ok = r_price == e_price
        list_ok = r_list == e_list
        member_ok = r_member == e_member
        stock_ok = r_stock == expected["in_stock"]
        all_ok = price_ok and list_ok and member_ok and stock_ok

        detail = (
            f"price={r_price}({e_price}) list={r_list}({e_list}) "
            f"member={r_member}({e_member}) stock={r_stock}"
        )
        check(f"{label}: prices match expected (Option A)", all_ok, detail)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    async def run_tier3_best_effort() -> None:
        try:
            await run_tier3()
        except Exception as exc:
            print(f"\n  Tier 3 error: {type(exc).__name__}: {exc}")

    return run_main(
        run_tier1,
        run_tier2,
        run_tier3_best_effort,
        title=(
            "M14 — Price-aware pre-pass + prompt rewrite + membership golden bucket\n"
            f"DATA_DIR={DATA_DIR}"
        ),
        width=70,
    )


if __name__ == "__main__":
    main()
