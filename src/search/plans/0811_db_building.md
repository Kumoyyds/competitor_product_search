# search_db —— 任务追踪与结果存储库

## Context

目前整条管道**不留任何过程痕迹**：跑完只往 `output/*.xlsx` 写 4 列，进程中途挂掉全部丢失，出了问题只能靠一个自由文本 `match_reason` 猜原因。

三个具体的痛点驱动这次改动：

1. **`no_match` 有 6 种完全不同的成因**，目前无法区分：无搜索结果 / `domain_map` 缺配置把候选全杀了 ([domain_filter.py:22-26](src/search/layers/domain_filter.py#L22-L26)) / 品牌不符 / 数值不符 / LLM 真判否 / LLM 崩了。最后这项尤其危险——[distinguishing.py:120-123](src/search/layers/distinguishing.py#L120-L123) 的裸 `except Exception` 把 API 超时、鉴权失败伪装成了普通的"没匹配上"。
2. **`search_errors` 已经被收集了却从没被人读过**（[search.py:53](src/search/layers/search.py#L53) 写入，下游零消费）。provider 报错、限流、预算耗尽的信息全部凭空蒸发。
3. **一个 task 会跑多遍图**：[pipeline.py:57](src/search/pipeline.py#L57) 的 provider 链循环每个 provider 跑一整遍完整 graph，只保留最后一次结果，前面的候选和错误全丢。目前唯一的 provider 归因是 `reason` 尾巴上拼的 `" (via serper)"` 字符串。

目标：建一个根目录下的 SQLite 库，做到 **(a) 能查任意任务在任意节点的报错，(b) 完整存储任务输出结果**，且流式写入——崩溃时已完成的部分全部保留。Excel 输出保持不变，DB 是并存的可查询层。

---

## 数据模型

先明确三层实体，这是整个 schema 的骨架：

```
run  (一次批处理 = 一次 main.py 执行)
 └── task  (一行 SKU)                      ← 目前只有 df 行号，没有稳定 ID
      └── attempt  (一个 provider = 跑一遍完整 graph)   ← 目前完全不可见
           ├── node_event  (一个节点的一次执行)         ← "任意节点报错" 落在这里
           ├── candidate   (一个搜到的 URL)
           └── llm_call    (一次 LLM 调用)
```

**关键点：`attempt` 这一层不能省。** 没有它，provider 链跑 2 遍图产生的两组候选和两组错误会混在一起无法区分。

### 表 1 `runs` —— 一次批处理

| 字段 | 类型 | 说明 |
|---|---|---|
| `run_id` | TEXT PK | uuid4 hex，`main.py` 开跑时铸造 |
| `started_at` / `finished_at` | TEXT | ISO8601 UTC |
| `status` | TEXT | `running` / `completed` / `failed` / `interrupted` |
| `input_file` / `input_sku_col` / `output_file` | TEXT | 来自 `config_search.yaml` |
| `country` / `website` | TEXT | 本次的 run 级常量 |
| `provider_chain` | TEXT | `"duckduckgo,serper"` |
| `llm_model` | TEXT | 来自 `search_config.yaml` 的 `llm.model` |
| `concurrency` / `serper_max_calls` | INTEGER | |
| `total_tasks` / `matched_count` / `no_match_count` / `error_count` | INTEGER | 收尾时回填 |
| `provider_calls` | TEXT | JSON `{"duckduckgo":40,"serper":12}`，来自 `p.calls_made()` |
| `job_config` / `pipeline_config` | TEXT | 两个 yaml 的 JSON 快照。**必需**——阈值调过之后，没有快照就无法解释历史 run 的结果 |
| `git_commit` | TEXT | `git rev-parse --short HEAD`，失败则 NULL |
| `error_message` | TEXT | `status=failed` 时填 |

### 表 2 `tasks` —— 一行 SKU，一条最终结果

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | INTEGER PK AUTOINCREMENT | |
| `run_id` | TEXT FK → runs | |
| `row_index` | INTEGER | DataFrame 行号，回写 Excel 用 |
| `product_name` | TEXT | |
| `product_key` | TEXT | `md5(lower(strip(name)))`，**跨 run join 用**——比较两次 run 同一 SKU 的结果变化 |
| `brand_input` / `website` / `country` | TEXT | |
| `status` | TEXT | `ok` / `error`（`error` = 抛异常到了 [main.py:37](src/search/main.py#L37)） |
| `verdict` | TEXT | `match` / `no_match` / `error` |
| **`failure_kind`** | TEXT | **本方案的核心列**，见下 |
| `matched_url` / `matched_title` | TEXT | |
| `reason` | TEXT | 原样保留 `MatchResult.reason` |
| `layer_trace` | TEXT | JSON，= `layer_trace.to_dict()` |
| `candidates_considered` | INTEGER | |
| `final_provider` | TEXT | **结构化的 provider 归因**，取代 `" (via X)"` 字符串拼接 |
| `attempt_count` | INTEGER | 跑了几个 provider |
| `error_type` / `error_message` / `traceback` | TEXT | 目前 [main.py:38](src/search/main.py#L38) 只留 `str(e)`，类型和栈全丢 |
| `started_at` / `finished_at` / `duration_ms` | | 找慢 SKU |
| | | `UNIQUE(run_id, row_index)` |

**`failure_kind` 取值**（在 `db.py` 里定义为常量，由聚合逻辑推导）：

```
matched              成功
no_search_results    搜索零结果
domain_map_missing   config 里没有该 website 的 domain_map 条目 —— 配置错误,不是数据问题
all_domain_filtered  搜到了但全不在目标站
brand_mismatch       品牌层全灭
numeric_mismatch     数值层全灭
llm_no_match         LLM 正常返回,判定无匹配   ← 真实的业务结论
llm_error            LLM 调用抛异常            ← 基础设施故障,必须和上面分开
llm_parse_error      LLM 返回了但 JSON 解析失败
budget_exhausted     Serper 预算用尽
provider_error       所有搜索 query 都失败
unknown_error        兜底
```

这一列就是为了消灭"6 种成因挤在一个 `no_match` 里"的问题。查 `llm_error` 的数量能立刻区分「算法效果差」和「API 在抽风」。

### 表 3 `attempts` —— 一次 provider 尝试 = 一遍完整 graph

| 字段 | 类型 | 说明 |
|---|---|---|
| `attempt_id` | INTEGER PK | |
| `task_id` | INTEGER FK → tasks ON DELETE CASCADE | |
| `run_id` | TEXT | 冗余，免 join |
| `attempt_no` | INTEGER | 1-based |
| `provider` | TEXT | `duckduckgo` / `serper` |
| `verdict` / `reason` | TEXT | 本次尝试的结果（区别于 task 的最终结果） |
| `candidates_found` | INTEGER | search 节点去重后的数量 |
| `alive_after_domain` / `alive_after_base` | INTEGER | 漏斗 |
| `budget_exhausted` | INTEGER | 0/1 |
| `query_variants` | TEXT | JSON list，实际发出的 query 文本 |
| `started_at` / `finished_at` / `duration_ms` | | |
| | | `UNIQUE(task_id, attempt_no)` |

### 表 4 `node_events` —— 「任意节点的报错」

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | INTEGER PK | |
| `attempt_id` | INTEGER FK → attempts ON DELETE CASCADE | |
| `task_id` / `run_id` | | 冗余，让"查全库所有报错"不用 join |
| `seq` | INTEGER | attempt 内的执行序号 |
| `node` | TEXT | `search` / `domain_filter` / `base_match` / `distinguishing` / `aggregate` |
| `status` | TEXT | `ok` / `error` / `warning` / `skipped` |
| `error_kind` | TEXT | `BudgetExhausted` / `SearchProviderError` / `RateLimit` / `LLMError` / `LLMParseError` / `DomainMapMissing` / `Unknown` |
| `error_message` | TEXT | |
| `traceback` | TEXT | 完整栈，只在 `status=error` 时填 |
| `detail` | TEXT | JSON。**放该节点的特有上下文**：search 放 `{"query":"...","provider":"serper","http_status":429}`；domain_filter 放 `{"expected_domain":"amazon."}` |
| `candidates_in` / `candidates_out` | INTEGER | 进/出的存活候选数，构成漏斗 |
| `started_at` / `duration_ms` | | 每层耗时分布 |

**一个节点可产生多行**：一条主行（`ok`/`error`/`skipped`）+ 每条 `search_errors` 一条 `warning` 行。所以 `seq` 不加唯一约束。`skipped` 用于被条件边短路掉的节点（[graph.py:46-54](src/search/graph.py#L46-L54)）——记下"因为候选全死所以没调 LLM"本身就是有价值的信息。

### 表 5 `candidates` —— 每个搜到的 URL

| 字段 | 类型 | 说明 |
|---|---|---|
| `candidate_id` | INTEGER PK | |
| `attempt_id` FK CASCADE / `task_id` / `run_id` | | |
| `rank` | INTEGER | search 去重后的 list 位置（0-based）。`CandidateEval` **没有 score 字段**，位置是唯一可用的排序代理 |
| `url` / `title` / `snippet` | TEXT | |
| `host` | TEXT | urlparse 出来的 host，便于"搜出来的都是些什么站"的聚合 |
| `brands` | TEXT | JSON list，`BaseAttributes.brands` |
| `numerics` | TEXT | JSON dict，`BaseAttributes.numerics` |
| `v_domain` / `v_brand` / `v_numeric` / `v_distinguishing` | TEXT | `pass`/`fail`/`unknown`/NULL，摊平的 `LayerTrace` 四个字段 |
| `alive` | INTEGER | 0/1 |
| `trace_depth` | INTEGER | `LayerTrace.depth()` 0-4 —— **死在第几层**，一列就能定位 |
| `is_matched` | INTEGER | 0/1 |
| `llm_index` | INTEGER | 送进 LLM prompt 时的编号，用于和 `llm_calls.prompt` 里的 `[i]` 对齐 |

摊平 `LayerTrace` 而不是存 JSON，是因为这四列是最主要的查询和聚合维度（"多少候选死在品牌层"），存 JSON 每次都要 `json_extract`。

### 表 6 `llm_calls`

| 字段 | 类型 | 说明 |
|---|---|---|
| `call_id` | INTEGER PK | |
| `attempt_id` FK CASCADE / `task_id` / `run_id` | | |
| `node` | TEXT | 默认 `distinguishing`，为未来的 LLM 节点留位 |
| `model` / `base_url` / `temperature` / `timeout_s` | | 来自 `resolve_llm_route()` |
| `prompt` | TEXT | 完整 prompt |
| `raw_response` | TEXT | 未解析的原始返回 |
| `parsed_match_idx` / `parsed_reason` | | 解析结果 |
| `status` | TEXT | `ok` / `error` / `parse_error` |
| `error_message` | TEXT | |
| `prompt_tokens` / `completion_tokens` / `total_tokens` | INTEGER | 成本核算；`response_metadata` 拿不到时留 NULL |
| `duration_ms` | INTEGER | |

### 表 7 `meta`

`(key TEXT PK, value TEXT)`，存 `schema_version`。将来加列时靠它判断是否要迁移。

### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_tasks_run      ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_verdict  ON tasks(run_id, verdict);
CREATE INDEX IF NOT EXISTS idx_tasks_failure  ON tasks(run_id, failure_kind);
CREATE INDEX IF NOT EXISTS idx_tasks_key      ON tasks(product_key);
CREATE INDEX IF NOT EXISTS idx_attempts_task  ON attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_events_attempt ON node_events(attempt_id);
CREATE INDEX IF NOT EXISTS idx_events_err     ON node_events(run_id, status, node);
CREATE INDEX IF NOT EXISTS idx_cand_attempt   ON candidates(attempt_id);
CREATE INDEX IF NOT EXISTS idx_cand_url       ON candidates(run_id, url);
CREATE INDEX IF NOT EXISTS idx_llm_attempt    ON llm_calls(attempt_id);
```

### 视图（把常用查询固化，免得每次手写 join）

- **`v_errors`** —— 一张表看全库所有报错：`node_events` 里 `status IN ('error','warning')` 的行，join 出 `row_index` / `product_name` / `provider`。**这是"查看每个任务任意节点报错"的直接答案**。
- **`v_task_result`** —— `tasks` join `runs`，给出可直接导出的结果宽表。
- **`v_funnel`** —— 按 run + node 聚合 `candidates_in` / `candidates_out`，看候选在哪一层掉得最多。
- **`v_run_summary`** —— 每个 run 一行：各 verdict 计数、`failure_kind` 分布、总耗时。

---

## 实现

### 新增文件

**`src/search/db.py`** —— `SearchDB` 类，**严格沿用 [cache.py](src/search/cache.py) 的既有约定**：stdlib `sqlite3`（仓库里没有 `aiosqlite`，不引入新依赖）、`threading.Lock` 守全部操作、`timeout=30.0`、`PRAGMA journal_mode=WAL`、`CREATE TABLE IF NOT EXISTS` 幂等建表于 `__init__`、JSON 存 TEXT 列、模块级懒加载单例 `get_db()`（对应 [cache.py:77-81](src/search/cache.py#L77-L81)）。

三处**刻意的偏离**，需要在代码注释里写明原因：

1. **显式关闭连接**。`cache.py:33` 的 `with self._connect() as conn` 只提交事务、**不关连接**（sqlite3 的上下文管理器是事务级而非连接级），泄漏的连接靠 GC 回收。缓存每次只做一两个操作还能忍，写入器每个 run 要做上万次操作，必须 `try/finally: conn.close()`。
2. **`PRAGMA foreign_keys = ON`**，配合 `ON DELETE CASCADE`，删一个 run 能连带清干净。
3. **每 task 一个事务**：`flush_task()` 在单个事务里写完 task + attempts + node_events + candidates + llm_calls，用 `executemany`。16 并发下写会串行化，但每个事务很短，WAL 模式下没有问题。

对外 API：`start_run()` / `flush_task()` / `finish_run()`。

**`src/search/trace.py`** —— `TaskRecorder`（纯内存累加器）+ `contextvars.ContextVar[TaskRecorder | None]`。

**这是整个设计的关键机制**：各节点只往 recorder 里 append，**从不碰 SQLite**。task 结束时一次性 flush。这样做的好处：不给热路径引入 IO、写库集中在一处、锁竞争最小。

用 `contextvars` 而不是给 `match_product()` 加参数，是因为 graph state 是每个 provider 尝试都重建的裸 dict（[pipeline.py:58](src/search/pipeline.py#L58)），把 recorder 塞进 state 会跟随 state 被丢弃。而 `asyncio.gather` 为每个协程创建 Task 时会复制 context，所以每行的 recorder 天然隔离。

### 改动文件

| 文件 | 改动 |
|---|---|
| [graph.py](src/search/graph.py) | **单点埋点**：用 `_instrument(name, fn)` 包装 5 个节点函数再 `add_node`。包装器负责计时、抓异常、数存活候选数、写 `node_event`。**5 个 layer 文件一个都不用改**——这是这个设计最值的一笔。条件边函数里补记 `skipped` 事件。 |
| [pipeline.py](src/search/pipeline.py) | provider 循环内 `recorder.begin_attempt(p.name)` / `end_attempt(result)`。给 `ainvoke` 包 try/except 记 task 级异常后重新抛出。`final_provider` 改为结构化记录（`" (via X)"` 字符串保留，不破坏 Excel 现状）。 |
| [distinguishing.py](src/search/layers/distinguishing.py) | 唯一需要改的 layer——只有它内部知道 prompt / 原始返回 / token 数。在 `_call_llm` 里记 `llm_call`；给现有 `except Exception` 补上结构化的 `error_kind`（区分 `LLMError` 和 `LLMParseError`），**保持现有的容错行为不变**。 |
| [main.py](src/search/main.py) | 铸造 `run_id`；插 `runs` 行（含 config 快照）；`_run_row` 里建 recorder + 设 contextvar + 结束时 `await asyncio.to_thread(db.flush_task, rec)`；`except` 分支补 `type(e).__name__` 和 `traceback.format_exc()`；结尾 `finish_run()` 回填汇总；`KeyboardInterrupt` 走 `status='interrupted'`。Excel 输出逻辑**完全不动**。 |
| [maintain/search_config.yaml](src/search/maintain/search_config.yaml) | 新增 `db:` 段：`sqlite_path: search_db.sqlite`、`enabled: true`、`store_candidates: true`、`store_llm_payload: true`。后两个开关让高频跑批时可以关掉体量最大的两部分。 |
| `.gitignore` | 加 `search_db.sqlite*`（WAL/SHM 一并） |

### `failure_kind` 的推导

`aggregate.py` 手上没有足够信息（比如它区分不了 domain_map 缺失和真实域名不符）。**放在 `trace.py` 里做**，在 task 结束时依据完整 recorder 推导：按 `node_events` 里的错误优先、再看候选的 `trace_depth` 分布。这是纯函数，好单测。

---

## 需要注意的两处现存问题

不属于本次范围，但会影响验收，实施时先确认：

1. **`config_search.yaml` 在 HEAD 已被删除**（commit `854e063`），[main.py:42](src/search/main.py#L42) 无条件打开它，现在直接 `FileNotFoundError`；`run.py` 也在工作区被删了。得先恢复才能端到端验证。
2. **`web: amazon.de` 与 `domain_map` 的键（`tesco`/`argos`/`amazon`）对不上**，`domain_for("amazon.de")` 返回 `None`，触发 [domain_filter.py:22](src/search/layers/domain_filter.py#L22) 的杀光分支。新的 `domain_map_missing` 这个 `failure_kind` 恰好会把这个问题暴露出来——正好当作验收用例。

---

## 验证

1. **单测**（离线，零 API 成本，沿用 `tests/unit/search/` 的 mock 模式）：
   - `db.py` 建表幂等——`SearchDB()` 连续构造两次不报错。
   - 用假的 recorder 走一遍 `flush_task`，断言 6 张表的行数和外键完整性。
   - `failure_kind` 推导的纯函数测试，覆盖上面列的每一种取值。
   - 全流程 mock（假 provider + 假 LLM）跑一个 task，断言 `node_events` 恰好 5 行 `ok`。
2. **短路径**：mock 成搜索零结果，断言 `node_events` 里 `search=ok`、其余 4 个 `skipped`，`failure_kind='no_search_results'`。
3. **LLM 故障路径**：让 mock LLM 抛异常，断言 `verdict='no_match'` 但 `failure_kind='llm_error'`（而**不是** `llm_no_match`）—— 这条直接验证本方案的核心价值。
4. **端到端**：修好 `config_search.yaml` 后，用 3-5 行的输入文件 `python run.py`，然后：
   ```sql
   SELECT * FROM v_run_summary;
   SELECT * FROM v_errors;
   SELECT node, status, COUNT(*), AVG(duration_ms) FROM node_events GROUP BY node, status;
   SELECT rank, url, trace_depth, alive, is_matched FROM candidates WHERE task_id = 1 ORDER BY rank;
   ```
5. **崩溃恢复**：跑到一半 Ctrl-C，断言已完成的 task 全在库里、`runs.status='interrupted'`。
6. **回归**：确认 Excel 输出的 4 列与改动前逐字节一致。
