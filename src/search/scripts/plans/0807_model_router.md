# LLM 路由表：给一个模型名，自动解析 base_url + key

## Context

现在换 LLM 的成本不对称：

- 换 DashScope 上的另一个模型 → 只改 `search_config.yaml` 的 `llm.model` 一行就行
- 换到别家 endpoint（DeepSeek 官方、OpenAI、本地 vLLM）→ 得同时改 `llm.base_url`，
  而且 API key 的环境变量名 `QWEN_KEY` 硬编码在
  `src/search/layers/distinguishing.py:88`，只能把别家的 key 塞进名叫 `QWEN_KEY` 的
  变量里，或者改代码

另外 `search_config.yaml:122` 的 `llm.provider: qwen` 是死配置——全仓库没有代码读它，
注释还写着 "currently only qwen is wired up"，会误导维护者以为改它能切供应商。

目标：新增 `maintain/llm_routes.yaml` 路由表，把「模型名 → base_url + key 环境变量名」
的对应关系集中存放。之后 **换任何模型、任何供应商都只改 `llm.model` 一行**；
只有接入全新一家厂商时才需要往路由表里加一个 provider 块。

## 改动

### 1. 新增 `src/search/maintain/llm_routes.yaml`

两层结构：`providers` 定义 endpoint，`models` 把模型挂到 endpoint 上。
同一家的新模型只加一行，base_url 不重复，改 endpoint 时不会漏改。

```yaml
# ============================================================
# LLM 路由表 —— 模型名 → endpoint + API key 环境变量名
# search_config.yaml 的 llm.model 在这里查表解析。
# 换模型 = 只改 llm.model；接新厂商 = 这里加 provider 块 + models 一行。
# api_key_env 存的是 .env 里的【变量名】，不是 key 本身。
# ============================================================

providers:
  dashscope:
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key_env: QWEN_KEY
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_KEY

models:
  qwen-flash:        dashscope
  qwen-plus:         dashscope
  qwen-max:          dashscope
  deepseek-chat:     deepseek
  deepseek-reasoner: deepseek
```

**注意**：初始只填我能确认存在的 model id。百炼上托管的 deepseek（`deepseek-v3.1` 等）
和你提到的 `deepseek-v4-flash`，要先去对应控制台确认真实 id 再加行——路由表只做映射，
不校验模型是否存在，写错要到运行时才报 model not found。

同一个模型名在两家都有（deepseek 既在百炼也在官方）时，`models` 里挂哪家就走哪家；
想同时用就起两个 key，如 `deepseek-chat` 和 `deepseek-chat@dashscope`。

### 2. `src/search/config.py` —— 加解析函数

复用现有 `lru_cache` + `yaml.safe_load` 的写法（和 `load_config()` 同款），
放在 `domain_for()` / `retailer_keyword_for()` 这批 helper 旁边：

```python
_ROUTES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "maintain", "llm_routes.yaml")

@lru_cache(maxsize=1)
def load_llm_routes() -> dict[str, Any]: ...

@lru_cache(maxsize=None)
def resolve_llm(model: str) -> tuple[str, str]:
    """model → (base_url, api_key_env)。search_config.yaml 里的显式值优先。"""
```

优先级：`search_config.yaml` 的 `llm.base_url` / `llm.api_key_env`（若存在）
> 路由表。这样临时调试可以绕过路由表，且旧配置文件不改也能跑。

报错要能自解释，这是这次改动的主要收益：

- 模型不在表里 → `Unknown LLM model 'deepseek-v4-flash'. Add it under models: in
  src/search/maintain/llm_routes.yaml. Known models: qwen-flash, qwen-plus, ...`
- provider 名拼错 → `Model 'x' points at provider 'deepsek' which is not defined
  under providers: in llm_routes.yaml`

### 3. `src/search/maintain/search_config.yaml` —— 精简 `llm` 段

- 删 `provider: qwen`（死配置）
- 删 `base_url`（移到路由表；保留注释说明可作为临时覆盖）
- `model` 的注释改成：base_url 和 key 由 `maintain/llm_routes.yaml` 按此名路由
- `temperature` / `timeout_s` 不动

### 4. `src/search/layers/distinguishing.py::_get_llm()`

```python
model = config.get("llm", "model")
base_url, key_env = config.resolve_llm(model)
api_key = os.getenv(key_env)
if not api_key:
    raise RuntimeError(
        f"{key_env} not set in .env — required by llm.model={model!r} "
        f"(see src/search/maintain/llm_routes.yaml)"
    )
return ChatOpenAI(api_key=api_key, base_url=base_url, model=model, ...)
```

函数签名不变，现有 4 处 `patch("...distinguishing._get_llm")` 的测试不受影响。

### 5. 文档（CLAUDE.md 明确要求：配置键变更必须同步 README）

- **`src/search/README.md`**
  - §5 "Files to maintain" 表加一行 `maintain/llm_routes.yaml`：什么时候改、
    api_key_env 存的是变量名不是 key、改完要重启
  - "Common maintenance tasks" 表：`Switch LLM model` 一行改为「改 `llm.model`；
    模型不在路由表里就先去 `llm_routes.yaml` 加一行」，再加一行
    `Switch LLM provider`（加 provider 块 + `.env` 补 key）
  - §Environment：`QWEN_KEY` 说明改为「由路由表 `api_key_env` 决定，当前 Qwen 走 `QWEN_KEY`」
  - §132 `maintain/search_config.yaml` 那格里删掉已迁走的 `base_url`
- **`src/search/CLAUDE.md`**：File map 加 `maintain/llm_routes.yaml` 行；
  "Config knobs" 表的 `llm` 行改成 `model` / `temperature` / `timeout_s` +
  指向路由表；"Environment" 段同步；"Adding things" 加一条 **New LLM model / provider**。
  ⚠️ `AGENTS.md` 由 pre-commit hook 自动同步，**不要手改**。
- **`.env.sample`**：`QWEN_KEY` 下加注释说明变量名由路由表决定；把 `DEEPSEEK_KEY`
  作为注释行放进去当例子

## 不做的事

- 不引入 provider 工厂/抽象类。候选供应商全是 OpenAI 兼容协议，
  `ChatOpenAI` + base_url 已经够用。
- 路由表只服务 `src/search/`。若以后 `src/scraping/` 也要用 LLM，
  再把它提到共享位置（目前全仓库只有 `distinguishing.py` 一处构造 LLM）。
- 不动 `search.provider`（搜索引擎链），那是另一回事。

## 验证

1. 新增 `tests/unit/search/test_llm_routes.py`（离线、零 API 成本）：
   - 已知模型 → 正确的 `(base_url, api_key_env)`
   - 未知模型 → 抛错且错误信息里列出了已知模型名
   - provider 名拼错 → 抛对应的错
   - `search_config.yaml` 显式 `base_url` 覆盖路由表
2. 回归：`python -m pytest tests/unit/search/ -v` 全绿
3. 真跑一次确认线上通路没坏：
   `python scripts/validate_search.py --sample 3 --budget 10`
4. 换模型冒烟：把 `llm.model` 改成路由表里另一个 DashScope 模型（如 `qwen-plus`），
   重跑第 3 步，确认走通
5. 反例：把 `llm.model` 改成 `deepseek-v4-flash`（不在表里），确认报的是
   那条「Add it under models: in llm_routes.yaml」而不是裸的 KeyError
6. `load_config()` / `load_llm_routes()` / `resolve_llm()` 都带 `lru_cache`，
   **每次改 yaml 后必须重开进程**；`python run.py` 每次新进程，自动生效
