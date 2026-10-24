"""Offline workflow tests: generators, fetch, sandbox and persistence are all stubbed."""
import asyncio
from types import SimpleNamespace

import pytest

from src.scraping import coldstart as cs
from src.scraping.coldstart_ledger import ExpectationLedger
from src.scraping.models.product_data import ProductData


def product(url="https://example/item", **overrides):
    raw = dict(title="Item", price="10", currency="EUR", in_stock=True,
               list_price="20", membership_price=None, image_urls=["https://image"])
    raw.update(overrides)
    return ProductData.model_validate(cs._wrap(raw, "example", url))


@pytest.fixture
def harness(monkeypatch):
    def setup(codes, outputs, answers, *, cap=10, rows=None, bucket_cap=3, existing=None):
        rows = rows or [cs.ColdStartRow("membership", "https://example/item", 2)]
        config = SimpleNamespace(cold_start_model_ladder=["one", "two"],
                                 cold_start_temperature_ladder=[.1, .4],
                                 cold_start_max_repair_rounds=cap,
                                 golden_max_for=lambda kind: bucket_cap)
        monkeypatch.setattr(cs, "get_config", lambda: config)
        monkeypatch.setattr(cs, "_pick_html_scraper", lambda site: type("FakeScraper", (), {}))
        monkeypatch.setattr(cs, "_get_existing_coverage", lambda site: existing or {})
        monkeypatch.setattr(cs, "_coverage_shortfall", lambda coverage, site: [])
        generated, repaired, asked, saved = [], [], [], []
        code_iterator = iter(codes)
        answer_iterator = iter(answers)

        async def fetch(*args, **kwargs):
            return [(row.url, 200, '<p>3.00 10 20</p>', 'cache') for row in rows]

        async def generate(*args, **kwargs):
            code = next(code_iterator)
            generated.append(code)
            return code

        async def repair(**kwargs):
            repaired.append({**kwargs, "records": list(kwargs["records"])})
            return await generate()

        async def run(site, actual_rows, fetched, code):
            result = []
            for row in actual_rows:
                output = outputs[code]
                if isinstance(output, dict):
                    output = output[row.url]
                if isinstance(output, str) and output in {"sandbox", "fetch"}:
                    result.append(cs._ReviewCase(row, '<p>3.00 10 20</p>', None,
                        "extraction_failed" if output == "fetch" else "sandbox_failed", output))
                else:
                    result.append(cs._ReviewCase(row, '<p>3.00 10 20</p>', output))
            return result

        def input_fn(prompt):
            asked.append(prompt)
            try:
                return next(answer_iterator)
            except StopIteration:
                pytest.fail(f"Unexpected human prompt: {prompt}")

        def seed(site, code, accepted):
            saved.append((code, accepted))
            return 7, len(accepted)

        monkeypatch.setattr(cs, "_resolve_html", fetch)
        monkeypatch.setattr(cs, "_gen_initial_parser", generate)
        monkeypatch.setattr(cs, "_gen_repaired_parser", repair)
        monkeypatch.setattr(cs, "_run_review_cases", run)
        monkeypatch.setattr(cs, "_seed", seed)
        result = asyncio.run(cs.run_coldstart("example", rows, input_fn))
        return SimpleNamespace(result=result, generated=generated, repaired=repaired,
                               asked=asked, saved=saved)
    return setup


def test_good_regression_merge_needs_no_second_human_review(harness, capsys):
    base = product()
    regressed = product(list_price=None, membership_price="3")
    merged = product(membership_price="3")
    run = harness(["base", "regressed", "merged"],
                  dict(base=base, regressed=regressed, merged=merged),
                  ["n", "membership_price", "3", "member price", "c"])
    assert sum("Accept?" in prompt for prompt in run.asked) == 1
    assert run.saved[0][0] == "merged"
    assert run.saved[0][1][0][2] is merged
    merge = run.repaired[-1]["merge_record"]
    assert merge.reviewer == "auto" and not merge.adopted
    assert merge.fixed == {("https://example/item", "membership_price")}
    assert merge.broke == {("https://example/item", "list_price")}
    assert "accepted automatically" in capsys.readouterr().out


