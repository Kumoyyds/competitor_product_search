# 扩展 check_database.ipynb 的按 site 清库能力

## Context

`src/scraping/scripts/check_database.ipynb` 最后一节 "清空数据库 (Clear database by site)" 是 re-cold-start 一个站点前的操作入口。它目前只能清 `parsers` 和 `golden_samples`（`ScrapeDB.clear_site()` 硬编码了这两张表），另外三张同样按 site 分区的表 —— `results`、`escalations`、`invalid_target_phrases` —— 无法清除，只能手写 SQL。

结果是"重新冷启动一个站点"并不干净：旧的 `results` 会和新 parser 产出的结果混在一起、旧的 `escalations`（signature 唯一去重）会让新的同类故障只累加 `affected_count` 而不产生新告警行、旧的 `invalid_target_phrases` 会继续影响新 parser 的 invalid-target 预判。

预览 cell 还有一个盲区：它只按 `site` 列枚举站点，而 `escalations` 没有 `site` 列，所以当前库里 `amazon` 的 2 条 escalation 完全不会出现在站点列表中。

目标：让 6 张表中全部 5 张 site 维度的表都可按站点选择性清除，逻辑落在 `ScrapeDB.clear_site()` 内、单事务、可测试；notebook 保持"选 site + 勾表 + CONFIRM"的单一入口。

## 已确认的设计选择

- `scrape_runs` **也**加入可清除集合（默认不勾选，必须显式列出才删）。
- notebook 用**统一的一个 `TABLES` 选择**，不分成两个小节。
- 清除逻辑**扩展 `ScrapeDB.clear_site()`**，notebook 只负责选择与展示。

## 实现

### 1. `src/scraping/storage/database.py` — 扩展 `clear_site()`

在模块级新增两个常量（notebook 与测试都从这里 import，避免表名散落）：

```python
CLEARABLE_TABLES = (
    "parsers", "golden_samples", "results",
    "escalations", "invalid_target_phrases", "scrape_runs",
)
DEFAULT_CLEAR_TABLES = ("parsers", "golden_samples")
```

`clear_site(self, site: str, tables: Sequence[str] | None = None) -> dict[str, int]`：

- `tables=None` → 用 `DEFAULT_CLEAR_TABLES`，**保持现有行为完全不变**（现有 notebook cell、`verify_clear_db.py` 的调用都不受影响）。
- 表名不在 `CLEARABLE_TABLES` 中 → 抛 `ValueError` 并列出合法值。表名是拼进 SQL 的，白名单校验同时是防注入的边界。**不要**接受空字符串/空列表以外的任何未知值；空列表视为 no-op，返回空 counts。
- 全部操作在**一个** `BEGIN IMMEDIATE` 事务里，异常 rollback（沿用现有 try/except 结构）。

删除顺序（FK 安全）：

1. `scrape_runs`（若选中）— 整行删除，`winning_parser_id` 随之消失。
2. `UPDATE scrape_runs SET winning_parser_id = NULL WHERE site = ? AND winning_parser_id IS NOT NULL`（**仅当 `parsers` 选中时**执行）→ 记为 `counts["scrape_runs_detached"]`。放在 1 之后，所以两张表都选时这一步自然是 0。
3. `DELETE FROM parsers WHERE site = ?`
4. `golden_samples` / `results` / `invalid_target_phrases`（若选中）— 都是直接 `WHERE site = ?`。
5. `escalations`（若选中）— **没有 `site` 列**，按 signature 前缀匹配：

   ```sql
   DELETE FROM escalations
   WHERE substr(signature, 1, instr(signature || '|', '|') - 1) = ?
   ```

   依据：所有写入点的 signature 都是 `{site}|{field_or_rule}|{parser_version}` —— `format_scrape_signature()`（`src/scraping/exceptions.py:32`）、`router.py:71` 的 `f"{site}|infra_failure|"`、`html_scraper.py:306` 的 `f"{self.site}|invalid_target_surge|"`。拼接 `|| '|'` 让无管道符的历史 signature 也能按整串精确匹配。
   **不要用 `LIKE site || '|%'`** —— site key 里的 `_`（例如测试用的 `site_a`）在 LIKE 中是通配符，会误伤。

返回的 `counts` 只包含被选中的表键 + （选中 parsers 时的）`scrape_runs_detached`。

### 2. notebook 清库小节改造（cell 19–24）

用现有的空 cell 21 作为配置 cell，删掉多余的空 cell 22；cell 25 尾部空 cell 保留。

- **cell 19（markdown 总标题）**：重写描述 —— 列出 6 个可清表、说明默认只清 `parsers` + `golden_samples`、说明 `escalations` 按 signature 前缀匹配、说明选中 `scrape_runs` 时运行历史会被真正删除（不再只是 detach）。保留 ⛔ DESTRUCTIVE 警示。
- **cell 20（`### golden and scraper`）**：改为反映通用用途的小标题，例如 `### 选择站点与要清除的表`。
- **cell 21（配置，原空 cell）**：

  ```python
  from src.scraping.storage.database import CLEARABLE_TABLES, DEFAULT_CLEAR_TABLES

  SITE = "argos"                       # ← canonical site key
  TABLES = list(DEFAULT_CLEAR_TABLES)  # ← 可加 'results' / 'escalations' /
                                       #    'invalid_target_phrases' / 'scrape_runs'
  print(f"Clearable tables: {', '.join(CLEARABLE_TABLES)}")
  print(f"Selected: site={SITE!r} tables={TABLES}")
  ```

