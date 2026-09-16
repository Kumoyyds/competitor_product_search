# Scraping Module

**Status**: Phase 0 complete — full lifecycle (extraction, gates, repair, cold start, escalation)
implemented and tested. For the change history, use `git log` on this directory; for design
rationale, use `docs/scraping/design.md`. This file is the architecture map.

## Responsibility

Extracts structured product data from marketplace pages. Takes `(url, website)` as input,
returns `ProductData` (Pydantic model), `InvalidTargetResult` (not-a-product sentinel), or
raises `ScrapeFailed` (terminal, all scrapers exhausted).

## Which doc do I read

- **This file** — architecture map, class hierarchy, invariants, conventions for changing code safely.
- [`docs/scraping/design.md`](../../docs/scraping/design.md) — mechanism-level design: how the
  gates, repair ladder, golden set, cold start, and escalation actually work and why, with
  diagrams.
- [`README.md`](README.md) — operator manual: install, run, cold-start workflow, config table,
  adding a site, exit codes.
- [`scraping_module_spec_v1_2.md`](scraping_module_spec_v1_2.md) — frozen original spec
  (Chinese), decisions D1–D29 with rationale. `docs/scraping/design.md` §0 lists where the spec
  is now out of date; the D-numbered rationale still holds.
- `docs/scraping/storage.md` — generated SQLite schema reference.

## Design Spec

Full spec: `scraping_module_spec_v1_2.md` (v1.2, 510 lines). Key decisions are numbered D1–D29
with rationale.

## Architecture

### Data Flow

`Router.scrape(url)` → host→site→ordered scraper list (two-hop) → try each scraper:
- **HTMLScraper route**: BrightData Web Unlocker → HTML → invalid-target pre-detection → ordered
  parser list (sandbox-executed) → two gates → success → ProductData. On failure: **Agent repair
  ladder** (attempt count = `len(repair_model_ladder)`; config-driven via `config.py`) →
  candidate parser → sandbox + golden test → promote if passes.
- **DirectAPIScraper route**: BrightData Datasets/DCA API → JSON → field mapping → two gates. On
  gate failure: **restricted JSON self-healing** (D25 red line — remaps existing keys only,
  never fabricates).

On terminal failure of a scraper: Router tries the next in the list; when exhausted →
`EscalationStore.upsert(signature, reason, snapshot)` with reason ∈ `{parser_broken,
api_malformed, infra_failure, mass_invalid_target}`.

### Class Hierarchy

Registered scrapers live under `scrapers/sites/` — check that directory for the current roster
rather than trusting this doc, since sites are added and removed over time (`hosts.yaml` /
`sites.yaml` are the live source of truth). Shape:

```
BaseScraper (ABC)
  ├── HTMLScraper (Template Method)
  │     └── e.g. TescoScraper (Web Unlocker, order=1)
  └── DirectAPIScraper
        └── e.g. AmazonUKScraper (Datasets API, order=1)
        └── e.g. TescoDCAScraper (DCA API, order=2, Tesco backup)
```

A site can register more than one scraper at different `order` values as a fallback chain (see
"Adding a fallback scraper" in the README); the router tries them in ascending order.

### Two Gates (Public Checkpoint)

- Gate 1: Pydantic type/structure validation (`price` optional at this layer)
- Gate 2: `feasible_check` cross-field semantics:
  - `in_stock=True + price=None/<=0` → fault; `price` is always the ordinary non-member price
  - `list_price` may be present only when `list_price > price` (higher Was/RRP)
  - `membership_price` may be present only when `membership_price < price` (lower gated member price)
  - `in_stock=False + no_images + no_price + no_list_price + no_membership_price` → fault (likely an error/stub page, not a real out-of-stock product)

See `docs/scraping/design.md` §2 for the full rule table, the canonical price contract, and why
the gates are route-agnostic.

### Repair Ladder (§5.5)

Config-driven: attempt count = `len(cfg.repair_model_ladder)`. Each attempt registers a full
`AttemptRecord` (code, capture summary, errors) fed back to the next attempt — no index
misalignment, works for any node count. Default: `["deepseek-v4-flash", "deepseek-v4-pro"]` (2
attempts).