def test_two_auto_retries_then_human_and_latest_truth(harness):
    base, regressed = product(), product(list_price=None, membership_price="3")
    run = harness(["base", "r1", "r2", "r3"],
                  dict(base=base, r1=regressed, r2=regressed, r3=regressed),
                  ["n", "membership_price", "3", "", "c", "y"])
    assert sum("Accept?" in prompt for prompt in run.asked) == 2
    assert run.saved[0][0] == "base"  # human y overwrote membership expectation
    assert sum(record.reviewer == "auto" for record in run.repaired[-1]["records"]) == 2


@pytest.mark.parametrize("last", ["regressed", None])
def test_cap_on_auto_retry_or_empty_saves_best_own_outputs(harness, last):
    url1, url2 = "https://example/1", "https://example/2"
    rows = [cs.ColdStartRow("membership", url1, 2), cs.ColdStartRow("membership", url2, 3)]
    first1, first2 = product(url1), product(url2)
    bad1, fixed2 = product(url1, list_price=None), product(url2, membership_price="3")
    run = harness(["base", last],
                  dict(base={url1: first1, url2: first2}, regressed={url1: bad1, url2: fixed2}),
                  ["y", "n", "membership_price", "3", "", "c", "s"],
                  cap=2, rows=rows)
    assert run.generated == ["base", last]
    assert run.result["partial"]
    assert run.saved[0][0] == "base"
    assert [(row.url, item) for row, _, item in run.saved[0][1]] == [(url1, first1)]
    assert run.result["accepted"] == run.result["seeded_goldens"] == 1


def test_cap_on_first_empty_and_two_consecutive_empty_abort(harness):
    run = harness([None], {}, [], cap=1)
    assert run.result["aborted"] and not run.saved
    run = harness([None, None], {}, [], cap=10)
    assert run.generated == [None, None] and run.result["aborted"] and not run.saved


def test_unspecified_rejection_must_be_reviewed_again(harness):
    current = product()
    run = harness(["base", "same"], dict(base=current, same=current),
                  ["n", "", "hint", "c", "y"])
    assert sum("Accept?" in prompt for prompt in run.asked) == 2
    assert run.saved[0][0] == "same"


def test_declined_absent_value_never_seeds_and_invalid_input_reprompts(harness):
    run = harness(["base"], dict(base=product()),
                  ["n", "price", "invalid", "17.99", "n", "", "s"], cap=1)
    assert run.saved[0][1] == [] and run.result["accepted"] == 0
    assert any("不在抓取到的 HTML" in prompt for prompt in run.asked)


def test_sandbox_failure_counts_as_regression_without_human_prompt(harness):
    run = harness(["base", "crash"], dict(base=product(), crash="sandbox"),
                  ["n", "membership_price", "3", "", "c", "s"], cap=2)
    assert sum("Accept?" in prompt for prompt in run.asked) == 1
    assert run.saved[0][0] == "base" and not run.saved[0][1]


def test_extraction_failure_does_not_block_seed(harness):
    url1, url2 = "https://example/1", "https://example/2"
    rows = [cs.ColdStartRow("standard", url1, 2), cs.ColdStartRow("standard", url2, 3)]
    run = harness(["base"], dict(base={url1: product(url1), url2: "fetch"}), ["y"], rows=rows)
    assert not run.result["aborted"] and run.result["seeded_goldens"] == 1


def test_spares_and_existing_full_buckets_are_not_reviewed(harness):
    url1, url2 = "https://example/1", "https://example/2"
    rows = [cs.ColdStartRow("standard", url1, 2), cs.ColdStartRow("standard", url2, 3)]
    run = harness(["base"], dict(base={url1: product(url1), url2: "sandbox"}), ["y"],
                  rows=rows, bucket_cap=1)
    assert run.result["seeded_goldens"] == 1
    run = harness(["base"], dict(base=product()), [], bucket_cap=1, existing={"membership": 1})
    assert not run.asked and not run.saved[0][1]