- **cell 23（预览）**：读上面的 `SITE` / `TABLES`，把 escalations 纳入统计。
  - 站点集合 = 5 张带 `site` 列的表的 `DISTINCT site` **并上** escalations 的 signature 前缀（`SELECT DISTINCT substr(signature, 1, instr(signature || '|', '|') - 1) FROM escalations`）—— 这样 `amazon` 这类只有 escalation 的站点不再隐形。
  - 每站点逐表计数，选中的表标 `← WILL BE CLEARED`，并给出该站点将删除的总行数。
  - 当 `TABLES` 含 `parsers` 且不含 `scrape_runs` 时，额外提示"N 条 scrape_runs 将被保留但 FK 置空"。
- **cell 24（执行）**：

  ```python
  CONFIRM = False   # ← set to True to execute the hard delete
  if SITE and TABLES and CONFIRM:
      counts = db.clear_site(SITE, tables=TABLES)
      ...
  ```
  打印每张表的删除行数 + `scrape_runs_detached`；未确认时打印现有的提示语。

注意：notebook 是 JSON，编辑用 `NotebookEdit`，保持 UTF-8 无 BOM（文件里有 `⛔ ← …` 等非 ASCII 字符，按 CLAUDE.md 的编码规则原样保留）。清库 cell 的 `outputs` 里现存一条 `✓ Site 'argos' cleared:` 执行记录，改写 cell 时一并清空该 cell 的 outputs，避免和新语义不符的旧输出留在文件里。

### 3. 测试 —— 迁移到 pytest

CLAUDE.md 的验证纪律要求新测试写成 `tests/unit/scraping/` 下的 pytest 主题文件，且旧的 `verify_mN` 脚本"等价覆盖后即删除"。因为本次正好改的就是 `clear_site`，把它的测试一次迁完：

- 新建 `tests/unit/scraping/__init__.py` 与 `tests/unit/scraping/test_clear_site.py`（该目录目前不存在；`tests/unit/search/` 是现成的组织范例）。
- 复用 `tests/_support/db.py` 的 `temp_scrape_db()` fixture 与 `fetchall()`，不要重新实现临时库逻辑。
- 移植 `src/scraping/tests/verify_clear_db.py` 的全部既有覆盖：默认行为（parsers/goldens 删除、runs 保留且 FK 置空）、跨站点隔离、幂等、未知站点 no-op、schema 不变、`PRAGMA foreign_keys=OFF` 下仍成功。
- 新增覆盖：
  - 每张新表按 site 单独清除，且不误伤其他站点；
  - `escalations` 按 signature 前缀删除，`site_a` 不会删掉 `site_ab` 的 signature（前缀相似用例），无管道符 signature 也能命中；
  - 同时选 `scrape_runs` + `parsers` 时无 FK 报错，且 `scrape_runs_detached == 0`；
  - 未选 `parsers` 时 counts 里没有 `scrape_runs_detached` 键；
  - 未知表名抛 `ValueError`；空 `tables` 列表是 no-op；
  - `tables=None` 与显式传默认值结果一致（向后兼容锁定）。
- 全部通过后删除 `src/scraping/tests/verify_clear_db.py`。

### 4. 文档

按 CLAUDE.md 的 Documentation Discipline，这是"新增的人工维护步骤"，更新 `src/scraping/README.md`：

- 文件结构清单（约 183 行，`scripts/live_batch_report.py` 那一行附近）补一行 `scripts/check_database.ipynb`，注明它是库审查 + 按站点清库的操作入口。
- 在 cold start 相关章节补一小段"重新冷启动一个站点前如何清库"：打开 notebook → 设 `SITE` / `TABLES` → 跑预览 → `CONFIRM = True`；说明默认只清 parser + golden，要真正清干净需勾上 `results` / `escalations` / `invalid_target_phrases`，以及 `scrape_runs` 会真删运行历史这一后果。

根 `README.md` 不需要改（未涉及项目级入口）。

## 验证

```bash
# 单元测试（离线、确定性）
python -m pytest tests/unit/scraping/test_clear_site.py -v

# 回归：确认没有其它测试依赖旧的 clear_site 签名
python -m pytest

# 编码检查（notebook 与 markdown 含非 ASCII）
python3 scripts/check_encoding.py --all
```

notebook 端的人工验证（对**副本**操作，别动仓库根的 `scraping.db`）：

1. 复制一份 `scraping.db` 到 scratchpad，把 cell 1 的 `DB_PATH` 临时指向副本。
2. 从头运行到预览 cell —— 确认站点列表里现在出现了 `amazon`（它只有 escalations），且各表计数与顶部各 section 的表格一致。
3. `TABLES = ["results", "escalations", "invalid_target_phrases"]`、`SITE = "amazon"`、`CONFIRM = True` 执行 —— 确认 escalations 的 2 条 amazon 行被删、tesco/argos 的行不受影响、`parsers` / `golden_samples` 一行未动。
4. 再执行一次同样的清除 —— 确认幂等（全部返回 0，不报错）。
5. `TABLES` 加上 `parsers` + `scrape_runs` 对副本上的 `tesco` 执行 —— 确认无 FK 报错、`scrape_runs_detached == 0`。
6. 还原 `DB_PATH`，清掉验证产生的 cell outputs 再提交。
