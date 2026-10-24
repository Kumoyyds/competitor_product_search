import asyncio
from types import SimpleNamespace

import pytest

from src.scraping import coldstart as cs
from src.scraping.coldstart_ledger import ExpectationLedger, RoundRecord, changes
from src.scraping.repair.prompts import (
    _failing_evidence, _render_pages, coldstart_repair_prompt, initial_parser_gen_prompt,
)


def context(size=10):
    return SimpleNamespace(canonical_title="Item", url_product_id="1234", promotion_signal=None,
        json_ld_blocks=["x" * size] * 10, price_evidence=[SimpleNamespace(
            value="3.31", currency="EUR", source="script", source_path="price", anchor_relation="inside_main",
            label_text="member", struck_through=False, valid_for_member_tier=True,
            css_hint=".price", snippet="y" * size)] * 20,
        unit_price_evidence=[], head_excerpt="h" * size, main_excerpt="m" * size)


def test_five_page_types_and_oversized_context_are_bounded_and_code_is_complete():
    pages = [(kind, f"https://example/{kind}", context(10000))
             for kind in ["standard", "discounted", "membership", "out_of_stock", "multipack"]]
    assert len(_render_pages(pages, budget=24000)) <= 24000
    assert len(_render_pages(pages, budget=12000)) <= 12000
    initial = initial_parser_gen_prompt(pages, "site")
    for kind, url, _ in pages:
        assert f"=== PAGE ({kind}) {url} ===" in initial[1]["content"]
    row = cs.ColdStartRow("membership", "https://example/1234", 2)
    case = cs._ReviewCase(row, '<div>3,31</div><script>{"downPrice":"13.49"}</script>',
                          {"price": "13.49"})
    ledger = ExpectationLedger()
    ledger.review(row.url, row.page_type, {"price": "3.31"}, accepted=True, hint="JoyPlus")
    code = "def parse(html, url):\n" + "    # keep complete\n" * 4000 + "    return {}\n"
    record = RoundRecord(2, "candidate", "d" * 10000, {},
                         {(row.url, "price")}, {(row.url, "list_price")}, False, "auto")
    prompt = coldstart_repair_prompt(pages, "site", code, ledger, [case], [record] * 6,
                                    role="last", attempt_index=3, model="fake", merge_record=record)
    user = prompt[1]["content"]
    assert code in user and "PRICE FIELD CONTRACT" in prompt[0]["content"]
    for text in ["[EXPECTED OUTPUT TABLE]", "[FAILING CASE EVIDENCE]", "[CHANGE LOG]",
                 "[MERGE INSTRUCTION]", "EXPECTED DOM", "ACTUAL script", "JoyPlus"]:
        assert text in user
    assert "human-confirmed" in prompt[0]["content"]
    assert user.count("Round 2:") == 3
    # Ancillary sections are bounded independently of arbitrary complete code/table length.
    assert len(user) - len(code) < 44000


def test_evidence_cap_keeps_all_failing_groups_and_handles_unspecified_and_crashes():
    ledger, cases = ExpectationLedger(), []
    for i in range(20):
        row = cs.ColdStartRow("standard" if i % 2 else "membership", f"https://example/{i}", i + 2)
        fields = dict(price="3.31", list_price="20", membership_price="2", in_stock=True,
                      currency="EUR", title="Title", image_urls=["a"], variant={"s": "M"})
        ledger.review(row.url, row.page_type, fields, accepted=True, hint="h" * 20000)
        cases.append(cs._ReviewCase(row, "<div>3,31</div>" * 1000, None,
                                    "sandbox_failed", "error" * 1000))
    evidence = _failing_evidence(ledger, cases, "site")
    assert len(evidence) <= 16000
    assert "GROUP standard / price" in evidence
    assert "GROUP membership / price" in evidence
    assert "sandbox" not in evidence or "failure:" in evidence
    ledger.review(cases[0].row.url, "membership", None, accepted=False)
    assert "<unconfirmed>" in _failing_evidence(ledger, cases[:1], "site")
    assert "rejected_unspecified" in _failing_evidence(ledger, cases[:1], "site")


def test_generation_builds_context_with_real_urls_and_reports_size(monkeypatch, capsys):
    pages = [("standard", "https://www.joybuy.de/dp/item/10563197", "small"),
             ("discounted", "https://example/discount", "large")]
    seen, prompts = [], []

    def build(html, url, *, budget):
        seen.append((html, url, budget))
        return context()

    class FakeLLM:
        async def ainvoke(self, prompt):
            prompts.append(prompt)
            return SimpleNamespace(content='{"parser_code":"def parse(html,url): return {}"}')

    monkeypatch.setattr(cs, "build_price_aware_context", build)
    monkeypatch.setattr(cs, "make_chat_client", lambda **kwargs: FakeLLM())
    assert asyncio.run(cs._gen_initial_parser("joybuy_de", pages))
    assert [entry[1] for entry in seen] == [page[1] for page in pages]
    assert all(entry[2] == 12000 for entry in seen)
    ledger = ExpectationLedger()
    assert asyncio.run(cs._gen_repaired_parser(site="site", pages=pages, current_code="base",
        ledger=ledger, cases=[], records=[], merge_record=None, index=1, model="fake",
        temperature=.1, enable_thinking=True, role="last"))
    assert all(entry[2] == 6000 for entry in seen[2:])
    assert "dummy" not in str(prompts) and "prompt ≈" in capsys.readouterr().out


def test_representatives_largest_per_type_and_real_url():
    rows = [cs.ColdStartRow("standard", "u1", 2), cs.ColdStartRow("standard", "u2", 3),
            cs.ColdStartRow("discounted", "u3", 4), cs.ColdStartRow("membership", "u4", 5)]
    fetched = [("u1", 200, "short", "cache"), ("u2", 200, "longer", "cache"),
               ("u3", 200, "discount", "cache"), ("u4", 500, "bad", "cache")]
    assert cs._pick_representative_pages(fetched, rows) == [
        ("standard", "u2", "longer"), ("discounted", "u3", "discount")]


