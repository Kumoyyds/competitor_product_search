# 修复 scraping 模块的进程/资源泄漏（BlockingIOError: [Errno 35]）

## Context

`await scrape(url)` 在 notebook 里抛出 `BlockingIOError: [Errno 35] Resource temporarily unavailable`，
栈为 `router.scrape` → `HTMLScraper.scrape` → `_run_parsers` → `run_in_sandbox` →
`asyncio.create_subprocess_exec`。同样的错误在 `src/scraping/tests/verify_m12_qwen_output.log`
里出现 7 次，全部记为 `[WARN] UNHANDLED (BlockingIOError)`。

**已定位的抛出点**（读 anaconda CPython 3.12 源码确认）：`subprocess.py:1886` 的
`self.pid = _fork_exec(...)`。用户 traceback 里显示的 1895 行是紧随其后的
`self._child_created = True`，不是真正的失败行。errno 35 = `EAGAIN`，由 `fork()` 返回，
含义是**进程数达上限**——不是 fd 耗尽（那会是 errno 24 `EMFILE`，`os.pipe()` 抛出）。

本机实测：`RLIMIT_NPROC = (2666, 4000)`、`kern.maxprocperuid = 2666`、
`kern.maxproc = 4000`、`RLIMIT_NOFILE = 1048576`（fd 不可能是瓶颈）。

调查结论：确实存在「用完不关资源」的问题。整个 `src/scraping` 里唯一 spawn 进程的地方是
`repair/sandbox.py:136`，而它**在任务被取消时会留下永久挂死、永不回收的孤儿子进程**——
这是能把进程数推到 2666 的累积机制。除此之外还有 4 类不及时释放的资源。

事后无法证明当时具体累积到了多少个孤儿（进程已随宿主退出而消失），所以本方案除了修复，
还在 spawn 失败路径加入现场取证，让下次复现可以一次定性。

## 审核结论（实施时修订）

痛点成立：C1 提供了可累积到 `RLIMIT_NPROC` 的完整泄漏链，C2 解释了为何日志表现为
`UNHANDLED` 而非 scraper fallback；C3–C6 则是会放大或掩盖问题的独立资源风险。
施工范围合理，实施时补强了四点：

1. 取消路径不是只同步 `kill()` 后立即从 `_LIVE` 移除，而是用受保护的清理任务执行
   `stdin.close()` → `kill()` → `await proc.wait()`；只有 `waitpid` 完成才注销 PID，避免“已发
   kill 信号”被误判成“已经回收”。
2. LLM client 缓存键除模型参数外还包含 provider、endpoint、key、ChatOpenAI 类和 event loop，
   避免 `set_config()`、密钥轮换、测试替身及跨 loop 复用异步连接池造成陈旧连接。
3. 新增配置值校验：进程/网络并发与 timeout 必须为正数，重试次数和间隔不得为负，避免
   `Semaphore(0)` 形成永久等待。
4. Router 验证拆成两个语义：备份成功时只验证确实 fallback（不应写 aggregate escalation）；
   所有 scraper 都失败时才验证 `escalations` 写入。

## 缺陷清单

### C1（主因）`run_in_sandbox` 不是取消安全的 — `repair/sandbox.py:145-156`

只 catch 了 `asyncio.TimeoutError`，**没有 `finally`、没有 `CancelledError` 分支**。
asyncio 的 `Process.communicate()` 是 `gather(_feed_stdin, _read_stream(1), _read_stream(2))`，
而 `_feed_stdin` 的 `self.stdin.close()` 在函数最后一行
（`/opt/anaconda3/lib/python3.12/asyncio/subprocess.py:69-87`）。任务被取消时——
notebook 中断 cell、`verify_m12.py:794` 的 KeyboardInterrupt、`as_completed` 拆除——
stdin 写端永不关闭，子进程就**永久阻塞在 `sandbox.py:207` 的 `sys.stdin.read()`**：
不退出、不被 waitpid 回收、`RLIMIT_CPU=5s` 也杀不掉它（阻塞不耗 CPU）。
每个孤儿占 1 个进程槽 + 6 个 fd + 1 个 `ThreadedChildWatcher` 线程，
在长命的 Jupyter kernel 里逐 cell 累积，直到 `fork()` 返回 EAGAIN。

