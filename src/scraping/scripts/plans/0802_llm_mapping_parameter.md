# LLM Provider 映射表 —— 让 repair ladder 支持 DeepSeek 等非 Qwen 模型

## Context

**问题**：`config.py:43-48` 的 `repair_model_ladder` 看起来像"只要改模型名就能换模型"，但实际上不行。当前所有 LLM 客户端都把 **模型名** 和 **认证/端点** 分开硬编码在三个地方：

| 位置 | 现状 |
|---|---|
| [agent.py:322-328](src/scraping/repair/agent.py#L322-L328) | `api_key=cfg.qwen_key`, `base_url=cfg.qwen_base_url`（DashScope 写死），model 来自 ladder |
| [json_healer.py:106-112](src/scraping/repair/json_healer.py#L106-L112) | 同上，且 **model 也写死** `"qwen3.7-plus"` |
| [coldstart.py:255-261](src/scraping/coldstart.py#L255-L261) | 同上，model 取 `ladder[0]` |

所以把 `repair_model_ladder` 改成 `["deepseek-v4-flash", ...]` 的实际效果是：把 DeepSeek 的模型名发到 DashScope 的端点、带着 QWEN_KEY → 直接 400/404 model not found。

另外两处 Qwen 专属耦合：
- [agent.py:319-320](src/scraping/repair/agent.py#L319-L320) `extra_body={"enable_thinking": True}` 是 DashScope 私有参数，DeepSeek 官方 API 不认（thinking 是靠选 reasoner 类模型，不是靠参数）。
- [api_scraper.py:106](src/scraping/scrapers/api_scraper.py#L106) `model_used="qwen-3.7-plus"` 是写死的日志字符串，换模型后 DB 里记录会说谎。

**目标**：按你的设想加一张 provider 映射表，之后"换模型 = 改 ladder 里的名字 + 往 .env 加一行 key"，不再动代码结构。已确认：走 DeepSeek 官方 API（`api.deepseek.com`），三个调用点全部统一。

## 维护契约（这个方案要保证的东西）

改完之后，日常维护只有三种动作，**都不需要读或改任何业务代码**：

| 场景 | 操作 | 涉及文件 |
|---|---|---|
| 换/加同厂商的模型 | 改 `repair_model_ladder` 的名字；若表里没有，往对应条目的 `models` 加一行 | `config.py` + `providers.py` |
| 接入全新厂商 | `PROVIDERS` 加一条 dict（base_url + key_name + models）+ `.env` 加一行 key | `providers.py` + `.env` |
| 调 ladder 层数/温度 | 改两个 list（长度必须一致，启动时已有校验） | `config.py` |

反过来说：**除了 `providers.py` 这一张表，代码里不会再有第二处出现厂商名、base_url 或 key 名**。这也是下面把 json_healer / coldstart / api_scraper 里那几处写死的 `"qwen3.7-plus"` 一并清掉的原因——留一处写死，将来换模型就一定会漏。

## 方案

### 1. 新文件 `src/scraping/providers.py` —— 唯一的模型注册表

```python
"""LLM provider registry — the single place to add a model/vendor.

Adding a model = one line in an existing entry's `models`.
Adding a vendor  = one dict entry + one line in .env.  No other code changes.
"""

@dataclass(frozen=True)
class ProviderSpec:
    base_url: str
    key_name: str                                  # .env 变量名
    models: tuple[str, ...]
    thinking_extra_body: dict | None = None        # None = 该厂商不支持参数式 thinking
    supports_json_object: bool = True              # response_format={"type":"json_object"}

PROVIDERS: dict[str, ProviderSpec] = {
    "qwen": ProviderSpec(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_name="QWEN_KEY",
        models=("qwen3.7-plus", "qwen-3.7-plus", "qwen3.7-flash", ...),
        thinking_extra_body={"enable_thinking": True},
    ),
    "deepseek": ProviderSpec(
        base_url="https://api.deepseek.com/v1",
        key_name="DEEPSEEK_KEY",
        models=("deepseek-v4-flash", "deepseek-v4-pro", ...),
        thinking_extra_body=None,                  # 见下方 thinking 降级规则
    ),
}

DEFAULT_PROVIDER = "qwen"   # 未知模型名的回退，保持向后兼容
```

解析函数（模型名 → provider，两种写法都吃）：

```python
def resolve_provider(model: str) -> tuple[str, ProviderSpec]:
    """'deepseek/deepseek-v4-pro' 显式前缀优先；否则反查 models 表；
    都没命中 → DEFAULT_PROVIDER + WARN（不抛异常）。返回 (裸模型名, spec)。"""
```

> **不做启动期硬校验**：`tests/verify_m17.py:263` 用 `repair_model_ladder=["configured-coldstart-model"]` 这类假模型名跑离线测试，硬校验会把它打挂。未知名回退到 qwen + 一条 WARN 即可。

### 2. `config.py` 加 key 解析入口（唯一的结构性改动，约 10 行）

`ScrapingConfig` 用 pydantic-settings 从 `.env` 读值，**但 `.env` 的变量不会进 `os.environ`**，所以 providers.py 里不能直接 `os.getenv("DEEPSEEK_KEY")`。在 `ScrapingConfig` 上加一个方法作为唯一取 key 的通道：

```python
def api_key_for(self, key_name: str) -> str:
    """Resolve a provider key by .env name. os.environ first, then the
    configured env_file (parsed once, cached). Honors the SCRAPING_ prefix
    convention already used by AliasChoices above."""
    # os.environ[SCRAPING_X] → os.environ[X] → dotenv_values(.env)[SCRAPING_X] → [X]
```

`python-dotenv` 已在 `requirements.txt:13`，直接用 `dotenv_values`。
`qwen_key` / `qwen_base_url` 两个现有字段**保留不删**（向后兼容 + 未知模型回退路径继续能用）。

### 3. 三个调用点收敛到一个工厂

在 `providers.py` 里放统一工厂，agent / json_healer / coldstart 都调它：

```python
def make_chat_client(model: str, temperature: float = 0.1,
                     enable_thinking: bool = False, *, purpose: str = ""):
    """Build a LangChain ChatOpenAI for any registered model.
    Returns None (with a WARN) when langchain_openai is missing or the
    provider's key is unset — preserves today's graceful-degradation contract
    that verify_m8.py asserts on."""
```

- **thinking 降级规则**：`enable_thinking=True` 且 `spec.thinking_extra_body is None` → 不注入任何 extra_body，只打一条 DEBUG（"provider X has no param-level thinking; select a reasoning model in the ladder instead"）。ladder 最后一档想要推理能力时，做法变成在 `repair_model_ladder` 末位直接填推理型模型名，而不是靠参数。
- `response_format={"type":"json_object"}` 按 `spec.supports_json_object` 决定是否注入（DeepSeek 支持，保留 True）。

改造点：
- [agent.py:296-328](src/scraping/repair/agent.py#L296-L328) `_make_llm` 变成薄封装转调 `make_chat_client`（**函数名和签名保持不变**，`verify_m8.py:70,83-87` 直接 import 它）。
- [json_healer.py:94-112](src/scraping/repair/json_healer.py#L94-L112) 删掉写死的 `"qwen3.7-plus"`，改用 `cfg.repair_model_ladder[0]`（和 coldstart 已有的做法一致）。
- [coldstart.py:255-261](src/scraping/coldstart.py#L255-L261) 换成 `make_chat_client(cfg.repair_model_ladder[0], 0.1)`。
- [api_scraper.py:106](src/scraping/scrapers/api_scraper.py#L106) `model_used="qwen-3.7-plus"` → `model_used=cfg.repair_model_ladder[0]`，让 DB 记录真实模型。

### 4. 配置与文档

- `.env.sample` 加 `DEEPSEEK_KEY = use your deepseek key`。
- `config.py:40-48` 的注释补一句：模型名从 `providers.py` 的 `PROVIDERS` 表里选；换厂商只需改名字 + 配 key。
- `src/scraping/CLAUDE.md`：Key Config 段落 + External Dependencies 段落说明 provider 表的存在（`AGENTS.md` 由 pre-commit hook 自动同步，只需改一个）。

### 换模型后的操作（最终形态）

```yaml
# config.py 或环境变量
repair_model_ladder = ["deepseek-v4-flash", "deepseek-v4-pro"]
repair_temperature_ladder = [0.1, 0.4]
# .env
DEEPSEEK_KEY = sk-...
```
就这两处，没有别的。

## 待落地时确认

`deepseek-v4-flash` / `deepseek-v4-pro` 的**准确模型 ID 和 base_url** 以 DeepSeek 官方文档为准（我不逐字保证这两个名字与厂商当前 API 一致）。实现时先用第 5 节的连通性冒烟脚本打一次真实请求确认，再把确认过的名字写进 `PROVIDERS["deepseek"].models`。

## 5. 验证（遵守模块的 Verification Discipline）

新增 `src/scraping/tests/verify_m18.py` + `verify_m18_output.log`，并更新 `tests/README.md` 表格。离线检查项：

1. `resolve_provider("deepseek-v4-pro")` → `("deepseek-v4-pro", PROVIDERS["deepseek"])`
2. `resolve_provider("deepseek/deepseek-v4-pro")` → 前缀被剥离，命中 deepseek
3. `resolve_provider("qwen3.7-plus")` → qwen；`resolve_provider("configured-coldstart-model")` → 回退 qwen 且不抛异常（护住 verify_m17）
4. `make_chat_client("deepseek-v4-flash")` 在 DEEPSEEK_KEY 存在时返回 client，`base_url` 为 deepseek 端点、key 非 qwen_key；key 缺失时返回 `None`
5. `make_chat_client(..., enable_thinking=True)` 对 deepseek **不** 注入 `enable_thinking`；对 qwen 注入
6. `cfg.api_key_for("DEEPSEEK_KEY")` 能读到只写在 `.env`（未导出到 os.environ）的值
7. `json_healer._make_llm()` 使用的 model == `cfg.repair_model_ladder[0]`（不再写死 qwen）

回归：重跑 `verify_m8.py`（`_make_llm` 契约）、`verify_m17.py`（假模型名 ladder）、`verify_m14/m15.py`（无 LLM 依赖，确保未误伤）。

真实连通性冒烟（需 DEEPSEEK_KEY）：
```bash
SCRAPING_REPAIR_MODEL_LADDER='["deepseek-v4-flash","deepseek-v4-pro"]' \
  python -m src.scraping.coldstart --site tesco --input src/scraping/data/cold_start/tesco.xlsx
```
确认日志里 provider=deepseek、请求打到 api.deepseek.com、能生成出可过 sandbox+gates 的 parser。
