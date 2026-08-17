# Scraping legacy verification suite

This directory temporarily contains the milestone-era `verify_mN.py` scripts.
They remain runnable while their checks are migrated by topic into
`tests/unit/scraping/`. New development tests belong in pytest, not here.

## Standard test commands

From the repository root:

```bash
# Default: every offline, free pytest test
python -m pytest

# Explicitly run tests that call real BrightData or LLM APIs
python -m pytest -m live

# Include only long-running subprocess/I/O tests
python -m pytest -m slow
```

Tests that can incur API charges must use `@pytest.mark.live`; the default
pytest configuration excludes that marker. Tests that launch real sandbox
subprocesses or perform multi-second I/O should use `@pytest.mark.slow`.

## Legacy scripts

The existing scripts share `_harness.py` for reporting and temporary-resource
cleanup. Run one from the repository root when auditing a historical milestone:

```bash
python -m src.scraping.tests.verify_m13
```

Do not add a new `verify_mN.py` or capture a new committed output log. When all
checks from a script have equivalent pytest coverage, delete that script.

The former M12 live batch utility is now an operational report tool:

```bash
python -m src.scraping.scripts.live_batch_report
```

It requires a BrightData key, may also call the configured LLM, incurs cost,
and writes its report to `output/live_batch_report.log`.

## Historical logs

Historical output is kept under `logs/archive/` as audit evidence only. The
archive is immutable: do not update those logs and do not add new ones.

`verify_m15_output.log` is historical evidence for a script that is no longer
present. The remaining M15 behavior is covered by later milestone scripts and
will move to topic-based pytest tests during the migration.