Three turns per the default 2-node ladder — Turn A (`no_product`, attempt 0 only), Turn B
(`source_absence`, evidence-gated, non-last attempt only), Turn C (parser generation, every
attempt). Full mechanics, convergence signals (price-aware pre-pass, promotion detection,
fast-path distrust guard, temperature/thinking ramp), and why each turn sits where it does:
`docs/scraping/design.md` §5.

### BrightData extraction, encoding, and API price normalization

`extraction/bright_data.py:_check_infra_error` classifies transient infra failures (status
codes, `x-brd-error-code` header, body-length fallback, upstream error markers) before a
response ever reaches parsing, and `BrightDataUnlocker.fetch()` forces UTF-8 decoding rather
than trusting an auto-guessed charset. Hand-written API mappings pass through
`scrapers/price_fields.py` as a single normalization choke point before validation; HTML output
is deliberately left unnormalized so a bad generated mapping still fails gates and triggers
repair. Both Datasets and DCA polling share one shape-tolerant JSON/JSONL response loop. Details
and rationale: `docs/scraping/design.md` §1 ("Extraction hardening") and §9.

### Run correlation and repaired-parser attribution

`results.run_id → scrape_runs.id` and `scrape_runs.escalation_id → escalations.id` give every
qualified result and every failed execution an exact producing/failing run; a repaired HTML
success records the promoted parser as `winning_parser_id`, and either repair route records the
actual LLM used in `scrape_runs.repair_model`. `scrape_runs` is strictly one row per execution —
there is no success-dedup window. Details: `docs/scraping/design.md` §1.

## File Structure

```
src/scraping/
├── __init__.py             # Public API: scrape(), ProductData, ScrapeFailed
├── config.py               # ScrapingConfig (spec §7)
├── providers.py             # LLM vendor call-capability registry + unified client factory
├── exceptions.py           # ScrapeFailed, BrightDataInfraError, SandboxSpawnError
├── detection.py            # Invalid page detection (5 signals)
├── router.py               # Two-hop dispatch + fallback loop + escalation writer
├── registry.py             # @register_scraper decorator
├── hosts.yaml              # host → site mapping (edit here to add sites)
├── sites.yaml              # site → page-type availability / cold-start profile
├── site_profile.py         # fail-open site-profile loader and policy accessors
├── coldstart.py            # CLI cold start (validated Excel + review/repair loop + golden HTML reuse)
├── models/                 # ProductData (availability_raw normalizer), enums, InvalidTargetResult
├── validation/             # gate1 (Pydantic), gate2 (feasible_check + structural price rules)
├── scrapers/
│   ├── base.py             # BaseScraper ABC
│   ├── html_scraper.py     # HTMLScraper Template Method (parser list, fast-path distrust guard)
│   ├── api_scraper.py      # DirectAPIScraper + JSON healer integration + heal cache
│   ├── price_fields.py     # API price-field normalization choke point
│   └── sites/              # Registered scrapers — check this directory for the current roster
├── extraction/
│   ├── bright_data.py      # Unlocker / Datasets / DCA async clients
│   └── retry.py            # Extraction retry (D7)
├── repair/
│   ├── sandbox.py          # AST isolation + bounded, cancellation-safe subprocess lifecycle
│   ├── agent.py            # Repair ladder (RepairContext, ladder driver, no_product/source_absence branches)
│   ├── prompts.py          # Prompt builders + SCHEMA_HINT + PriceContext renderer
│   ├── prepass.py          # Price-aware context builder (PriceEvidence, PriceContext, anchoring, cross-sell delete, promotion signal detector)
│   ├── json_healer.py      # Restricted JSON remap (D25 3-layer enforcement)
│   └── golden.py           # classify_page_type (5 buckets incl. membership), promote_candidate, prune
├── storage/
│   ├── database.py         # 6 SQLite tables
│   └── ...                 # store classes (golden, parser, run, result, escalation, phrase)
└── tests/                  # Legacy verify_mN.py scripts pending topic-based pytest migration
```

## Public API

