# M21 — Cold-start repair: human-terminated loop + sliding feedback context

## Context

Cold start today can only ever repair the parser **once**. The single loop in
[coldstart.py:128](src/scraping/coldstart.py#L128) is `for node_index, model in enumerate(ladder)`, so the
number of repair rounds is hard-wired to `len(cold_start_model_ladder)` (default 2: node 0 generates,
node 1 repairs). The `Continue 纠错? [y/N]` prompt at
[coldstart.py:267](src/scraping/coldstart.py#L267) exists but is unreachable on the final round —
[L260](src/scraping/coldstart.py#L260) checks `is_last` first and exits with "ladder exhausted". One
repair pass rarely converges a parser across five page-type buckets, and the run then throws away both
the parser and every human-accepted golden.

Two changes:

1. **Decouple repair rounds from the model ladder.** The loop runs until the human stops it (or all
   cases pass, or a safety cap trips). The ladder becomes a warm-up schedule; its last rung repeats for
   every later round.
2. **Keep the repair prompt from growing with round count.** The parser context is already correct —
   `coldstart_repair_prompt` ([prompts.py:434](src/scraping/repair/prompts.py#L434)) builds exactly one
   record holding only `current_code`, so parser history never accumulates. The real growth is
   `feedback_history` / `failure_history`, which `.extend()` and are never cleared
   ([coldstart.py:253-257](src/scraping/coldstart.py#L253-L257)). At round 8 the prompt would carry all
   seven prior rounds of corrections, most of them stale — actively misleading the model about fields it
   already fixed. Replace with **current round only + a compact "already fixed, don't regress" ledger**.

Decisions confirmed with the user: last ladder rung repeats; sliding feedback window plus ledger; add a
"save and quit" exit so multi-round human review isn't discarded.

## Changes

### 1. `src/scraping/config.py` — safety cap

Add next to the existing cold-start ladder fields (L58-66):

```python
# The repair loop is human-terminated; this is only a runaway guard.
cold_start_max_repair_rounds: int = Field(default=10)
```

Ladder-length assertion at [coldstart.py:102](src/scraping/coldstart.py#L102) stays unchanged.

### 2. `src/scraping/coldstart.py` — loop restructure

Replace the `for node_index, model in enumerate(ladder)` loop with a round-indexed `while` loop.

**Per-round model selection** (ladder as warm-up schedule, last rung repeats):

```python
rung = min(round_index, len(ladder) - 1)
model, temperature = ladder[rung], temperatures[rung]
# thinking turns on at the last ladder rung and stays on for every later round;
# for the default 2-rung ladder this reproduces today's behaviour exactly
enable_thinking = round_index >= len(ladder) - 1
role = "last" if enable_thinking else "middle"
```

**Termination**, in order of evaluation:

| Condition | Action |
|---|---|
| no `sandbox_failed` and no `human_rejected` | `_seed(...)` + `break` — unchanged success path |
| human answers `q` at a per-case `Accept?` prompt | unchanged: nothing written, exit 1 |
| human answers `q` at the round prompt | nothing written, exit 1 |
| human answers `s` at the round prompt | `_seed(...)` with current `parser_code` + this round's `accepted`, then return a `partial=True` result |
| `round_index + 1 >= cfg.cold_start_max_repair_rounds` | print the cap notice, then offer only `s` / `q` (no `c`) |
| 2 consecutive unusable LLM replies | abort — replaces today's `is_last` check |

**Round prompt** replaces [L260-273](src/scraping/coldstart.py#L260-L273):

```
Continue 纠错? [c=继续修复 / s=保存当前结果并退出 / q=放弃退出]
```

Accept `y` as an alias for `c` (existing muscle memory and `verify_m19` fixtures feed `"y"`).
Ladder exhaustion no longer terminates anything — delete that branch.

**Unusable LLM reply** ([L159-168](src/scraping/coldstart.py#L159-L168)): keep the fall-through
semantics (`parser_code is None` → next round generates from scratch; a usable `parser_code` is
retained rather than discarded), but gate on a `consecutive_empty` counter instead of `is_last`, so an
LLM that returns nothing forever cannot spin against the round cap.

### 3. `src/scraping/coldstart.py` — sliding feedback + ledger

Replace the two cumulative lists with round-scoped state:

```python
round_feedbacks: list[ReviewFeedback] = []      # this round only, rebuilt each round
round_failures: list[str] = []                  # this round only, rebuilt each round
reported: dict[str, set[str]] = {}              # url -> every field ever reported wrong
resolved: dict[str, set[str]] = {}              # url -> fields reported wrong, now accepted
regressions: dict[str, set[str]] = {}           # url -> fields in `resolved` that broke again
```

Bookkeeping at the end of each failing round (where L253-258 does its `extend` today):

- every rejected URL: `reported[url] |= {c.field for c in feedback.corrections}`; if any of those fields
  are already in `resolved[url]`, move them into `regressions[url]` — the repair broke something that
  previously passed.
- every accepted URL: `resolved[url] |= reported.get(url, set())` — reported-then-accepted means fixed.
- URLs that crashed the sandbox in an earlier round and now produce a valid product also land in
  `resolved` (keyed by a sentinel field name such as `"<sandbox>"`).
- `previously_accepted` keeps its current role (auto-accept of unchanged results).

`regressions` is also surfaced to the human in `_print_failure_summary`
([L707](src/scraping/coldstart.py#L707)) as a `[REGRESSION]` line — today a repair that breaks a
previously-passing URL is completely invisible.

Drop the `confirmed_fields=_display_fields()` argument at
[L156](src/scraping/coldstart.py#L156): it passes the same static list of all reviewable field names
regardless of what was actually accepted, so the prompt's 【已确认正确】 block carries no information.
The ledger replaces it.

### 4. `src/scraping/repair/prompts.py` — `coldstart_repair_prompt`

Signature: drop `confirmed_fields`, add `resolved_ledger: dict[str, list[str]]` and
`regressions: dict[str, list[str]]`. `feedbacks` / `failures` now carry only the current round.
The existing structure (one `SimpleNamespace` record → `parser_gen_prompt`) is unchanged; only the
`errors` block list changes. Ordering, most urgent first:

1. `【回退警告 — 上一轮修复弄坏了已通过的字段】` — one line per regressed url + fields. Emitted only when
   `regressions` is non-empty.
2. `【提取错误，需修复】(本轮)` — unchanged rendering of the current round's `ReviewFeedback` objects
   ([prompts.py:414-426](src/scraping/repair/prompts.py#L414-L426)).
3. `【Sandbox / Gate 失败，需修复】(本轮)` — unchanged rendering, current round only.
4. `【历史已修复，保持现状勿回退】` — one line per url: `url: field, field`. This is the whole ledger, and
   it stays O(urls × fields) regardless of round count.

Pass `attempt_index=round_index` so the record header reads `--- Attempt N ---` with the true round.

### 5. Result dict + exit code

`run_coldstart` gains `"partial": bool` in its return value. `_result_exit_code`
([L869](src/scraping/coldstart.py#L869)) returns **2** whenever `partial` is true — a save-and-quit
parser was persisted but never passed a clean round, so it must not report success even when golden
coverage happens to be complete.

## Verification

Per the module's mandatory verification discipline, this ships as **M21**:

1. New `src/scraping/tests/verify_m21.py`, offline, reusing the `run_driver` mock-LLM harness in
   [verify_m19.py:104](src/scraping/tests/verify_m19.py#L104) (feeds scripted parser code and scripted
   `input_fn` answers). Checks to cover:
   - a 5-round run on a 2-rung ladder converges and seeds (rounds are no longer capped at ladder length)
   - rounds 3+ use `ladder[-1]` / `temperatures[-1]` with `enable_thinking=True` (assert on the recorded
     `make_chat_client` kwargs, as M19.3 already does)
   - `c` continues, `y` still continues, `q` writes nothing (exit 1), `s` seeds parser + accepted goldens
     and yields `partial=True` / exit 2
   - the round-N prompt contains only round-N feedback — assert an earlier round's correction string is
     **absent** from the last prompt, and that prompt length does not grow monotonically with rounds
   - the ledger block appears with the right url→field pairs; a regression produces both the
     `【回退警告】` prompt block and the `[REGRESSION]` console line
   - `cold_start_max_repair_rounds` trips and the prompt then offers only `s`/`q`
   - two consecutive unusable LLM replies abort
2. `python -m src.scraping.tests.verify_m21 | tee src/scraping/tests/verify_m21_output.log`
3. **Update `verify_m19.py`** — two checks encode the old contract and will now fail:
   `"single-node exhaustion asks no continue question"` ([L212](src/scraping/tests/verify_m19.py#L212))
   and `"exhausted repair ladder does not persist"` ([L219](src/scraping/tests/verify_m19.py#L219)).
   Re-express them against the new termination rules, then re-run and refresh
   `verify_m19_output.log`.
4. Add both files to [tests/README.md](src/scraping/tests/README.md).
5. Live smoke: `python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx`
   — run at least three repair rounds, confirm rounds 3+ log the last-rung model, then exercise `s` and
   confirm the parser row + goldens land in SQLite.

## Docs

- `src/scraping/CLAUDE.md` (AGENTS.md syncs via the pre-commit hook): add an **M21** section, add
  `cold_start_max_repair_rounds` to Key Config, and rewrite the "Cold Start (new site)" paragraph — it
  currently states "the next cold-start model node repairs the current parser", which stops being true.
- Fix a pre-existing drift while in there: `README.md` L280 and `CLAUDE.md` Key Config both document the
  cold-start ladder default as `["deepseek-v4-flash", "deepseek-v4-pro"]`, but
  [config.py:61](src/scraping/config.py#L61) ships `["deepseek-v4-flash", "deepseek-v4-flash"]`. Align the
  docs to the code (do not silently change the default model — that is a cost decision), and document the
  new "last rung repeats" semantics.

## Out of scope

- Per-URL HTML for repair. Every round still prompts against the single `representative_html`
  ([L116](src/scraping/coldstart.py#L116)), so feedback naming a URL points at a page the model cannot
  see. Real limitation, but orthogonal to loop control and a much larger change.
- Recording `actual_value` alongside `correct_value` in `ReviewFeedback`, and persisting rounds to SQLite.
- Replacing the human reviewer with an LLM reviewer.
