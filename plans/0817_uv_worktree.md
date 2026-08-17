# 迁移到 uv 管理依赖（支持多 worktree 并发）

## Context

目标是让多个 git worktree 各自持有独立环境、并发开发。现状阻碍：

1. **依赖装在单一根 `.venv`（430M）**，worktree 之间无法隔离；用 pip 为每个 worktree 重建要重新下载 ~126 个包。
2. **`pyproject.toml` 没有任何 `[project.dependencies]`** —— 依赖只存在于 `requirements.txt`。
3. **`requirements.txt` 已与实际 `.venv` 严重脱节**（实测）：60 个包版本漂移、19 个包 pin 了但没装。其中 `pandas` pin `2.3.2` 而实装 `3.0.3`（跨大版本 breaking change），`scikit-learn`/`scipy`/`joblib` 完全没装。**照 README 跑 `pip install -r requirements.txt` 建出的环境，与实际开发环境不是一个东西。**
4. `pywin32==312` 是 Windows-only，会让 macOS 上的解析直接失败。

迁移后：`uv.lock` 提交进 git，任何 worktree / 机器 `uv sync` 得到字节一致的依赖集；uv 从全局 cache（现 3.5G）硬链接，新 worktree 建环境是秒级、磁盘增量接近 0。

### 已确认的决策

| 决策点 | 选择 |
|---|---|
| 依赖声明 | `pyproject.toml` + `uv.lock` + `uv sync`，**删除** `requirements.txt` |
| worktree 的 `.env` | symlink 到主 checkout |
| worktree 的 `scraping.db` | symlink 共享主 checkout（保住昂贵的 parser/golden 资产） |
| `quantulum3` | 只声明 `quantulum3`，**不加** `[classifier]` —— 锁住当前实际在跑的无分类器行为，迁移不引入行为变化 |

---

## Phase 0 — 前置与基线

1. `uv self update`（当前 0.7.2 / 2025-04，过老；`dependency-groups`、`uv sync` 行为需新版本）。
2. **抓基线**，作为 lock 结果的比对靶子（`pip` 不在 `.venv` 里，用 uv）：
   ```bash
   uv pip freeze --python .venv/bin/python > /tmp/baseline-freeze.txt
   ```
3. 备份现有 `.venv` 目录名（`mv .venv .venv.bak`），出问题可秒退回。

---

## Phase 1 — 重写 `pyproject.toml`

改 [pyproject.toml](pyproject.toml)：

```toml
[project]
name = "pricescope"
version = "0.1.0"
description = "Find competitor product URLs on marketplaces using LLM-powered search"
requires-python = ">=3.12,<3.13"     # 收紧：原 ">=3.12" 会让 resolver 选 3.13/3.14
readme = "README.md"
dependencies = [
    "aiohttp", "beautifulsoup4", "lxml", "ddgs", "httpx",
    "langchain-openai", "langgraph", "openpyxl", "pandas",
    "pydantic", "pydantic-settings", "quantulum3", "rapidfuzz",
    "tqdm", "PyYAML", "python-dotenv",
]

[dependency-groups]
dev = ["pytest", "pytest-asyncio"]
notebook = ["ipykernel", "ipython", "requests"]

[tool.uv]
package = false      # 关键：见下

# [build-system] 与 [tool.setuptools.*] 整段删除
# [tool.pytest.ini_options] 原样保留
```

**要点说明**