超时分支本身是对的（`kill()` + `await wait()`），问题只在取消/异常路径。

### C2 `OSError` 穿透整条链路 — `scrapers/html_scraper.py:105, 128`

`_run_parsers` 和 `run_repair_ladder` 没有 try 包裹；`router.py:66` 只 catch
`BrightDataInfraError` 和 `ScrapeFailed`。于是 `BlockingIOError` 直接冒出 `scrape()`：
不降级到备份 scraper、不写 `escalations`、不写 `scrape_runs`。日志里 7 条 `UNHANDLED`
就是这个——那些 URL 连 TescoDCA 备份都没试过。

### C3 spawn 放大 — `repair/golden.py:146, 315`

`promote_candidate` 对每个非 stale golden spawn 一次；失败时再对每个 active parser
spawn 一次做 stale 对照。按当前 DB 实测（tesco 10 个 golden / argos 6 个，各 1 个 active parser），
单 URL 峰值约 13–25 次冷启动解释器；填满 `per_site_parser_limit=4` 与 golden 上限后可到 60+。
子进程启动实测 ~0.17s，不是延迟瓶颈，但它是 fork 压力的量级来源。
本轮不改架构（按选定范围），改为加全局并发闸门限制**同时存活**的子进程数。

### C4 `db.close()` 不在 `finally`

`router.py:157`、`html_scraper.py:178 / 302 / 316`、`repair/agent.py:409`。
WAL 模式每连接 3 个 fd（db/-wal/-shm），`ScrapeDB` 没有 `__del__`，且这些位置都被宽
`except Exception` 包住，泄漏完全无声。
`scrapers/base.py:53-77` 和 `:100-110` 已有正确写法（`db: ScrapeDB | None = None` + `finally`），照抄即可。

### C5 LLM client 从不复用也从不关闭 — `providers.py:172`

每次调用返回全新 `ChatOpenAI`，内含 `openai.OpenAI` + `openai.AsyncOpenAI` 两个 httpx
连接池。调用点：`repair/agent.py:216`（判断）、`:239`（生成）——**每次 ladder attempt 建 2 个**，
默认 2 节 ladder 即单 URL 4 个；另有 `repair/json_healer.py:133`、`coldstart.py:519 / 556`。
全模块没有任何 `close` / `aclose` / `atexit` / `__del__`。

### C6 coldstart 无上限并发 — `coldstart.py:436`

`await asyncio.gather(*[_one(u) for u in urls])`，对整张 cold-start 表一次性铺开。
纯网络（不 spawn 进程），但同样无节制。

## 实施方案

### 1. `src/scraping/repair/sandbox.py` — 取消安全 + EAGAIN 容错 + 并发闸门

- 新增 `SandboxSpawnError(OSError)`（放 `exceptions.py`，与 `BrightDataInfraError` 并列），
  表示「起不来子进程」这一类基础设施故障，与 parser 本身的失败区分开。
- 新增 `_spawn(...)`：包住 `asyncio.create_subprocess_exec`，捕获 `OSError`；
  当 `e.errno in (errno.EAGAIN, errno.ENOMEM)` 时按 `sandbox_spawn_retry_interval`
  退避重试 `sandbox_spawn_retries` 次，仍失败则抛 `SandboxSpawnError`。
  失败日志带上**现场取证**：当前存活子进程数、`len(asyncio.all_tasks())`、
  `threading.active_count()`、`RLIMIT_NPROC` soft 值。这是复现时一次定性的依据。
- 并发闸门：模块级 `WeakKeyDictionary[loop, asyncio.Semaphore]`（按 event loop 建，
  Jupyter 重启 kernel / 多个 `asyncio.run()` 都不会串），容量 `sandbox_max_concurrency`。
  `run_in_sandbox` 全程持有。
- 存活登记：模块级 `_LIVE: set[asyncio.subprocess.Process]`，配套只读 `active_child_pids()`
  供测试与诊断使用；`atexit` 钩子在解释器退出时 kill 残留。
- 核心改写：

