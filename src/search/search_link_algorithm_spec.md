# Search-Link 匹配算法 — 技术方案

> 交付目标：描述「给定商品名 + 目标网站，判断该商品是否在目标网站上存在对应 listing」的匹配管道的设计决策与逻辑规范。文件目录与具体代码由 Claude Code 根据现有仓库自行决定。
> 核心设计原则：**base 与 distinguishing 解耦**、**只在确认不同时 reject**、**逐层 trace + 短路**、**异步并行**。

---

## 1. 输入输出

### 输入
| 字段 | 必需 | 说明 |
|---|---|---|
| `product_name` | ✅ | 待匹配商品名 |
| `website` | ✅ | 目标网站，枚举：`amazon` / `tesco` / `argos` |
| `brand` | ⬜ | 若提供，直接用于 brand 层，省去从 title 提取 query 品牌 |

### 输出
每个候选在管道中的每一层都记录自己的判定，统称 `LayerTrace`。未到达的层为 `None`。

- `domain`：**两态** `pass / fail`
- `brand`：**三态** `pass / fail / unknown`
- `numeric`：**三态** `pass / fail / unknown`
- `distinguishing`：**两态** `pass / fail`

> 设计含义：brand 与 numeric 都 `unknown` 时，base 层无法定夺，交由 distinguishing 层决定。

最终输出包含：

- `verdict`：`match / no_match`
- `matched_candidate`：命中的候选（未命中为 null）
- `layer_trace`：代表性候选的逐层结果（见下方规则）
- `candidates_considered`：管道总共处理了多少候选
- `reason`：distinguishing 层的判定理由（若到达该层）

**`layer_trace` 取哪个候选**：命中时取被判为同款的候选；未命中时取**走得最远的候选**（到达层数最深者），诊断价值最高。

| 场景 | `layer_trace` |
|---|---|
| 命中 | `{domain: pass, brand: pass, numeric: pass, distinguishing: pass}` |
| 最远候选进了 LLM 层被否 | `{domain: pass, brand: pass, numeric: unknown, distinguishing: fail}` |
| 最远候选卡在数值层 | `{domain: pass, brand: pass, numeric: fail, distinguishing: None}` |
| 搜到了但全不在目标域名 | `{domain: fail, brand: None, numeric: None, distinguishing: None}` |
| 搜索 0 结果 | `{domain: None, brand: None, numeric: None, distinguishing: None}` |

---

## 2. 架构概览

使用 **LangGraph** 编排，全链路异步。管道共五个节点，依次为：

```
search → domain_filter → base_match → distinguishing → aggregate
```

`base` 与 `distinguishing` 物理解耦：两者互不感知，仅通过 orchestrator state 传数据。改 base 规则或换 distinguishing 模型互不影响，亦可独立测试。

每个候选从进入管道开始就带一份 `LayerTrace` 和一个 `alive` 标志，随各层推进逐步填充。某层所有候选 alive 归零时，条件边直接跳到 aggregate，跳过后续昂贵层（尤其 LLM）。

---

## 3. 各层逻辑

### 第 1 层：搜索

**职责**：把 `product_name + website (+ brand)` 转成搜索 query，调 search provider，产出候选列表；同时提取 query 侧的 `BaseAttributes` 供后续 base 比对用。

**Search provider** 设计为可插拔抽象接口。当前实现接 Serper（异步 HTTP）与基于 `ddgs` 库的 DuckDuckGo provider，实现同一接口即可替换，无需改调用方。在配置里可按名字选择单 provider 或有序 fallback chain。

**Query 构建**：由 `search.query_mode` 按 provider 选择 `keyword`（`product_name + domain_map key`）、`sitename`（`product_name + site:domain_map value`）或 `both`。`both` 的两种形式并发发出并合并去重；因为部分 provider 不可靠地支持 `site:`，未配置的 provider 默认保持 `keyword`。`search.strip_parens` 开启时为每种形式额外生成去括号变体。`domain_map` 缺失时，`sitename` / `both` 优雅降级为 keyword，不在 query builder 抛错。

