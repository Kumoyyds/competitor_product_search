# 修复 repair ladder 的 Turn B 死代码（M25）

## Context

`repair/agent.py` 的 ladder 每轮开头设计了两道 LLM 判断题：

- **Turn A** `no_product` — "这是不是商品页"，只看 HTML 就能答，放在 index 0，**目前正常工作**。
- **Turn B** `source_absence` — "数据是难抓（solvable）还是页面上根本没有（source_absent，反爬墙/渲染不全/价格不显示）"。判 `source_absent` 就终止 ladder，不再浪费后续轮次。

Turn B 的触发条件写的是 `index == 1 and not is_last`（[agent.py:225](src/scraping/repair/agent.py#L225)）。`not is_last` 的本意是"最后一轮问了也省不下后续轮次，别浪费一次 LLM 调用"，但漏算了"提前停能省下本轮自己的 parser 生成"。当前默认 ladder 是 2 节点，index 1 恰好就是 last，**条件恒假 —— Turn B 从未执行过一次**。

后果（非正确性问题）：

1. **浪费** — 反爬/渲染不全的页面必然烧完第 2 轮，而第 2 轮恰是开了 thinking、最贵的一轮。
2. **诊断丢失** — 这类失败全被打成 `parser_broken` / `repair_budget_exhausted`，与"parser 写得烂"混在同一个桶里。`source_absent` 本该是指向"查反爬 / 换数据源"的独立信号。

**已确认的设计决定**：Turn B 保持在第 2 轮之前（`index == 1`），**不前置到 ladder 最开头**。前置虽能多省一轮，但那时唯一证据是旧 parser 的报错 —— 而旧 parser 失效最常见的原因是页面改版，其报错与"页面真没数据"无法区分。让第 1 轮照当前页面重新生成一个 parser、它也抓不到，证据才排除了"parser 过时"这一可能。

## 方案

### 1. 修复触发条件并加证据门槛 —— `repair/agent.py`

`_try_repair` 中把 Turn B 分支改为：

```python
    # Turn B — source_absence, from the second attempt onward.
    # Requires *evidence*: the previous attempt must have produced a runnable
    # parser that still missed required fields.  A sandbox crash means the
    # generated code was broken, which says nothing about whether the page
    # carries the data — asking then would invite a false source_absent.
    if index == 1 and _has_source_absence_evidence(ctx):
        absent = await _ask_source_absence(judgment_llm, ctx)
        if absent and absent.get("decision") == "source_absent":
            return SourceAbsent(reason=absent.get("reason", "source_absent"))
```

新增模块级 helper：

```python
def _has_source_absence_evidence(ctx: RepairContext) -> bool:
    """True when the previous attempt ran but could not find required fields."""
    if not ctx.attempts:
        return False
    prev = ctx.attempts[-1]
    if prev.failure_stage != "gate":
        return False
    return bool((prev.capture or {}).get("missing_required"))
```

三处取值都是现成的：`failure_stage` 在 [agent.py:270](src/scraping/repair/agent.py#L270) 赋值，`capture` 由 [`summarize_capture`](src/scraping/repair/agent.py#L98-L118) 在 [agent.py:258](src/scraping/repair/agent.py#L258) 填充，`missing_required` 是它的既有输出键。无需新增数据结构。

行为矩阵（默认 2 节点 ladder）：

| 上一轮失败于 | 问 Turn B？ | 理由 |
|---|---|---|
| `sandbox`（代码崩了） | 否 | 说明代码烂，不说明页面没数据 |
| `gate` 且缺必需字段 | **是** | 跑通了仍抓不到 → 真证据 |
| `gate` 但只缺可选字段 | 否 | 必需字段拿到了，页面显然有数据 |
| `golden`（复现不了旧样本） | 否 | 本页数据抓到了，是泛化问题 |

**降级安全**：`_ask_source_absence` 已在异常时返回 `None`（[agent.py:344-346](src/scraping/repair/agent.py#L344-L346)），`if absent and ...` 保证 LLM 挂掉或返回 `solvable` 时照常跑 Turn C —— 与现状行为一致。

### 2. 同步三处已过时的注释

- [agent.py:9](src/scraping/repair/agent.py#L9) 模块 docstring：`Turn B (source_absence): index==1 and not last (skip when nothing left to skip)`
- [agent.py:207](src/scraping/repair/agent.py#L207) `_try_repair` docstring：同上表述
- `src/scraping/CLAUDE.md`（及 pre-commit 同步的 `AGENTS.md`）"Repair Ladder (§5.5)" 一节里 `When the ladder has 2 nodes, Turn B (source_absence) is skipped ...` 整段需重写，并在末尾 Milestone 表补 M25 行

### 3. 诊断落点：零 schema 改动

已核实链路是通的，**不需要动数据库**：

- `SourceAbsent` 分支已构造 `signature=(site, "source_absent", "")`（[agent.py:173](src/scraping/repair/agent.py#L173)）
- [`_record_failure`](src/scraping/scrapers/base.py#L79-L97) 用 `format_scrape_signature(...)` 把它写进 `scrape_runs.signature` → 每次执行可查
- [`_derive_signature`](src/scraping/router.py#L142-L147) 同样透传，escalation 行的 **signature** 也会带 `source_absent`

唯一限制：`escalations.reason` 列有 CHECK 约束只认 4 个值（[database.py:59](src/scraping/storage/database.py#L59)），且 [`_derive_reason`](src/scraping/router.py#L121-L139) 无 `source_absent` 分支，故 reason 仍聚合为 `parser_broken`。**本次不做这个迁移** —— signature 已足够区分，查询用：

```sql
SELECT site, COUNT(*) FROM scrape_runs
WHERE outcome = 'escalated' AND signature LIKE '%|source_absent|%'
GROUP BY site;
```

若日后确认这类失败量大到需要独立告警，再照 M24 的 `init_db()` 增量迁移先例扩 CHECK。

### 4. 风险与兜底

CLAUDE.md 记录的 M12 实测是 **6 次成功修复中 4 次赢在 `agent_attempt_1`（第 2 轮）** —— Turn B 生效后就成了主力轮次前的闸门，LLM 误判会直接损失该轮。三重缓解：

1. §1 的证据门槛把提问场景收窄到"跑通但抓不到必需字段"
2. `SourceAbsent` 返回的是 `ScrapeFailed`，router 捕获后**继续尝试下一个 scraper**（[router.py:82-87](src/scraping/router.py#L82-L87)），Tesco/Argos 均有 DCA 备份源
3. LLM 异常/`solvable` 一律继续跑 Turn C

不新增 config 开关：ladder 已是 config 驱动，再加布尔量徒增复杂度，且会触发 README 文档纪律。

## 待改文件

| 文件 | 改动 |
|---|---|
| `src/scraping/repair/agent.py` | Turn B 条件 + `_has_source_absence_evidence` helper + 2 处 docstring |
| `src/scraping/CLAUDE.md` / `AGENTS.md` | 重写 Turn B 段落 + M25 行（pre-commit 自动同步另一份） |
| `src/scraping/tests/verify_m25.py` | 新增 |
| `src/scraping/tests/verify_m25_output.log` | 新增 |
| `src/scraping/tests/README.md` | 补 M25 条目 |

**不触发根 README 更新**：无 CLI/config key/数据文件/输入输出格式变更，属 operator-invisible 修复。

## 验证

新建 `src/scraping/tests/verify_m25.py`，**全离线**（mock LLM 与 sandbox，遵循 tests/README.md 的既有形式：命名检查 + `[PASS]`/`[FAIL]` + 结尾 `SUMMARY: N passed, M failed` + 失败非零退出）。覆盖：

1. 2 节点 ladder + 上一轮 gate 失败缺必需字段 → `_ask_source_absence` **被调用**（当前代码此项必失败，正是 bug 存在的证据）
2. 上一轮 `failure_stage="sandbox"` → **不**调用
3. 上一轮 `failure_stage="golden"` → **不**调用
4. 上一轮 gate 失败但 `missing_required` 为空 → **不**调用
5. 判 `source_absent` → 返回 `ScrapeFailed(failed_stage="source_absent")`，且该轮 `parser_gen` **未被调用**
6. 判 `solvable` → Turn C 照常执行，ladder 行为与修复前一致
7. `_ask_source_absence` 抛异常 → 不叫停，Turn C 照常执行
8. 1 节点 ladder → Turn B 永不执行（无 index 1）
9. 3 节点 ladder → 仅 index 1 问一次，index 2 不问（回归保护）
10. `source_absent` 终止后 `scrape_runs.signature == "{site}|source_absent|"`（临时 SQLite）
11. router 在 `source_absent` 后仍继续尝试下一个 scraper

运行并留存日志：

```bash
python src/scraping/tests/verify_m25.py | tee src/scraping/tests/verify_m25_output.log
```

回归：重跑 `verify_m8.py`（ladder 机制）与 `verify_m10.py`（fallback/escalation），确认未破坏既有行为。
