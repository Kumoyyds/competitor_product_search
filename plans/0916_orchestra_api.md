# 操作类 FastAPI 接口 — 实现计划

## Context

业务闭环是：客户触发 New Input 或 Rerun → 结果写入 `orchestrator.db` → 基于 DB 做分析。现在 orchestrator 只能通过 CLI（`python -m src.orchestrator`）调用，`src/api/` 只有一个空的 `__init__.py`。

这次只做**操作类 API**：触发 batch、查状态和进度、查明细。分析类接口以后单独做。

阻塞点：`run_new_input()` 和 `rerun()`（[workflow.py:286](src/orchestrator/workflow.py#L286)、[workflow.py:347](src/orchestrator/workflow.py#L347)）在函数内部生成 batch_id，而且要等整个 batch 跑完才返回。异步 API 的 POST 需要立刻拿到 batch_id，所以第一步要把"准备"和"执行"拆开。

## Step 1 — orchestrator：拆开 prepare / execute（CLI 行为不变）

在 `src/orchestrator/workflow.py` 中：

- 新增 `PreparedBatch` dataclass（frozen），字段包括 `batch_id`、`operation`、`db_path`、`vision_enabled`、`concurrency`，以及执行需要的工作数据：new_input 的 `works: list[WorkItem]`；rerun 的 `works` 和 `sources`（`RerunSource`，按 item_id 对应）。
- `prepare_new_input(source, *, vision_enabled, concurrency, db_path) -> PreparedBatch`（同步）
  - 把现有 `run_new_input` 开头到 `add_item` / `record_failure` 循环为止的代码原样移进来。
  - 结构性输入错误仍然在 `create_new_batch` 之前抛出，不产生 batch，行为和现在一致（见 `test_structural_json_error_is_rejected_before_batch_creation`）。
  - 在 add_item 循环里出错时，调用 `finish_batch(error_message=...)` 后再抛出。
- `prepare_rerun(batch_id, *, search_titles, vision_enabled, concurrency, db_path) -> PreparedBatch`（同步）
  - 移入 `rerun_sources` → `create_rerun_batch` → `add_item` 循环。
  - 前置校验失败（batch 不存在 `KeyError`、`search_title not found` / `no valid products` 的 `ValueError`）时不创建子 batch，行为不变。
  - **新增**：父 batch 的 `status == 'running'` 时抛 `ValueError`（API 映射成 409），避免基于还没跑完的结果做 rerun。
- `async execute_batch(prepared) -> BatchResult`
  - 自己打开 `OrchestratorDB`，执行剩下的流程：new_input 走 `_run_full_pipeline`；rerun 走 stored_url scrape → identity revalidation → fallback。
  - 保留现有的 `except BaseException → finish_batch(interrupted=...)` 和 `finally: db.close()`。
- `run_new_input` / `rerun` 改成 `prepare_*` 加 `await execute_batch(...)`，签名和返回值不变，`__main__.py` 不用改。
  - `validate_model_ladders()` 放在 prepare 里。
- `src/orchestrator/__init__.py` 导出 `PreparedBatch`、`prepare_new_input`、`prepare_rerun`、`execute_batch`。
- `database.py` 新增 `mark_running_batches_interrupted(reason) -> list[str]`：把所有 `status='running'` 的 batch 更新为 `interrupted`，设置 `finished_at` 和 `error_message`。复用 `utc_now()` 和 `_lock`。

## Step 2 — `src/api/` 应用骨架

依赖：`uv add fastapi uvicorn python-multipart`；dev 组加 `httpx`（主依赖里已经有 httpx，TestClient 可以直接用）。

```
src/api/
  app.py            create_app()：lifespan、挂载 router、异常映射
  settings.py       ApiSettings(pydantic-settings)：db_path、upload_dir、max_concurrent_batches、default_concurrency
  runner.py         BatchRunner：后台执行 + 并发上限 + 状态登记
  deps.py           get_settings / get_runner / get_db（每个请求一个只读 OrchestratorDB，结束时关闭）
  routes/batches.py 所有操作类路由
  schemas/batches.py Pydantic 请求和响应模型
  __main__.py       uvicorn 入口：python -m src.api --host --port
```

**settings**（环境变量，`.env.sample` 里补上）：
- `ORCHESTRATOR_DB_PATH`（复用现有变量）
- `API_UPLOAD_DIR`，默认 `uploads/`，需要加进 `.gitignore`
- `API_MAX_CONCURRENT_BATCHES`，默认 1
- `API_DEFAULT_CONCURRENCY`，默认 8

**runner.py — BatchRunner**
- 持有 `asyncio.Semaphore(max_concurrent_batches)` 和 `dict[batch_id, asyncio.Task]`。
- `submit(prepared)`：`create_task(self._run(prepared))`。`_run` 先获取 semaphore，获取期间把状态登记为 `queued`，之后 `await execute_batch(prepared)`，结束时从 dict 里移除。异常只记日志（batch 状态已经由 `execute_batch` 写进 DB）。
- `phase(batch_id) -> "queued" | "running" | None`：给 GET 接口补充 DB 里看不出来的"排队中"状态。
- `shutdown()`：cancel 所有 task 并 gather。`execute_batch` 捕获 `CancelledError` 后会把 batch 标记为 `interrupted`。

**app.py lifespan**
- 启动：`OrchestratorDB(db_path).mark_running_batches_interrupted("server restarted")`，清理上次崩溃遗留的 running batch；创建上传目录。
- 关闭：`runner.shutdown()`。
- 异常映射：`KeyError` → 404，`ValueError` → 400（父 batch 在运行时由路由层返回 409）。

## Step 3 — 路由（`routes/batches.py`）

| Method | Path | 行为 |
|---|---|---|
| POST | `/batches/new-input` | multipart：`file`（xlsx/csv/json）+ `vision_enabled` + `concurrency`。文件存到 `upload_dir/<uuid>_<原文件名>`，保留后缀。 |
| POST | `/batches/new-input/items` | JSON body：`{items: list[InputItem], vision_enabled, concurrency}`，复用 `src.models` 里的 `InputItem`。 |
| POST | `/batches/{batch_id}/rerun` | JSON：`{search_titles?, vision_enabled?, concurrency?}` |
| GET | `/batches` | 分页（`limit`、`offset`），可按 `operation`、`status`、`root_batch_id` 过滤，读 `batch_summary` view |
| GET | `/batches/{batch_id}` | `batch_summary` 的一行 + `pending/running` 计数 + runner 的 `phase` |
| GET | `/batches/{batch_id}/items` | 分页，可按 `status` 过滤，读 `item_outcomes` view，`product_data`、`stage_trace`、`failure_detail` 反序列化成 JSON |

POST 的共同流程：`await asyncio.to_thread(prepare_*)`（pandas 读 xlsx 和 SQLite 写入都是同步操作，不能阻塞事件循环）→ `runner.submit(prepared)` → 返回 `202 {batch_id, status_url}`。

读查询放在 `src/api/queries.py`，写成纯函数，参数是 `sqlite3.Connection` 加过滤条件，返回 dict。这些函数以后分析模块也能复用。只查 `batch_summary` / `item_outcomes` 这两个现有 view，不新建表，所以**不改 DDL，也不需要重新生成 storage 文档**。

## Step 4 — 文档（按 CLAUDE.md 文档规范，必须做）

- 新建 `src/api/README.md`：启动命令、环境变量、接口列表加 curl 示例、重启后 running batch 会被标记为 interrupted 的说明、"不要让 CLI 和 API 同时写同一个 DB"的说明（启动时的清理会误伤 CLI 正在跑的 batch）。
- 重写 `src/api/CLAUDE.md`：状态改为 Implemented，写清楚 prepare/execute 的拆分、runner 模型和查询层（AGENTS.md 由 hook 自动同步）。
- 根目录 `CLAUDE.md` 的模块表把 api 改为 Implemented，Setup & Run 里加 `uv run python -m src.api`；根 `README.md` 加入口说明。
- `src/orchestrator/CLAUDE.md` 补充新的公开函数 `prepare_*` / `execute_batch`。
- 更新 `.env.sample` 和 `.gitignore`（`uploads/`）。

## Verification

1. **回归**：`uv run pytest tests/unit/orchestrator` 全部通过，证明拆分没有改变 CLI 和工作流的行为。
2. **新增 orchestrator 单测**（`tests/unit/orchestrator/test_workflows.py`）：
   - prepare 之后 batch 为 running，items 为 pending；execute 之后进入终态；
   - 父 batch 在 running 时 `prepare_rerun` 被拒绝；
   - `mark_running_batches_interrupted` 的效果。
3. **API 单测**（`tests/unit/api/test_batches.py`）：用 `TestClient` 加 `tmp_path` 里的 DB，按现有测试的做法 monkeypatch `src.orchestrator.workflow.match_products / scrape / verify_products`，覆盖：
   - 上传 csv → 202 → 轮询 GET 直到 completed → items 里有 product_data；
   - 结构错误的文件 → 400 且没有创建 batch；
   - rerun 不存在的 batch → 404；父 batch 在运行 → 409；
   - 预置一个 running batch，启动 app 后变成 interrupted；
   - `max_concurrent_batches=1` 时第二个 batch 的 phase 为 queued。
4. **端到端手动验证**：`uv run python -m src.api`，用 curl 上传一个小的 csv（2–3 行 tesco/argos 商品）、轮询 `/batches/{id}`，完成后调用 `/rerun`，确认 `root_batch_id` / `rerun_no` 的链路正确；打开 `/docs` 检查 OpenAPI 页面。

## 不在本次范围内

鉴权、分析接口、跨 batch 的 logical 商品身份统一、队列系统（Celery/arq）、SSE 进度推送。
