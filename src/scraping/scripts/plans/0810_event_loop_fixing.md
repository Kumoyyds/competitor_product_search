# Plan: Fix `close_chat_clients` atexit crash

## Context

When the M12 live test (`verify_m12.py`) completes, `asyncio.run()` closes the event loop. Python then fires `atexit` handlers, including `close_chat_clients()` registered in `providers.py:280`. Since there is no running event loop at that point, the fallback `asyncio.run(coroutine)` creates a **new** event loop. But the async HTTP transport objects inside the cached chat clients hold references to the **original** (now closed) event loop. When `_close_async_roots` tries to close them, they attempt to schedule callbacks on the closed loop → `RuntimeError: Event loop is closed`.

This produces noisy traceback output after every test run but does **not** affect test results — the error is caught and logged inside `_close_async_roots`.

The trigger: `atexit.register(close_chat_clients)` at [providers.py:280](src/scraping/providers.py#L280).

## Fix

**Single file**: [src/scraping/providers.py](src/scraping/providers.py)

In `close_chat_clients()` (lines 239–272), change the `except RuntimeError` branch (line 266–267):

**Current**:
```python
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    asyncio.run(coroutine)
```

**Replace with**:
```python
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    # atexit / interpreter shutdown: the original event loop is already
    # closed, so async transports can't be cleanly torn down from a fresh
    # loop. Skip async-root close; they'll be GC'd during shutdown.
    logger.debug("no running event loop — skipping async LLM client close")
```

This is safe because:
1. **During normal operation** (event loop running): `asyncio.get_running_loop()` succeeds → task is scheduled on the active loop → closes cleanly. No change.
2. **During atexit** (no event loop): the old code tried `asyncio.run()` which fails because transports reference a closed loop. The new code skips async close — sync roots are still closed, and async roots are garbage-collected during interpreter shutdown.
3. **In verify_m26.py tests**: `close_chat_clients()` is called while the event loop IS running (inside `asyncio.run()`), so path 1 applies. No change.

## Side note: "Unavailable URL" test failure

The other issue in the log — `FAILED: Unavailable URL handled (success)` for `https://www.tesco.com/shop/en-GB/products/299425748` — is a **test data** problem: that Tesco product page is now live (returns "Tesco White Toastie Bread Thick Sliced 800g" at £0.75). The spreadsheet label is stale. This is not a code bug and is out of scope for this fix.

## Verification

1. Run `python -m src.scraping.tests.verify_m25` — already passes (11/11), should continue to pass (no functional change to client creation/reuse)
2. Run `python -m src.scraping.tests.verify_m26` — the `M26.6` section explicitly tests `close_chat_clients()` with a running event loop, should continue to pass
3. Run `python -m src.scraping.tests.verify_m12` — the atexit crash traceback should no longer appear after the test summary