```python
async with _gate():
    proc = await _spawn(...)
    _LIVE.add(proc)
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=payload.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        return SandboxTimeout(timeout=timeout)
    finally:
        cleanup = asyncio.create_task(_terminate_and_wait(proc))
        await asyncio.shield(cleanup)
```

  `_terminate_and_wait(proc)` 先关闭 stdin，再 kill 仍存活的进程，并 `await proc.wait()`；清理任务
  由 `shield` 保护并持有强引用。只有 waitpid 完成后才从 `_LIVE` 注销，超时分支的
  `SandboxTimeout` 返回值保持不变，回收交给同一个 `finally`。

### 2. `src/scraping/scrapers/html_scraper.py` — 让 OSError 变成可降级失败

- `scrape()` 里给 `_run_parsers`（:105）和 `run_repair_ladder`（:128）各加一层
  `except (SandboxSpawnError, OSError)`，转成
  `ScrapeFailed(failed_stage="parser_list" / "repair", signature=(self.site, "sandbox_spawn", ""))`，
  调 `self._record_failure(...)` 后 raise。效果：router 继续试 TescoDCA 备份、
  写 `scrape_runs` 的 escalated 行、写 `escalations`——正是那 7 条 UNHANDLED 缺的东西。
- `_run_parsers`（:175-178）、`_check_mass_invalid_target`（:272-302）、`_load_phrases`（:313-316）
  三处改用 `base.py:53-77` 的 `db: ScrapeDB | None = None` + `finally: db.close()` 写法。

### 3. `src/scraping/router.py` / `src/scraping/repair/agent.py` — db 进 finally

`router.py:150-160` 的 `_write_escalation`、`agent.py:401-412` 的 `_backfill_phrase`，同样改写。

### 4. `src/scraping/providers.py` — client 复用 + 退出时关闭

- `make_chat_client` 前加缓存：key 覆盖 model/temperature/thinking/output cap/provider/endpoint/key/
  event loop/ChatOpenAI class，`purpose` 无关；用模块级 dict（不用 `lru_cache`，因为要能显式
  清理）。`None` 结果不缓存。
- 导出 `reset_chat_clients()`（测试用）与 `close_chat_clients()`，后者遍历缓存对
  `root_client` / `root_async_client` 尽力 `close()`，注册进 `atexit`。
- `ChatOpenAI` 可并发复用，语义不变。**需回归 `verify_m18.py`**：它多次调用
  `make_chat_client` 并断言构造参数，参数不同则 key 不同、行为不变，但要跑一遍确认。

### 5. `src/scraping/coldstart.py` — `_batch_fetch` 限流

`:424-437` 加 `sem = asyncio.Semaphore(min(cfg.per_site_concurrency, len(urls)))`，
`_one` 内 `async with sem`。复用已有的 `per_site_concurrency`（默认 16），不新增配置项。

### 6. `src/scraping/config.py` — 3 个新键

```python
# --- sandbox ---
sandbox_timeout: int = 10
sandbox_max_concurrency: int = 8          # 同时存活的沙箱子进程上限
sandbox_spawn_retries: int = 2            # fork EAGAIN/ENOMEM 时的重试次数
sandbox_spawn_retry_interval: float = 1.0 # 重试退避秒数
```

> 注：本项目 `CLAUDE.md` 的 Documentation Discipline 把「config key 变更」列为 README 必更触发条件，
> 且该指令标注为 OVERRIDE。虽然你没勾选文档项，这三个键会触发该硬性规则，
> 所以我会在 `src/scraping/README.md` 的配置表加 3 行、`CLAUDE.md`/`AGENTS.md` 的 Key Config 加 1 行，
> 仅此而已，不写变更日志。

### 7. `src/scraping/tests/verify_m26.py` — 离线验证

按 CLAUDE.md 的 Verification Discipline：命名检查 + `[PASS]`/`[FAIL]` + 结尾
`SUMMARY: N passed, M failed` + 非零退出码。覆盖：

1. **取消安全**：`asyncio.create_task(run_in_sandbox("def parse(h,u):\n while True: pass", ...))`，
   0.3s 后 `task.cancel()`；断言 `active_child_pids()` 为空，且原 pid 已不存在（`os.kill(pid, 0)` → `ProcessLookupError`）。
