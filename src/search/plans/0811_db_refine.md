# 批处理改为参数化模块 + 单次调用也入库

## Context

当前批处理入口是 [main.py](src/search/main.py)：它无条件打开仓库根目录的 `config_search.yaml` 取全部参数，再给 `input_file` 拼上 `os.getcwd()/input/` 前缀。这条路径现在已经是死代码——`config_search.yaml` 在 commit `854e063` 里被删掉了，`run.py` 也已从工作区删除，跑 `main.py` 只会 `FileNotFoundError`。

同时，DB 追踪（`runs`/`tasks`/`attempts`/`node_events`/`candidates`/`llm_calls`，见 [plans/0811_db_building.md](src/search/plans/0811_db_building.md)）的埋点只在 `main.py::_run_row` 里装配：只有批处理会建 `TaskRecorder`、设 contextvar、flush 入库。直接调 `match_product()` 的单次运行——notebook、脚本、将来的 API——全流程零痕迹，因为 [pipeline.py:59](src/search/pipeline.py#L59) 只在 contextvar 里已有 recorder 时才记录。

两个目标：

1. 批处理变成和 `match_product()` 同构的**参数化模块** `match_product_batch()`：输入文件走完整路径（不再拼 `input/` 前缀），其余参数全部走函数签名。删除 `main.py`，不再依赖任何 per-run yaml。
2. **任何一次运行都入库**，不论单次还是批量。

## 设计决策（已与用户确认）

- 新函数 `match_product_batch()`，独立文件 `src/search/batch.py`，与 `match_product()` 并列导出（不做成 `match_product` 的 batch 模式）。
- 单次调用 = **一次独立的 run**：每次无 recorder 的 `match_product()` 铸造自己的 `run_id`，`mode='single'`、`total_tasks=1`。
- `batch.py` 保留一个 argparse CLI，纯 flag，无 yaml。

---

## 改动

### 1. 新增 `src/search/batch.py`（替代 `main.py`，后者删除）

```python
@dataclass
class BatchResult:
    df: pd.DataFrame          # 输入 df + 4 个结果列
    run_id: str
    provider_calls: dict[str, int]
    output_path: str | None

async def match_product_batch(
    input_file: str,                 # 完整路径，原样传给 pd.read_excel —— 不加任何前缀
    *,
    sku_col: str,
    website: str,
    country: str = "uk",
    output_file: str | None = None,  # 完整路径；None = 不写 Excel，只返回 df
    concurrency: int = 16,
    serper_max_calls: int | None = None,
    provider: SearchProvider | list[SearchProvider] | None = None,
    progress: bool = True,
) -> BatchResult
```

主体从 `main.py::_amain` 平移，去掉 yaml 读取和路径拼接：

- provider 链沿用现有解析——`provider is None` 时读 `search_config.yaml` 的 `search.provider` 并用 [`make_provider_chain`](src/search/providers/__init__.py) 构造（本函数负责 `aclose`）；调用方传入的链不代管。
- 并发/收集逻辑不变：`asyncio.Semaphore` + `tqdm_asyncio.gather`（`progress=False` 时退回 `asyncio.gather`）。
- 输出 4 列不变：`url_search_1` / `match_verdict` / `match_layer_trace` / `match_reason`。
- 仅当 `output_file` 非空时写 Excel（`os.makedirs(os.path.dirname(...) or ".", exist_ok=True)`），永远返回 df。

CLI（薄壳，只解析 flag 后 `asyncio.run(match_product_batch(...))`）：

```
python -m src.search.batch --input input/x.xlsx --sku-col item_sku_name_en_new \
    --web amazon --country de --output output/y.xlsx [--concurrency 16] [--serper-max-calls 200]
```

### 2. 抽出 run/task 两层记录的公共装配

现在 `_run_row` + `start_run`/`finish_run` 的样板只存在于 `main.py`，单次路径要复用，必须先抽出来。

**[db.py](src/search/db.py)** 新增两个模块级工具：

- `git_commit() -> str | None` —— 从 `main.py::_git_commit` 平移。
- `run_scope(...)` —— `@asynccontextmanager`，yield `run_id`：
  - 入口：`get_db()`（返回 `None` 即禁用，全程 no-op）+ `start_run(...)`，含 `pipeline_config=config.load_config()` 快照与 `git_commit()`。
  - 出口：`finish_run(status=...)`，`completed` / `failed` / `interrupted`（`KeyboardInterrupt`、`CancelledError`）。把 `main.py:172-194` 那三段重复的 except 收敛成一处。
  - `provider_calls` 参数收 `Callable[[], dict[str, int]]`，退出时才求值——provider 链构造到一半就抛异常时也能拿到已有计数（保持 `main.py` 现有行为）。

**[trace.py](src/search/trace.py)** 新增 `record_task(db, *, run_id, row_index, product_name, website, country, brand=None)` —— `@asynccontextmanager`，yield `TaskRecorder`：建 recorder、`set_recorder`、异常时 `recorder.fail(exc, traceback.format_exc())` 后**重新抛出**、`finally` 里 `reset_recorder` + `await asyncio.to_thread(db.flush_task, recorder)`。批处理在外层 catch 成 error 行；单次直接向上抛。`db` 参数用 `TYPE_CHECKING` 声明类型，避免 `trace ← db` 的循环导入。

### 3. `runs` 表加 `mode` 列

`mode TEXT`，取值 `batch` / `single`——单次运行会大量产生 `total_tasks=1` 的 run 行，没有这一列就无法把它们和批处理区分开来做聚合。

- 加进 `_init_schema` 的建表语句和 `start_run` 的列清单。
- `_init_schema` 里补一段幂等的补列逻辑（`PRAGMA table_info(runs)` 查缺，缺则 `ALTER TABLE runs ADD COLUMN mode TEXT`），让已存在的本地 `search_db.sqlite` 不必删库。
- `SCHEMA_VERSION` 升到 `"2"`，`meta` 的写入从 `INSERT OR IGNORE` 改成 upsert（`ON CONFLICT(key) DO UPDATE`），否则老库的版本号永远停在 1。

### 4. [pipeline.py](src/search/pipeline.py)：单次调用自带 run + task 记录

- 现有函数体改名为私有的 `_match_product(...)`，并把 provider 解析/`own_providers`/`aclose` 那段（[pipeline.py:45-53](src/search/pipeline.py#L45-L53)、[100-103](src/search/pipeline.py#L100-L103)）**上提到公开的 `match_product`**。`_match_product` 只收一个已解析好的 `providers: list[SearchProvider]`。这样公开层才知道链是不是自己建的，进而决定 `provider_calls` 是否可信（调用方传入的共享链，`calls_made()` 是跨调用累计值，此时记 `None`）。
- 公开 `match_product` 签名只加一个可选参数，其余不变：

```python
async def match_product(
    product_name, website, brand=None, country="uk", provider=None,
    record: bool | None = None,   # None = 跟随 search_config.yaml 的 db.enabled
) -> MatchResult
```

- 分支逻辑：
  - `get_recorder() is not None`（说明处在批处理的 task 上下文里）→ 直接跑，**不**建新 run，保持现状。
  - `record is False` 或 `get_db()` 返回 `None` → 直接跑，零 IO。
  - 否则 → `async with run_scope(mode="single", total_tasks=1, job_config={product_name, website, brand, country})` 套 `async with record_task(row_index=0)`，成功时 `recorder.complete(result, recorder.final_provider)`。
- `finish_run` 现有的计数 SQL 按 `run_id` 聚合 tasks，单次 run 天然正确，无需改动。

### 5. 出口与文档

- [`src/search/__init__.py`](src/search/__init__.py)：导出 `match_product_batch`、`BatchResult`。
- 文档：根 [CLAUDE.md](CLAUDE.md)（Setup & Run、Data Flow、Key Files、Config files 三处都要去掉 `run.py` / `config_search.yaml`）、[src/search/CLAUDE.md](src/search/CLAUDE.md)、[src/search/README.md](src/search/README.md)、[README.md](README.md#L35-L37)、[docs/architecture.md](docs/architecture.md#L6-L9)。`AGENTS.md` 由 [scripts/sync_agent_docs.py](scripts/sync_agent_docs.py) 的 pre-commit 钩子自动同步，不手改。
- [maintain/search_config.yaml](src/search/maintain/search_config.yaml) 顶部注释里"per-run settings live in config_search.yaml"要改成"per-run settings are function arguments"。
- [scripts/validate_search.py](scripts/validate_search.py) 保持不动：它循环调 `match_product`，按新语义每行产生一条 `mode='single'` 的 run 记录，这与已确认的设计一致；需要关掉时传 `record=False` 即可。

---

## 验证

1. **新增 `tests/unit/search/conftest.py`**：autouse fixture 把 `src.search.pipeline.get_db` patch 成返回 `None`。现有的 [test_pipeline_shortcircuit.py](tests/unit/search/test_pipeline_shortcircuit.py) 等直接调 `match_product`，不隔离的话会在 cwd 里凭空写出 `search_db.sqlite`。
2. **新增单测**（沿用 [test_db_trace.py](tests/unit/search/test_db_trace.py) 的 `FakeProvider` + `AsyncMock` LLM 模式，零 API 成本）：
   - 单次 `match_product(record=True)` + patch 成 `SearchDB(tmp_path)` → 断言 `runs` 恰好 1 行且 `mode='single'`、`total_tasks=1`、`status='completed'`，`tasks` 1 行且 `run_id` 对得上，`PRAGMA foreign_key_check` 为空。
   - `match_product_batch` 跑 3 行的临时 xlsx（fake provider + fake LLM）→ 断言返回的 df 有那 4 列、`output_file` 落盘、`runs` 1 行 `mode='batch'` 且 `total_tasks=3`、`tasks` 3 行。
   - 一行抛异常时：批处理不中断，该行 `url_search_1='not found'`/`verdict='error'`，run 仍 `completed`。
   - 老库升级：先用旧 schema（无 `mode` 列）建库，再 `SearchDB(same_path)`，断言不报错且 `mode` 列已存在。
3. **回归**：`python -m pytest tests/unit/search/ -v` 全绿。
4. **端到端**（需要 API key）：
   ```
   python -m src.search.batch --input input/amazon_de_url.xlsx --sku-col <col> \
       --web amazon --country de --output output/smoke.xlsx --concurrency 4
   ```
   然后 `SELECT run_id, mode, status, total_tasks, matched_count FROM v_run_summary;`，以及在 notebook 里单调一次 `await match_product(...)` 后确认新增了一条 `mode='single'` 的记录。
5. **中断恢复**：批处理跑到一半 Ctrl-C，断言已完成的 task 都在库里、该 run `status='interrupted'`。
