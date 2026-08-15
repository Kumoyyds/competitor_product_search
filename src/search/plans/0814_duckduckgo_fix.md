# Query 构建改造：keyword / sitename / both + ddgs 引擎列表

## Context

排查 `search_db.sqlite` 的 `tasks` 表时发现 `final_provider` 绝大多数是 `serper`（268/346），`duckduckgo` 只搞定 62 个。根因不是"没返回结果"（两家返回条数相近，10.3 vs 9.9），而是**返回结果落在目标域名上的比例**：duckduckgo 7.6%，serper 26.0%。

进一步查明两件事：

1. **query 里的零售商关键词只对 Google 有效。** 同一条 query `Casa Del Sud Pastasaus 520g amazon.nl`，serper 过域名 2 条、duckduckgo 过 0 条。query 里已明写域名，DDG 侧依然返回 ah.nl / jumbo.com。需要 `site:` 操作符才能真正限定站点。
2. **`duckduckgo` provider 名不副实。** `src/search/providers/duckduckgo.py:150` 调 `ddgs.text()` 时没传 `backend=`，而 `ddgs==9.14.4` 是多引擎元搜索聚合器。trace DB 的报错 URL 里出现 `search.brave.com` 和 `yandex.com/search/site/`。`auto` 模式下：强制把 `wikipedia`/`grokipedia` 排最前（`ddgs.py:311-312`）、`k=10` 时 `max_workers=2` 只发 2 个引擎（`ddgs.py:407`）、引擎顺序每次 `shuffle()`、结果被本地 `SimpleFilterRanker` 重排丢掉各引擎自己的排序信号。这解释了 865 个散射 host 和 13.9s 的平均 attempt 耗时。

**目标**：让 query 构建方式按 provider 可选（`keyword` / `sitename` / `both`），并把 ddgs 的引擎列表变成显式可配置项，从而提升 ddgs 这条链路的域名命中率、减少对 serper 的依赖。

**风险控制**：第 3、4 步涉及 ddgs 引擎稳定性，属于未验证区域，用烟雾测试做 go/no-go 闸门，不达标则整体回退。

---

## 已确认的决策

- `domain_map` 中的 `amazon: amazon.` **删除**（尾点通配无法构造合法 `site:`）。现有数据只用到 `tesco` / `argos` / `amazon.co.uk` / `amazon.nl`，无裸 `amazon` 行，删除不影响存量输入。
- 第 3 步的 go/no-go 闸门：错误率 ≤ auto 基线 2 倍（≤3%）、429/ratelimit 为 0、`alive_after_domain` 不低于 auto 基线、run 不中断。任一不满足 → 放弃第 3、4 步。
- 第 3 步若被放弃，**provider 改名一并放弃**，保持 `duckduckgo`，只保留第 1、2 步成果。

---

## 步骤 1：删除 `retailer_keywords`（无条件执行）

`retailer_keywords` 现在是空操作——`tesco: Tesco` / `argos: Argos` / `amazon: Amazon` 的 value 除首字母大小写外与 key 完全相同，而 `config.retailer_keyword_for()` 的 fallback 本就是 website 本身。删掉它，关键词直接取 `domain_map` 的 key。

**改动**

| 文件 | 改动 |
|---|---|
| `src/search/maintain/search_config.yaml` | 删 `search.retailer_keywords` 整块及其注释；从 `domain_map` 删 `amazon: amazon.` |
| `src/search/config.py` | 删 `retailer_keyword_for()`（`config.py:72-73`）。`domain_for()` 保留不动 |
| `src/search/layers/query_builder.py` | 不再调 `retailer_keyword_for`，直接用 `website.strip()` 作关键词 |
| `scripts/gen_capability_docs.py` | `collect_websites()`（第 217-238 行）去掉 `keywords` 读取；`render_websites_table()`（第 320-330 行）去掉 "Retailer keyword" 列，表变两列 |

`domain_filter._host_matches()` 的尾点前缀分支保留（`test_domain_filter.py` 覆盖了它），只是配置不再使用。

---

## 步骤 2：`query_mode` 按 provider 配置（无条件执行）

**配置形状**（字典而非与 `provider` 平行的 list，避免下标错位）：

```yaml
search:
  provider: [duckduckgo, serper]
  # 每个 provider 用哪种 query 构建方式：
  #   keyword  – "{product_name} {domain_map key}"      例：... Tesco
  #   sitename – "{product_name} site:{domain_map value}" 例：... site:tesco.com
  #   both     – 两条都发，并发打出后合并去重进同一候选池
  query_mode:
    duckduckgo: both
    serper: keyword
  # 商品名含括号时，在上述每种形式之外额外发一条去括号的 query
  strip_parens: true
```

同时**删除 `query_variants`**——它的 `raw` 和 `with_retailer_kw` 生成同一字符串、被去重吃掉，语义已被 `query_mode` + `strip_parens` 完全覆盖。

