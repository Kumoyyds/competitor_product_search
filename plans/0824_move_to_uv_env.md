# 迁移到 uv 管理环境

## Context

当前依赖管理已经失效，不是「想用新工具」而是「现在这套是坏的」。实测（2026-08-24）：

- **`pyproject.toml` 里没有任何 `dependencies`** —— 依赖只存在于 `requirements.txt`，而 `pyproject.toml` 又声明了 `[build-system]` + setuptools，与「`src/` 是 implicit namespace package、无 `src/__init__.py`」的约定互相矛盾。
- **`requirements.txt` 与实际 `.venv` 已严重脱节**：125 条 pin vs 113 个实装包；**59 个版本漂移**（其中 6 个跨大版本：`pandas` 声明 `2.3.2` 实装 `3.0.3`、`ipykernel` 6→7、`packaging` 25→26 等）；**19 个包 pin 了但根本没装**（`scikit-learn`/`scipy`/`joblib`/`sympy` 等）；**7 个装了但没 pin**（`langchain-classic`、`langchain-text-splitters`、`sqlalchemy` 等）。
- **`pywin32==312` 是 Windows-only**，在 macOS 上 `pip install -r requirements.txt` 会直接失败。

也就是说：**照 README 跑 `pip install -r requirements.txt` 建出的环境，与实际在跑的开发环境不是一个东西，而且在本机根本建不起来。** 迁移后单一事实来源是 `pyproject.toml` + `uv.lock`，任何机器 `uv sync` 得到字节一致的依赖集。

### 已确认的决策

| 决策点 | 选择 |
|---|---|
| 范围 | **只做 uv 迁移**；多 worktree 隔离（`setup_worktree.sh`、symlink、per-worktree kernel）留到真正需要时再做 |
| 锁定基线 | **以现装 `.venv` 为准** —— 先抓 freeze 基线，lock 后逐项 diff，跨大版本差异用下界约束拉回 |
| `requirements.txt` | **删除** |
| `quantulum3` | 只声明 `quantulum3`，**不加** `[classifier]` extra —— 锁住当前实际在跑的无分类器行为 |

---

## 风险评估

按「会不会真的咬人」排序，每条都给了处置。

| # | 风险 | 判断 | 处置 |
|---|---|---|---|
| 1 | **现装 `.venv` 是不可复现的雪花环境**（anaconda 3.12.4 + 长期演化）。一旦删掉，唯一可信基线就永久丢失 | **高**，但完全可防 | Phase 0 先 `uv pip freeze` 存盘 + `mv .venv .venv.bak`，验收通过前不删 |
| 2 | **`uv sync` 默认会 build 本项目**，撞上 `src/` 无 `__init__.py` 的 namespace 布局，会失败或产出空 wheel | **高**，必踩 | `[tool.uv] package = false`，并删掉 `[build-system]` / `[tool.setuptools.*]`。项目本来就是从 repo root `python -m src.xxx` 跑，不需要被安装 |
| 3 | **隐式依赖 AST 扫不出来，漏声明会在运行时才炸**：`lxml`（`detection.py:73` 用字符串 `BeautifulSoup(html,"lxml")`，且在 sandbox import 白名单 `config.py:83` 里，LLM 生成的 parser 会 `import lxml`）；`pytest-asyncio`（`asyncio_mode="auto"` 依赖它，无 import 语句） | **高** | 两个都手工写进依赖清单，已核实无第三处 |
| 4 | **解释器从 anaconda CPython 换成 uv 管理的 CPython**。scraping 的 sandbox 用 `sys.executable` 拉子进程（`repair/sandbox.py:172`），继承新解释器 | 中 | `uv run pytest -m slow` 专门覆盖真实 sandbox 子进程；再跑一次 scraping 冒烟。包全部来自 PyPI wheel，与 anaconda 无绑定 |
| 5 | **`uv` 0.7.2（2025-04）过老**，`dependency-groups` / `uv sync` 语义需要新版 | 中 | Phase 0 先 `uv self update` |
| 6 | **删 `requirements.txt` 会打破 `searxng.ipynb`** —— `find_repo_root()` 拿它当仓库根哨兵，文件没了直接 `RuntimeError` | 中，确定会发生 | 同批把哨兵换成 `pyproject.toml` |
| 7 | **`duckduckgo.ipynb` cell 0 有运行时 `pip install ddgs`** —— 全仓唯一的带外安装，会污染 uv 同步出的环境 | 中 | `ddgs` 已是声明依赖，删掉 try/except 改裸 import |
| 8 | 依赖清单从 126 行缩到 16 个直接依赖，可能漏掉某个真正被 import 的包 | 低 | 已用 AST 扫全仓 `.py` + 7 个 `.ipynb` 交叉验证；`langchain`/`langchain-core`/`openai`/`numpy`/`scipy`/`tiktoken`/`tenacity` 等**零直接 import**，全由 `langchain-openai`/`langgraph`/`pandas` 传递带入，仍会出现在 `uv.lock` 里 |
| 9 | pre-commit hook 会不会挂 | **无风险，已验证** | 四个 hook 脚本全部 stdlib-only（`gen_capability_docs.py` 的 `yaml` 是 try/except 软导入），在 `/usr/bin/python3` 和 anaconda python 下都跑通。hook 用 `git rev-parse --show-toplevel` 定位根目录，与 uv 无关 |
| 10 | `.gitignore` 会不会误吞 `uv.lock` / `.python-version` | **无风险，已验证** | `git check-ignore` 确认两者均未被任何 pattern 命中，会被正常追踪。uv 用全局 cache `~/.cache/uv`（现 3.6G），不产生仓库内缓存目录 |
| 11 | Windows 协作者 | 低 | uv 在 Windows 一等支持；`pywin32` 本来就没被 import，删掉反而修好了 macOS 上的安装 |

