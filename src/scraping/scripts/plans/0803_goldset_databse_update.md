# 移除 `add_golden_created_by` 迁移，改由 `init_db()` 自愈保证 schema

## Context

`golden_samples.created_by` 是 M17 引入的溯源列（`coldstart` vs `auto`），冷启动 seeding 和 `prune_goldens` 的淘汰排序都依赖它。它有两个来源：新库由 `database.py` 的 `_DDL` 直接建出；旧库靠一次性脚本 `storage/migrations/add_golden_created_by.py` 补。因此 README / CLAUDE.md 把"先跑迁移"写成了冷启动和剪枝的前置步骤。

这个前置步骤没有长期价值：它是手动的、可遗忘的，而且只对"M17 之前建的库"有意义。用户希望直接把列补上、删掉脚本和文档要求。

**关键约束（用户明确提出）：要保证功能的完整性。** 所以不能只是"删掉脚本留个洞"——删掉迁移后，必须有别的机制继续保证 `created_by` 一定存在，否则任何一个历史库（备份、另一台机器、同事的 clone）都会在 `GoldenStore.seed()` 的 INSERT 上直接抛 `no such column: created_by`。

方案是**把一次性手动迁移换成 `ScrapeDB.init_db()` 里的幂等 schema 守卫**。`init_db()` 在每个入口都会被调用（coldstart.py、router.py、prune_goldens.py、html_scraper.py、api_scraper.py、repair/golden.py、repair/agent.py），所以守卫一次到位、永久生效、且完全自动——比手动跑脚本更可靠，而不是更弱。

### 现状核查（已确认）

- 本地 `scraping.db` **6 张表全部 0 行**；`golden_samples` 的 CHECK 已含 `membership`，但缺 `created_by`——是一张 M17 之前建的陈旧表。
- `migrations/` 下三个脚本同性质：`add_golden_created_by.py`、`add_membership_bucket.py`、`relabel_backup_path.py`（后者已无任何引用）。
- `storage/__init__.py` **没有** 导入 `migrations`，删除目录不影响包导入。

### 用户已确认的两个决定

1. 本地库：**删文件重建**（0 行，无数据损失，顺带修掉任何其他 schema 漂移）
2. 删除范围：**三个迁移脚本全删**（整个 `migrations/` 目录）

---

## 实施步骤

### 1. `storage/database.py` — 加幂等 schema 守卫（**功能完整性的核心**）

这一步先做，它是删除迁移脚本的前提。

在 `_DDL` 之后新增期望列表，并让 `init_db()` 在执行 DDL 后补齐缺失列：

```python
# golden_samples 的增量列：新库由 _DDL 建出，历史库在 init_db() 时补齐
_GOLDEN_ADDED_COLUMNS = {
    "created_by": "TEXT NOT NULL DEFAULT 'auto' CHECK(created_by IN ('coldstart', 'auto'))",
}

def init_db(self) -> None:
    self.conn.executescript(_DDL)
    self._ensure_columns()

def _ensure_columns(self) -> None:
    """幂等补齐历史库缺失的增量列（替代已删除的一次性迁移脚本）。"""
    existing = {
        row["name"] for row in self.conn.execute("PRAGMA table_info(golden_samples)")
    }
    for column, ddl in _GOLDEN_ADDED_COLUMNS.items():
        if column not in existing:
            self.conn.execute(
                f"ALTER TABLE golden_samples ADD COLUMN {column} {ddl}"
            )
    self.conn.commit()
```

语义与被删掉的 `add_golden_created_by.run_migration()` **完全一致**：同一条 `ALTER TABLE ADD COLUMN ... DEFAULT 'auto'`，SQLite 自动把历史行回填为 `auto`。`row["name"]` 可用是因为 `conn` 已设 `row_factory = sqlite3.Row`（`database.py:93`）。

### 2. 删除 `src/scraping/storage/migrations/` 整个目录

含 `__init__.py`、`add_golden_created_by.py`、`add_membership_bucket.py`、`relabel_backup_path.py`，以及 `__pycache__/`。

注意 `add_membership_bucket.py` 里的 `has_created_by` 条件分支（`add_membership_bucket.py:45-75`）本就是为"两个迁移的执行顺序"而写的，随目录一并消失。

### 3. `scripts/prune_goldens.py` — 移除现已多余的守卫

- 删除 `_created_by_column_exists()`（30-33 行）
- 删除 `build_prune_plan()` 开头引用已删脚本的 `RuntimeError` 分支（38-42 行）
- `main()` 里的 `except RuntimeError` 成为死代码（160-162 行）→ 把 `try/except/finally` 收敛为 `try/finally`，保留 `db.close()`

