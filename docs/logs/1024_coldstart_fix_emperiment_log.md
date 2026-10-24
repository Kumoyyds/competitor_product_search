# Implementation report — 1024 cold-start convergence

Status: **DONE**. Integrated A–E implementation is ready for independent review. No commits, pushes, subagents, network calls, or paid API calls were made. Existing user changes were preserved; no notebook, detection, site scraper, site registration, or unrelated module edits were made by this task.

## Changed files

New:
- `src/scraping/coldstart_ledger.py`
- `src/scraping/repair/evidence.py`
- `tests/unit/scraping/test_coldstart_ledger.py`
- `tests/unit/scraping/test_value_evidence.py`
- `tests/unit/scraping/test_coldstart_convergence.py`
- `tests/unit/scraping/test_coldstart_prompts.py`
- `tests/unit/scraping/fixtures/joybuy_de_golden_membership_evidence.html` (genuine raw fragments, golden sample 34)

Modified:
- `src/scraping/coldstart.py`
- `src/scraping/repair/prompts.py`
- `src/scraping/repair/prepass.py`
- `src/scraping/hosts.yaml` (only missing Joybuy bare/www entries)
- `src/scraping/README.md` (preserved prior dirty additions)
- `src/scraping/CLAUDE.md` and byte-identical `AGENTS.md` (preserved prior dirty edits)
- `docs/scraping/design.md` §8

## Design decisions and behavior

- Session expectations hold all eleven golden comparison fields, current confirmed/unspecified status, and accumulated URL hints. Latest human answers replace previous field values. Typed corrections parse finite Decimal-compatible values, comma decimals, European/US grouped decimals, explicit null markers, booleans, JSON image lists/variant objects, and identifier text. Invalid input reprompts. Declined absent-value corrections keep the entire rejection unconfirmed.
- Decimal scale equivalence is local to cold start (`3.30 == 3.3`). Runtime `golden._normalize` / `_matches_expected` behavior was deliberately left unchanged. Cold-start comparisons reuse `_normalize` after local price canonicalization.
- Every candidate retains an immutable code/result pair with references to shared case HTML. Latest truth is used to rescore all retained versions whenever selection matters, including after human review and immediately before saving. Full matching page count dominates weighted field matches. Stable ties prefer current base.
- Candidates that regress previously correct fields do not replace base during the first two consecutive regressions; they are retained in records for the next merge prompt. The third regression selects the best retained version for review. A no-regression candidate with a lower score cannot replace base. Known sandbox/gate failures count as field failures/regressions; extraction failures are excluded.
- Every generation attempt consumes the same bounded `for` loop slot, including empty replies and automatic regression retries. At the cap only save-best/quit remains; two consecutive empty/unusable responses abort. Non-object JSON and non-string/blank parser_code responses are treated as unusable code.
- Exact confirmed matches auto-pass. Mismatches show expected/actual values before review. Explicit save seeds only the selected best parser's own validated matching results, under bucket caps. Unknown/unspecified/failed cases cannot seed. Spare rows and existing full buckets do not block completion. Duplicate identical declarations collapse; conflicting page types fail before fetching.
- Evidence scans original raw HTML, including scripts and attributes, and handles visible amounts split across inline nodes. Numeric patterns reject misleading larger tokens. Ranking favors actual product-ID proximity, then site hints/display switches, then source position. Returned snippets are bounded and display-unescaped only. Snapshot indexes have a two-entry cache; prompt evidence requests are batched by URL; the scanner retains only its best bounded snippet set even in repeated long scripts.
- Joybuy DE now has narrowly scoped `/dp/<slug>/<numeric-id>` anchoring and exact bare/www host resolution in prepass, preserving Argos/Amazon/Tesco behavior.
- Initial prompts use one largest fetched real-URL page per declared type. Rendered initial contexts have a combined 24k cap; repair contexts have a 12k cap. Evidence is capped at 16k; only the last three code diffs are rendered (3k each); optional merge instructions/diff are bounded. Full parser code/schema are never truncated. The overall total intentionally varies with complete parser source and the one-row-per-URL table; each actual prompt character count is printed.
- Updated operator review/save/correction instructions, architecture map, and authoritative design §8; no migration is required.

## Verification

All commands below used local `.venv/bin/python`; no uv sync, external service, or real sandbox process was required for new tests.