**磁盘**：现 `.venv` 432M，uv cache 已有 3.6G，重建走硬链接，秒级完成、增量接近 0。

---

## Phase 0 — 基线与退路

```bash
uv self update
uv pip freeze --python .venv/bin/python > /tmp/baseline-freeze.txt   # pip 不在 .venv 里，用 uv
mv .venv .venv.bak                                                   # 秒级回退用
```

`/tmp/baseline-freeze.txt` 是后面 diff 的靶子，**验收通过前不要删 `.venv.bak`**。

---

## Phase 1 — 重写 `pyproject.toml`

改 [pyproject.toml](pyproject.toml)。版本下界取自当前实装版本（决策：以现装为准），大版本用 `>=`，不写精确 pin —— 精确版本是 `uv.lock` 的职责，pin 在两处会打架。

```toml
[project]
name = "pricescope"
version = "0.1.0"
description = "Find competitor product URLs on marketplaces using LLM-powered search"
requires-python = ">=3.12,<3.13"     # 收紧：原 ">=3.12" 会让 resolver 选到 3.13/3.14
readme = "README.md"
dependencies = [
    "aiohttp>=3.14",
    "beautifulsoup4>=4.15",
    "ddgs>=9.14",
    "httpx>=0.28",
    "langchain-openai>=1.4",
    "langgraph>=1.2",
    "lxml>=6.1",              # 无 import 语句：见风险 #3
    "openpyxl>=3.1",
    "pandas>=3.0",            # 关键下界：requirements.txt 停留在 2.x，实装已 3.0.3
    "pydantic>=2.13",
    "pydantic-settings>=2.14",
    "python-dotenv>=1.2",
    "PyYAML>=6.0",
    "quantulum3>=0.10",       # 不加 [classifier] extra
    "rapidfuzz>=3.14",
    "tqdm>=4.69",
]

[dependency-groups]
dev = ["pytest>=9.1", "pytest-asyncio>=1.3"]      # pytest-asyncio 无 import：见风险 #3
notebook = ["ipykernel>=7.3", "ipython>=9.16", "requests>=2.34"]

[tool.uv]
package = false      # 见风险 #2

# 整段删除：[build-system] 与 [tool.setuptools.packages.find]
# 原样保留：[tool.pytest.ini_options]
```

然后 `uv python pin 3.12`（生成 `.python-version`，提交）。

---

## Phase 2 — 生成 lock 并比对基线

```bash
uv lock
uv sync --group dev --group notebook
uv pip freeze > /tmp/after-freeze.txt
diff <(sort /tmp/baseline-freeze.txt) <(sort /tmp/after-freeze.txt)
```

审 diff 的规则：**补丁级/次版本差异忽略；跨大版本差异逐个看**。若某包 resolve 出的大版本与基线不同，在 `dependencies` 里给它补下界约束（如已经给 `pandas>=3.0` 做的那样），**不要**精确 pin。基线里的传递依赖（`langchain-classic`、`sqlalchemy`、`soupsieve` 等）应当照样出现在 lock 里；若某个消失了，说明它其实是被 pip 手工装进去的孤儿，确认无 import 后可以放掉。

---

## Phase 3 — 修掉迁移会打破的东西