安全性依据：`main()` 在 `build_prune_plan()` 之前已调用 `db.init_db()`（154 行），守卫由第 1 步接管。

### 4. 文档（4 个文件，5 处）

- [README.md:128](src/scraping/README.md#L128) — 冷启动代码块删掉迁移那行，只留 `coldstart` 命令
- [README.md:299](src/scraping/README.md#L299) — "Shrinking the golden set" 代码块同样删掉迁移行
- [CLAUDE.md:148-150](src/scraping/CLAUDE.md#L148-L150) — 文件结构树删掉 `migrations/` 三行
- [CLAUDE.md:190](src/scraping/CLAUDE.md#L190) — "Cold Start (new site)" 代码块删掉迁移行
- [tests/README.md:157](src/scraping/tests/README.md#L157) — 删掉句尾 "one-time migration script at `storage/migrations/add_membership_bucket.py`"

`AGENTS.md` **不要手改**——pre-commit hook (`scripts/sync_agent_docs.py`) 会从 `CLAUDE.md` 自动同步。

编码纪律：这些文件含 `—│├└✔` 等非 ASCII 字符，按 CLAUDE.md 的规则必须 UTF-8 无 BOM 原样保留。

### 5. `tests/verify_m17.py` — 改测新机制，**不减少覆盖**

`verify_prune_and_migration()`（322-397 行）当前 9 个 check。改法：

- 函数更名 `verify_prune_and_schema_guard`，section 标签改为 `"M17.5 - prune ordering and schema guard"`（403 行调用点同步）
- 删掉两条迁移 import（325-326 行）
- **保留** 剪枝排序的 4 个 check（329-356 行）原封不动
- **替换** 358-397 行的 5 个迁移 check，改为验证 `init_db()` 自愈——仍然手工建一张无 `created_by` 的历史表，然后：
  - `init_db()` 后 `created_by` 列存在
  - 历史行被回填为 `auto`
  - 再次 `init_db()` 幂等（列不重复、值不变）
  - `build_prune_plan()` 能直接在这张自愈后的历史库上跑通（端到端证明"不需要手动迁移"）

`sqlite3` import 仍在使用（继续用它建历史表），不需要动 import 块。预期 check 数 40 → 39。

### 6. 重建本地数据库

```bash
rm scraping.db scraping.db-shm scraping.db-wal
```

三个文件都要删（WAL 模式有 sidecar）。下次任何入口调用 `init_db()` 时按当前 `_DDL` 完整重建。

---

## 验证

```bash
# 1. schema 守卫本身：全新库 + 历史库都拿到 created_by
python -c "
from src.scraping.storage import ScrapeDB
db = ScrapeDB('scraping.db'); db.init_db()
cols = [r['name'] for r in db.conn.execute('PRAGMA table_info(golden_samples)')]
print('created_by present:', 'created_by' in cols); db.close()
"

# 2. M17 全量（含改写后的守卫 + 剪枝检查），须 0 failed
PYTHONIOENCODING=utf-8 python -m src.scraping.tests.verify_m17 \
  | tee src/scraping/tests/verify_m17_output.log

# 3. 回归：涉及 golden set / DB schema 的离线套件
PYTHONIOENCODING=utf-8 python -m src.scraping.tests.verify_m1_m3
PYTHONIOENCODING=utf-8 python -m src.scraping.tests.verify_m9

# 4. 剪枝 CLI 在没有任何迁移命令的情况下直接可用（dry run，不删数据）
python -m src.scraping.scripts.prune_goldens --site tesco

# 5. 确认无残留引用
grep -rn "migrations" src/ --include="*.py" --include="*.md" | grep -v scripts/plans/

# 6. 编码守卫（CLAUDE.md 强制）
python3 scripts/check_encoding.py --all
```

第 5 步预期只剩 `scripts/plans/` 下的历史规划文档——那些是已归档的决策记录，按原样保留，不修改。

第 2 步的 log 必须重新捕获：CLAUDE.md 的 "Verification Discipline" 要求每个里程碑在 `src/scraping/tests/` 留下可复跑的持久化产物。

---

## 不做的事

- 不动 `scripts/plans/` 下的历史规划文档（`0802_coldstart_excel_input_golden_caps.md`、`solving_price_detection_pro.md`）——历史记录，不是活文档
- 不给 `page_type` 的 CHECK 约束做自动重建。CHECK 无法用 ALTER 修改，只能整表重建，风险远高于加列；且本地库的 CHECK 已含 `membership`，没有任何库需要它。如果将来真出现 CHECK 漂移的老库，正确做法是重建库而不是留一个半吊子的自动重建路径。