- `.venv/bin/python -m compileall -q src/scraping/coldstart.py src/scraping/coldstart_ledger.py src/scraping/repair/prompts.py src/scraping/repair/evidence.py` — passed.
- `.venv/bin/python -m pytest tests/unit/scraping/test_coldstart_ledger.py tests/unit/scraping/test_value_evidence.py -q` — initially 23 passed; one additional US grouped-decimal case was subsequently added and included in the final full suite.
- `.venv/bin/python -m pytest tests/unit/scraping/test_coldstart_convergence.py -q` — final targeted run: **22 passed** in 0.18s.
- `.venv/bin/python -m pytest tests/unit/scraping/test_value_evidence.py tests/unit/scraping/test_coldstart_prompts.py -q` — final targeted run: **8 passed** in 0.22s.
- `.venv/bin/python -m pytest tests/unit/scraping -q` — final run: **182 passed** in **2.76s**.
- `.venv/bin/python -m pytest -q` — final stable implementation: **410 passed**, one existing quantulum3 optional-classifier warning, in **4.46s**, including the final genuine-golden fixture correction. Baseline was 360 passed; this implementation adds 50 deterministic tests/cases.
- `.venv/bin/python scripts/check_encoding.py --all` — **249 tracked text files clean**.
- `git diff --check` — passed.
- Explicit byte checks of all six newly created Python files — valid UTF-8 without BOM; module `CLAUDE.md` and `AGENTS.md` byte-identical.

Deterministic coverage includes typed ledger updates/overwrites/hints, price scale, weighted scoring and sandbox regressions; good → regression → merge; two automatic retries followed by human review; unspecified/declined corrections; total cap during empty responses and automatic retries; extraction-only failures; spares/existing caps/duplicate declarations; quit without writes; and a complete workflow where updated human truth makes a historic parser win while a different current parser is reviewed, with exact historic code/product identities/counts asserted at `_seed`. Prompt tests cover five page types, real URL propagation, oversized JSON-LD/evidence, all failing groups, merge records, and preservation of long parser code/schema.

## Limits and fixture findings

- The paid interactive Joybuy cold-start command was intentionally not run under the controller's explicit offline ruling. Real page judgments and model convergence still need a human-run acceptance session.
- Per the user's authoritative correction, genuine Joybuy evidence comes only from the actual golden set, never the casually downloaded `joybuy_de_standard_page.html`. The database was opened read-only with SQLite URI `mode=ro`. Golden sample 34 contains the real membership URL `/dp/schulanfangbai-jia-a-kuan-breite-nudeln/10137761`, expected ordinary price 3.49 and membership price 3.31. Its exact raw DOM span (3,31 €) and script elements 162/163 (plusMockPrice 3.31 and JoyPlus text) were copied into a small self-contained fixture with sample-ID/URL provenance. Tests do not depend on or access the local database. Synthetic cases still exercise grouped amounts, substring boundaries, split nodes, attributes, ranking, and long-script bounds.

### Golden-source correction verification

- `.venv/bin/python -m pytest tests/unit/scraping/test_value_evidence.py -q` — **4 passed** in **0.08s**, using only genuine golden sample 34 fragments and synthetic cases.
- Genuine fixture: **5121 characters**, exact raw DOM span + script elements 162/163, with source provenance. The local database is not a test dependency and was never written.
- `git diff --check` and byte-identical module agent-doc check pass after the correction.

## Self-review

Reviewed candidate/base separation, historic-version rescoring after expectation changes, best-code/output pairing, every early-continue path against the round cap, unspecified/failure auto-pass exclusion, prompt section budgets, original user delta preservation, and module documentation synchronization. No remaining implementation blocker was identified. Independent review should concentrate on interactive selection edge cases and the documented difference between bounded auxiliary context and variable complete code/table size.

## Fix round 1 — independent review P2s

Status: **DONE**. Both counterexamples from `task-review.md` are fixed together. No paid/network calls, database access/writes, commits, or subagents were used for this fix round. The genuine golden-sample fixture and all existing user changes remain intact.

### Focused merge context

`RoundRecord` now retains the original complete `base_code` alongside full candidate `code`; the change-log diff remains capped at 3000 characters. Merge instructions independently build a <=3000-character source excerpt from that original pair. For broken fields they select original-base logic; for fixed fields they select candidate logic. Field-bearing changed lines are prioritized across the entire source, with nearby context, before budgets are applied. Long field lines retain their field-bearing center rather than losing it behind preceding context. A later selection of a different current base cannot replace the original source pair used for this diagnosis. The combined merge instruction/excerpt budget remains <=4500 characters. Design §8 documents this distinction.

The new deterministic prompt test creates 200 early differing lines whose diff fills the entire 3000-character log prefix, places the useful membership-price fix and the broken list-price logic afterward, then passes an unrelated current base. It verifies the prompt still contains both `membership_price = extract_plus_mock_price(html)` and original `list_price = extract_visible_rrp(html)` in the bounded merge section.

### Nested DOM split amounts

