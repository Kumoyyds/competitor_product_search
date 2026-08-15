# SearXNG 可用性验证 notebook

## Context

项目当前的搜索层支持 DuckDuckGo（免费、有速率限制）和 Serper（付费）两个 provider，`search_config.yaml` 里以 `provider: [duckduckgo, serper]` 的链式降级方式使用。README 里把 exa / tavily / brave 列为候选，但没人评估过 SearXNG——一个自建的元搜索聚合器，理论上能免费聚合 Google/Bing 结果而不受单一引擎的速率限制。

在写任何 provider 代码之前，需要先回答：**SearXNG 对本项目到底可不可用**。可用性的判定标准由现有 provider 契约决定：

- 能否稳定返回 `title / url / snippet` 三元组（`RawCandidate`，[models.py:49-53](src/search/models.py#L49-L53)）
- 能否按国家定向（架构强制要求每个 provider 有 `_COUNTRY_TO_*` 映射，见 [duckduckgo.py:16-44](src/search/providers/duckduckgo.py#L16-L44)）—— 这是本项目的**核心难点**，因为 domain_filter 层要求结果落在 `tesco.com` / `amazon.de` 这类本地站点上
- 速率限制是否比 DuckDuckGo 的 1s/call 更宽松
- 结果质量相对 DuckDuckGo 是否有增量

交付物只有一个验证 notebook，**不改动任何生产代码**。

### 已完成的前置调研（结论已确定，写进 notebook 作为基线）

实测了 17 个公共 SearXNG 实例的 `?format=json`：

| 结果 | 实例 |
|---|---|
| HTTP 429 限流 | priv.au, search.inetol.net, opnxng.com, search.rhscz.eu, searx.tiekoetter.com, paulgo.io, searx.perennialte.ch |
| HTTP 200 但返回反爬挑战页（Anubis / captcha），非 JSON | searx.be, baresearch.org, search.hbubli.cc, search.disroot.org |
| 403 / 502 / 连不上 / 已关停 | searxng.site, copp.gg, northboot.xyz, searxng.world, search.bus-hit.me, search.projectsegfau.lt |

**可用率 0/17。** SearXNG 官方 API 文档亦明确：`format=json` 默认关闭，未开启时返回 403，"many public instances have these formats disabled"。

→ **结论：验证必须基于自建实例。** 本机已装 Docker Desktop 27.4.0（daemon 当前未启动），因此走 Docker 路线；notebook 只打印命令，由你手动执行，不代你动 Docker。

## 轻量性保证

- **零 pip 安装**：只用 `httpx`（venv 已有，`ddgs` 的依赖）+ 标准库。notebook 里不出现任何 `pip install`。
- **不碰生产代码**：不改 `providers/`、`search_config.yaml`、`requirements.txt`。只读 `src.search.models.RawCandidate` 用于格式验证。
- **不代跑 Docker**：notebook 只 `print()` 命令；容器用 `--rm`，停止即自动删除，不留镜像以外的残留。
- **生成文件集中一处**：只写 `src/search/script/.searxng/settings.yml`，随时可删。

## 要创建/修改的文件

| 文件 | 动作 |
|---|---|
| `src/search/script/searxng.ipynb` | **新建**（主交付物） |
| `.gitignore` | 追加一行 `.searxng/`（避免生成的 settings.yml 进版本库） |

## Notebook 结构

对齐 [duckduckgo.ipynb](src/search/script/duckduckgo.ipynb) 的 spike 风格：全同步、无 async、`print()` 输出、cell 有可读 id、末尾 markdown 结论模板。共 9 个 code cell + 1 个 markdown。

**cell 0 `setup`**
- repo root 用向上查找 `requirements.txt` 定位（duckduckgo.ipynb 里 `os.path.join(os.getcwd(), "..", "..")` 实际解析到 `src/` 而非 repo root，是个隐患，新 notebook 不复制这个写法）
- `import httpx`，失败则报错提示（不自动安装）
- `SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")`
- 定义两个贯穿全篇的 helper：
  - `sx_search(q, *, language=None, k=10, engines=None, timeout=20) -> tuple[list[dict], dict]` —— GET `{SEARXNG_URL}/search`，`format=json`，返回 `(results, meta)`，meta 含耗时与 `unresponsive_engines`
  - `sx_up() -> bool` —— 健康探测，后续每个 cell 开头 `if not sx_up(): print("实例未启动，跳过"); ...` 保证任何 cell 单独跑都不会炸

**cell 1 `public_instances`** —— 复现上面那张表：遍历 17 个实例，打印 `host | HTTP code | JSON?`。让结论可复核而不是听我一面之词。

**cell 2 `docker_setup`** —— 写 `.searxng/settings.yml`：
```yaml
use_default_settings: true
server:
  secret_key: "searxng-feasibility-spike"
  limiter: false          # 关掉 bot 检测，否则 notebook 的请求会被自家实例挡掉
  public_instance: false
  image_proxy: false
search:
  formats: [html, json]   # 默认只有 html；不加这行 format=json 返回 403
  safe_search: 0
```
然后打印（不执行）：
```bash
open -a Docker          # daemon 当前未启动
docker run --rm -d --name searxng-spike -p 8888:8080 \
  -v "<repo>/src/search/script/.searxng:/etc/searxng" \
  docker.io/searxng/searxng:latest
# 用完：docker stop searxng-spike   （--rm 会自动删容器）
```
末尾调 `sx_up()` 告知当前是否已就绪。

**cell 3 `basic_feasibility`** —— 与 duckduckgo.ipynb cell 1 用**同一条 query**（`"Magic Rock Saucery 4 X 330ML Tesco"`）便于横向对比：打印原始 JSON 字段名、耗时、逐条 title/url/content，映射成 `RawCandidate`，统计 `tesco.com` 命中数。这一步同时验证字段契约（预期 `url/title/content` → `url/title/snippet`）。

**cell 4 `format_compatibility`** —— 沿用 duckduckgo.ipynb cell 2 的 5 组 `(query, category)`：统计结果数、缺 url/title 的条数、netloc 分布，确认无需额外清洗即可喂给 `layers/search.py`。

**cell 5 `country_test`（本次验证的关键 cell）** —— SearXNG **没有** Serper 的 `gl` 或 DDG 的 `region` 这种国家参数，最接近的只有 `language`。所以要实证回答"能不能做国家定向"：
- 提出候选映射 `_COUNTRY_TO_LANGUAGE = {"uk": "en-GB", "de": "de-DE", "fr": "fr-FR", "us": "en-US", ...}`，键与现有两个 provider 的 15 个国家码对齐
- 对 uk/de/fr/us 各跑一条本地零售商 query（如 `"Nescafe Gold Tesco"` / `"Nescafe Gold Amazon"`），打印各自的 TLD 分布与域名集合
- **对照组**：同一 query 在 `language=` 有/无 两种情况下的结果差异，判断 `language` 是否真的影响地域倾向，还是只影响界面语言
- 输出明确结论：能定向 / 只能靠 query 里的零售商关键词兜底 / 完全不能

**cell 6 `rate_limiting`** —— 复用 duckduckgo.ipynb cell 5 的 `test_rate(delay_s, n) -> dict` 同名同返回结构，0s / 1s / 2s × 15 次。额外统计每次的 `unresponsive_engines`——本地实例本身不限流，真正的瓶颈是上游 Google/Bing 把容器 IP 封掉，这才是决定"能不能跑批量"的指标。

**cell 7 `edge_cases`** —— 沿用 duckduckgo.ipynb cell 8 的 5 个用例：超短 query、德语变音、法语重音、乱码、通用商品名。

**cell 8 `vs_duckduckgo`** —— 同一批 query 分别走 SearXNG 与 `ddgs`，对比结果数、URL 重合率、各自独有 URL、目标域名命中数、平均耗时。这是"值不值得加这个 provider"的直接证据。

**cell 9 `summary`（markdown）** —— 填空式结论模板 + checkbox 判定（可用 / 有条件可用 / 不可用），并预留一节记录：若要落地成正式 provider 还差什么（`_COUNTRY_TO_LANGUAGE` 映射、是否需要 `engines=` 参数固定上游、自建实例的部署与运维成本、`aiohttp` 异步改写）。公共实例那节结论直接写死（已实测）。

## 验证方式

1. `open -a Docker` 等 daemon 起来，粘贴 cell 2 打印的 `docker run` 命令
2. `curl "http://localhost:8888/search?q=test&format=json" | head -c 200` 应返回 `{"query": "test", ...}` 而非 HTML —— 若返回 403 说明 settings.yml 没挂上
3. notebook 从上到下执行；cell 1 和 cell 2 在没有实例时也应正常出结果，cell 3-8 无实例时打印跳过提示而不抛异常
4. `docker stop searxng-spike` 收尾，确认 `docker ps` 干净

## 明确不做

不新建 `src/search/providers/searxng.py`，不改 `make_provider` / `search_config.yaml` / `requirements.txt`。是否落地成正式 provider，由这个 notebook 的结论决定，下一轮再谈。
