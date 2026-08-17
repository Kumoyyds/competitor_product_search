# Auto-generate the capability lists in the READMEs from their real sources

## Context

Three "what can this tool handle" lists are documented in the READMEs by hand, and each has a
different source of truth in the code:

| Capability | Real source | Documented in |
|---|---|---|
| Country codes | `_COUNTRY_TO_REGION` in `src/search/providers/duckduckgo.py:16-32`, `_COUNTRY_TO_GL` in `src/search/providers/serper.py:18-34` | `README.md:31` (inline list), `src/search/README.md:111-126` (table) |
| Websites / marketplaces | `domain_map` + `search.retailer_keywords` in `src/search/maintain/search_config.yaml:26-41` | `README.md:30` (inline list), `src/search/README.md:97-101` (table) |
| LLM vendors | `providers:` in `src/search/maintain/llm_router_config.yaml` | **nowhere** — only implied by `README.md:25` ("get your api keys: qwen"), which is already stale since `llm.model` now routes to deepseek |

Editing a provider dict or a `maintain/` YAML silently leaves the docs behind, and the LLM list
was never written down at all. The fix is to make those doc regions generated, and to regenerate
them from a git pre-commit hook so no edit path can skip it — the repo already has that
infrastructure (`.githooks/pre-commit` → `scripts/sync_agent_docs.py` + `scripts/check_encoding.py`,
enabled via `core.hooksPath = .githooks`).

Outcome: adding a country to a provider, a marketplace to `search_config.yaml`, or a vendor to
`llm_router_config.yaml` updates both READMEs automatically at commit time.

## Approach

Reuse the existing hook pattern exactly: one new pure-stdlib script under `scripts/`, invoked
with `--pre-commit --root "$ROOT"`, which rewrites marked regions and `git add`s what it changed
(same auto-fix-and-stage behavior as `sync_agent_docs.py`).

### 1. New: `src/search/providers/countries.py`

Display-name lookup only, no logic — the country dicts carry codes, not names:

```python
COUNTRY_NAMES: dict[str, str] = {"uk": "United Kingdom", "gb": "United Kingdom", "de": "Germany", ...}
```

Seed it from the trailing comments already in `duckduckgo.py:17-31`. A code missing here renders
as `—` plus a non-blocking warning, so adding a country never blocks a commit.

### 2. New: `scripts/gen_capability_docs.py`

Copy the skeleton of [scripts/sync_agent_docs.py](scripts/sync_agent_docs.py) — `fail()` with a
bracketed stderr tag, `repo_root()`, `run_git()`, `stage()`, `argparse` with `--root` /
`--pre-commit`. Add `--check` (exit 1 if stale, writes nothing) for manual/CI use.

**Collectors** (all read-only):

- `collect_countries()` — walk `src/search/providers/*.py`, `ast.parse` each file, pick up every
  module-level assignment named `_COUNTRY_TO_*`, `ast.literal_eval` the dict. Parsing rather than
  importing keeps the script stdlib-only and avoids pulling in `ddgs`/`aiohttp` at commit time.
  A new provider that follows the `_COUNTRY_TO_*` convention documented in
  [src/search/CLAUDE.md](src/search/CLAUDE.md) gets its own column with no script change; column
  header derives from module stem + var suffix (`duckduckgo` → `region`, `serper` → `gl`).
  Rows = union of all codes; codes whose value tuple is identical across every provider collapse
  into one row (`uk` / `gb`), preserving today's table shape.
- `collect_websites()` — `domain_map` and `search.retailer_keywords` from `search_config.yaml`.
  Row set = union of both keys, so a half-added marketplace shows up as a visible gap.
- `collect_llm_vendors()` — `providers:` from `llm_router_config.yaml` (keyword, `base_url`,
  `key_name`) plus the active `llm.model` from `search_config.yaml`, so the docs stop naming qwen
  when deepseek is what's wired.

YAML needs PyYAML (already a hard dep via `requirements.txt` and `src/search/config.py`). Guard
with `try: import yaml / except ImportError:` → warn and `exit 0`, matching the hook's existing
"no python found, skip rather than block" stance.

**Renderers** produce one string per block id: `countries-inline`, `countries-table`,
`websites-inline`, `websites-table`, `llm-inline`, `llm-table`.

