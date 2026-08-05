# Cold Start 流程改进需求（Scraping Module）

> 背景：当前 cold start 流程已跑通，但在「人工确认面板」「反馈回灌」「parser 落库门槛」「goldset 复用」四处存在缺口。
> **实施前先读 `scraping_module_spec.md`**，本文件描述的是对已有设计的补全，不是新机制。凡与已有 D 编号决策冲突的，以 spec 为准并回报冲突点。

---

## 0. 当前流程（现状）

```
cold-start URL 集合（按 page_type 分桶）
  ├─ standard
  ├─ discounted
  ├─ membership
  └─ out_of_stock
        │
        ▼  执行抓取 + parser 生成
   终端打印提取结果
   （title / brand / price / currency / list_price / in_stock / gtin）
        │
        ▼  human in the loop
   Y: accept   N: skip   q: quit
        │
        ▼
   保存 parser  →  保存 golden set
```

---

## R1. 确认面板字段补全与展示规则

### 现状问题
`membership_price` 和 `image_urls` **在 `models/product_data.py` 中已经存在**，但确认面板只打印了 7 个字段，这两项没有输出——人工无法确认它们是否被成功提取。这是**展示层缺口，不是 schema 缺口**。

### 需求
1. **不新增字段。** 以 `models/product_data.py` 的实际字段定义为准，确认面板的输出字段列表应与 `ProductData` 的字段保持同步，而不是硬编码一份子集。建议直接遍历模型字段生成面板，避免以后加字段又漏打印。
2. **确认面板打印全部关键字段**，包括 `membership_price`、`image_urls`。
   - 相关问题：Gate 2 现有「in_stock 时 price 必填」的规则，与「只有会员价、无普通价」的页面可能冲突。这是校验逻辑问题，不是字段问题，见 §决策点 A。
3. **展示遵循「证明提取成功」原则，而非逐字校对**：

| 字段类型 | 展示方式 |
|---|---|
| 短标量（price / currency / in_stock / gtin / brand） | 完整打印 |
| 长字符串（title、description） | 截断至 ~80 字符 + `…` |
| 列表（image_urls） | 只打印数量 + 首元素截断，如 `image_urls: 6 项 (https://…/_SL1500_.jpg)` |
| 空值 | **显式区分**：`MISSING`（未提取到）vs `N/A`（页面本身没有该信息） |

最后一条很重要——人工需要能区分「parser 没抓到」和「这个页面本来就没有会员价」，否则 N/Y 的判断会失真。

### 验收
对 membership 桶的一条 URL 跑 cold start，终端能看到 `member_price` 有值、`image_urls` 显示条数，且 out_of_stock 桶的 `price` 为空时显示为可区分的标记。

---

## R2. 拒绝（N）时采集结构化人工反馈

### 现状问题
`N: skip` 直接跳过，人类脑子里的「为什么不对」完全丢失，下一轮 LLM 只能盲猜。

### 需求
按下 `N` 后进入反馈采集，三个问题，**全部可留空跳过**（不强制，避免拖慢冷启动）：

1. **哪些字段错了**：给出字段名编号列表，支持多选（如 `2,5`）。
2. **为什么判定不匹配**：自由文本，一行。
3. **正确值 / 提示**：自由文本，可选。例如「会员价在 `.price-club__value` 里」「这个 price 抓到的是每 100g 单价」。

反馈以结构化对象存储（字段名列表 + 原因 + 提示 + URL + page_type + 时间戳），不要存成一整坨自由文本。存储位置：优先复用现有 `escalations` 表的结构，若语义不符再新增列/表，**先确认再动 schema**。

### 验收
N 之后能落库一条带 `wrong_fields` 数组的反馈记录；直接回车能跳过全部三问且不报错。

---

## R3. 反馈回灌给 backup LLM（repair / cold-start agent）

### 现状问题
修复阶段的 LLM 拿不到上一轮的对错信息，容易整段重写 parser，把本来正确的字段一起改坏。

### 需求
构造 agent 上下文时，**显式分成两块**：

```
【已确认正确，不要改动】
  - title: "..."   (人工 accept)
  - brand: "..."   (人工 accept)

【提取错误，需修复】
  - price: 抓到 "£1.20"
    人工反馈：这是每 100g 单价，商品总价在下方
    人工提示：总价在 .pdp-price__amount
```

要点：
- 正确项要明确标注「保留现有 selector」，让 agent 做**局部修复**而非全量重写。
- 同一 site 的冷启动过程中，历次反馈**累积**传入（第 3 桶失败时，agent 应看得到第 1、2 桶的确认结果）。
- 该上下文注入到三级修复阶梯的哪几级，需按 spec 中修复阶梯的设计确认（flash → flash-with-context → pro，注意 `flash-with-context` 这一级是否已经是这个用途）。