def test_quit_never_writes(harness):
    run = harness(["base"], dict(base=product()), ["q"])
    assert run.result["aborted"] and not run.saved


def test_historic_winner_is_rescored_after_truth_changes():
    row = cs.ColdStartRow("membership", "https://example/item", 2)
    a = cs._Candidate("a", (cs._ReviewCase(row, "html", product()),))
    b = cs._Candidate("b", (cs._ReviewCase(row, "html", product(membership_price="3")),))
    ledger = ExpectationLedger()
    ledger.review(row.url, row.page_type, a.cases[0].product, accepted=True)
    assert cs._select_best([a, b], b, ledger) is a
    ledger.review(row.url, row.page_type, b.cases[0].product, accepted=True)
    assert cs._select_best([a, b], a, ledger) is b


def test_conflicting_url_declarations_fail_before_fetch(monkeypatch):
    rows = [cs.ColdStartRow("standard", "u", 2), cs.ColdStartRow("membership", "u", 3)]
    with pytest.raises(cs.ColdStartInputError, match="Conflicting"):
        asyncio.run(cs.run_coldstart("site", rows))


def test_save_historic_parser_after_human_overwrites_truth_uses_its_own_outputs(harness):
    url1, url2 = "https://example/1", "https://example/2"
    rows = [cs.ColdStartRow("standard", url1, 2), cs.ColdStartRow("standard", url2, 3)]
    historic1, historic2 = product(url1), product(url2)
    reviewed1, reviewed2 = product(url1), product(url2, price="4")
    run = harness(["historic", "reviewed"],
        dict(historic={url1: historic1, url2: historic2}, reviewed={url1: reviewed1, url2: reviewed2}),
        ["y", "n", "price", "3", "", "c", "n", "price", "10", "", "s"],
        rows=rows, cap=2)
    # The second candidate was adopted (no known field regressed), then its
    # correction changed truth to match the earlier version. Save must rescore.
    assert run.generated == ["historic", "reviewed"]
    assert run.saved[0][0] == "historic"
    assert [item for _, _, item in run.saved[0][1]] == [historic1, historic2]
    assert run.saved[0][1][0][2] is historic1
    assert run.saved[0][1][1][2] is historic2
    assert run.result["accepted"] == run.result["seeded_goldens"] == 2
    assert run.result["partial"]


def test_identical_duplicate_declarations_only_review_and_seed_once(harness):
    row = cs.ColdStartRow("standard", "https://example/item", 2)
    duplicate = cs.ColdStartRow("standard", row.url, 3)
    run = harness(["base"], dict(base=product()), ["y"], rows=[row, duplicate])
    assert sum("Accept?" in prompt for prompt in run.asked) == 1
    assert run.result["seeded_goldens"] == 1


def test_real_review_runner_marks_gate_sandbox_and_fetch_failures(monkeypatch):
    rows = [cs.ColdStartRow("standard", f"https://example/{i}", i + 2) for i in range(3)]
    fetched = [(rows[0].url, 200, "html", "cache"),
               (rows[1].url, 200, "html", "cache"),
               (rows[2].url, 500, "", "fetch")]

    async def sandbox(code, html, url):
        if url.endswith("/0"):
            return SimpleNamespace(reason="sandbox crash")
        return {"in_stock": True}  # missing required title -> gate failure

    monkeypatch.setattr(cs, "run_in_sandbox", sandbox)
    cases = asyncio.run(cs._run_review_cases("site", rows, fetched, "code"))
    assert [case.failure_kind for case in cases] == ["sandbox_failed", "sandbox_failed", "extraction_failed"]
    assert "gate:" in cases[1].failure_detail


@pytest.mark.parametrize("response", ['[]', 'null', '"text"', '{"parser_code": 12}',
                                      '{"parser_code":"   "}', 'broken JSON'])
def test_noncode_llm_responses_are_empty_attempts(response):
    assert cs._extract_parser_code(response) is None