def test_merge_preserves_late_candidate_fix_and_original_base_logic_after_large_early_diff():
    url = "https://example/1234"
    early_base = "".join(f"    option_{index} = 'base-{index}'\n" for index in range(200))
    early_candidate = "".join(f"    option_{index} = 'candidate-{index}'\n" for index in range(200))
    base = ("def parse(html, url):\n" + early_base +
            "    list_price = extract_visible_rrp(html)\n"
            "    membership_price = None\n"
            "    return dict(list_price=list_price, membership_price=membership_price)\n")
    candidate = ("def parse(html, url):\n" + early_candidate +
                 "    list_price = None\n"
                 "    membership_price = extract_plus_mock_price(html)\n"
                 "    return dict(list_price=list_price, membership_price=membership_price)\n")
    ledger = ExpectationLedger()
    record = RoundRecord.create(2, candidate, base, {}, ledger,
        {(url, "membership_price")}, {(url, "list_price")}, False, "auto")
    assert len(record.diff_vs_base) == 3000
    assert "extract_plus_mock_price" not in record.diff_vs_base
    assert "extract_visible_rrp" not in record.diff_vs_base
    assert record.base_code == base
    # A later selection may have changed the current base. The merge must
    # still use the original full source pair behind the recorded regression.
    prompt = coldstart_repair_prompt(
        [("membership", url, context())], "site", "def parse(html, url): return {}",
        ledger, [], [record], role="last", attempt_index=3, model="fake", merge_record=record)
    user = prompt[1]["content"]
    merge = user.split("[MERGE INSTRUCTION]", 1)[1].split("[CURRENT BASE CODE]", 1)[0]
    assert "membership_price = extract_plus_mock_price(html)" in merge
    assert "list_price = extract_visible_rrp(html)" in merge
    assert "[ORIGINAL BASE:" in merge and "[REJECTED CANDIDATE:" in merge
    assert len(merge) <= 4500


@pytest.mark.parametrize("mode", ["direct", "aliases", "mapping", "returned_callable"])
def test_merge_traces_unchanged_fields_to_changed_helpers_from_original_source_pair(mode):
    url = "https://example/1234"

    def parser(version):
        early = "".join(f"    option_{index} = '{version}-{index}'\n" for index in range(200))
        # Even unrelated edits INSIDE a relevant helper must not bury its return.
        helper_noise = "".join(f"        unused_{index} = '{version}-{index}'\n" for index in range(100))
        body = ("def parse(data, url):\n" + early + "    def read_member(data):\n" + helper_noise +
                ("        return data.get('downPrice')\n" if version == "base" else
                 "        return data.get('plusMockPrice')\n") +
                "    def read_rrp(data):\n" +
                ("        return data.get('crossLine')\n" if version == "base" else
                 "        return None\n"))
        body += "".join(f"    unrelated_{index} = {index}\n" for index in range(10))
        if mode == "direct":
            return body + "    return dict(membership_price=read_member(data), list_price=read_rrp(data))\n"
        if mode == "aliases":
            return (body + "    member_reader = read_member\n    rrp_reader = read_rrp\n"
                    "    member_value = member_reader(data)\n    rrp_value = rrp_reader(data)\n"
                    "    return dict(membership_price=member_value, list_price=rrp_value)\n")
        if mode == "mapping":
            return (body + "    readers = {'member': read_member, 'rrp': read_rrp}\n"
                    "    return {'membership_price': readers['member'](data), 'list_price': readers['rrp'](data)}\n")
        return (body + "    def choose_member():\n        return read_member\n"
                "    def choose_rrp():\n        return read_rrp\n"
                "    return dict(membership_price=choose_member()(data), list_price=choose_rrp()(data))\n")

    base, candidate = parser("base"), parser("candidate")
    sample = dict(downPrice="3.49", plusMockPrice="3.31", crossLine="3.99")
    before_namespace, after_namespace = {}, {}
    # These are test-owned pure functions; no production parser is executed.
    exec(base, before_namespace)
    exec(candidate, after_namespace)
    before = before_namespace["parse"](sample, url)
    after = after_namespace["parse"](sample, url)
    assert before == dict(membership_price="3.49", list_price="3.99")
    assert after == dict(membership_price="3.31", list_price=None)
    ledger = ExpectationLedger()
    ledger.review(url, "membership", dict(membership_price="3.31", list_price="3.99"), accepted=True)
    fixed, broke = changes({url: before}, {url: after}, ledger)
    assert fixed == {(url, "membership_price")} and broke == {(url, "list_price")}
    record = RoundRecord.create(2, candidate, base, {url: after}, ledger, fixed, broke, False, "auto")
    assert len(record.diff_vs_base) == 3000 and "plusMockPrice" not in record.diff_vs_base
    prompt = coldstart_repair_prompt(
        [("membership", url, context())], "site", "def parse(data, url): return {}",
        ledger, [], [record], role="last", attempt_index=3, model="fake", merge_record=record)
    merge = prompt[1]["content"].split("[MERGE INSTRUCTION]", 1)[1].split("[CURRENT BASE CODE]", 1)[0]
    original_base, rejected_candidate = merge.split("[REJECTED CANDIDATE:", 1)
    assert "def read_rrp(data):" in original_base
    assert "return data.get('crossLine')" in original_base
    assert "def read_member(data):" in rejected_candidate
    assert "return data.get('plusMockPrice')" in rejected_candidate
    assert "unused_0" not in merge and "option_0" not in merge
    assert len(merge) <= 4500