The DOM index now walks ancestors until reaching the first serialized element exceeding 600 characters (outer ancestors can only be larger), or a previously indexed ancestor chain. It preserves existing deduplication and snippet budgets. The new test exercises `<div class="price"><span><b>3</b></span><span><b>,31</b></span></div>` inside an oversized outer section and verifies DOM evidence, CSS path, <=600-character snippets and deduplication.

### Files changed in fix round 1

- `src/scraping/coldstart_ledger.py`
- `src/scraping/repair/prompts.py`
- `src/scraping/repair/evidence.py`
- `tests/unit/scraping/test_coldstart_prompts.py`
- `tests/unit/scraping/test_value_evidence.py`
- `docs/scraping/design.md`
- This report.

### Verification

- `.venv/bin/python -m pytest tests/unit/scraping/test_value_evidence.py tests/unit/scraping/test_coldstart_prompts.py tests/unit/scraping/test_coldstart_ledger.py -q` — **30 passed** in **0.20s**.
- `.venv/bin/python -m pytest -q` — **412 passed**, one existing quantulum3 optional-classifier warning, in **4.40s**.
- `.venv/bin/python scripts/check_encoding.py --all` — **249 tracked text files clean**.
- `git diff --check` — passed.

Self-review checked that the 3k log prefix remains bounded, the merge excerpts use retained original source rather than a later current-base selection, field logic past the early prefix is included, and ancestor walking stops at a monotonic size bound. No remaining blocker identified. Live human/model convergence remains intentionally deferred under the original offline ruling.

## Fix round 2 — trace field values into changed helpers

Status: **DONE**. The remaining merge-context P2 from `final-review.md` is fixed with static source dependency inspection rather than wider line windows or a diff-tail shortcut. No paid/network calls, database access, commits, subagents, or unrelated user-file changes were made. The golden-derived fixture is unchanged.

### Implementation

New `src/scraping/repair/merge_context.py` parses source with the standard-library AST and traces output-field values through lexical name bindings, local assignments, callable aliases, helper calls, helper return expressions, and names passed through indirect call expressions. Function arguments stop resolution into shadowed outer names. Relevant return/assignment control conditions are included. Helper return dependencies are inspected instead of rendering the entire helper body, so unrelated changed assignments inside a helper cannot bury its actual price extraction. Changed dependency statements are selected before unchanged output mappings and explanatory headers/alias links; only then are the existing per-field/source budgets applied.

`prompts.py` delegates focused-source selection to this helper. The original full base/candidate pair retained in `RoundRecord` remains the authority, independent of the current selected base. The same <=3000-character merge-source and <=4500-character total merge-section budgets remain enforced. The bounded 3000-character change-log prefix is unchanged. Design §8 now describes AST field/dependency selection.

### Regression coverage

The actual executable counterexample has 200 differing irrelevant assignments before its helpers, unchanged output mappings far from the helper bodies, base `read_member` extracting `downPrice` versus candidate `plusMockPrice`, and base `read_rrp` extracting `crossLine` versus candidate `None`. Test-owned pure function execution confirms the real ledger classifies membership as fixed and list_price as broken. A different current base is passed to the prompt to verify association with the original sources.

Four parameter cases cover direct helper calls, callable/local aliases, functions selected through a dictionary, and returned callables. All assert that candidate `return data.get('plusMockPrice')` and original-base `return data.get('crossLine')`, along with their helper definitions, survive in the bounded merge section. Each case additionally places 100 unrelated changed statements inside the relevant member helper to ensure dependency selection reaches the actual returned extraction rather than copying a function prefix. Existing direct late-fix and nested DOM regression tests continue to pass.

### Files changed in fix round 2

- New `src/scraping/repair/merge_context.py`
- `src/scraping/repair/prompts.py`
- `tests/unit/scraping/test_coldstart_prompts.py`
- `docs/scraping/design.md`
- This report.

### Verification

- `.venv/bin/python -m pytest tests/unit/scraping/test_coldstart_prompts.py tests/unit/scraping/test_value_evidence.py tests/unit/scraping/test_coldstart_ledger.py -q` — **34 passed** in **0.32s**.
- `.venv/bin/python -m pytest -q` — **416 passed**, one existing quantulum3 optional-classifier warning, in **4.53s**.
- `.venv/bin/python scripts/check_encoding.py --all` — **249 tracked text files clean**.
- `git diff --check` — passed.
- Explicit new-file byte validation — AST helper is UTF-8 without BOM; module `CLAUDE.md`/`AGENTS.md` remain byte-identical.

Self-review checked original-source association, lexical helper/alias resolution, argument shadowing, recursion termination via visited nodes, changed-statement priority before budget application, and preservation of existing direct-fix/DOM tests. Static inspection does not execute arbitrary parser logic or promise to resolve runtime-computed attribute/call targets; the evidenced helper/alias cases are resolved. No remaining blocker identified for this scoped fix.
