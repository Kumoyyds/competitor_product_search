# Scraping Module — Verification Artifacts

Each milestone's verification is persisted here so you can audit and re-run at any time.

## Files

| File | Purpose | LLM |
|------|---------|-----|
| `verify_m1_m3.py` | M1 (ProductData + gates), M2 (Router + Registry), M3 (SQLite 6 tables) | offline |
| `verify_m1_m3_output.log` | Latest run — 33 checks, 0 failed | — |
| `verify_m4_m5.py` | M4 (DirectAPIScraper + field mapping), M5 (HTMLScraper + invalid page detection) | offline |
| `verify_m4_m5_output.log` | Latest run — 44 checks, 0 failed | — |
| `verify_m6.py` | M6 — parser list ordering by hit rate, tiebreak, winning_parser_id write, empty-list handling | offline |
| `verify_m6_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m7.py` | M7 — sandbox AST scan (imports/names/dunder), timeout, exception isolation, whitelisted imports, Windows | offline |
| `verify_m7_output.log` | Latest run — 21 checks, 0 failed | — |
| `verify_m8.py` | M8 — repair agent no_product judgment, JSON healer D25 red line, end-to-end parser gen on real HTML | **real DeepSeek** |
| `verify_m8_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m9.py` | M9 — page_type classification, promote_candidate (accept/reject), hard-cap prune, natural prune | offline |
| `verify_m9_output.log` | Latest run — 17 checks, 0 failed | — |
| `verify_m10.py` | M10 — escalation writing (parser_broken / api_malformed / infra_failure), signature dedup, mass_invalid_target thresholds, INFRA ALERT log | offline |
| `verify_m10_output.log` | Latest run — 14 checks, 0 failed | — |
| `verify_m11.py` | M11 — cold start CLI end-to-end: fetch → LLM gen → user confirm (y/n/q) → seed parser + goldens | **real DeepSeek** |
| `verify_m11_output.log` | Latest run — 15 checks, 0 failed | — |

**Total: 172 checks passed across all milestones.**

## How to re-run

From repo root, with `.venv` activated:

```bash
python -m src.scraping.tests.verify_m1_m3 | tee src/scraping/tests/verify_m1_m3_output.log
python -m src.scraping.tests.verify_m4_m5 | tee src/scraping/tests/verify_m4_m5_output.log
python -m src.scraping.tests.verify_m6   | tee src/scraping/tests/verify_m6_output.log
python -m src.scraping.tests.verify_m7   | tee src/scraping/tests/verify_m7_output.log
python -m src.scraping.tests.verify_m8   | tee src/scraping/tests/verify_m8_output.log
python -m src.scraping.tests.verify_m9   | tee src/scraping/tests/verify_m9_output.log
python -m src.scraping.tests.verify_m10  | tee src/scraping/tests/verify_m10_output.log
python -m src.scraping.tests.verify_m11  | tee src/scraping/tests/verify_m11_output.log
```

On Windows, prefix with `PYTHONIOENCODING=utf-8` (or use PowerShell's `$env:PYTHONIOENCODING="utf-8"`) so `->` and similar ASCII arrows don't crash cp1252.

## LLM-dependent tests

**verify_m8** and **verify_m11** hit the real DeepSeek API (per user decision — see plan file). Requirements:
- `DEEPSEEK_KEY` set in `.env` (loaded automatically via `python-dotenv`).
- Small cost per full run: ~$0.01–0.05 across a handful of `deepseek-chat` requests.
- **LLM output varies**: parser code generated on the same HTML can differ between runs. The verify scripts test *machinery* (ladder progresses, phrases backfill, D25 red line, coldstart seeds correctly), not exact parser code output. A rerun that produces a slightly worse parser may show more `[SKIP]` outputs but should not `[FAIL]`.

## Reading the log

- Section headers (`===...===`) mark which milestone/component is being checked
- `[PASS]` = expected result observed; the detail column shows the actual value
- `[FAIL]` = mismatch; detail shows expected vs actual
- `[SKIP]` = optional dependency missing (e.g., DEEPSEEK_KEY absent for M8/M11)

## What is NOT covered here

- Real BrightData network calls — exercised via `playground.ipynb`
- End-to-end scraping against live sites — deferred to Phase 1 integration tests

## Convention

Any future milestone verification MUST add:
1. A `verify_mN.py` script here (offline preferred; real API only when strictly needed)
2. The captured `verify_mN_output.log` alongside it
3. An entry in this README's table