**`both` 是并发合并，不是串行重试。** `search.py:23-28` 的 `asyncio.gather` 加 `search.py:31-46` 的 URL 去重已经实现了"多 query 并发 + 合并成单一候选池"，`build_queries` 多返回一个字符串即可，无需新增管道。串行重试方案已否决：`pipeline.py:46-69` 的 provider 链是整图重跑（含 LLM 层），失败后重试会在 81 行的 batch 上多产生约 250 次重复 LLM 调用、每行多等约 5s，且第一轮候选在重试前被丢弃，信息量反而更少。

**改动**

| 文件 | 改动 |
|---|---|
| `src/search/config.py` | 新增 `query_mode_for(provider_name: str) -> str`，默认 `"keyword"`；新增 `strip_parens_enabled() -> bool`，默认 `True` |
| `src/search/layers/query_builder.py` | `build_queries()` 增加 `provider_name: str \| None = None` 形参；按 mode 生成基础 query（`keyword` 用 `website`，`sitename` 用 `config.domain_for(website)`），再按 `strip_parens` 追加去括号变体；沿用现有 `_add()` 去重 |
| `src/search/layers/search.py` | 第 21 行调用处传 `provider_name=provider.name`（`state["provider"]` 已是 provider 实例） |

**边界情况**：`domain_for(website)` 返回 `None`（website 不在 `domain_map`）时，`sitename` / `both` 退化为只发 keyword query，不抛异常——与现有"未知 website 不 raise、走到 domain_filter 全灭"的行为一致。

**query 条数上限**：`both` + 含括号商品名 = 4 条。ddgs 侧受 `_enforce_interval` 全局 1s 串行限制（`duckduckgo.py:104-113` 在持锁状态下 sleep），意味着每行搜索阶段最长约 4s。serper 为 `keyword` 模式，最多 2 条，不增加付费调用量级。

**新增测试** `tests/unit/search/test_query_builder.py`（目前该模块无测试）：
- `keyword` 模式产出 `"{name} {website}"`
- `sitename` 模式产出 `"{name} site:{domain}"`
- `both` 模式产出两条且顺序稳定
- `strip_parens` 对含括号名产出额外一条、对无括号名不产出重复条目
- 未配置的 provider 名回落 `keyword`
- `domain_for` 返回 `None` 时 `sitename` 优雅降级

---

## 步骤 3：ddgs 引擎列表 + provider 改名（**受闸门约束**）

### 3a. 先建立 auto 基线

步骤 1、2 落地后，用固定 20 行样本跑一次基线（`backend` 不传 = 当前 auto 行为），记下 run_id。

```powershell
python -m src.search.batch --input input/tesco_algos_amazon_test.xlsx `
    --sku-col product_name --web-col web --country-col country --serper-max-calls 0
```

`--serper-max-calls 0` 让 serper 立即 `BudgetExhausted`，把观测隔离在 ddgs 这条链路上、且不烧付费额度。

### 3b. 实施改动

| 文件 | 改动 |
|---|---|
| `src/search/providers/duckduckgo.py` → `ddgs.py` | 文件改名；类 `DuckDuckGoProvider` → `DdgsProvider`；`name = "ddgs"`；`__init__` 增加 `backends: list[str] \| None = None`；`search()` 中当 `backends` 非空时传 `backend=",".join(backends)` 给 `ddgs.text()`；`_COUNTRY_TO_REGION` 与限流/重试逻辑不动 |
| `src/search/providers/__init__.py` | `make_provider` 接受 `"ddgs"` 与旧名 `"duckduckgo"`（别名，保护 `src/search/script/` 下的实验 notebook）；`make_provider_chain` 在 provider 名为 ddgs 时从 `config.get("search", "backends", name)` 读列表并传入 |
| `src/search/maintain/search_config.yaml` | `provider: [ddgs, serper]`；`query_mode` 的 key 同步改为 `ddgs`；新增 `backends` 段 |

```yaml
  # ddgs 是多引擎聚合器。语义是「按此顺序逐个试，凑够 k 条结果就停」，
  # 不是「并发查这 N 个引擎」—— max_workers = ceil(k/10)+1，所以 k=10 时
  # 首波只发 2 个引擎。想真正用上后面的引擎，需要同步调大 search.k。
  # 排除 wikipedia / grokipedia（百科，不含商品页，auto 模式下会被强制排最前）
  # 与 yandex（ddgs 走的是站内搜索控件端点，site: 语义存疑）。
  backends:
    ddgs: [duckduckgo, brave, mojeek, startpage]
```

### 3c. 闸门判定

用同一份 20 行样本、显式 backends 再跑一次，与 3a 基线对比：

```sql
-- 错误率（须 ≤ 3%，且 ratelimit/429 计数为 0）
SELECT COUNT(*) FROM node_events
WHERE run_id = :run AND node = 'search' AND error_message IS NOT NULL;

