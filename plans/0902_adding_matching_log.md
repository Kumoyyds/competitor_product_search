# Persist the Matching decision process in `orchestrator.db`

## Context

`orchestrator.db` records *where an item ended up* (`valid_results` / `failure_results`) but not *how Matching got there*. The decision evidence that Matching produces today is scattered, partly lossy, and in one case thrown away entirely:

| Outcome | Today | Problem |
|---|---|---|
| MATCH | `valid_results.matching_result` (full `ProductMatchResult` JSON) | Only reachable via the Valid table; no node ordering |
| NO_MATCH (terminal) | `failure_results.detail` | Mixed in with scraping/search failure diagnostics |
| LLM technical error | nothing — [workflow.py:85-92](src/orchestrator/workflow.py#L85-L92) writes only `str(exc)` | All rule evidence lost |
| Rerun revalidation NO_MATCH | **discarded** — [workflow.py:365-370](src/orchestrator/workflow.py#L365-L370) pushes the item to fallback and drops the result | No record that a revalidation decision ever happened |
| Rerun stored-URL identity match | `matching_result=None` ([workflow.py:319-327](src/orchestrator/workflow.py#L319-L327)) | Indistinguishable from "Matching ran and said match" |

Even where evidence survives, it is a flat snapshot: `ProductMatchResult` ([match_result.py:35-43](src/models/match_result.py#L35-L43)) carries `gtin_status` / `variant_status` / `vision_status` / `evidence`, but nothing says which nodes actually *ran* versus were short-circuited past, which model decided, how many LLM attempts it took, or how long it took. On the GTIN short-circuit path `compare_variants` never runs, so `variant_status=unknown` is ambiguous between "ran, inconclusive" and "never ran".

Goal: one queryable, append-only table where each row is one Matching invocation for one item, carrying an ordered node-by-node `decision_process` JSON — GTIN → variant rule → Vision → LLM — including the reasoning the LLM already produces.

**Reasoning is already always on and required** ([service.py:53](src/matching/service.py#L53), [service.py:104-106](src/matching/service.py#L104-L106) rejects an empty one). No change there; this plan just makes it queryable. Deferring the production speed toggle.

---

## 1. Shared model — `src/models/match_result.py`

Add an ordered trace to the existing result, default-empty so every current reader keeps working.

```python
class DecisionNode(StrEnum):
    GTIN = "gtin"
    VARIANT_RULE = "variant_rule"
    VISION = "vision"
    LLM = "llm"
    IDENTITY_GUARD = "identity_guard"  # emitted by the orchestrator, not by Matching


class DecisionNodeRecord(BaseModel):
    node: DecisionNode
    status: str                                    # EvidenceStatus / VisionStatus / verdict / "reused" / "error"
    terminal: bool = False                         # this node settled the verdict
    detail: dict[str, Any] = Field(default_factory=dict)


class ProductMatchResult(BaseModel):
    ...                                            # unchanged fields
    trace: list[DecisionNodeRecord] = Field(default_factory=list)
```

Keep `DecisionNodeRecord` deliberately thin — model ids, latency, attempt counts and evidence all live in `detail`, which is the flexible JSON the table stores.

## 2. Matching emits the trace — `src/matching/service.py`

Populate `trace` as each stage runs; no behavioural change to any verdict.

- **GTIN** ([service.py:185-200](src/matching/service.py#L185-L200)) — always append a `gtin` node with `status=gtin_status.value` and `detail={"normalized_input_gtin", "normalized_product_gtin"}`. `terminal=True` on the PASS short-circuit.
- **Variant rule** ([service.py:201-213](src/matching/service.py#L201-L213)) — append a `variant_rule` node with `status=variant_status.value` and `detail` = the `compare_variants` evidence keys (`brand_status`, `input_brands`, `product_brands`, `numeric_status`, `input_numerics`, `product_numerics`, `multipack_status`, `multipack_notes`, both multipack signature dicts) from [attributes.py:213-224](src/matching/attributes.py#L213-L224). `terminal=True` on CONFLICT. Absent from the trace entirely on the GTIN short-circuit — which is exactly the "did not run" signal that is missing today.
- **Vision** ([service.py:217-239](src/matching/service.py#L217-L239)) — append a `vision` node only when `vision_enabled`. Capture the `CompareResult` fields currently discarded at [service.py:228-236](src/matching/service.py#L228-L236) into `detail`: `comment`, `set_a_images_used`, `set_b_images_used`, `dropped_urls`, `model`, `prompt_tokens`, `completion_tokens`, `error_detail` (all via `getattr(raw, ..., None)` so a batch-wide exception still yields a well-formed node). Never terminal.
- **LLM** ([service.py:250-292](src/matching/service.py#L250-L292)) — append an `llm` node with `status=verdict.value`, `terminal=True`, `detail={"model", "temperature", "attempts", "latency_ms", "reasoning"}`. Time with `time.perf_counter()` around the retry loop; `attempts` = `_attempt + 1`.
- **Technical failure** — when all retries are exhausted ([service.py:290-292](src/matching/service.py#L290-L292)) there is no `ProductMatchResult` to carry the trace. Attach it to the error instead: give `MatchingError` a `trace: list[DecisionNodeRecord]` attribute (default `[]`) and set it when raising, with a final `llm` node `status="error"`, `terminal=True`, `detail={"model", "attempts", "error": str(last_error)}`. `MatchingBatchError.errors[index].trace` then reaches the orchestrator.

## 3. New table — `src/orchestrator/database.py`

**Naming note:** you asked for `matching_decision_node`. I've used **`matching_decisions`** — every other table here is plural, and a row is one whole Matching invocation, not one node (the nodes live inside `decision_process`). Singular `..._node` would read as one-row-per-node. Say the word at approval and I'll use your name instead.

Append to `_DDL` (every column needs its `--` meaning comment or `gen_storage_docs.py` fails the commit):

```sql
-- Append-only node-by-node record of every Matching invocation, including rule short-circuits and skipped decisions.
CREATE TABLE IF NOT EXISTS matching_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT, -- Surrogate matching-decision identifier
    batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE, -- Denormalized owning batch for direct queries
    item_id INTEGER NOT NULL REFERENCES batch_items(item_id) ON DELETE CASCADE, -- Item execution the decision belongs to
    logical_item_id TEXT NOT NULL, -- Stable product identity for comparing decisions across a rerun lineage
    attempt_no INTEGER NOT NULL, -- One-based Matching invocation counter within one item execution
    execution_path TEXT NOT NULL, -- Pipeline path that requested the decision: new_input, stored_url, identity_revalidation, revalidated, or fallback
    url TEXT, -- Scraped product URL the decision was made against
    verdict TEXT NOT NULL CHECK(verdict IN ('match', 'no_match', 'error')), -- Business outcome, or error when Matching failed technically
    decision_source TEXT NOT NULL, -- Node that settled the verdict: gtin, variant_rule, llm, identity_guard, or technical_error
    gtin_status TEXT, -- EvidenceStatus of the GTIN node, or NULL when that node did not run
    variant_status TEXT, -- EvidenceStatus of the brand/numeric/multipack node, or NULL when that node did not run
    vision_status TEXT, -- VisionStatus of the image-comparison node, or NULL when that node did not run
    reasoning TEXT, -- Rule sentence or LLM one-sentence reasoning explaining the verdict
    decision_process TEXT NOT NULL DEFAULT '{}', -- JSON ordered node trace with per-node status and detail
    created_at TEXT NOT NULL, -- UTC ISO timestamp when the decision was recorded
    UNIQUE(item_id, attempt_no)
);
```

Append to `_INDEX_DDL`:

```sql
-- Supports replaying every Matching decision for one item execution in order.
CREATE INDEX IF NOT EXISTS idx_orch_match_item ON matching_decisions(item_id, attempt_no);
-- Supports batch-level decision auditing without joining through items.
CREATE INDEX IF NOT EXISTS idx_orch_match_batch ON matching_decisions(batch_id, decision_id);
```

Bump `SCHEMA_VERSION = 2` ([database.py:15](src/orchestrator/database.py#L15)). No migration code needed: `init_db()` already runs idempotent `CREATE ... IF NOT EXISTS` and stamps `PRAGMA user_version`, so a v1 database gains the table on next open. Pre-v2 rows simply have no decision history.

`decision_process` shape — note the trailing `terminated_at` makes "which node decided" a one-line `json_extract`:

```json
{
  "nodes": [
    {"node": "gtin", "status": "conflict", "terminal": false, "detail": {"normalized_input_gtin": "...", "normalized_product_gtin": "..."}},
    {"node": "variant_rule", "status": "unknown", "terminal": false, "detail": {"brand_status": "pass", "...": "..."}},
    {"node": "vision", "status": "success", "terminal": false, "detail": {"model": "qwen3.7-flash", "comment": "...", "prompt_tokens": 1204}},
    {"node": "llm", "status": "match", "terminal": true, "detail": {"model": "deepseek-v4-flash", "attempts": 1, "latency_ms": 812, "reasoning": "..."}}
  ],
  "terminated_at": "llm"
}
```

Two store helpers, both following the existing `BEGIN IMMEDIATE` … `commit()` pattern of `record_valid` / `record_failure` ([database.py:306-390](src/orchestrator/database.py#L306-L390)), and **neither calling `_terminal_item()`** — a decision must be recordable for an item that is not (yet, or ever) terminal:

```python
def record_matching_decision(
    self, item_id: int, *, execution_path: str, url: str | None,
    verdict: str, decision_source: str, reasoning: str | None,
    gtin_status: str | None = None, variant_status: str | None = None,
    vision_status: str | None = None,
    decision_process: dict[str, Any] | None = None,
) -> int: ...

def record_matching_result(
    self, item_id: int, *, execution_path: str, url: str | None,
    result: ProductMatchResult,
) -> int: ...          # flattens the result onto the columns above, then delegates
```

`attempt_no` is allocated inside the transaction with `SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM matching_decisions WHERE item_id = ?` — same pattern as the rerun-number allocation at [database.py:196-202](src/orchestrator/database.py#L196-L202). `batch_id` / `logical_item_id` are read off `batch_items`, matching how the two terminal tables denormalize them. Serialize with `json.dumps(..., ensure_ascii=False, sort_keys=True)`, the convention at [database.py:377](src/orchestrator/database.py#L377) (list order inside `nodes` is unaffected by `sort_keys`).

## 4. Wire up all five call sites — `src/orchestrator/workflow.py`

Record the decision **before** the terminal row in each case, so a `record_valid` conflict still leaves the decision captured.

1. `_match_and_persist` technical error ([workflow.py:85-92](src/orchestrator/workflow.py#L85-L92)) — `record_matching_decision(verdict="error", decision_source="technical_error", reasoning=str(errors[index]), decision_process={"nodes": [...errors[index].trace...], "terminated_at": "technical_error"})`.
2. `_match_and_persist` MATCH / NO_MATCH ([workflow.py:94-115](src/orchestrator/workflow.py#L94-L115)) — `record_matching_result(..., execution_path=execution_path, url=url, result=result)`.
3. Rerun stored-URL identity shortcut ([workflow.py:318-327](src/orchestrator/workflow.py#L318-L327)) — synthesize the skip: `verdict="match"`, `decision_source="identity_guard"`, reasoning "Stored-URL rescrape reproduced the prior qualified identity; Matching was skipped.", one `identity_guard` node with `status="reused"`, `terminal=True`, `detail={"source_valid_result_id": source.valid_result_id}`. All three `*_status` columns stay NULL.
4. Rerun revalidation error / MATCH ([workflow.py:350-363](src/orchestrator/workflow.py#L350-L363)) — as (1) and (2), `execution_path="identity_revalidation"`.
5. Rerun revalidation NO_MATCH ([workflow.py:365-370](src/orchestrator/workflow.py#L365-L370)) — **the gap this closes.** Record the decision, *then* push to fallback as today.

An item that revalidates to no-match and then re-enters the full pipeline gets `attempt_no=1` (`identity_revalidation`) and `attempt_no=2` (`fallback`) — the reason `UNIQUE(item_id)` would have been wrong here.

## 5. Docs (all mandatory — the pre-commit hook enforces the generated ones)

- `scripts/gen_storage_docs.py` — update `render_orchestrator_migrations()` ([line 552-559](scripts/gen_storage_docs.py#L552-L559)); its current prose ("this is the initial orchestrator schema") is wrong at v2. New text: v2 adds `matching_decisions` via idempotent CREATE, no ALTER or backfill, v1 databases pick it up on next open with no decision history for existing items.
- Regenerate `docs/orchestrator_storage.md` with `uv run python scripts/gen_storage_docs.py --pre-commit` — never hand-edit inside the `BEGIN/END GENERATED` markers. Do hand-edit the "Operational queries" prose ([line 197](docs/orchestrator_storage.md#L197)) to describe replaying a decision history.
- `src/orchestrator/CLAUDE.md` — add the invariant (one row per Matching invocation, including short-circuits, the identity-guard skip, and technical failures; `attempt_no` orders them within an item execution) and mention the table under `database.py`. `AGENTS.md` syncs automatically.
- `src/matching/CLAUDE.md` — add the invariant that every result (and every `MatchingError`) carries an ordered `trace`, and that node *absence* means the node did not run.
- `src/orchestrator/README.md` — operators browse this DB; add the table to whatever it describes about `orchestrator.db`.
- `docs/architecture.md` — add the table if it enumerates orchestrator storage.
- `src/orchestrator/script/database_check.ipynb` — the notebook claims to browse *every* table. Add a `## matching_decisions` markdown + code cell following the existing per-table pattern (`overview(frame, ["decision_process", "reasoning"])`), a per-item decision-replay cell, and `"matching_decisions": "decision_id"` to `REVIEW_TABLES` so `show_blob` can pretty-print `decision_process`.

## 6. Tests

- `tests/unit/matching/test_matching.py` — trace assertions per path: GTIN short-circuit → exactly one terminal `gtin` node (no `variant_rule` node); variant conflict → `gtin` + terminal `variant_rule`; vision-enabled LLM path → four nodes in order with the vision detail fields populated; exhausted retries → `MatchingError.trace` ends in an `llm` node with `status="error"`.
- `tests/unit/orchestrator/test_database.py` — both helpers insert; `attempt_no` increments per item and is independent across items; `UNIQUE(item_id, attempt_no)` holds; `decision_process` round-trips through `json.loads`; a decision can be written for a non-terminal item; deleting a batch cascades.
- `tests/unit/orchestrator/` workflow tests — new_input match / no-match / technical-error each write exactly one row with the right `decision_source`; rerun identity-equal writes an `identity_guard` row; rerun revalidation-no-match writes attempt 1 then the fallback pass writes attempt 2.

## Verification

```bash
uv run pytest tests/unit/matching tests/unit/orchestrator tests/unit/test_gen_storage_docs.py -q
uv run python scripts/gen_storage_docs.py --check          # must be clean after regeneration
uv run python scripts/check_encoding.py --all

# End-to-end against a real batch
uv run python -m src.orchestrator new --input input/products.xlsx --db-path /tmp/orch_check.db
```

Then confirm every item has a decision history, and that the node that decided is directly queryable:

```sql
SELECT i.row_index, i.input_title, d.attempt_no, d.execution_path, d.verdict,
       d.decision_source, d.gtin_status, d.variant_status, d.vision_status,
       json_extract(d.decision_process, '$.terminated_at') AS terminated_at,
       json_array_length(d.decision_process, '$.nodes')    AS node_count
FROM matching_decisions AS d
JOIN batch_items AS i ON i.item_id = d.item_id
ORDER BY i.row_index, d.attempt_no;
```

Expect: one row per item that reached Matching; `terminated_at` = `gtin` for GTIN short-circuits (`node_count` 1), `variant_rule` for rule rejections (2), `llm` for LLM decisions (3, or 4 with `--vision`). Re-run `rerun --batch-id <id>` and confirm unchanged items get an `identity_guard` row, and that any item revalidating to no-match shows two rows with `attempt_no` 1 and 2.

## Out of scope (flagged, not changed)

Two pre-existing default mismatches in `src/matching/service.py`, harmless while the config keys are present: [service.py:174](src/matching/service.py#L174) defaults `llm.concurrency` to 8 while `matching_config.yaml` sets 16, and [service.py:148](src/matching/service.py#L148) defaults `vision.model` to `qwen3-vl-flash` while the config pins `qwen3.7-flash`. Worth a separate cleanup.
