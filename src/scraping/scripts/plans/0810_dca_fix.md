# M27 — Fix false DCA poll timeouts (jsonl response body never recognized as terminal)

## Context

In `verify_m12_qwen_output.log`, items `[01/16]` (Tesco) and `[10/16]` (Argos) both ended with:

```
BrightDataInfraError: DCA collection polling timed out after 303s (id=j_msnzz5m62idum6i70v)
```

The BrightData console showed those collections completing in ~10 seconds. I re-fetched both
collection ids live against the user's BD account — the results are still there and fine:

| collection id | status | content-type | body |
|---|---|---|---|
| `j_msnzz5m62idum6i70v` (Tesco) | 200 | `application/jsonl` | 1 line, single JSON object, `product_name` / `current_price` = 3.75 |
| `j_mso00ezgsa0ashqom` (Argos) | 200 | `application/jsonl` | 1 line, single JSON object, `price` = 140, `list_price` = 176 |

**Root cause.** `/dca/dataset` returns JSON Lines, not a JSON array. For a single-record result
the body is one object plus a trailing newline, so `resp.json()` parses it into a **dict**.
`BrightDataDCA._poll()` ([bright_data.py:291-296](src/scraping/extraction/bright_data.py#L291-L296))
only terminates on `isinstance(data, list) and data`:

```python
if status_resp.status_code == 200:
    data = status_resp.json()
    # DCA's terminal detection: only list check (no status field)
    if isinstance(data, list) and data:
        return data[0]
# non-200 (including 202 "still running") — keep polling
```

A dict falls through the terminal check and through the "still running" comment, so the ready
result is thrown away on every poll for the full `bd_async_poll_max_seconds` (300s) budget, then
raises a timeout. Every DCA fallback in that run burned 300s and returned nothing — the reported
per-URL latencies of 454s / 446s are almost entirely this dead wait.

Two things also made this hard to diagnose and are worth fixing in the same pass:

- `_poll` logs nothing about what it actually received; the timeout message carries only the id.
- A multi-record jsonl body would fail differently again: `resp.json()` raises `JSONDecodeError`
  (a `ValueError`, not `httpx.HTTPError`), so it escapes the `except httpx.HTTPError` guard and
  surfaces as a generic `api_fetch` failure rather than a parse problem.

**Intended outcome**: a DCA collection that BD finishes in 10s is picked up on the next poll tick;
timeouts mean a real timeout, and their message says what the last response looked like.

**Explicitly out of scope** (agreed with the user, report only): the Tesco DCA collector emits no
Clubcard/member field at all — the live record's keys are
`product_name, current_price, currency, on_sale, product_description, product_image, in_stock, input`.
So `TescoDCAScraper._map_fields`'s `member_price` / `clubcard_price` / `loyalty_price` lookups can
never populate `membership_price`, and the API fallback answers a Clubcard page with the plain
price. Fixing that needs the collector definition changed in the BrightData console, not code here.

## Changes

### 1. `src/scraping/extraction/bright_data.py` — shared, format-tolerant poll parsing

Add two module-level helpers above the client classes:

```python
_POLL_PENDING_STATUSES = {"building", "pending", "running", "collecting",
                          "queued", "scheduled", "started", "in_progress"}
_POLL_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "aborted"}
```

`_parse_poll_records(resp) -> list[Any] | None` — normalize any ready-body shape to a record list:

- Prefer line-parsing when the content-type mentions `jsonl` / `ndjson`, **or** the body has more
  than one non-empty line: `json.loads` each non-empty line, skipping lines that fail to decode.
  This is what makes the real single-line jsonl body terminal, and makes a multi-record body work
  instead of raising `JSONDecodeError` out of the loop.
- Otherwise fall back to `resp.json()` inside `try/except (ValueError, json.JSONDecodeError)`
  returning `None` on failure (never let a decode error escape `_poll`).
- Normalize: `list` → itself; `dict` → `[dict]`; anything else / empty → `None`.

  Note: the existing `verify_m13.py` mocks set `resp.text = ""` and `resp.headers = {}`, so they
  take the `resp.json()` fallback path unchanged — old 200+list and 202+`None` fixtures keep working.

`_classify_poll_record(record) -> "pending" | "failed" | "ready"` — status-envelope detection
shared by both clients: a dict whose `status` value (lowercased) is in `_POLL_PENDING_STATUSES`
is BD's `{"status": "building", "message": "Dataset is not ready yet, try again in XXs"}` envelope
→ keep polling; `_POLL_FAILED_STATUSES` → raise `BrightDataInfraError`; anything else (including
a dict with no `status` key) is a real record → return it.

Then collapse the two near-duplicate `_poll` bodies. `BrightDataDatasets._poll` and
`BrightDataDCA._poll` become thin wrappers over one shared coroutine
`_poll_until_ready(get_url, params, job_id, kind)` that:

- keeps the current deadline/interval/`first-GET-is-immediate` semantics
  (`cfg.bd_async_poll_max_seconds` / `bd_async_poll_interval_seconds`, unchanged defaults);
- on a 200, runs `_parse_poll_records` + `_classify_poll_record` and returns `records[0]` when ready;
- raises immediately on 401 / 403 (a permanent auth failure should not consume the 300s budget);
  every other non-200 (including 202) keeps polling as today;
- keeps the existing `except httpx.HTTPError` tolerance for a single transient GET;
- records `last_status` / `last_body_preview` (first ~200 chars) each iteration.

This removes the divergence that caused the bug: DCA's "only list check" comment goes away, and the
Datasets client stops treating a legitimate record that happens to carry a `status` key as pending.

### 2. Observability

- One `logger.info` per poll cycle at first non-terminal 200 (`status`, content-type, body preview),
  and `logger.debug` thereafter — so a future shape change is visible in the log rather than
  silent.
- Timeout message becomes e.g.
  `DCA collection polling timed out after 303s (id=..., polls=75, last_status=200, last_body='{"status":"building"...')`.
  Keep the existing `f"{kind} polling timed out after {elapsed:.0f}s"` prefix wording so the
  escalation signature (`site|api_infra|`) and existing log-grep habits are unaffected.

No changes to `config.py`, `api_scraper.py`, `tesco_dca.py`, `argos_dca.py`, `retry.py`, or the
router — `_trigger` stays the only retried call (the M13 invariant: at most one BD trigger per URL).

### 3. Verification — `src/scraping/tests/verify_m27.py` (offline, mocked httpx)

Follow the module's verification discipline (`verify_mN.py` + `[PASS]`/`[FAIL]` + `SUMMARY:` +
non-zero exit). Extend `verify_m13.py`'s `FakeAsyncClient` / `_make_response` pattern with a
response builder that carries real `text` + `content-type` headers. Checks:

1. **Regression (the actual bug)**: DCA poll, 200 + `application/jsonl` + single-line Tesco record
   → returns the record on the first poll; asserts elapsed is one tick, not the deadline.
2. Same for the Argos single-line record (`price` / `list_price` dicts map through
   `ArgosDCAScraper._map_fields` to a gate-passing `ProductData`).
3. Multi-line jsonl body → first record returned, no `JSONDecodeError` escaping.
4. 202 + `{"status":"building","message":...}` envelope → keeps polling, then terminal on the
   ready body.
5. 200 + `{"status":"building"}` (envelope served with a 200) → still treated as pending.
6. `{"status":"failed"}` → raises `BrightDataInfraError` promptly, no 300s wait.
7. Empty body / empty list / undecodable body → keeps polling (not a false terminal).
8. Genuine timeout → `BrightDataInfraError` whose message contains `last_status` and the body
   preview; exactly 1 POST (M13 no-re-trigger invariant preserved).
9. 401 → raises immediately without consuming the poll budget.
10. Datasets client: 200 + JSON array (today's Amazon shape) still returns `data[0]`; and a record
    dict containing a `status` key that is *not* a pending value is returned rather than polled on.

Run: `python -m src.scraping.tests.verify_m27 | tee src/scraping/tests/verify_m27_output.log`
Re-run `verify_m13` (and its log) to prove the trigger/poll split is untouched.

### 4. Docs

- `src/scraping/tests/README.md` — add `verify_m27.py` / `verify_m27_output.log` to the inventory table.
- `src/scraping/CLAUDE.md` — add an M27 row to the milestone table and a short section under the
  M13 polling section recording that `/dca/dataset` serves **jsonl**, that terminal detection is
  now shape-tolerant and shared by both clients, and the Tesco-collector membership gap as a known
  limitation. (`AGENTS.md` is synced by the pre-commit hook; UTF-8 no BOM.)
- No README change: no CLI, config key, input/output path, or manual-file change
  (`bd_async_poll_max_seconds` = 300 / interval 4s stay as they are).

## Verification (end to end)

```bash
python -m src.scraping.tests.verify_m27 | tee src/scraping/tests/verify_m27_output.log
python -m src.scraping.tests.verify_m13 | tee src/scraping/tests/verify_m13_output.log
python3 scripts/check_encoding.py --all
```

Both suites must end `SUMMARY: N passed, 0 failed`. The decisive check is #1: with the pre-fix
`_poll`, a 200 + jsonl single-record body polls to the deadline and raises; after the fix it
returns on the first tick.

Optional live confirmation (BD credits, only if the user asks later): re-run one Tesco and one
Argos URL through the DCA scraper and confirm the run latency drops from ~300s to ~10-20s.
