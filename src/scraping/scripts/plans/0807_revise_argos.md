# 修复 Argos 运行时 scrape 失败 + 引入站点档案（M23）

## Context

Argos cold start 已通过全部 6 条 gold set 用例并写入 parser `cs_20260807_194728` (id=5)，
但 `playground.ipynb` 中 `await scrape("https://www.argos.co.uk/product/tuc143428469")`
长时间无返回且最终失败。

用仓库内 16 条 golden HTML（argos 6 + tesco 10）实测复现，根因是**四个独立缺陷**，
其中两个是站点无关的、tesco 上同样存在但一直沉默。

### 为什么 cold start 没考出来

cold start 的执行路径严格窄于 runtime：

- cold start `_run_review_cases` 只做 `sandbox(code, html, url)` → `validate()`（两道 gate）。
- runtime `HTMLScraper.scrape` 多两步：`_fast_path_sane`（[html_scraper.py:204](src/scraping/scrapers/html_scraper.py#L204)）
  与 `promote_candidate` 金标测试。
- **`_fast_path_sane` / `detect_promotion` 在 cold start 中一次都不会被调用。**

cold start 验证「parser 对不对」，runtime 额外验证「我们对页面的理解对不对」。
argos 的 parser 一直是对的，错的是页面理解——这题 cold start 没考。

thinking 的 token bug 同理：cold start 有独立 ladder，
`enable_thinking = round_index >= len(ladder)-1`，2 档下 round 0 关闭 thinking
（[coldstart.py:138](src/scraping/coldstart.py#L138)），argos 第 0 轮就全过，从未走到开 thinking 的轮次。

---

## 缺陷 1 — Nectar 积分累积被误判为会员门槛价（argos 崩溃主因）

`_check_membership_gating` 路径 C（badge 扫描）在 `<svg class="nectar-icon">` 上命中
`_MEMBERSHIP_GATING_HINTS` 里的 `"nectar"`（[prepass.py:119](src/scraping/repair/prepass.py#L119)）。
该图标来自 "Collect 519 Nectar points"——积分**累积**，不是**门槛价**，且 Argos 几乎每页都有。

后果：`_fast_path_sane` 看到 `kind == "membership"` 而 parser 未返回
`list_price`/`membership_price`，判定 parser 不可信 →
**每个 Argos standard 页都被强制推进修复梯子，永远走不到快速路径**
（`scrape_runs` 表 argos 记录为 0 条，正是印证）。

Tesco 不受影响的原因是语义差异而非运气：Clubcard Price 确实是门槛价，
badge/class 上的 `clubcard` 名副其实。同一词表，两种相反语义。

## 缺陷 2 — current/reference price 取容器内 min/max（站点无关，tesco 同样中招）

`detect_promotion` 第 4 步把容器内**所有**金额排序，最低者当 `current_price`、
最高者当 `reference_price`（[prepass.py:1036](src/scraping/repair/prepass.py#L1036)）。
PS5 页 `section.pdp-pricing-module` 内金额为
`['519.99','20.87','10','2.89','64.99','584.98','1200']`（分期月供、礼卡、套装价混杂），
于是 prompt 里写着「PS5 售价 1200，会员价 2.89」。

实测 tesco 10 条 golden：**`kind` 10/10 全对，`current_price` 错 5 条**——

| golden | 期望 price | detect current_price |
|---|---|---|
| g12 discounted | 29.99 | **2.99** |
| g13 discounted | 119.99 | **5.99** |
| g14 membership | 6.75 (mem 4.50) | **0.30** |
| g21 discounted | 4.29 | **2.95** |
| g23 standard | 100.75 | **2.95** |

一直没暴露，是因为 `_fast_path_sane` 只读 `signal["kind"]`、从不读值——
错值只安静地污染每一次修复 prompt。

`detect_promotion(soup, trusted_values)` 已有锚定入参，但
(a) 只用于挑容器、不用于挑 current/reference；
(b) `_fast_path_sane` 调用时**完全没传**（[html_scraper.py:392](src/scraping/scrapers/html_scraper.py#L392)）。

## 缺陷 3 — thinking 档把 token 预算全烧在 reasoning 上

notebook stderr 记录 `_gen_parser`（[agent.py:358](src/scraping/repair/agent.py#L358)）抛出
`openai.LengthFinishReasonError: CompletionUsage(completion_tokens=32768, reasoning_tokens=32768, ...)`。

`repair_model_ladder` 为 2 档 deepseek，最后一档 `is_last=True → enable_thinking=True`。
DeepSeek 的 `max_tokens` 涵盖 reasoning，`ProviderSpec.max_output_tokens=32768`
被 reasoning 全吃掉，content 为空，SDK 丢弃内容并抛错 →
该档恒定返回 `parser_gen returned nothing`。**运行时修复梯子实际只剩 1 次有效尝试**（tesco 同样）。

## 缺陷 4 — Argos 的 URL product id 正则漏掉 `tuc…` 型 SKU

`_URL_PID_PATTERNS["argos"] = r"/product/(\d+)"`（[prepass.py:80](src/scraping/repair/prepass.py#L80)）
匹配不到 `/product/tuc143428469`（本次失败的 URL）与 `/product/tuc148159202`（golden 29），
`URL PRODUCT ID` 锚点缺失。

## 「长时间无返回」的来源

HTML 路线烧完 2 次修复后抛 `ScrapeFailed`，router 回退 `ArgosDCAScraper`，
其 `_poll` 独占 `bd_async_poll_max_seconds = 300s` 后超时
（DB 中 8/6 escalation 记录 `DCA collection polling timed out after 303s`）。
按决定**本次不动 DCA**：HTML 路线修好后基本不会走到这一步。

---

# 变更

## A. 站点档案 `sites.yaml`（新增）

### A1. `src/scraping/sites.yaml`

与 [hosts.yaml](src/scraping/hosts.yaml) 同级——后者已确立「声明式站点事实放在
`src/scraping/` 下的 YAML、由 `_load_host_map()` 式加载器读取」的先例。
两者 key 空间不同（host→site vs site→profile），因此是同级新文件而非合并；
**也不放 `scrapers/config`**：消费方是 prepass（签名只吃 `site: str`）、coldstart、
validation，不只是 scraper。

```yaml
# 站点声明式事实。这里是【约束】，不是【检测器】：
# 只用于否决检测结果与驱动 cold start 覆盖要求，
# 判别逻辑一律留在 prepass.py，避免退化成站点硬编码。
argos:
  page_types:
    standard:     {available: true,  cold_start_required: true}
    discounted:   {available: true,  cold_start_required: true}
    out_of_stock: {available: true,  cold_start_required: false}
    multipack:    {available: true,  cold_start_required: false}
    # Argos 无会员门槛价；页面上的 "Collect N Nectar points" 是积分累积，不是门槛价。
    membership:   {available: false}

tesco:
  page_types:
    standard:     {available: true,  cold_start_required: true}
    discounted:   {available: true,  cold_start_required: true}
    out_of_stock: {available: true,  cold_start_required: true}
    multipack:    {available: true,  cold_start_required: false}
    membership:   {available: true,  cold_start_required: true, hints: ["clubcard"]}
```

### A2. `src/scraping/site_profile.py`（新增加载器）

照抄 [router.py:20-33](src/scraping/router.py#L20) 的加载/热重载形态：

- `_load_profiles()` / `SITE_PROFILES` / `reload_profiles()`
- `page_type_available(site, page_type) -> bool`
- `is_mandatory_page_type(site, page_type) -> bool`
- `golden_min_for(site, page_type) -> int`
- `membership_hints(site) -> set[str]`

**默认 fail-open**：未声明的站点/页面类型一律视为 `available: true`，
`cold_start_required` 回落到 `cfg.is_mandatory_page_type(page_type)`。
这样 `amazon_uk` 等没有条目的站点行为完全不变，新增站点也不强制先改这个文件。
隐含规则：`available: false` 的类型永不 mandatory。

### A3. 消费点

1. **`detect_promotion` 的否决层**（`prepass.py`）——新增可选 `site` 参数
   （`build_price_aware_context` 第 211 行已有 `site = _resolve_site(url)`，直接透传）。
   在 `_check_membership_gating` 之后插入：

   ```python
   if has_gating and site and not page_type_available(site, "membership"):
       has_gating, gating_program = False, None   # 继续走 reference/discount 判定
   ```

   **注意是降级、不是 return None**——否则 argos 上真实的 Was/RRP 折扣会被一并丢掉。

2. **`_fast_path_sane`**（`html_scraper.py`）——签名加 `site`，调用处传 `self.site`。

3. **cold start 覆盖要求**（`coldstart.py`）——
   `read_coldstart_input(path)` → `read_coldstart_input(path, site)`（`main()` 第 1008 行同步改），
   第 945 行与 `_coverage_shortfall`（第 394 行）改用 `site_profile` 的站点感知版本。
   同时：输入行声明了该站点 `available: false` 的 page_type 时，
   按既有风格并入 `ColdStartInputError` 快速失败。

4. **反向校验**（`coldstart.py` 评审段）——对每条 accepted case 跑
   `classify_page_type(product)`，若落进该站点声明为 `available: false` 的桶，
   打印醒目告警并列出 URL。**没有这条，一个写错的 `false` 会永久且静默地致盲整个站点。**

5. **恢复 `config.py` 的已提交默认值**——把工作区里被翻成 `False` 的
   `out_of_stock` / `membership`（[config.py:89-90](src/scraping/config.py#L89)）改回 `True`。
   你当初翻它是为了让 argos 跑起来，但它是**全局**的，同时也悄悄放松了 tesco 的要求；
   per-site 真相现在由 `sites.yaml` 承载，全局值退回为「未声明站点」的兜底。
   `repair_model_ladder` 的 deepseek 改动保持不动。

## B. 移除 nectar 词条 — `prepass.py:119`

`_MEMBERSHIP_GATING_HINTS` 删除 `"nectar"`，并在集合上方补注释说明
「积分累积类忠诚度标记不构成门槛价信号」。`"nectar"` 在仓库中仅此一处引用。

A 的否决层与 B 是两道独立防线，都要有：A 拦站点级（argos 声明无会员价），
B 拦词表级（任何站点的积分累积图标都不该被当门槛价）。

**已实测**：仅此一改，argos golden 25/26/29 的 `kind` 即由 `membership` 变为 `None`，
`_fast_path_sane` 不再蒸馏；tesco 10 条 golden 改前改后输出逐字节相同，
且无一条 tesco golden 的 HTML 含 `nectar` 字样。

## C. 用锚定值挑 current/reference — `prepass.py` + `html_scraper.py`

**核心规则：JSON-LD 主 offer 价（trusted value）恒定映射到 `price`。**
这与 M20 的价格契约完全对齐（`price` = 普通非会员现价，schema.org `offers.price` 正是此值）。
由此：

- **discount 分支**：`current_price` = trusted 值；
  `reference_price` = 高于它、且**确实带删除线或 Was/RRP 标签**的最高价。
  现行代码先取全部更高价、再用 `has_reference` 做布尔判断，却仍用 `ref_candidates[-1]`
  （无过滤的最高价）当原价——分期/套装/礼卡金额因此被当原价。
- **membership 分支**：trusted 值 → `regular_price`（即 `price`）；
  `member_price` = 门槛子容器内低于它的价格。
  **不要**把 struck/labeled 过滤套到这里——tesco 的会员页常规价通常不带删除线，
  套上会把 `regular_price` 打成 None。
- **无 trusted 值时**回落到现行 min/max 行为（保持既有站点不回归）。
- **锚定不出自洽结果时，只发 `current_price`、其余置 None**——
  宁可 prompt 信息不足，也不能对模型说谎。

`_fast_path_sane` 的 trusted 值直接取**刚通过两道 gate 的 `product`** 自身：

```python
trusted = {str(v) for v in (product.price, product.list_price, product.membership_price) if v}
signal = detect_promotion(soup, trusted, site=site)
```

比重跑 `build_price_aware_context`（全 DOM 扫描）便宜得多，且把守卫的问题
从「页面上有没有促销」收窄为「parser 找到的那个价格所在容器里，是不是还有它漏掉的第二个价」。

容器打分（`_find_price_container`，第 1140 行）追加一项：
容器内除 trusted 值外**还含带删除线/Was 标签的更高价**时 +40。
目标是修 argos g27/g28（真实折扣但当前 `kind=None`，因为选中的容器只有 `'£349.00*'`）。
若打分改动仍选不中，`kind=None` 是可接受结果——**漏检安全，错值不安全**。

顺带把第 408 行吞掉一切异常的 `except Exception: pass` 改为
`logger.debug(..., exc_info=True)`：目前 `detect_promotion` 一抛错就静默放行，排障看不见。

## D. thinking 档单独的输出上限 — `providers.py`

按既定选择，**只提高上限，不加降级重试**。

- `ProviderSpec` 增加 `thinking_max_output_tokens: Optional[int] = None`，
  紧邻现有 `max_output_tokens` 并沿用同段注释风格，写明 reasoning 与 content 共享预算。
- `deepseek` 设为 `65536`（V4 上限 384K，注释已记录）。
- `make_chat_client` 第 148 行取值顺序改为：
  显式 `max_tokens` 入参 > `enable_thinking` 时的 `thinking_max_output_tokens` >
  `max_output_tokens` > provider 默认。
  仍写进 `extra_body["max_tokens"]`——[CLAUDE.md](src/scraping/CLAUDE.md) 已记录
  `ChatOpenAI(max_tokens=)` 会被 langchain 改名为 `max_completion_tokens` 而被 DeepSeek 静默忽略，
  这条不能动。
- 第 152 行 INFO 日志补上 thinking 标记，便于事后核对实际下发的上限。

## E. Argos URL product id 正则 — `prepass.py:80`

`r"/product/(\d+)"` → `r"/product/([A-Za-z0-9]+)"`，同时覆盖 `7726851` 与 `tuc143428469`。

## F. `playground.ipynb` 变量名笔误

单元格写 `url_1 = "...tuc143428469"` 却调用 `scrape(url)`；本次能打到目标 URL 全靠 kernel
里残留的 `url` 变量，换新 kernel 会直接 `NameError`。改为 `scrape(url_1)`。

---

## 现有约束（实施与验证时必须遵守）

- `get_config()` 是模块级单例（[config.py:189](src/scraping/config.py#L189)）；
  `SITE_PROFILES` 同样在 import 期加载。改完代码 **Jupyter kernel 必须重启**。
- `cfg.db_path` 是相对路径 `Path("scraping.db")`，notebook/脚本 CWD 必须是仓库根，
  否则静默新建空库（读不到 parser id=5 与 16 条 golden）；
  `sandbox.py` 用 `-m src.scraping.repair.sandbox` 拉子进程且不传 `cwd=`，同样依赖这一点。
- 用 `.venv/bin/python`，不要用系统/anaconda 解释器（后者缺 bs4/lxml/openpyxl）。

---

## 验证

### 新增 `src/scraping/tests/verify_m23.py`（遵循仓库既有验证纪律）

只读打开 `scraping.db`，取 16 条 golden 与两个 active parser。核心断言是
**让 `detect_promotion` 的输出与人工评审过的 `expected_output` 对齐**——
这是有数据支撑的验收线，而不是我预测的值。

对每条 golden，以 `expected_output["price"]` 作为 trusted 值（fast-path 场景的等价代理）：

1. `kind` 必须等于 golden 隐含的类型
   （`list_price > price` → discount；有 `membership_price` → membership；否则 None）。
2. `kind == "discount"` 时：`current_price == expected price` 且
   `reference_price == expected list_price`。
3. `kind == "membership"` 时：`regular_price == expected price` 且
   `member_price == expected membership_price`。
4. `kind is None` 时：`current_price` 要么等于 expected price，要么缺省——
   **绝不能是页面上的其它金额**（当前 argos g25 给 2.89 / tesco g23 给 2.95，都在此条下失败）。
5. `_fast_path_sane` 对全部 16 条 golden 返回 `None`（不蒸馏）。
6. 再跑一遍 `trusted_values=None` 的变体，断言不抛异常且不产生比现状更差的结果
   （回落路径可用性）。

站点档案与其余项：

7. `page_type_available("argos","membership") is False`；
   `page_type_available("amazon","membership") is True`（未声明站点 fail-open）。
8. `is_mandatory_page_type("tesco","out_of_stock") is True` 且
   `is_mandatory_page_type("argos","out_of_stock") is False`——
   即**恢复全局默认后 tesco 的要求不再被 argos 的需要拖累**。
9. 一份声明 `page_type: membership` 的 argos 输入会抛 `ColdStartInputError`。
10. `_URL_PID_PATTERNS["argos"]` 能从 `/product/tuc143428469` 抽出 `tuc143428469`。
11. `make_chat_client("deepseek-v4-flash", enable_thinking=True)` 的
    `extra_body["max_tokens"] == 65536`，`enable_thinking=False` 时为 `32768`
    （断言构造出的 client 属性，不发网络请求）。

输出 `[PASS]`/`[FAIL]` 与 `SUMMARY: N passed, M failed`，失败非零退出，
`| tee src/scraping/tests/verify_m23_output.log`。

### 回归（C 是唯一触碰 tesco 的改动，必须有证据）

```bash
.venv/bin/python src/scraping/tests/verify_m14.py
.venv/bin/python src/scraping/tests/verify_m17.py
.venv/bin/python src/scraping/tests/verify_m20.py
.venv/bin/python src/scraping/tests/verify_m21.py
.venv/bin/python src/scraping/tests/verify_m22.py
```

（`verify_m15.py` 脚本本身在仓库中缺失、只剩日志，无法重跑；其覆盖面由上面第 1–6 条断言接管。）

风险控制点：C 不得改变 tesco 的 `kind`。
`has_reference` 现在就是 `any(struck_through or _has_reference_label(...))`，
把 `ref_candidates` 收窄到确实带删除线/Was 标签的那些之后，
g12/g13/g21 选中的仍应是 39.99/169.99/7.29——由断言 1、2 兜底，不靠推理。

### 端到端（真实网络，需重启 kernel / 新进程）

```bash
.venv/bin/python -c "
import asyncio; from src.scraping import scrape
print(asyncio.run(scrape('https://www.argos.co.uk/product/tuc143428469')))
"
```

预期：走 HTML 快速路径复用 parser id=5，或至多 1 次修复即成功，数分钟内返回
`ProductData`，且不再回退 `ArgosDCAScraper`。随后查 `scrape_runs` 应出现首条 argos 记录，
`path` 为 `fast path (parser)` 或 `agent_repaired`。

再跑一条已知的 tesco URL（取自 `scrape_runs` 现有三条之一）确认仍走 `fast` 路径。

### 文档

`src/scraping/CLAUDE.md` 追加 M23 小节并补里程碑表行，说明
`sites.yaml` 是新增站点时需要一并维护的文件；
同时修正该文档中已过时的 `repair_model_ladder` 默认值描述（仍写着 qwen）。
`AGENTS.md` 由 pre-commit hook 自动同步，无需手改。
