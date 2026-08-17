# 测试套件精简与统一

## Context

仓库里存在两个互不相干的测试世界，且都有明显冗余：

| | `src/scraping/tests/` | `tests/unit/search/` |
|---|---|---|
| 风格 | 手写 `verify_mN.py` 脚本 | pytest |
| 规模 | 23 文件 / **8,116 行** / ~588 个 `check()` | 10 文件 / **1,748 行** / 99 个 `test_*` |
| 运行方式 | `python -m src.scraping.tests.verify_mN` | `python -m pytest` |
| 交叉覆盖 | 0 个 pytest 测试 | 0 处 scraping 覆盖 |

具体问题（均已核实）：

1. **纯复制粘贴的脚手架约 1,600–1,800 行**（占 scraping 套件 20–22%）。`check()` 在 22 个文件里各写一遍，只有 4 个不同版本，其中 14 个逐字节相同；`section()` / `PASSED` / `FAILED` / `main()` 汇总块同样重复 22–23 次。
2. **没有任何 pytest 配置**。`pyproject.toml` 无 `[tool.pytest.ini_options]`，无 `pytest.ini`/`setup.cfg`/`tox.ini`，无 marker，无 `pytest-asyncio`；全仓库只有一个 8 行的 conftest。异步一律靠在同步函数里套 `asyncio.run(...)`。
3. **Mock 手法各行其是**。BrightData 有 5 种打桩方式，LLM 有 6 种，临时 SQLite 有 5 种互不兼容的写法；`FakeChatOpenAI` 有 3 个近乎相同的类，`FakeAsyncClient` 有 2 个，临时 scraper 子类有 13 个。
4. **已经烂掉的脚本**：[verify_m6.py:142](src/scraping/tests/verify_m6.py#L142) 与 [verify_m8.py:169](src/scraping/tests/verify_m8.py#L169) 读取 `argos_response_1.html`，该文件不存在（真实 fixture 在 `data/html_sample/` 下且名字不同）——两者今天就是坏的。`verify_m15.py` 已丢失但 `verify_m15_output.log` 还在。[verify_m23.py:26](src/scraping/tests/verify_m23.py#L26) 断言开发机上真实的 15 MB `scraping.db`，全新 checkout 无法通过。
5. **污染与泄漏**：`verify_m13`/`verify_m27` 把 `.db` 写进 CWD；`verify_m17`/`m19`/`m21` 用模块级 `mkdtemp` 且从不清理；`verify_m18` 直接改 `os.environ` 不还原；search 侧 conftest 只挡了 trace DB，没挡 `get_cache`，导致 3 个测试实际读写开发者真实的 `.cache/base_extraction.sqlite`。
6. **目录残留**：`src/search/tests/` 空目录、`tests/integration/` 只有 `.gitkeep`、2 个 Excel 锁文件、`src/scraping/scripts/` 下 2.2 MB 无人引用的 HTML。

**目标**：统一到 pytest 一套约定，去掉重复脚手架，让默认 `pytest` 跑一次即离线、免费、可在干净 checkout 上通过。采用**分阶段**推进——本次先做基建与去重（脚本行为不变），迁移作为后续独立提交按主题逐个进行。

**已确认的三项决策**：
- 分阶段：先抽公共 harness，再迁移。
- 退休 `verify_mN.py` + 提交 `.log` 的强制流程，旧 log 归档保留审计痕迹。
- 花钱的测试打 `@pytest.mark.live`，默认跳过。

---

## Phase 1 — 基建、去重、清理（本次改动）

### 1. pytest 配置

`pyproject.toml` 新增：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-m 'not live'"
asyncio_mode = "auto"
markers = [
    "live: 调用真实 BrightData / LLM API，产生费用；默认跳过，用 -m live 显式运行",
    "slow: 会真实拉起 sandbox 子进程或多秒级 I/O",
]
```

`requirements.txt` 增加 `pytest-asyncio`。`asyncio_mode = "auto"` 只影响 `async def test_`，现有同步 + `asyncio.run` 的测试不受影响，是纯增量。

### 2. 共享支撑包 `tests/_support/`（迁移后仍然保留）

下划线开头，pytest 不收集，但可被 pytest 测试和遗留 verify 脚本同时 import（两者都从仓库根运行，`tests` 包已有 `__init__.py`）。

| 文件 | 内容 | 取代 |
|---|---|---|
| `llm.py` | `fake_llm(content)` 工厂、`FakeChatClient`（记录 `calls`）、`ExplodingLLM` | 8 个各自定义的假 LLM 类；search 侧 5 份 `type("Msg", (), {...})()` 桩 |
| `providers.py` | 单个 `FakeSearchProvider(results=None, error=None, name=...)`，带调用计数和 `aclose` | `test_pipeline_shortcircuit.py:11`、`test_db_trace.py:32`、`test_batch_recording.py:16` 三个签名不同的 `FakeProvider` |
| `http.py` | `FakeAsyncClient`（可编排响应队列）、`FakeClock` | `verify_m13.py:59`、`verify_m27.py:60` |
| `db.py` | `temp_scrape_db()` / `temp_search_db()` 上下文管理器、`fetchall(path, sql)` | 5 种临时 SQLite 写法；`test_batch_recording.py:34` 的 `_fetchall` 与 `test_db_trace.py:293` 的内联版本 |
| `factories.py` | `product_data(**overrides)`、`raw_candidate(**overrides)`、`candidate(**overrides)`、`sku_workbook(tmp_path, rows)` | 6 个文件里的 `ProductData(...)` 构造；`_cand`/`_make` 两个同义 helper；`test_batch_recording.py` 里 5 份 `pd.DataFrame(...).to_excel(...)` |
| `scrapers.py` | 一个可配置的 `FakeScraper(result=..., raises=...)` | 13 个临时 scraper 子类 + 2 个 `FakeScraper` |

### 3. 抽取 scraping 遗留 harness

新建 `src/scraping/tests/_harness.py`（约 80 行，Phase 2 结束时删除）：`check()`、`section()`、`skip()`、`run_main(*sections)`（统一 try/except + SUMMARY + 退出码）、以及统一的 `SCRAPING_DB_PATH` 临时库前置逻辑。

23 个脚本各删掉自己的副本，改为 `from ._harness import check, section, run_main`。这一步机械且可逐文件验证：**改完后每个脚本的 SUMMARY 计数必须与归档 log 完全一致**。

顺带修掉的行为问题：`skip()` 目前只有 `verify_m14` 有，统一后所有脚本都能表达「跳过」；`mkdtemp` 泄漏和 CWD 写库改由 harness 统一用上下文管理器处理。

### 4. 清理

- **修坏引用**：`verify_m6.py` / `verify_m8.py` 的 `DATA_DIR` 指向 `data/html_sample/`，`argos_response_1.html` 改用现存的 `argos_game_normal.html`；若断言依赖那个特定页面则删掉该 check 并在 README 注明。
- **`verify_m12.py`（829 行）移出测试目录** → `src/scraping/scripts/live_batch_report.py`。它是带 `TeeWriter`、`PerURLReport`、并发限流和报告渲染的实时批量抓取工具，不是测试。
- **`verify_m23.py` 解耦真实 DB**：加 skip-if-absent 守卫（`scraping.db` 不存在就整体跳过），Phase 2 换成committed 的小型种子库。
- **归档 log**：`src/scraping/tests/*.log` → `src/scraping/tests/logs/archive/`，README 说明这是历史审计记录、不再新增。`verify_m15_output.log` 单独注明脚本已佚失。
- **删除**：`src/search/tests/`（空）、`tests/integration/`（只有 `.gitkeep`）、`~$tesco_argos.xlsx`、`~$argos.xlsx`、`src/scraping/scripts/{argos_response_844,response,response_error}.html`（2.2 MB，无引用）、各处 `.DS_Store`。
- **`.gitignore`** 补 `.pytest_cache/`、`.coverage`、`htmlcov/`、`*.egg-info/`（`.pytest_cache/` 目前是被跟踪状态）。

### 5. search 套件去重与修 bug

- `tests/conftest.py`（新建，仓库根级）：跨模块通用守卫。`tests/unit/search/conftest.py` 保留 search 专有部分。
- **修真实 bug**：现有 conftest 只 patch 了 `pipeline.get_db` / `batch.get_db`，没挡 [base_match.py:56](src/search/layers/base_match.py#L56) 的 `get_cache()`，它默认落到 CWD 的 `.cache/base_extraction.sqlite`。`test_pipeline_shortcircuit.py` 中 3 个测试因此读写开发者真实缓存，结果与执行顺序和缓存状态相关。在 conftest 里加 autouse 的 `get_cache` 中和。
- 三个 `FakeProvider`、5 份 LLM 桩、4 份「LLM 不得被调用」守卫（`test_pipeline_shortcircuit.py:35,51,72,89`）改用 `tests/_support/`。
- `test_batch_recording.py` 里 7 处 `async def run(): with patch(...): ...` + `asyncio.run(run())` 三明治，配合 `asyncio_mode="auto"` 改写成 `async def test_`。
- 统一到 `monkeypatch`：目前同一件事（禁用 `get_db`）在 conftest 用 `monkeypatch`、在 `test_batch_recording.py` 用 `unittest.mock.patch`。
- **归位错放的测试**：`test_url_rules.py:86`（search_node 去重）→ search node 测试；`:104`（distinguishing prompt 文本）→ distinguishing 测试；`test_numeric.py:283`（`BaseExtractionCache` 分区）→ 新建 `test_cache.py`。

### 6. 文档（CLAUDE.md 文档纪律强制要求）

- **`src/scraping/CLAUDE.md` §Verification Discipline 重写**：从「必须新增 `verify_mN.py` + tee 一个 `.log` + 更新表格」改为「按主题新增 pytest 测试到 `tests/unit/scraping/`，花钱的打 `@pytest.mark.live`」。`AGENTS.md` 由 pre-commit hook 自动同步——**不要手改**。
- **`src/scraping/tests/README.md` 重写**：现有 check 计数与源码对不上（m23 写 90 实际 18、m7 写 21 实际 12、m22 写 21 实际 16），不要再维护这张数字表；改为说明 marker、运行方式、以及归档 log 的位置。顺带修掉 `README.md:319` 的 `src/scraping.tests/` 路径笔误。
- **根 `README.md:55-56`** 与 **`src/search/README.md:219-224, 284`**：命令从 `python -m pytest tests/unit/search/ -v` 改为 `python -m pytest`（默认离线免费），并补一行 `python -m pytest -m live`（需要 API key，产生费用）。

---

## Phase 2 — 迁移到 pytest（后续独立提交，按主题逐个进行）

按**主题**而非里程碑重组到 `tests/unit/scraping/`。每次提交迁移一个主题，旧 `verify_mN.py` 在其所有 check 都有 pytest 对应物后才删除：

| 新文件 | 来源 |
|---|---|
| `test_gates.py` | m1_m3、m20、m24 |
| `test_router_registry.py` | m1_m3、m25 |
| `test_scrapers_api.py` | m4_m5、m24 |
| `test_scrapers_html.py` | m4_m5、m14 |
| `test_sandbox.py` | m7、m26 |
| `test_repair_ladder.py` | m6、m8(live)、m21、m22、m25 |
| `test_golden.py` | m9、m17、m19、m23 |
| `test_escalation.py` | m10 |
| `test_coldstart.py` | m11(live)、m17、m19、m21 |
| `test_brightdata.py` | m13、m27 |
| `test_providers.py` | m18 |
| `test_storage.py` | m24、verify_clear_db |
| `test_cancellation.py` | m26 |

自由文本 check 名（`check("Argos alphanumeric product ID is extracted", ...)`）转成可寻址的 `test_*` 函数名 + `@pytest.mark.parametrize`。全部迁完后删除 `src/scraping/tests/_harness.py` 与整个 `src/scraping/tests/` 脚本目录（`logs/archive/` 迁至 `docs/` 或保留）。

**不要动的东西**：`src/scraping/repair/golden.py` 的 "golden test" 是**生产功能**（每次修复时在 sandbox 里跑候选 parser 对比 `golden_samples` 表），与开发者测试无关；`golden_samples` 里的 HTML 快照不是 test fixture，不要并入 `tests/fixtures/`。而 `src/scraping/data/html_sample/*.html`（13.3 MB）**是**开发者 fixture，保留原位。

**规模预期**：测试目录当前 9,784 行 → Phase 1 后约 7,100 行（去重 ~1,800 + 移出 m12 的 829）→ Phase 2 后约 3,500–4,000 行，且全部在一个 runner 下。

---

## Verification

Phase 1 每一步都必须可证明「行为未变」：

1. **基线**：改动前对每个脚本跑一遍并存下 SUMMARY 计数（离线的 20 个即可）：
   ```bash
   for f in src/scraping/tests/verify_m*.py; do
     m=$(basename $f .py)
     python -m src.scraping.tests.$m 2>&1 | tail -3
   done
   ```
2. **harness 抽取后**：重跑同一循环，逐脚本比对 `SUMMARY: N passed, M failed` 与基线**完全一致**。这是 Phase 1 步骤 3 的唯一验收标准。
3. **search 套件**：`python -m pytest tests/ -v` —— 99 个测试全绿，且数量不减。
4. **缓存泄漏已修**：`rm -rf .cache/ && python -m pytest tests/ -v && test ! -d .cache` —— 跑完不应生成 `.cache/`，且 `git status` 干净。再单独跑受影响的 3 个测试确认不依赖执行顺序：`python -m pytest tests/unit/search/test_pipeline_shortcircuit.py -v`。
5. **默认运行免费**：在 `.env` 重命名（无 API key）的情况下 `python -m pytest` 仍全绿——证明 `-m 'not live'` 生效。
6. **干净 checkout 可跑**：`git stash -u && rm -f scraping.db` 后 `python -m pytest` 通过（m23 应 skip 而非 fail）。
7. **文档同步**：`git add -A && git commit` 时 pre-commit hook（`sync_agent_docs.py` + `check_encoding.py`）必须通过——确认 `src/scraping/AGENTS.md` 已被 hook 自动同步，且没有引入 BOM/mojibake（本次要改多个含 `—`、`→` 的 UTF-8 文档）。