**并行点**：多 query 变体并发发出（`asyncio.gather`），是搜索层的速度主收益。

---

### 第 2 层：域名与 URL 形态过滤

**职责**：先保留 URL 域名命中目标 website 的候选，再对已配置规则的网站验证其路径是否为单品页；域名不符或属于搜索结果、分类、browse、品牌店等非单品页面的候选均写 `domain=fail`、`alive=False`。

搜索结果进入管道时先按 `url_rules.strip_query_params` 的 denylist 清除追踪参数，再基于清洗后的 URL 去重。`domain_map` 维护 website 到允许域名的映射，`url_rules.product_path` 可选地维护 website 到单品页路径正则；没有路径规则的网站保持原有的仅域名过滤行为。该层为纯规则判断、无 I/O，并记录 host 与非单品页两类拒绝数量供 trace 排查。

---

### 第 3 层：base 匹配（brand + numeric）

**设计原则**：**只有确认不同才 reject**；提取不到或判断不了一律放行（`unknown`）。品牌表天然不可能涵盖所有商品，数值属性也可能缺失，这些都不应成为 reject 的理由。

#### Brand（三态）

提取逻辑（按优先级）：
1. 用户直接传入的 `brand` 字段（query 侧优先使用）
2. 品牌词表 + fuzzy 匹配（`rapidfuzz`，阈值 88 分）——返回标准化品牌名
3. 标题首词启发式（首词大写且非数字）——返回原始 token

比对逻辑：
- fuzzy score ≥ 88 → `SAME`（写 `brand=pass`）
- fuzzy score ≤ 40 → `DIFFER`（写 `brand=fail`，`alive=False`，不再计算 numeric）
- 中间地带或任一侧提取不到 → `UNKNOWN`（写 `brand=unknown`，继续）

> 阈值（88 / 40）外置到配置，可调参无需改代码。

#### Numeric（三态）

提取工具：`quantulum3`——用 `entity.name`（物理维度）映射属性 key，有歧义的维度（`digital_storage`、`length`）按量值前后 20 字符的上下文关键词消歧，再统一换算到基准单位。映射表、消歧规则、换算表、离散/连续属性集合全部集中在一处静态配置文件，方便维护。

比对逻辑（只比对两侧都有的 key）：
- 无共有 key → `UNKNOWN`（写 `numeric=unknown`，继续）
- **离散属性**（`storage_gb`、`ram_gb`、`screen_inch`、`voltage_v` 等）：要求精确相等，不等 → `CONFLICT`
- **连续属性**（`weight_g`、`volume_ml` 等）：允许 ±10% 容差（吸收 "500g" vs "0.5kg" 的舍入），超出 → `CONFLICT`
- 无 CONFLICT → `CONSISTENT`

`CONFLICT` 写 `numeric=fail`，`alive=False`；否则写 `numeric=pass` 或 `numeric=unknown`，继续。

> brand 和 numeric 均为 `unknown` 时，`alive` 保持 `True`——交给 distinguishing 定夺。

**缓存**：base 提取结果存 SQLite，key = `md5(title)`，命中直接复用，不重复解析。

**并行点**：候选间 base 提取与比对可并发（CPU 密集，用 `asyncio.to_thread` 丢线程池）。

---

### 第 4 层：distinguishing 匹配

**职责**：对通过 base 的少数存活候选，做变体级语义判定——口味、颜色、版本、套装等 base 抓不到的区分点。

**调用策略**：一次 LLM 调用，输入 = query（title + brand + numeric）+ 全部存活候选，输出 = 最佳匹配候选的下标（或 null）+ 理由文本。存活数本就很少，单次 batched 调用最省 token。

**不依赖 LLM 自报 confidence**，只取它的「是/否同款」判断 (仅非命中时)。

命中的候选写 `distinguishing=pass`；无一命中则全部写 `distinguishing=fail`。