2. **超时路径不回归**：仍返回 `SandboxTimeout`，且子进程已回收。
3. **EAGAIN 重试**：monkeypatch `asyncio.create_subprocess_exec` 前 N 次抛
   `BlockingIOError(errno.EAGAIN, ...)` 再放行 → 成功；抛满 N+1 次 → `SandboxSpawnError`。
4. **并发闸门**：并发发起 20 个 sandbox 调用，采样 `len(active_child_pids())` 不超过配置上限。
5. **OSError 降级**：patch `html_scraper.run_in_sandbox` 抛 `OSError`，断言得到
   `ScrapeFailed(failed_stage="parser_list")` 且 `scrape_runs` 写入 escalated 行。
6. **router 降级**：第二个 scraper 成功时验证 fallback 且不写 aggregate escalation；所有
   scraper 都失败时验证 `escalations` 写入。
7. **异常路径关连接**：patch `ParserStore.get_active_ordered_by_hits` 抛异常，
   断言 `ScrapeDB.close` 被调用（spy）。
8. **client 复用**：同参两次 `make_chat_client` 返回同一对象，异参返回不同对象。
9. **coldstart 限流**：instrument `_one` 的并发计数，断言不超过 `per_site_concurrency`。

产出 `verify_m26_output.log`（`| tee`），并在 `src/scraping/tests/README.md` 表格加一行。

## 关键文件

| 文件 | 改动 |
|---|---|
| `src/scraping/repair/sandbox.py` | 取消安全 `finally` + terminate/wait + EAGAIN 重试 + 并发闸门 + `active_child_pids()` + atexit |
| `src/scraping/exceptions.py` | 新增 `SandboxSpawnError` |
| `src/scraping/scrapers/html_scraper.py` | OSError→ScrapeFailed（:105/:128）；3 处 db 进 finally |
| `src/scraping/router.py` | `_write_escalation` db 进 finally |
| `src/scraping/repair/agent.py` | `_backfill_phrase` db 进 finally |
| `src/scraping/providers.py` | client 缓存 + `close_chat_clients()` + atexit |
| `src/scraping/coldstart.py` | `_batch_fetch` 加 Semaphore |
| `src/scraping/config.py` | 3 个 sandbox 键 |
| `src/scraping/tests/verify_m26.py` + `_output.log` + `tests/README.md` | 新增验证 |
| `src/scraping/README.md`、`CLAUDE.md`/`AGENTS.md` | 配置表 3 行 / Key Config 1 行 |

## 验证

```bash
# 新增验证（离线）
python -m src.scraping.tests.verify_m26 | tee src/scraping/tests/verify_m26_output.log

# 回归：直接受影响的既有里程碑
python -m src.scraping.tests.verify_m7   # sandbox 契约（AST/超时/隔离）
python -m src.scraping.tests.verify_m18  # providers client 构造参数
python -m src.scraping.tests.verify_m19  # coldstart
python -m src.scraping.tests.verify_m21  # coldstart 修复循环
python -m src.scraping.tests.verify_m24  # scrape_runs 观测
python -m src.scraping.tests.verify_m25  # source-absence 预筛
```

**孤儿泄漏的端到端确认**（这是本次问题的直接复现）：

```bash
ps -u $(id -u) | wc -l          # 记录基线
```

然后在 notebook 里跑 `await scrape(url_1)`，中途按停止键中断 cell，重复 5 次；
再次 `ps -u $(id -u) -o command | grep -c src.scraping.repair.sandbox`
—— 修复前会残留挂死子进程，修复后应为 0。同时 `ps -u $(id -u) | wc -l` 应回到基线。

**live 复核**：`python -m src.scraping.tests.verify_m12`（真实 BrightData + LLM，
需 `.env` key）跑完后检查日志中不再出现 `UNHANDLED (BlockingIOError)`；
即便再遇到 fork EAGAIN，也应表现为重试后成功，或降级为 `ESCALATED` 并带
`sandbox_spawn` signature，而不是穿透。