### 验收
修复阶梯第二次调用的 prompt 中能看到上一轮的正确字段清单和人工反馈原文。

---

## R4. parser 落库门槛：全 bucket 通过才保存

### 现状问题（疑似缺陷）
当前 repair agent **首次成功即写入 parsers 表并直接复用**，没有对每个 bucket 做 `expected_output` 精确比对——这正是 golden set promote/prune 门（M9）与 cold-start 路径集成（M11）**未接线**时的预期症状。

### 需求
1. 候选 parser 必须在**所有** golden bucket（standard / discounted / membership / out_of_stock）上通过**逐桶 `expected_output` 精确比对**，才允许写入 `parsers` 表并置为 active。
2. 未全通过的 parser 保持 **candidate / 临时态**，不进入正式路由。
3. 通过 Gate 1 + Gate 2 **不等于**通过 promote——Gate 只做类型与结构可行性检查，精确比对是更强的条件，两者不可互相替代。
4. 任一 bucket 失败时，终端要明确打印是哪个 bucket、哪个字段不一致（expected vs actual）。

**先做的事**：核对 M9 / M11 的接线状态，确认 promote 步骤实际执行的是逐桶精确比对，而不是只跑了 Gate1/Gate2 就放行。这是本次改动的根因项。

### 验收
构造一个只在 3/4 桶上正确的 parser，它不应出现在 `parsers` 表的 active 记录中，且终端报出失败的桶名与差异字段。

---

## R5. Golden set HTML 快照复用，避免重复抓取

### 现状问题
每次 cold start 都重新走 Bright Data 取 HTML，成本高、速度慢，且同一页面不同时间的 HTML 有差异，不利于复现调试。

### 需求
1. **写入**：人工确认通过后，把该次抓取的 **HTML 快照 + expected_output** 存入 `golden_samples`（该表已存在，复用其现有字段）。
2. **读取**：下次触发 cold start 时，**先按 URL 查 `golden_samples`**：
   - 命中且 `is_stale = False` → 直接使用本地 HTML，**不调用 Bright Data**；
   - 未命中或 `is_stale = True` → 正常抓取，并在成功后回写快照。
3. **来源提示**：终端每条 URL 都打印数据来源，如 `[goldset] https://…` / `[brightdata] https://…`，便于人工判断这轮测的是缓存还是实网。
4. **强制刷新开关**：提供 `--force-fetch` 参数绕过缓存，用于验证页面改版。
5. 与已有的 `is_stale` 自动过期检测与重新补给逻辑衔接，**不要另造一套缓存失效机制**。

### 验收
同一条 URL 连跑两次 cold start，第二次终端显示 `[goldset]`，且无 Bright Data 请求产生。

---

## 需要先拍板的决策点

请在动代码前给出结论：

**A. member-only 定价页面的 Gate 2 处理**

字段本身已存在，要定的是校验规则：若某页面**只有会员价、没有普通价**，`price` 该填什么？
- 留空 → 触发 Gate 2 的「in_stock 必须有 price」规则，整条数据被判不可行；
- 填入会员价 → 通过校验，但丢失了「这是会员专属价」的信息，下游比价会把它当成人人可付的价格。

建议方向（待确认）：`price` 保持「任何人可付的价格」语义，member-only 页面 `price` 留空、`membership_price` 有值时，在 Gate 2 中作为条件例外放行。**这是对 Gate 2 新增一条例外，需要显式决定并写进 spec，不要默默改 `feasible_check`。**

**B. 人工反馈的存储归属**
- 复用 `escalations` 表，还是新建 `review_feedback` 表？
- 反馈中提到的错误短语是否要进 `invalid_target_phrases`？（倾向：不进——该表是为无效目标检测服务的，语义不同。）

---

## 给 Claude Code 的执行约束

- **先读 `scraping_module_spec.md` 全文**，特别是 golden set promote/prune（§5.4③）、冷启动确认通道（D22 / §5.9）、Gate 2 范围（D18）、修复阶梯相关章节，再动手。
- 本文件的需求若与 spec 已有决策重复或冲突，**先报告并引用对应 D 编号 / 章节号**，不要直接实现第二套机制。
- **R1 不涉及 `ProductData` schema 变更**，字段已存在，只改展示层。涉及新表/新列的部分（R2）**先给出改动方案待确认，不要直接落地**。
- 优先级：**R4 > R1 > R5 > R2 > R3**（R4 是正确性根因，R1 是它的前置数据完整性，R5 是效率，R2/R3 是修复质量提升）。