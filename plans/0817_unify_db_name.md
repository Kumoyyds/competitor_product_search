# 统一 SQLite 文件后缀为 `.db`

## Context

仓库里两个模块的 SQLite 文件后缀不一致，纯粹是各自独立演进、没有对齐命名的历史产物 —— 两边都是标准 `sqlite3`，功能上零区别：

| 模块 | 当前路径 | 定义处 |
|---|---|---|
| scraping | `scraping.db` | [config.py:108](src/scraping/config.py#L108) |
| search trace | `search_db.sqlite` | [db.py:35](src/search/db.py#L35) + [search_config.yaml:253](src/search/maintain/search_config.yaml#L253) |
| search cache | `.cache/base_extraction.sqlite` | [cache.py:26](src/search/cache.py#L26) + [search_config.yaml:249](src/search/maintain/search_config.yaml#L249) |

目标：全部收敛到 `.db`，并让 search 侧的主库和 `scraping.db` 形成对称命名 —— `search.db`。scraping 侧已经是 `.db`，**完全不动**。

## 风险评估：低

支撑理由：

1. **真正改变行为的只有 4 行**（2 行 Python 默认值 + 2 行 YAML）。其余全是文档和显式传参的测试临时文件名。
2. **没有任何测试耦合默认路径** —— `tests/unit/search/` 里 10 处 `SearchDB(...)` 调用全部显式传 `tmp_path`，且 [conftest.py:5](tests/unit/search/conftest.py#L5) 的 autouse fixture 把 `get_db` patch 成 `None`，测试根本不会碰默认路径。
3. **单一读取点**：search DB 路径没有 env var 覆盖、没有 CLI flag、没有外部消费者，只经 `config.get("db", "sqlite_path")` 读一次。
4. **失败模式是响的，不是哑的**：inspect notebook 的 setup cell 显式 `if not path.is_file(): raise FileNotFoundError`；漏迁移只会得到一个明显空的库，不会静默写坏数据。

真正会咬人的三点，方案里都已覆盖：

- **旧数据变孤儿**（最大的一个）—— trace DB 有 4 runs / 164 tasks / 3007 candidates 的历史，多个 plan 文档（`0813_numeric_extraction_fixing.md`、`0814_duckduckgo_fix.md` 等）都引用过里面的 run。靠下面的 `mv` 步骤解决。
- **.gitignore 失配** —— 第 16 行 `search_db.sqlite*` 改名后不再匹配，而 gitignore 里**没有**通用的 `*.db` 规则（只有 `*.db-shm` / `*.db-wal`），7.5MB 的 `search.db` 会变成 git 可见的未忽略文件，`git add .` 可能误提交。靠第 16 行的编辑解决。
- **WAL 边车文件** —— `.cache/base_extraction.sqlite-wal` 当前是活的（115KB）。三个文件必须一起改名（SQLite 按 `<db>-wal` 约定找），且改名时不能有进程持有该库。

## 改动清单

### 1. 代码默认值（2 文件 2 行）

- [src/search/db.py:35](src/search/db.py#L35) — `default="search_db.sqlite"` → `default="search.db"`
- [src/search/cache.py:26](src/search/cache.py#L26) — `default=".cache/base_extraction.sqlite"` → `default=".cache/base_extraction.db"`

### 2. 配置文件（1 文件 2 行）— 这才是实际生效的值

- [search_config.yaml:249](src/search/maintain/search_config.yaml#L249) — `cache.sqlite_path: .cache/base_extraction.db`
- [search_config.yaml:253](src/search/maintain/search_config.yaml#L253) — `db.sqlite_path: search.db`

> **配置 key 名 `sqlite_path` 保持不变。** 它描述的仍然是一个 SQLite 路径，语义准确；改成 `db_path` 会破坏任何自定义过 YAML 的环境，且需要加双 key 兼容读取 —— 零收益的风险。

### 3. `.gitignore`（1 行）

- 第 16 行：`search_db.sqlite*` → `search.db*`（一并覆盖 `-wal` / `-shm`）
- 缓存库**不需要改** —— 第 18 行 `.cache/` 已整目录忽略。

### 4. 数据迁移（一次性，运行前确认没有 Python 进程在跑）

```bash
mv search_db.sqlite search.db
# 根目录当前有 search_db.sqlite-wal；若有 -shm 也一并改名

mv .cache/base_extraction.sqlite     .cache/base_extraction.db
mv .cache/base_extraction.sqlite-wal .cache/base_extraction.db-wal
mv .cache/base_extraction.sqlite-shm .cache/base_extraction.db-shm
```

三个缓存文件必须一起移动，否则 115KB 的 WAL 内容会丢失。

### 5. Notebook — [src/search/script/inspect_tables.ipynb](src/search/script/inspect_tables.ipynb)

用 **NotebookEdit**（不要直接改 JSON）：

- 第 51–52 行的两个硬编码常量 `TRACE_DB_PATH` / `CACHE_DB_PATH`
- 第 201 行的 label 列表 `[(".cache/base_extraction.sqlite", ...), ("search_db.sqlite", ...)]`
- 第 10 行 intro cell，以及 9 个 markdown 标题 cell（`## search_db.sqlite — runs` 等，位于第 215/266/284/624/642/660/678/696/714 行）
- **存量 output 不动**（第 24–25、133–192 行）—— 那是上次执行的历史输出，重跑自然刷新。

### 6. 文档（CLAUDE.md 的 Documentation Discipline：config 默认值变更 → 同一 commit 更新 README）

- [CLAUDE.md:129,131](CLAUDE.md#L129)（根）—— Data Files 段两条
- [src/search/CLAUDE.md:51,83](src/search/CLAUDE.md#L51) —— `cache.py` 行 + config 旋钮表
- [src/search/README.md:182,229](src/search/README.md#L182) —— 两处 `.cache/base_extraction.sqlite`
- README 第 193 行提到的是配置 **key** `cache.sqlite_path`，key 不变，**不要改**
- 根 `README.md` 已确认无 search DB 路径提及，跳过

> **每对 `CLAUDE.md` / `AGENTS.md` 只改其中一个。** [scripts/sync_agent_docs.py](scripts/sync_agent_docs.py) 的 pre-commit 模式会双向同步；若两个文件都改过且内容不同，钩子会判定冲突并 **abort commit**。

### 7. 明确不改

- `src/scraping/**` —— 已经是 `.db`
- `src/search/plans/*.md` —— 带日期的历史记录，当时引用的就是 `search_db.sqlite`，重写等于篡改历史
- 配置 key `db.sqlite_path` / `cache.sqlite_path`
- notebook 的存量 output

### 8. 立规矩：以后新建的 SQLite 库一律用 `.db`

这次不只是改名，还要把约定固化下来，否则下一个模块又会随手写个 `.sqlite`。落两个地方：

**(a) 仓库约定** —— [CLAUDE.md](CLAUDE.md) 的 `## Code Conventions` 段追加一条：

> - **SQLite 数据库文件一律用 `.db` 后缀**，不用 `.sqlite` / `.sqlite3`。库名取模块名（`scraping.db`、`search.db`）；WAL/SHM 边车文件由 SQLite 自动按 `<name>.db-wal` / `<name>.db-shm` 生成，`.gitignore` 里已有通配规则覆盖。新建库时同步在 `.gitignore` 决定它是跟踪还是忽略。

同样**只改 `CLAUDE.md`**，`AGENTS.md` 由 pre-commit 钩子同步。这条属于 Code Conventions，不是 operator-facing 变更，不额外触发 README。

**(b) 跨会话记忆** —— 在 `~/.claude/projects/-Users-kumo-programming-competitor-product-search/memory/` 新建 `sqlite-files-use-db-suffix.md`（`type: feedback`），正文写清 **Why**（两个模块独立演进导致 `.sqlite` / `.db` 混用，用户明确要求统一）和 **How to apply**（新建任何 SQLite 库时默认 `.db`，并检查 `.gitignore` 是否覆盖），并在 `MEMORY.md` 加一行索引。

### 9. 测试临时库命名

`tests/unit/search/` 里约 11 处 `tmp_path / "batch.sqlite"`、`"single.sqlite"`、`"old.sqlite"`、`"base.sqlite"` 都是显式传给 `SearchDB(...)` 的临时文件名，与默认值无耦合；仍应一并改为 `.db`，以保证“所有 SQLite 文件统一 `.db`”没有测试例外。

## 验证

```bash
# 1. 除历史 plans 和 notebook 的保留输出外，源码无残留引用
git grep -n "search_db\.sqlite\|base_extraction\.sqlite" -- \
  ':!src/search/plans/**' ':!plans/**' ':!src/search/script/inspect_tables.ipynb'  # 应无输出
# notebook 的 source 同样应无残留；其历史 output 按本计划保留

# 2. 迁移后的文件存在且大小与原文件一致（7.5MB / 180KB）
ls -la search.db .cache/base_extraction.db

# 3. 单测（离线，mock Serper + LLM，零 API 花费）
python -m pytest tests/unit/search/ -v

# 4. 迁移后的 trace DB 行数对得上（应为 runs=4, tasks=164, candidates=3007）
python -c "import sqlite3;c=sqlite3.connect('search.db');print([(t,c.execute(f'select count(*) from {t}').fetchone()[0]) for t in ('runs','tasks','candidates')])"

# 5. 编码检查（改了含 — 等非 ASCII 字符的文档）
python3 scripts/check_encoding.py --all

# 6. gitignore 生效 —— search.db 不应出现在未跟踪列表里
git status --short
```

7. **跑通 notebook**：全量执行 `src/search/script/inspect_tables.ipynb`，setup cell 能只读打开两个库、不抛 `FileNotFoundError`，各表正常渲染。
8. **一次实跑冒烟**：跑一次 `python -m src.search.batch`（或单条 `match_product`），确认 `search.db` 的 `runs` 从 4 变成 5，且根目录**没有**重新长出 `search_db.sqlite`。
9. **规则落地**：`CLAUDE.md` 与 `AGENTS.md` 的 Code Conventions 段都含新条目；`memory/MEMORY.md` 多一行索引且 `sqlite-files-use-db-suffix.md` 存在。
10. **提交**：pre-commit 钩子同步 `AGENTS.md` 并通过编码检查。

## 计划归档

用户已在 [plans/0817_unify_db_name.md](plans/0817_unify_db_name.md) 建了空文件 —— 执行时把本计划内容写进去（仓库内的计划目录，和 `src/search/plans/` 的既有惯例一致）。