```python
from src.scraping import scrape, ProductData, InvalidTargetResult, ScrapeFailed

result = await scrape("https://www.argos.co.uk/product/3284476")
# result is either ProductData or InvalidTargetResult; ScrapeFailed raised on terminal failure
```

## Cold Start (new site)

```bash
uv run python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx
```

Operational detail (workbook format, review keys, exit codes) lives in the README's "Cold start
a new site" section; the human-in-the-loop rationale and round-based review mechanics live in
`docs/scraping/design.md` §8.

## Key Config (all in `ScrapingConfig`, spec §7)

The authoritative default table is the README's [configuration table](README.md#configuration)
— do not duplicate values here, they will drift. Conventions an agent needs when touching
config:

- Model ladders (`repair_model_ladder`, `cold_start_model_ladder`) are **lists**, not counts —
  attempt count is `len(ladder)`; the matching temperature ladder must be the same length
  (asserted at runtime).
- `sites.yaml` per-site keys override `config.py` global defaults; both are fail-open
  (`site_profile.py`) — an undeclared site or type never raises, it falls back to the global
  default.
- `validate_model_ladders()` resolves every configured ladder entry eagerly at the cold-start CLI
  and orchestrator batch entry points, so a typo'd model name fails before any paid scraping
  spend, not after N BrightData fetches.
- `SCRAPING_DB_PATH` overrides the SQLite path (default `scraping.db`).

## Phase 0 Known Compromises

- **Windows sandbox**: `resource.setrlimit` is POSIX-only. On Windows only the subprocess timeout provides isolation. Phase 2 will use Docker.
- **JSON heal cache**: In-memory class-level dict (`DirectAPIScraper._json_heal_cache`), lost on process restart. Next scrape re-heals (~1 LLM call).
- **INFRA ALERT**: Logged via `logger.error`, no email/IM. Phase 1 hook.
- **LLM output variance**: Verify scripts test *machinery*, not exact parser code. Different runs may produce different (but correct) parsers.

## External Dependencies

- **BrightData Web Unlocker** — raw HTML route
- **BrightData Datasets API** / **DCA** — structured JSON route (primary or fallback, per site)
- **Provider-aware LLMs** — runtime repair and cold start both currently default to DeepSeek via
  its official OpenAI-compatible API (Qwen via DashScope is the alternative). Model → vendor →
  (base_url, key_name) routing is resolved by keyword match against the shared
  `src/common/llm_router_config.yaml` (also used by Search and Matching); add a new vendor
  there, then set its key in `.env`. `providers.py` separately holds optional per-vendor call
  capabilities (thinking params, output caps, JSON-mode support) keyed by the same vendor name —
  add an entry there only if a vendor needs one of those overrides. An unroutable model name
  raises `UnknownModelError` (`src/common/llm_client.py`).

### Output cap (`ProviderSpec.max_output_tokens`)

Parser generation emits the whole `parse()` source as a JSON-escaped string, so replies run
long. The cap is injected into `extra_body` as a body-level `max_tokens`, **not** passed as
`ChatOpenAI(max_tokens=...)` — langchain rewrites that field to `max_completion_tokens`, which
DeepSeek accepts and silently ignores. Anything raising a cap must keep using the body-level
name; `docs/scraping/design.md` §11 has the full failure mode this prevents
(`LengthFinishReasonError` discarding partial content).

## Verification Discipline (mandatory)

Add new developer tests by topic under `tests/unit/scraping/` and run them with
pytest. Keep the default suite offline and deterministic by mocking BrightData,
HTTP, and LLM clients. Any test that calls a real paid API MUST use
`@pytest.mark.live`; it is excluded from the default `uv run pytest` run.
Tests that launch real sandbox subprocesses or perform multi-second I/O SHOULD
also use `@pytest.mark.slow`.

Do not add new `verify_mN.py` scripts or committed run logs. Existing milestone
scripts are migration inputs only: move their checks to topic-based pytest files,
then delete a script once every check has equivalent coverage. Historical logs
under `tests/logs/archive/` are immutable audit evidence.

The paid end-to-end batch report is operational tooling, not a test. Run it only
when explicitly needed with `uv run python -m src.scraping.scripts.live_batch_report`.