-- 域名存活（须 ≥ auto 基线）
SELECT provider, COUNT(*) n, ROUND(AVG(alive_after_domain), 2) avg_alive
FROM attempts WHERE run_id = :run GROUP BY provider;
```

**任一不满足 → 回滚步骤 3 的全部改动**（含改名），`provider` 保持 `duckduckgo`，`query_mode` 的 key 保持 `duckduckgo`，步骤 1、2 的成果保留，跳过步骤 4。

---

## 步骤 4：单引擎对比，定出最终 backends（**仅当步骤 3 通过**）

trace DB 不记录"候选来自哪个引擎"，所以只能用单引擎 run 反推。对每个候选引擎（`duckduckgo` / `brave` / `mojeek` / `startpage` / `yahoo` / `google`）把 `backends.ddgs` 临时设为单元素列表，跑同一份 20 行样本，对比：

```sql
SELECT r.run_id, ROUND(AVG(a.alive_after_domain), 2) avg_alive,
       ROUND(AVG(a.candidates_found), 1) found,
       ROUND(AVG(a.duration_ms)) avg_ms,
       SUM(a.verdict = 'match') matched
FROM attempts a JOIN runs r USING(run_id)
WHERE a.provider = 'ddgs' GROUP BY r.run_id ORDER BY r.started_at;
```

按 `avg_alive` 排序，剔除错误率高或耗时异常的引擎，定出最终 `backends.ddgs` 列表写回配置。

---

## 步骤 5：文档更新

**注意**：`.githooks/pre-commit` 会双向同步 `CLAUDE.md` ↔ `AGENTS.md`（`scripts/sync_agent_docs.py`）并重生成 README 的 `<!-- BEGIN GENERATED -->` 区块。先确认 `git config core.hooksPath` 已指向 `.githooks`；若未启用则需手动同步两侧或手动跑生成脚本。只编辑 CLAUDE.md 一侧。

| 文件 | 要点 |
|---|---|
| `README.md` | 第 7 行 duckduckgo 描述改为 ddgs 多引擎聚合；第 32 行 "Add more in `domain_map` + `search.retailer_keywords`" → 只留 `domain_map`；第 53 行引擎名 |
| `src/search/README.md` | 第 15 行 provider 列举；第 95 / 109 行"两处查找"改为只查 `domain_map`（key 作关键词、value 作域名过滤 + `site:`）；第 194 / 205 行 maintain 表去掉 `retailer_keywords`、加 `query_mode` / `backends`；第 238 行流程图 provider 名 |
| `CLAUDE.md`（根） | 第 7 / 63 / 68 / 96 行的 DuckDuckGo 表述；Config files 表补 `query_mode` / `backends` |
| `src/search/CLAUDE.md` | 第 47 行 `config.py` 职责（去 `retailer_keyword_for`，加 `query_mode_for`）；第 50 行 providers 描述；**第 65 行不变量 "No `site:` operator" 必须重写**——改为"`site:` 由 `query_mode` 按 provider 控制，默认 `keyword` 保持旧行为"；第 52 行 `query_builder.py` 行同理；第 77 行配置表把 `query_variants` / `retailer_keywords` 换成 `query_mode` / `strip_parens` / `backends`；第 120 / 121 行新增 provider / 新增 marketplace 的操作说明 |
| `docs/architecture.md` | 第 21 行 provider 链名称 |
| `src/search/search_link_algorithm_spec.md` | 第 69 行 §3.1 的"**不要用 `site:` 限制**"与新行为直接冲突，改为按 provider 可选，并记录原判断（"部分 provider 不支持 site:"）在多引擎场景下的适用边界 |

---

## Verification

1. **单元测试**：`pytest tests/unit/search -q`。新增 `test_query_builder.py` 须全绿；`test_domain_filter.py` / `test_url_rules.py` / `test_pipeline_shortcircuit.py` 不得回归（步骤 1 动了 `domain_map`，重点看 domain_filter 相关用例）。
2. **文档生成器**：`python scripts/gen_capability_docs.py --root .`，随后 `git diff` 确认 README 生成区块与配置一致、无残留 "Retailer keyword" 列。
3. **端到端烟雾**：20 行 batch（命令见 3a），检查 `runs` 表 `status` 正常结束、`tasks` 无 `error_type`。
4. **效果对比**：改造前后各跑一次同一份 81 行输入，对比

```sql
SELECT provider, COUNT(*) attempts,
       ROUND(AVG(alive_after_domain), 2) avg_alive,
       SUM(verdict = 'match') matched
FROM attempts WHERE run_id = :run GROUP BY provider;
```

预期：ddgs 侧 `avg_alive` 明显高于改造前的 0.4，`final_provider = serper` 的占比下降。

## 回滚

步骤 1、2 是纯配置 + query 构建改动，回滚即 revert 对应 commit。步骤 3、4 按闸门独立回滚，不影响 1、2。建议 1+2 一个 commit、3 一个 commit、4 的配置结论一个 commit，便于单独回退。