**Injector** — one regex pass over each README:
`<!-- BEGIN GENERATED: (id) -->(.*?)<!-- END GENERATED: \1 -->` (DOTALL). Inline blocks live
mid-sentence so the hand-written prose around them survives; table blocks span whole lines.
An unknown id in a README is a hard `fail()`; a registered id absent from both READMEs is fine.

### 3. Marked regions in the docs

**`README.md`**
- L30 — wrap only the code enumeration in `websites-inline`; the amazon-TLD explanation stays hand-written.
- L31 — wrap only the code enumeration in `countries-inline`.
- New bullet after L12 under `# core logic`, block `llm-inline`: the routed vendors and the active
  `llm.model`, mirroring the existing "search engine chain" bullet.
- L25 — replace the hardcoded "qwen" api-key link with a pointer to that new bullet.

**`src/search/README.md`**
- L97-101 → `websites-table` (header + body).
- L111-126 → `countries-table`.
- New `### Accepted LLM vendors` subsection between L128 and L130 (`### Programmatic`), block
  `llm-table`: keyword | base_url | required `.env` key. Sits alongside the two existing capability
  tables under `## 3. Input`, and the `### Environment` block at L138-141 links to it.

Prose that explains *semantics* (registrable-prefix rule at L103, unmapped-code degradation at
L128, maintenance instructions at L175/L186/L191) stays outside the markers — it does not change
when a row is added.

### 4. Wire into the hook

In [.githooks/pre-commit](.githooks/pre-commit), add between the two existing script calls — after
the doc sync, before `check_encoding.py`, since it writes files that the encoding check then sees
staged:

```sh
"$PY" "$ROOT/scripts/gen_capability_docs.py" --pre-commit --root "$ROOT" || exit 1
```

Note the inherited behavior: like `sync_agent_docs.py`, staging is per-file, so an unrelated
unstaged edit elsewhere in a README gets swept into the commit. The script prints exactly which
files it rewrote so this is visible, not silent.

### 5. Tests — `tests/unit/test_gen_capability_docs.py`

- `ast`-based country collection over a fixture provider file, including a third provider module
  appearing as a new column.
- Alias collapsing (`uk` / `gb`) and the `—` fallback for a code missing from `COUNTRY_NAMES`.
- Injection is idempotent: rendering twice produces byte-identical output.
- **Freshness guard**: run `--check` against the real repo and assert exit 0, so a stale README
  fails the test suite even if someone commits with `--no-verify`.

### 6. Docs about the docs

Add a row to the "Things that drift over time" / maintenance tables in
`src/search/README.md:193` and `README.md:43` noting these regions are generated and hand-edits
inside the markers are overwritten. `CLAUDE.md` / `AGENTS.md` stay untouched (per your scope
choice) — they only use `tesco`/`amazon` as examples, not as authoritative lists.

## Files touched

| File | Change |
|---|---|
| `scripts/gen_capability_docs.py` | new — collectors, renderers, marker injector, `--pre-commit` / `--check` |
| `src/search/providers/countries.py` | new — `COUNTRY_NAMES` display map |
| `.githooks/pre-commit` | one added line |
| `README.md` | 3 marked regions + maintenance note |
| `src/search/README.md` | 2 marked regions + new LLM subsection + maintenance note |
| `tests/unit/test_gen_capability_docs.py` | new |

## Verification

1. `python scripts/gen_capability_docs.py` then `git diff` — first run should reproduce today's
   tables essentially verbatim; any diff must be an intended improvement, not a regression.
2. Run it a second time → `git diff` empty (idempotent).
3. Add `"kr": "kr-ko"` to `_COUNTRY_TO_REGION`, run the script: a `kr` row appears in both READMEs
   with `—` under serper and a stderr warning about the missing display name. Add
   `"kr": "South Korea"` to `countries.py` → name fills in, warning gone. Revert both.
4. Add `boots: boots.com` to `domain_map` + `boots: Boots` to `retailer_keywords`, run → website
   table and both inline lists update. Revert.
5. Add a vendor block to `llm_router_config.yaml`, run → LLM table updates. Revert.
6. End-to-end hook: edit `domain_map`, `git add` it, `git commit` → hook prints the rewrite line,
   and `git show --stat HEAD` lists both READMEs in the commit.
7. `python -m pytest tests/unit/ -v` (existing search tests must still pass).
8. Confirm `git status` is clean of scratch edits when done.