---

### 第 5 层：aggregate（汇总）

无 I/O，纯逻辑：
- 若某候选 `distinguishing=pass` → `verdict=MATCH`，`layer_trace` 取该候选。
- 否则 → `verdict=NO_MATCH`，`layer_trace` 取走得最远的候选（trace 中最深的非 `None` 层最靠后者）。搜索 0 结果时 trace 全为 `None`。

---

## 4. 短路规则

| 节点执行完毕后 | 条件 | 下一步 |
|---|---|---|
| `search` | 无 raw candidates | 直接 → aggregate |
| `domain_filter` | alive 归零 | 直接 → aggregate |
| `base_match` | alive 归零 | 直接 → aggregate |
| `distinguishing` | 无条件 | → aggregate |

---

## 5. 异步与并行策略

- 搜索层（多 query 变体）和 distinguishing 层是真正的 I/O 并行点，用 `asyncio.gather`，是速度主收益。
- base 提取是 CPU 活（regex / quantulum3 / rapidfuzz），候选量大时用 `asyncio.to_thread` 丢线程池，避免阻塞事件循环。
- 全链路 async node，provider / LLM 客户端复用单例连接池。

---

## 6. 配置（外置，不改代码）

以下内容全部外置到配置文件：

- Serper API key、当前 search provider 名（`serper` / `duckduckgo`）
- 各 provider 的 query 构建方式（`keyword` / `sitename` / `both`）与是否生成去括号变体
- LLM provider 名（`qwen` / `deepseek`）及 API key
- Brand fuzzy 阈值（SAME 阈值默认 88，DIFFER 阈值默认 40）
- Numeric 连续属性容差（默认 ±10%）
- SQLite cache 路径

---

## 7. 建议实现顺序

1. 数据模型：`LayerTrace`、`CandidateEval`、`BaseAttributes`、`MatchResult` 等核心结构。
2. 静态配置：域名映射表、品牌词表（先放几百个种子品牌）、numeric 映射/消歧/换算/离散集合。
3. 搜索层：SerperProvider + DuckDuckGoProvider + provider-specific query builder。
4. base 匹配层：brand 提取与比对 → numeric 提取与比对 → 合并，**每个子模块配单元测试**（最核心）。
5. SQLite 缓存接入 base 提取。
6. distinguishing 层：prompt 模板 + LLM 调用（batched 单调用）。
7. orchestrator：state → 各层 node → graph 边与短路条件。
8. 端到端测试：用真实例子（如 `"Dyson V15 Detect"` + `argos`）跑通，打印 `layer_trace`。

---

## 8. 测试要点

- **brand 三态**：相同 / 拼写变体（如 L'Oréal vs Loreal）/ 完全不同 / 一侧缺失，四种各一例。
- **numeric 三态**：离散冲突（128GB vs 256GB）、连续容差内（500g vs 0.5kg）、单侧缺失（→ UNKNOWN）。
- **短路**：构造域名过滤后归零的输入，断言不触发 LLM 调用，`layer_trace.domain == fail`，brand/numeric/distinguishing 均为 `None`。
- **逐层 trace**：各层失败场景各一例，断言代表 trace 中未到达层均为 `None`。
- **解耦回归**：mock 掉 distinguishing，base 层可独立完整测试；反之亦然。

---

## 附：核心设计原则速记

1. **base 与 distinguishing 物理解耦**——互不 import，由 orchestrator 编排顺序。
2. **三态比对，只在确认不同时 reject**——品牌表不全、数值缺失均走 `unknown` 放行，不误杀。
3. **逐层 trace + 短路**——每个候选带 `LayerTrace`，alive 归零即跳过 LLM。输出取命中候选或走得最远候选的 trace。
4. **LLM 只看 base 筛剩的少数候选**——省 token，且不信任 LLM 自报 confidence。
5. **可插拔**——search provider、LLM provider、品牌表、域名表、所有阈值全部外置配置。
