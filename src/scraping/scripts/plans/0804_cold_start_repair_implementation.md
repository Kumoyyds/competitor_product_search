# Cold Start 纠错循环 — 评估结论 + 实施方案

## Context

`0804_cold_start_repair.md` 提出了 5 项 cold start 改进（R1-R5）。逐条对照代码核验后：R1/R2/R3 的缺口真实存在；R4 的根因分析有误但用户澄清了真实意图（全用例通过才入库）；R5 需求合理但依赖一个尚未实现的 stub；决策点 A 不存在（Gate 2 已正确处理）。

核心发现：**cold start 目前没有任何纠错路径** — [`_gen_initial_parser`](src/scraping/coldstart.py#L248) 是一次性 LLM 调用，人工拒绝后直接跳过。R2+R3+R4 合在一起实际上是在补这条缺失的循环，不是三个独立增强。

已确认的设计决策：
- 审完一轮后统一纠错（不是每条拒绝就立即修）
- 反馈仅内存传递，不落库
- cold start 用自己的模型阶梯（默认 DeepSeek），节点 0 生成初版、后续节点各跑一轮纠错

---

## 原计划评估结论

| 需求 | 评估 | 处置 |
|------|------|------|
| R1 面板字段补全 | ✅ 缺口真实（[coldstart.py:133](src/scraping/coldstart.py#L133) 硬编码 7 字段） | 实施，但砍掉 MISSING/N/A 区分（见下） |
| R2 结构化反馈 | ✅ 缺口真实 | 实施，改用内存对象，**不建表** |
| R3 反馈回灌 | ✅ 逻辑正确 | 实施，复用现有 `AttemptRecord` + `role="middle"` |
| R4 parser 落库门槛 | ❌ 原因分析有误 | 按用户澄清实施：全用例通过才入库 |
| R5 HTML 复用 | ⚠️ 依赖 stub | 实施，但**只读不写**（见下）+ 先修 stub |
| 决策点 A member 定价 | ❌ 代码误读 | 不做 — Gate 2 已正确处理 |
| 决策点 B 反馈存储 | — | 不建表，内存即可 |

### 原计划文档中不成立的论断（供修订原文档参考）

1. **「M9/M11 未接线」** — `promote_candidate` ([golden.py:102-171](src/scraping/repair/golden.py#L102-L171)) 已完整实现逐桶精确比对，运行时路径在用。cold start 不走它是因为 bootstrap 阶段 goldens 为空。
2. **「Gate 2 有 in_stock 时 price 必填规则」** — [`_core_price_rule`](src/scraping/validation/gate2.py#L39-L59) 是 `has_price OR has_list OR has_member`，member-only 页面已可通过。
3. **「与已有的 is_stale 自动过期检测衔接」** — [`_no_active_parser_passes`](src/scraping/repair/golden.py#L286-L290) 是永远返回 `False` 的 stub，该机制不存在。
4. **引用 `scraping_module_spec.md`** — 实际文件名是 `scraping_module_spec_v1_2.md`。
5. **修复阶梯描述为 3 级 flash→flash+context→pro** — 当前默认是 2 级 `["qwen3.7-plus", "qwen3.7-plus"]`（[config.py:51](src/scraping/config.py#L51)）。
6. **「优先复用 escalations 表」** — 该表有 UNIQUE signature + operational reason 枚举，语义完全不匹配。

---

## P0-A. 纠错循环（R2 + R3 + R4 合并实施）

三者是一个整体：R4 的「不通过就不存」需要一条出路，否则用户只能反复手动重跑；R2 提供修复素材；R3 把素材喂给 LLM。

### 改动文件：`src/scraping/coldstart.py`（主体）

#### 1. 新增内存数据结构

```python
@dataclass(frozen=True)
class FieldCorrection:
    field: str
    correct_value: str          # 人工填写的正确值，空串表示只知错不知对

@dataclass(frozen=True)
class ReviewFeedback:
    url: str
    page_type: str
    corrections: tuple[FieldCorrection, ...]
    hint: str                   # 自由文本提示，可空
```

#### 2. 重构 `run_coldstart` 为多轮循环

当前是线性流程；改为：

```
节点0 (flash) 生成初版 parser
  ↓
┌─→ 对全部 URL 跑 sandbox + validate
│     ↓
│   人工逐条审核（跳过输出未变且上轮已 accept 的）
│     ↓
│   全部 accept? ──yes──→ _seed() 入库 → 完成
│     │ no
│     ↓
│   阶梯还有下一个节点?
│     │ yes
│     ↓
│   打印本轮失败摘要 + "Continue 纠错? [y/N]"
│     │ y
│     ↓
└── 节点N 带反馈 + 当前代码重新生成 parser
    （最后一个节点开 thinking mode）

阶梯耗尽 或 人工答 n → 不入库，返回失败
```

驱动逻辑与 [`run_repair_ladder`](src/scraping/repair/agent.py#L134-L159) 的 `for i, model in enumerate(ladder)` 同形，只是每个节点之间插入了人工审核与反馈采集。

**重跑全部 URL（不只失败的）** — 修复可能改坏已通过的字段，这正是 R4 要防的。

**审核复用优化**：重跑后，若某 URL 的 `ProductData` 与上一轮**逐字段相同**且上轮已 accept，自动沿用该 accept，不再打扰人工。仅输出变化的条目需要重审。复用 [`_matches_expected`](src/scraping/repair/golden.py#L248) 做比对（它已有 Decimal 归一化逻辑）。

#### 3. 拒绝时的反馈采集

替换 [coldstart.py:146-147](src/scraping/coldstart.py#L146-L147) 的 `else` 分支：

```
  → rejected. Which fields are wrong?
    1.title  2.brand  3.price  4.currency  5.list_price
    6.membership_price  7.in_stock  8.gtin  9.image_urls
    (comma-separated, Enter to skip)
  > 3,5
    correct price?      > 2.50
    correct list_price? > 3.00
  Any hint? (Enter to skip)
  > 总价在 .pdp-price__amount，抓到的是每100g单价
```

- 字段编号列表由 R1 的展示字段列表动态生成，两者始终同步
- **逐字段问正确值** — 比笼统一个「为什么不对」更可执行，且可作断言用
- 三步全部可留空跳过，直接回车不报错

#### 4. 三类失败分开追踪

```python
sandbox_failed: list[ColdStartRow]     # parser 崩溃 → 阻塞入库
human_rejected: list[ColdStartRow]     # 人工拒绝   → 阻塞入库
extraction_failed: list[ColdStartRow]  # 抓取失败   → 不阻塞（parser 无机会证明自己）
```

`extraction_failed` 不阻塞 parser 入库，但该 bucket 缺 golden 会由现有 `coverage_shortfall` 机制报出（exit code 2），无需新增逻辑。

失败时的终端输出：

```
Parser NOT saved — 2 of 4 cold-start cases did not pass:
  [PARSER CRASH] tesco.com/xxx (discounted)  AttributeError: 'NoneType' has no 'text'
  [REJECTED]     tesco.com/yyy (membership)  wrong: price, list_price

These URLs could not be fetched (not the parser's fault):
  [FETCH FAIL]   tesco.com/zzz (standard)
```

### 改动文件：`src/scraping/repair/prompts.py`

新增一个薄封装，**复用现有 `parser_gen_prompt`**，不另造一套：

```python
def coldstart_repair_prompt(price_context, site, current_code, feedbacks, failures):
    """Cold-start repair round: current parser + human corrections → fixed parser."""
```

内部构造一个 `AttemptRecord`（`code=current_code`，`errors=` 格式化后的人工反馈 + sandbox 报错），调 `parser_gen_prompt(role=..., attempts=[record])` —— role 按节点位置取 `"middle"` / `"last"`，与运行时阶梯一致。

**为什么复用**：
- [`_format_record`](src/scraping/repair/prompts.py#L350) 已经会把 `rec.code` 完整放进 prompt —— 这正是「让 LLM 知道修哪里」所需，运行时 ladder 一直这么做，只有 cold start 缺
- [`_ROLE_STRATEGY["middle"]`](src/scraping/repair/prompts.py#L154) 已写明 *"focus on fixing the SPECIFIC missing field(s) — do not rewrite the whole parser from scratch"*，正是 R3 想要的语义

人工反馈格式化为「已确认正确 / 需修复」两块（R3 原设计），拼进 `errors`：

```
【已确认正确，保留现有 selector，不要改动】
  - title, brand, in_stock, gtin  (人工 accept，3 个 URL 上均正确)

【提取错误，需修复】
  URL: tesco.com/yyy (membership)
  - price: 抓到 "1.20"，正确值应为 "2.50"
  - list_price: 抓到 None，正确值应为 "3.00"
  人工提示：总价在 .pdp-price__amount，抓到的是每100g单价
```

累积传入 —— 第 3 轮能看到第 1、2 轮的全部反馈。

### 改动文件：`src/scraping/config.py`

**不用 `max_rounds: int`，用模型阶梯** —— 列表长度即轮数，模型名即模型，与现有 `repair_model_ladder` 完全同构，顺带白拿「换名即换模型」：

```python
# --- cold start (own ladder, independent of the runtime repair ladder) ---
# Node 0 generates the first parser; each later node runs one repair round.
# Model ids resolve through providers.py — swapping vendors means changing
# the name here plus that provider's key in .env. Edit both lists together
# (lengths are checked at runtime, same as the repair ladder).
cold_start_model_ladder: list[str] = Field(
    default=["deepseek-v4-flash", "deepseek-v4-pro"]
)
cold_start_temperature_ladder: list[float] = Field(
    default=[0.1, 0.4]
)
```

`deepseek-v4-flash` / `deepseek-v4-pro` 已注册于 [providers.py:43](src/scraping/providers.py#L43)（provider=deepseek，key=`DEEPSEEK_KEY`，且 `non_thinking_extra_body` 已处理 V4 默认开思考的问题）—— **无需改 providers.py**。

**沿用运行时阶梯的三条既有约定**（[agent.py:148](src/scraping/repair/agent.py#L148)、[agent.py:233](src/scraping/repair/agent.py#L233)、[agent.py:239](src/scraping/repair/agent.py#L239)）：
- 两个 list 长度必须相等，运行时 assert
- 最后一个节点开 thinking mode（`enable_thinking=is_last`）
- role 映射：节点 0 → `"first"`，最后节点 → `"last"`，中间 → `"middle"`

轮数由长度决定：2 节点 = 初版 + 1 轮纠错；想要 2 轮纠错就写 3 个节点。长度为 1 时退化为当前的一次性行为（无纠错）。

**行为变更需注意**：[`_gen_initial_parser`](src/scraping/coldstart.py#L251) 当前用 `cfg.repair_model_ladder[0]`（qwen3.7-plus），改为 `cfg.cold_start_model_ladder[0]`（deepseek-v4-flash）后**需要 `.env` 里有 `DEEPSEEK_KEY`**，否则 `make_chat_client` 返回 `None` 并中止 cold start。连带两处需同步：
- `verify_m17.py` 中断言「cold start 使用 `repair_model_ladder[0]`」的那条检查
- [CLAUDE.md](src/scraping/CLAUDE.md) 里「cold start and JSON healing use the first configured model」这句（JSON healing 仍用 `repair_model_ladder[0]`，cold start 不再是）

---

## P0-B. 修复 `is_stale` 检测 stub（R5 Part A）

[`_no_active_parser_passes`](src/scraping/repair/golden.py#L286-L290) 当前硬编码 `return False`，导致 `mark_stale` 永不触发 —— golden 腐烂检测（spec §5.7、D15）实际未生效。

### 改动文件：`src/scraping/repair/golden.py`

```python
async def _no_active_parser_passes(
    db: ScrapeDB, site: str, sample: dict[str, Any]
) -> bool:
    """True iff NO active parser can reproduce this golden's expected_output.

    Used to distinguish "candidate is bad" from "golden has rotted" — if no
    active parser can reproduce it either, the sample is stale, not the
    candidate's fault.
    """
```

逐个跑 active parser 的 sandbox，任一能复现 `expected_output`（用现有 `_matches_expected`）即返回 `False`；全部失败返回 `True`。无 active parser 时返回 `True`（孤儿 golden）。

**两处调用点**（[golden.py:143](src/scraping/repair/golden.py#L143)、[golden.py:154](src/scraping/repair/golden.py#L154)）改为 `await`。`promote_candidate` 本身已是 async，无需改签名。

**性能约束**：最坏情况是 `golden 数 × active parser 数` 次 sandbox subprocess（3×3×4 = 36 次 × 10s timeout）。两点缓解：
1. 仅在 candidate 已经失败时才调用，不在正常路径上
2. 单次 `promote_candidate` 调用内加 `dict[int, bool]` 缓存，同一 sample 只判定一次

---

## P1. 确认面板字段补全（R1）

### 改动文件：`src/scraping/coldstart.py`

替换 [coldstart.py:132-135](src/scraping/coldstart.py#L132-L135) 的硬编码字段列表。

**按类型推导展示方式，不建字段名映射表** —— 否则加字段还是会漏，等于换个地方硬编码：

```python
_TRACING_FIELDS = frozenset({"url", "website", "scraped_at", "source_type",
                             "parser_version", "raw"})

def _display_fields() -> list[str]:
    """Reviewable ProductData fields, in model declaration order."""
    return [n for n in ProductData.model_fields if n not in _TRACING_FIELDS]
```

展示规则由值的类型决定：
- `list` → `image_urls: 6 项 (https://…/_SL1500_.jpg)`
- `str` 且 len > 80 → 截断 + `…`
- `None` → `—`
- 其余 → 原样打印

**高亮 declared page_type 的关键字段**（替代原计划做不到的 MISSING/N/A 区分）：

| declared page_type | 高亮字段 |
|---|---|
| membership | `membership_price` |
| discounted | `list_price` + `price` |
| out_of_stock | `in_stock` + `image_urls` |
| multipack | `variant` |

空值时标 `!! 该桶关键字段为空`。这比 MISSING/N/A 更实用 —— membership 桶的 `membership_price` 为空一定有问题，standard 桶为空则完全正常，上下文本身就给出了答案。

### 砍掉的需求：MISSING vs N/A 区分

原计划 R1 第 3 点要求区分 `MISSING`（未提取到）和 `N/A`（页面本身没有）。**做不到，建议从原文档移除**：parser 返回的 dict 里两者都是 `None`/缺 key，要区分必须改 parser 契约（让 LLM 显式返回 `"N/A"` 标记）+ 改 SCHEMA_HINT + 改 Gate 1，且 LLM 判断「页面本身有没有」本身不可靠。成本远超收益，上面的桶级高亮已覆盖实际需求。

---

## P2. Golden HTML 快照复用（R5 Part B）

### 改动文件：`src/scraping/coldstart.py`

新增 `--force-fetch` 参数；`_batch_fetch` 前按 URL 查 `golden_samples`：

```python
async def _resolve_html(
    scraper, rows: list[ColdStartRow], force_fetch: bool
) -> list[tuple[str, int, str, str]]:   # (url, status, html, source)
```

- 命中 `is_stale=0` 的同 URL 记录 → 用其 `html_snapshot`，source=`goldset`
- 未命中 / `is_stale=1` / `--force-fetch` → 走 BrightData，source=`brightdata`
- 每条 URL 打印来源：`[goldset] https://…` / `[brightdata] https://…`

按 URL 查询需要匹配 `expected_output` 里的 `url` 字段（golden_samples 没有独立 url 列），复用 [`_bucket_accepts_product`](src/scraping/repair/golden.py#L331) 的同款 JSON 取值方式。

### 只读，不新增回写路径

原计划提出「抓取成功后回写快照」。**不做** —— 两个原因：

1. `golden_samples.expected_output` 是 `NOT NULL`，抓取阶段还没有 expected_output 可写
2. 更重要的语义问题：golden 是「**人工确认过的**标准答案 + 快照」。在人工确认前就往表里塞数据，破坏了这个不变量，也会污染 `promote_candidate` 的考核集

缓存写入沿用现有路径即可 —— 人工 accept 后 [`_seed`](src/scraping/coldstart.py#L288) 已经在存 `html_snapshot`。第二次跑同一 URL 时命中的正是它。原计划的验收标准（连跑两次，第二次显示 `[goldset]`）在「第一次被 accept」的前提下自然成立。

---

## 不做的事

| 项 | 理由 |
|---|---|
| 新建 `review_feedback` 表 | 反馈在纠错循环内消费完毕，内存对象足够。若日后想让运行时 ladder 复用 site 级人工提示，再单独立项 |
| Gate 2 新增 member-only 例外 | `_core_price_rule` 已是 `price OR list_price OR membership_price`，member-only 页面已可通过 |
| 区分 MISSING / N/A | parser 契约层面做不到，桶级关键字段高亮已覆盖需求 |
| 改 `ProductData` schema | R1 是纯展示层改动 |
| 抓取阶段回写 golden 快照 | 破坏「golden = 人工确认过」的不变量，且 `expected_output` NOT NULL |

---

## 文件清单

| 文件 | 改动 |
|------|------|
| [coldstart.py](src/scraping/coldstart.py) | 主体：多轮循环、反馈采集、面板重构、全通过门槛、HTML 缓存读取 |
| [repair/golden.py](src/scraping/repair/golden.py) | `_no_active_parser_passes` 实现 + 两处调用改 `await` |
| [repair/prompts.py](src/scraping/repair/prompts.py) | 新增 `coldstart_repair_prompt`（封装现有 `parser_gen_prompt`） |
| [config.py](src/scraping/config.py) | 新增 `cold_start_model_ladder` + `cold_start_temperature_ladder` + 长度校验 validator |
| `tests/verify_m19.py` | 新建（见下） |
| [tests/verify_m17.py](src/scraping/tests/verify_m17.py) | 修：断言 cold start 用 `repair_model_ladder[0]` 的那条检查改为 `cold_start_model_ladder[0]` |
| `tests/README.md` | 登记新文件 |
| [CLAUDE.md](src/scraping/CLAUDE.md) | M19 条目 + 修正「cold start 使用 first configured model」这句（AGENTS.md 由 pre-commit hook 自动同步） |

**不改动**：`models/product_data.py`、`validation/gate2.py`、`storage/database.py`、`providers.py`（DeepSeek 已注册）

---

## 验证

按模块 CLAUDE.md 的验证纪律，新建 `src/scraping/tests/verify_m19.py`，全离线（mock BrightData + LLM），输出 `[PASS]`/`[FAIL]` + `SUMMARY`，日志 tee 到 `verify_m19_output.log`。

**M19.1 全通过门槛（R4）**
- 4 个 URL，mock 人工输入 `y y n y` → parser 未入库，`parsers` 表无新增
- 同样 4 个，输入 `y y y y` → parser 入库 + 4 条 golden
- 1 个 URL sandbox 崩溃、其余 accept → parser 未入库，终端打印 `[PARSER CRASH]`
- 1 个 URL 抓取失败、其余 accept → parser **入库**（不阻塞），`coverage_shortfall` 报缺失桶，exit code 2

**M19.2 纠错循环（R2+R3）**
- mock LLM 节点 0 返回坏 parser、节点 1 返回好 parser → 纠错后全通过并入库
- 断言纠错 prompt 中包含：上一轮的 parser 源码、人工填的正确值、hint 原文
- 反馈三问全部直接回车 → 不报错，`corrections` 为空
- 单节点阶梯（`["deepseek-v4-flash"]`）→ 退化为一次性行为，拒绝即终止不入库
- 阶梯耗尽仍未全通过 → 不入库，退出码非 0
- 「Continue 纠错?」答 `n` → 立即终止，不消耗剩余节点
- 重跑后输出未变的已 accept 条目不再提示人工（断言 `input_fn` 调用次数）

**M19.2b 阶梯配置（config）**
- 两个 ladder 长度不等 → 运行时 assert 报错（同 [agent.py:148](src/scraping/repair/agent.py#L148) 的既有约定）
- 断言节点 0 用 `cold_start_model_ladder[0]`、最后节点 `enable_thinking=True`（mock `make_chat_client` 捕获入参）
- 把 ladder 换成 `["qwen3.7-plus", "qwen3.7-plus"]` → 仅靠改名即切回 Qwen，无其他改动

**M19.3 面板字段（R1）**
- 捕获 stdout，断言含 `membership_price`、`image_urls`、`unit_price`、`availability_raw`
- `image_urls` 显示为数量 + 首元素形式
- 超长 title 被截断至 ~80 字符
- membership 桶 + `membership_price=None` → 打印关键字段告警

**M19.4 is_stale 检测（R5-A）**
- 构造 golden，其 `expected_output` 无任何 active parser 能复现 → `_no_active_parser_passes` 返回 `True`
- 存在一个能复现的 active parser → 返回 `False`
- active parser 列表为空 → 返回 `True`
- `promote_candidate` 中 candidate 在腐烂 golden 上失配 → golden 被 `mark_stale`，candidate **不**被拒
- 缓存生效：同一 sample 在单次 `promote_candidate` 内只判定一次

**M19.5 HTML 缓存（R5-B）**
- 已有非 stale golden 的 URL → 打印 `[goldset]`，mock unlocker 的 `fetch` 未被调用
- `is_stale=1` 的 URL → 走 `[brightdata]`
- `--force-fetch` → 即使命中也走 `[brightdata]`
- 断言 `golden_samples` 行数在抓取阶段未增加（只读不写）

**回归**：`verify_m9.py`（promote/prune，受 `_no_active_parser_passes` 改动影响最大）、`verify_m11.py`（cold start CLI）、`verify_m17.py`（含需修改的模型断言）、`verify_m18.py`（provider registry）需全部重跑通过。

**前置条件**：`.env` 需有 `DEEPSEEK_KEY`，否则 cold start 在初版生成阶段就会因 `make_chat_client` 返回 `None` 而中止。离线 verify 脚本 mock 掉 LLM，不受影响。