1. **删除 `requirements.txt`**。
2. **[src/search/script/searxng.ipynb](src/search/script/searxng.ipynb) cell 0** — `find_repo_root()` 的哨兵 `requirements.txt` → `pyproject.toml`。同 cell 还手工把 `.venv/Lib/site-packages` 和 `.venv/lib/pythonX.Y/site-packages` 塞进 `sys.path` 当 `httpx` 的 fallback，一并删掉这段脆弱逻辑。
3. **[src/search/script/duckduckgo.ipynb](src/search/script/duckduckgo.ipynb) cell 0** — 删掉 `subprocess.check_call([sys.executable,"-m","pip","install","ddgs"])` 的 try/except，改成裸 `from ddgs import DDGS`。同 cell 的 `PROJECT_ROOT = os.getcwd()/../..` 解析到 `src/` 而非 repo root，是既有隐患，顺手修成与 `searxng.ipynb` 一致的哨兵查找。
4. **[.claude/settings.local.json](.claude/settings.local.json)** — 整个 allowlist 是 Windows 时代死条目（`PowerShell(.\\.venv\\Scripts\\Activate.ps1)`、`Bash(... .venv/Scripts/python -m ...)`），在 macOS/uv 下永远匹配不上。替换为 `Bash(uv run ...)` 形式。
5. **[src/scraping/playground.ipynb](src/scraping/playground.ipynb)** — kernelspec 记录的是 Python **3.11.9**，与 `requires-python >=3.12` 冲突，是陈旧元数据，清掉。

---

## Phase 4 — 文档（项目强制「同 commit 更新 README」）

**只编辑 `CLAUDE.md`，不要手改 `AGENTS.md`** —— pre-commit 的 `scripts/sync_agent_docs.py` 会自动镜像。README 里 `<!-- BEGIN GENERATED -->` 区块由 `scripts/gen_capability_docs.py` 覆写，也不要手改。

| 文件 | 改动 |
|---|---|
| [CLAUDE.md:17-20](CLAUDE.md#L17-L20) | setup 块 → `uv sync --group dev --group notebook`；删掉 `py -3.12 -m venv` / `Activate.ps1` / `pip install` |
| [CLAUDE.md:140](CLAUDE.md#L140) | 「Dependencies managed via `requirements.txt` (pip freeze format)」→ `pyproject.toml` 声明 + `uv.lock` 锁定，新增依赖用 `uv add` |
| [CLAUDE.md:161](CLAUDE.md#L161) | `python3 scripts/check_encoding.py --all` → `uv run python scripts/check_encoding.py --all` |
| [README.md:31-34](README.md#L31-L34) | 三行 venv+pip → `uv sync --group dev --group notebook`；补一句 uv 的安装方式 |
| [src/scraping/README.md:90-92](src/scraping/README.md#L90-L92) | 同上 |
| [src/search/README.md:33-35](src/search/README.md#L33-L35) | 同上 |
| [src/search/CLAUDE.md:113](src/search/CLAUDE.md#L113) | 「Deps … all in `requirements.txt`」→ `pyproject.toml` |
| [.claude/commands/write-readme.md:46](.claude/commands/write-readme.md#L46) | 提及 `requirements.txt` 的指令改为 `pyproject.toml` |

**不改**：`plans/` 与 `*/plans/` 下的历史文档（含硬编码 `.venv/bin/python` 的 `0807_revise_argos.md`），它们是历史记录不是维护对象。`.gitignore` 无需改动（风险 #10）。

---

## Verification

按顺序，每步都要过：

1. **干净重建**：`rm -rf .venv && uv sync --group dev --group notebook` —— 应秒级完成（走 cache 硬链接）。
2. **测试**：`uv run pytest` 全绿（默认 `-m 'not live'`）；`uv run pytest -m slow` 覆盖真实 sandbox 子进程，验证 `sys.executable` 在 uv 环境下正确继承（风险 #4 的正面验证）。
3. **两个模块冒烟**：
   ```bash
   uv run python -m src.search.batch --input input/products.xlsx --sku-col product_name \
       --web-col web --country-col country --output output/results.xlsx
   uv run python -c "import asyncio; from src.scraping import scrape; \
       print(asyncio.run(scrape('https://www.argos.co.uk/product/3284476')))"
   ```
   第二条实打实验证 sandbox 子进程 + `lxml` + LLM parser 路径。
4. **pre-commit hook**：改一行 `CLAUDE.md` 后 `git commit`，确认 `AGENTS.md` 被同步、四个生成/检查脚本全过。
5. **notebook**：VS Code 里打开 `searxng.ipynb` 和 `duckduckgo.ipynb`，选新 `.venv` kernel，跑 cell 0，确认改过的哨兵与裸 import 正常。
6. **确认 `uv.lock` / `.python-version` 已被 git 追踪**（`git status` 里应出现，不在 ignore 列表）。

## Rollback

`.venv.bak` 保留到验收通过为止：

```bash
git checkout -- pyproject.toml && rm -f uv.lock .python-version && rm -rf .venv && mv .venv.bak .venv
git checkout -- requirements.txt   # 若已删除
```