- **`package = false` 是必须的。** 现有 `[build-system]` + `[tool.setuptools.packages.find] include=["src*"]` 与「`src/` 是 implicit namespace package、无 `src/__init__.py`」矛盾；`uv sync` 默认会 build 本项目，这里会失败或产出空 wheel。项目实际的运行方式一直是从 repo root `python -m src.search.batch`，本就不需要被安装。
- **依赖清单是 AST 扫全仓 `.py` + 7 个 `.ipynb` 得出的 16 个直接依赖**，不是 freeze 的 126 行。`langchain` / `langchain-core` / `langchain-community` / `langsmith` / `openai` / `numpy` / `scipy` / `sklearn` / `tiktoken` / `tenacity` / `nest-asyncio` 等**在代码里零直接 import**，全部由 `langchain-openai`/`langgraph`/`pandas` 传递带入，不再显式声明。
- **`lxml` 必须留**，虽然没有 import 语句：[src/scraping/detection.py:73](src/scraping/detection.py#L73) 用 `BeautifulSoup(html, "lxml")`，且它在 sandbox import 白名单 [src/scraping/config.py:83](src/scraping/config.py#L83) 里，LLM 生成的 parser 会 `import lxml`。
- **`pytest-asyncio` 必须留**，虽然没有 import：`pyproject.toml` 的 `asyncio_mode = "auto"` 依赖它。
- **`pywin32` / `appnope` 直接删**，全仓无 import。
- 不 pin 版本，交给 `uv.lock` 锁精确版本（这正是 lock 的职责；pin 在两处会打架）。

再执行 `uv python pin 3.12`（生成 `.python-version`，提交）。

---

## Phase 2 — 生成 lock 并验证无回归

```bash
uv lock
uv sync --group dev --group notebook
uv pip freeze > /tmp/after-freeze.txt
diff <(sort /tmp/baseline-freeze.txt) <(sort /tmp/after-freeze.txt)
```

审查 diff：**跨大版本的差异要逐个看**，同版本/补丁级差异忽略。当前实装版本已接近最新（`pandas 3.0.3`、`langchain-openai 1.4.0`、`langgraph 1.2.9`、`pydantic 2.13.4`），预期 fresh resolve 落点很近。若某包 resolve 出的大版本与基线不同，在 `dependencies` 里对该包加下界约束（如 `"pandas>=3.0"`）而不是精确 pin。

跑测试兜底：

```bash
uv run pytest                        # 默认 -m 'not live'
uv run pytest tests/unit/search/ -v
uv run python scripts/check_encoding.py --all
```

---

## Phase 3 — 修掉迁移会打破的东西

删掉 `requirements.txt` 会连带打破两处，必须同批修：

1. **[src/search/script/searxng.ipynb](src/search/script/searxng.ipynb) cell 0** — `find_repo_root()` 靠 `(candidate / "requirements.txt").is_file()` 定位仓库根，文件没了会抛 `RuntimeError`。哨兵改为 `pyproject.toml`。同 cell 还手工拼 `.venv/Lib/site-packages` 与 `.venv/lib/pythonX.Y/site-packages` 塞进 `sys.path` 作为 `httpx` 的 fallback —— uv 布局相同故不会坏，但既然在改这个 cell，一并删掉这段脆弱逻辑。
2. **[src/search/script/duckduckgo.ipynb](src/search/script/duckduckgo.ipynb) cell 0** — `subprocess.check_call([sys.executable, "-m", "pip", "install", "ddgs"])`。这是全仓唯一的运行时安装，会在 uv 同步出的 env 上带外写入。`ddgs` 已是声明依赖，直接删掉 try/except 改成裸 import。（同 cell 的 `PROJECT_ROOT = os.getcwd()/../..` 解析到 `src/` 而非 repo root，是既有隐患，顺手修。）

另外两处清理：

3. **[.claude/settings.local.json](.claude/settings.local.json)** — 整个 allowlist 是 Windows 时代的死条目（`PowerShell(.\\.venv\\Scripts\\Activate.ps1)`、`Bash(... .venv/Scripts/python -m ...)`），在 macOS/uv 下永远匹配不上。替换为 `Bash(uv run ...)` 形式。
4. **[src/scraping/storage/database.py:110](src/scraping/storage/database.py#L110)** — `sqlite3.connect(self._db_path)` 没传 `timeout`，用的是 Python 默认 5s busy timeout。既然决定多 worktree **共享** `scraping.db`，并发写 parser 表时 5s 偏紧；改为 `timeout=30.0`，与 [src/search/db.py:51](src/search/db.py#L51) 已有的 30s 对齐。

---

## Phase 4 — worktree 初始化脚本

新建 `scripts/setup_worktree.sh`，把所有 per-worktree 步骤收敛到一条命令。核心逻辑：

```sh
ROOT="$(git rev-parse --show-toplevel)"          # worktree 自身路径（worktree-safe）
MAIN="$(git worktree list --porcelain | head -1 | cut -d' ' -f2)"   # 主 checkout

uv sync --group dev --group notebook             # 各自 .venv，硬链接自全局 cache
[ -e "$ROOT/.env" ]        || ln -s "$MAIN/.env" "$ROOT/.env"
[ -e "$ROOT/scraping.db" ] || ln -s "$MAIN/scraping.db" "$ROOT/scraping.db"
uv run python -m ipykernel install --user --name "cps-$(basename "$ROOT")" \
    --display-name "PriceScope ($(basename "$ROOT"))"
```

**要点**

- `scraping.db` symlink 后，SQLite 会 canonicalize 路径，WAL/SHM sidecar 落在主 checkout 旁边——这正是共享想要的。`search.db` **不**共享（每 worktree 的 trace 应各自独立）。
- **Jupyter kernel 必须按 worktree 命名。** 现有 7 个 notebook 的 kernelspec 全是 `name: "python3"` + `display_name: ".venv (3.12.x)"`；多 worktree 下 VS Code 的 picker 会出现 N 个同名 `.venv`，无法分辨。注册带 worktree 名的 kernel 解决。（notebook 里的 `kernelspec.name` 保持 `python3` 不动，由 picker 选择即可——改成固定 worktree 名反而会绑死。）
- 顺带修正：[src/scraping/playground.ipynb](src/scraping/playground.ipynb) 的 kernelspec 记录的是 Python **3.11.9**，与 `requires-python >=3.12` 冲突，是陈旧元数据。

---

## Phase 5 — 文档（项目强制「同 commit 更新 README」）

**只编辑 `CLAUDE.md`，不要手改 `AGENTS.md`** —— `.githooks/pre-commit` → `scripts/sync_agent_docs.py` 会自动镜像。同理 README 里 `<!-- BEGIN GENERATED -->` 区块由 `scripts/gen_capability_docs.py` 覆写，不要手改。

| 文件 | 改动 |
|---|---|
| [CLAUDE.md:17-20](CLAUDE.md#L17-L20) | setup 块 → `uv sync`；删掉 `py -3.12 -m venv` / `Activate.ps1` / `pip install` |
| [CLAUDE.md:140](CLAUDE.md#L140) | 「Dependencies managed via `requirements.txt` (pip freeze format)」→ 描述 `pyproject.toml` + `uv.lock` |
| [CLAUDE.md:161](CLAUDE.md#L161) | `python3 scripts/check_encoding.py --all` → `uv run python scripts/check_encoding.py --all` |
| [README.md:21](README.md#L21) | `pip install -r requirements.txt` → `uv sync`；新增 worktree 章节讲 `setup_worktree.sh` |
| [src/scraping/README.md:90-92](src/scraping/README.md#L90-L92) | 同上 |
| [src/search/README.md:32-35](src/search/README.md#L32-L35) | 同上 |
| [src/search/CLAUDE.md:113](src/search/CLAUDE.md#L113) | 「Deps … all in `requirements.txt`」→ `pyproject.toml` |
| [.claude/commands/write-readme.md:46](.claude/commands/write-readme.md#L46) | 提及 `requirements.txt` 的指令改为 `pyproject.toml` |

`.gitignore`：`.venv/`（L1）已覆盖，`uv.lock` 与 `.python-version` 均未被任何 pattern 命中（已用 `git check-ignore` 验证），会被正常追踪——**无需改动**。uv 默认用全局 cache `~/.cache/uv`，不产生仓库内缓存目录。

**不改**：`plans/` 与 `*/plans/` 下的历史文档（含 `src/scraping/scripts/plans/0807_revise_argos.md` 里硬编码的 `.venv/bin/python`），它们是历史记录不是维护对象。

---

## Verification

按顺序，每步都要过：

1. **干净重建**：`rm -rf .venv && uv sync --group dev --group notebook` —— 应秒级完成（走 cache 硬链接）。
2. **测试**：`uv run pytest` 全绿；`uv run pytest -m slow` 覆盖真实 sandbox 子进程（验证 `sys.executable` 在 uv env 下正确继承）。
3. **两个模块冒烟**：
   ```bash
   uv run python -m src.search.batch --input input/products.xlsx --sku-col product_name \
       --web-col web --country-col country --output output/results.xlsx
   uv run python -c "import asyncio; from src.scraping import scrape; \
       print(asyncio.run(scrape('https://www.argos.co.uk/product/3284476')))"
   ```
   scraping 那条会实打实验证 sandbox 子进程 + `lxml` + LLM parser 路径。
4. **pre-commit hook**：改一行 `CLAUDE.md` 后 `git commit`，确认 `AGENTS.md` 被同步、encoding check 通过。hook 用 `command -v python3` 探系统 python，且三个脚本（`sync_agent_docs.py` / `gen_capability_docs.py` / `check_encoding.py`）**均已核实为纯 stdlib**（`gen_capability_docs.py` 用 `ast` 解析而非 `yaml`），故 hook 无需改动、不受 uv 影响。它用 `git rev-parse --show-toplevel` 定位根目录，本身就是 worktree-safe 的。
5. **worktree 端到端**（真正的验收）：
   ```bash
   git worktree add ../cps-test -b test-uv
   cd ../cps-test && sh scripts/setup_worktree.sh
   uv run pytest
   ```
   确认：独立 `.venv` 建成、`.env` 与 `scraping.db` symlink 生效、测试通过。再在两个 worktree 里**同时**跑一次 scraping，观察共享 `scraping.db` 有无 `database is locked`。
6. **notebook**：在 VS Code 里打开 `src/search/script/searxng.ipynb`，选 `PriceScope (cps-test)` kernel，跑 cell 0，确认改过哨兵的 `find_repo_root()` 正常。

## Rollback

`.venv.bak` 保留到验收通过为止；`git checkout -- pyproject.toml && rm uv.lock .python-version && mv .venv.bak .venv` 即可完全回退。
