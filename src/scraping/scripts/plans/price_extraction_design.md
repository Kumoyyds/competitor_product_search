# 商品价格提取 Agent 改造方案

> 目标:解决自动生成的 parser 频繁**漏抓价格**(`price` / `list_price` / `membership_price`)以及**无法在三者间区分**的问题。

---

## 1. 问题诊断

### 1.1 现象
自动生成的 parser 在折扣页、会员折扣页上:
- 只抓到当前售价 `price`,漏掉原价 `list_price`;
- 漏掉会员价 `membership_price`;
- 或抓到多个价格但归类错误(把会员价当普通价、把原价当现价)。

### 1.2 根因(以 Argos 折扣页实测为证)

在一个真实 Argos 折扣页上验证得到以下事实:

| 价格 | 数值 | 存在位置 |
|---|---|---|
| `price`(现价) | 52.20 | ✅ JSON-LD `Offer.price` |
| `list_price`(原价) | 58.00 | ❌ 仅存在于渲染后的 DOM 文本节点 `<span data-test="price-was">Was £58.00</span>` |
| `window.__data.wasPrice` | 0 | ⚠️ 脏数据,未被填充 |

**结论:`list_price` / `membership_price` 这类字段在结构上几乎永远不在 JSON-LD 里。**
JSON-LD 的 `Offer.price` 语义上只表达"当前售价",促销原价、会员价通常只出现在展示层 DOM 中(struck-through 价格 + `Was` / `RRP` / `Clubcard` / `Save X%` 等 label)。

### 1.3 现有策略为何在系统性丢价格

当前策略存在两处相互叠加的问题:

1. **截断策略"JSON-LD 最优先 + 24k 字符上限"**
   → 携带 `list_price` 的 DOM 子树(如 `price-was` span)在截断阶段就可能被丢弃,根本进不了 LLM 上下文。

2. **Prompt 里无条件的 "PREFER JSON-LD-based extraction over DOM selectors"**
   → 即使 DOM 子树侥幸没被截断,该指令也在引导模型看到 JSON-LD 有 `price` 就收工,主动忽略 DOM 里的原价/会员价。

**核心判断:约 80% 的漏抓不是模型能力问题,而是"目标数据在截断阶段就没进上下文 / 或被 prompt 劝退"。模型再强也无法抽取不存在于其上下文中的信息。**

---

## 2. 方案总览

围绕现有 agent 架构做一次改造,不推翻重来。五个改动,按杠杆从大到小:

| # | 改动 | 解决什么 | 杠杆 |
|---|---|---|---|
| 1 | **价格感知的 pre-pass**(替代字符截断) | 保证所有价格证据保真进入上下文 | ★★★★★ |
| 1.5 | **主商品锚定 + 推荐位硬删**(pre-pass 的必备补充) | 避免抓到"猜你喜欢/推荐商品"的价格、并减少 LLM 输入 | ★★★★★ |
| 2 | **"抽取"与"分类"两步拆分** | 解决三种价格混淆 | ★★★★ |
| 3 | **确定性召回校验 + 自愈重试** | 自动发现并修复漏抓 | ★★★★ |
| 4 | **多变体页面驱动 parser 生成** | parser 见全所有价格结构 | ★★★ |
| 5 | **改写 prompt 里的 JSON-LD 指令** | 停止引导模型忽略 DOM 价格 | ★★ |

如果只做一件事,做 #1;但 #1 与 #1.5 是**一对**——#1 为不漏抓而无条件捞入所有价格,#1.5 负责把其中的推荐位价格剔除,二者必须同时上线,否则 #1 会放大"混入推荐位价格"的问题。

---

## 3. 组件详细设计

### 3.1 组件一:价格感知 Pre-pass(最高优先级)

**定位**:插在"把 HTML 喂给 LLM"之前的一层确定性预处理。把通用的字符数截断,替换成"价格证据优先保真"的定向压缩。

**输入**:原始 HTML 字符串。
**输出**:一个压缩上下文包,结构如下:

```
{
  "json_ld_blocks": [ ... ],        # 所有 application/ld+json 原样保留
  "price_evidence": [ ... ],        # 所有带货币的 DOM 子树(见下)
  "head_excerpt": "...",            # <head> 关键 meta(og:*, canonical, title)
  "main_excerpt": "..."             # <main> / product div 的剩余内容(用剩余预算填充)
}
```

**保真规则(关键)**:

1. **无条件保留全部 JSON-LD block**(它们本就紧凑,用于产品身份 + 现价)。

2. **扫描所有"价格证据节点"并连同上下文子树一起保留**:
   - **主信号——货币金额正则**(不依赖任何关键词):`[£€$]\s?\d[\d,]*\.?\d*` 或 `\d+\.\d{2}\s?(GBP|EUR|USD)`;
   - **辅助信号——价格语义关键词种子表**(大小写不敏感,多语言):
     `was / rrp / save / off / clubcard / prime / member / loyalty / 会员 / 原价 / 现价 / 优惠 / Auf Lager` 等;
   - 对每个命中节点,**向上取 1–2 层父节点**,保留该子树的:
     - 完整文本;
     - class / id / 所有 `data-*` 属性(label 常藏在 `data-test="price-was"` 这类属性里);
     - 是否含 struck-through 样式线索(`line-through` / class 名含 `was`/`strike`/`old`)。

> **关于关键词表:需要自行维护吗?** —— 需要维护,但只是一份**小而稳的种子表**,不是穷举所有站点/语言的大 corpus。分工如下:
>
> - **真正的兜底是货币金额正则,不是关键词。** 只要节点里有"货币符号 + 数字",就无条件保留,和关键词无关。这一层决定了绝大部分召回率,而它是纯确定性的、跨站点通用的,基本不用维护。
> - **关键词只是额外一层保险**,覆盖极少数"金额写法怪异导致正则漏掉、但有明显 label"的情况。这里放宽是安全的——pre-pass 的目标是**召回**(多留几个无关子树顶多浪费点预算,漏留才致命),所以种子表**宁多勿少、命中即保留**,不需要精确。
> - **不要在这一层做语义判断**(判断"这是原价还是会员价")。那是组件2-B 分类层的职责,由规则 + agent 语义兜底完成。pre-pass 只管"值不值得留",不管"它是什么"。
>
> **实践建议**:种子表初始化 20–30 个词覆盖你的目标市场(英/德 + 中文)即可上线;后续只在**召回校验(组件3)发现某个金额因无货币符号又无关键词而被漏掉**时,才回补一个词。换句话说,种子表的增长由数据驱动、按需扩充,而不是一开始就试图写全。

3. **剩余字符预算**再分配给 `head`(og meta + canonical)和 `main`。

**要点**:不是"给更多字符",而是"保证正确的字符活下来"。价格证据享有最高保留优先级,`main` 的普通描述文字才是被截断的对象。

---

### 3.1.5 主商品锚定与推荐位剔除(pre-pass 的必备补充)

**问题**:pre-pass 为了不漏抓,把"所有带货币的子树"无条件捞进上下文——这会把页面上"猜你喜欢 / 推荐商品 / 交叉销售"里那 N 个商品的价格**也一起捞进来**。于是"解决漏抓主商品原价"反而放大了"混入推荐位价格"的风险。这两个目标是对立的,pre-pass 必须同时处理,否则下游会在一堆价格里选错。

不能靠 LLM prompt 事后猜哪个是主商品——那是最不可靠的一环。正确做法是在 pre-pass 阶段**用确定性硬信号锚定主商品**,给每个价格标注归属,并把确定是推荐位的价格**直接删掉**,不送进 LLM。

#### (A) 如何确定"主商品锚点"——三重信号交叉印证

按可靠性从高到低:

1. **URL 商品 ID(最硬)**。单商品页 URL 一定带 ID:`/product/3284476`、`/dp/<ASIN>`、`/shop/en-GB/products/<id>`。这个 ID 会出现在主商品区块里(JSON-LD 的 `sku`/`url`、canonical link、主图 src、主 CTA 的 data 属性),而**推荐位商品的 ID 与它不同**。→ 含 URL 目标 ID 的子树 = 主商品锚点,基本不会错。
2. **页面级单例标题(标题内容的来源)**。`<title>`、`og:title` 是**整页唯一**的,定义上就是主商品,推荐位不可能占用这个位置。JSON-LD `Product.name` 通常与之一致。→ 三者取交集得到 **canonical 标题**。
3. **`<h1>` 只用于验证/定位,不作为标题来源**。一个页面可能有多个 h1(某些推荐块也用 h1),所以**先用 1、2 得到 canonical 标题内容,再去 DOM 里找哪个 h1 文本与之匹配**,匹配上的 h1 所在子树才是主商品区块的**位置锚**。

> **关键原则:标题的"内容"从页面级单例(title / og:title / JSON-LD name)取,标题的"DOM 位置"再靠 h1 匹配去定位。** 二者交叉验证,就不会出现"锚点标题其实是推荐位标题"的循环依赖问题。
>
> 理想情况下三者应互相印证:`og:title` ≈ JSON-LD `name`,且 JSON-LD `sku` == URL ID。三重对上时锚点几乎不可能错;只有当它们互相矛盾(罕见)时,才降级交给 LLM 裁决。刚才的 Argos 实测里 `sku:3284476` 与 URL、`name` 与 `og:title` 完全闭环,即最干净的锚。

#### (B) 三态归属判定——硬条件删,存疑留

pre-pass 给每个价格证据判定归属,分三种处理:

| 归属判定 | 依据(需满足) | 动作 |
|---|---|---|
| **确定是推荐位** | 落在已知 cross-sell 容器内(class/id 含 `recommend/related/similar/carousel/also-bought/rail/sponsored` 等)**且**附近标题 ≠ canonical 标题(**双重命中**) | **直接删除**,不进 LLM 上下文 |
| **确定是主商品** | 落在"含 URL 商品 ID 的锚点子树"内,**或**附近标题 == canonical 标题 | 保留,标 `inside_main` |
| **存疑** | 两个硬条件都不满足(既不在已知推荐容器,也没匹配上锚点) | **保留 + 标 `ambiguous`**,交 LLM 裁决 |

> **为什么删除必须"双重命中"**:只靠单一弱信号(如"class 名含 related")就删是危险的——个别站点主商品容器的 class 命名也可能碰巧含某个关键词。必须**在已知推荐容器内 AND 标题对不上**同时成立才删。这样既拿到"减少 LLM 输入 / 省 token / 降低选错概率"的收益,又守住"绝不误删主商品"的底线。存疑的一律保留,宁可多送给 LLM 几个也不误删。

#### (C) 输出:每个价格证据带上归属标记

pre-pass 输出的 `price_evidence` 每一项新增三个字段:

```
{
  "value": "58.00",
  "currency": "GBP",
  "label_text": "Was",
  "css_hint": "price-was",
  "anchor_relation": "inside_main",   # inside_main | cross_sell | ambiguous
  "container_type": "product-main",   # 最近祖先容器类型
  "matches_canonical_title": true     # 附近标题是否匹配 canonical 标题
}
```

其中 `anchor_relation == "cross_sell"` 的项在(B)阶段已被删除、不会出现在输出里;留下的只有 `inside_main` 和 `ambiguous`。下游据此:分类层(组件2-B)优先只从 `inside_main` 选价格,`ambiguous` 再交 LLM 用标题匹配裁决;召回校验(组件3)也只统计这两类金额(见 3.3)。

---

### 3.2 组件二:"抽取"与"分类"两步拆分

**问题**:当前 prompt 让生成的一个函数同时干两件难事——(a) 在页面上定位每个价格,(b) 语义上判断每个是 `price` / `list_price` / `membership_price`。耦合导致脆弱。

**改法**:拆成两层。

**Step A — 全量价格候选抽取**
Parser 只负责"把页面上每个价格连同上下文摘出来",不做归类。每个候选输出:

```
{
  "value": "58.00",
  "currency": "GBP",
  "label_text": "Was",            # 邻近的 label / 前后文本
  "struck_through": true,          # 是否划线
  "css_hint": "price-was",         # class / data-* 线索
  "source": "dom",                 # dom | json_ld
  "anchor_relation": "inside_main" # 来自 3.1.5:inside_main | ambiguous(cross_sell 已被删)
}
```

**Step B — 分类(规则优先,LLM 兜底)**
根据候选的 label / 划线 / css_hint 归位。规则示例:

- `struck_through == true` 或 label ∈ {was, rrp, 原价} → `list_price`
- label / css 含 {clubcard, prime, member, loyalty, 会员} → `membership_price`
- 其余唯一的、当前展示的价格 → `price`
- coupon / promo-code 折扣 → 归入 `price`(普通折扣,不是会员折扣)
- **三价并存**(现价 + 原价 + 会员价)时全部填,它们不互斥
- **归属优先级**:优先只从 `anchor_relation == inside_main` 的候选里选 `price/list_price/membership_price`;`ambiguous` 的候选仅在主商品范围内价格不足时,由 LLM 用"附近标题是否匹配 canonical 标题"裁决后再采用。

优势:把 "Was £58" / "Clubcard £X" / "£52.20" 连同标签一起摆给分类器,语义线索齐全,比让模型在 codegen 阶段凭空推理稳得多。

---

### 3.3 组件三:确定性召回校验 + 自愈重试

**契合你现有的 verify / 自愈循环(execute→analyze→fix→retry)。**

**召回校验逻辑**:
1. 用正则数出**主商品范围内**的不同货币金额——即 3.1.5 锚定后 `anchor_relation ∈ {inside_main, ambiguous}` 的金额,**去重**。**注意:统计范围不是"页面上所有金额",否则会因为"没抓那 N 个推荐位价格"而误判漏抓,触发无意义重试。已被判为 `cross_sell` 而删除的价格不计入。**
2. 对比 parser 输出里实际填了几个价格字段;
3. 若主商品范围内有 N 个不同金额、parser 只返回 M 个且 M < N → **判定漏抓**。

**触发自愈**:
- 判定漏抓 → 触发 parser 重新生成;
- 把**漏掉的那些金额及其上下文子树**高亮塞回 prompt(而不是笼统地说"你漏了")。

这给你的自愈循环在"价格召回"这个维度上一个**明确、廉价、确定性**的判据,比"跑通不报错就算过"强得多。

**误报控制**:同一金额多处出现(如面包屑)已被去重排除;推荐位/交叉销售区块的价格已在 3.1.5 主商品锚定阶段被硬条件删除(不进上下文、也不进本校验的统计分母),因此不会造成"页面有 20 个价格但主商品只有 3 个"式的误判漏抓。

---

### 3.4 组件四:多变体页面驱动 parser 生成

**隐蔽的结构性坑**:若从**单张普通页**生成 parser,它压根没见过 `price-was` 结构,之后必然在折扣页/会员页漏抓。

**改法(二选一或叠加)**:

- **A. 变体组生成**:每个 site 用一**组**页面(普通 + 折扣 + 会员 + 缺货)一起生成 / 精炼 parser,强制它见全所有价格结构。
- **B. Per-site 价格选择器 hint**:为每个 site 维护一份价格字段位置 map,注入 prompt。示例:

  ```
  Argos:
    price        → JSON-LD Offer.price
    list_price   → [data-test="price-was"]   (文本形如 "Was £58.00")
    discount     → [data-test="price-save"]  (文本形如 "Save 10%")
  Tesco:
    membership_price → Clubcard 价格组件(loyalty label)
    price            → 常规价格组件
  ```

  这份 map 可由一个"site profiling"步骤**自动建一次并缓存**(契合你已有的 caching 差异化能力)。

---

### 3.5 组件五:改写 prompt 中的 JSON-LD 指令

把当前无条件的 "PREFER JSON-LD-based extraction over DOM selectors" 改成**有条件**的。建议替换为:

> Use JSON-LD for the canonical **current** price and product identity (title, brand, sku, current `price`).
> However, `list_price` (RRP / "was" price) and `membership_price` are **usually NOT present in JSON-LD** — you MUST inspect the rendered DOM for struck-through prices and labels such as "Was", "RRP", "Save X%", "Clubcard", "Prime price", "member price".
> On a discount page or member page, returning `None` for `list_price` / `membership_price` when a struck-through or labeled price is visible in the DOM is a **recall failure**, not an acceptable result.
> When `price` + `list_price` + `membership_price` co-occur, fill in **all three** — they are not mutually exclusive.

---

## 4. 数据流(改造后)

```
原始 HTML
   │
   ▼
[组件1]   价格感知 Pre-pass ── 捞出 JSON-LD + 全量带货币子树
   │
   ▼
[组件1.5] 主商品锚定(URL 商品 ID + canonical 标题 + DOM 容器)
   │          ├─ cross_sell(双重命中)──► 直接删除,不进上下文
   │          └─ inside_main / ambiguous ──► 保留并标注归属
   ▼
压缩上下文包(JSON-LD + 已剔除推荐位的价格证据 + head + main)
   │
   ▼
[组件5] 改写后的 Prompt + 上下文包
   │
   ▼
LLM 生成 parser
   │
   ▼
[组件2-A] Parser 抽取全量价格候选(带 label / 划线 / css_hint / anchor_relation)
   │
   ▼
[组件2-B] 分类(优先 inside_main):price / list_price / membership_price
   │
   ▼
[组件3] 召回校验:主商品范围金额数(去重) vs parser 输出数
   │
   ├─ 通过 ──► 输出 ProductData
   │
   └─ 漏抓 ──► 高亮漏掉的金额+上下文 ──► 回到 LLM 重新生成(自愈)
```

---

## 5. 分阶段落地

| 阶段 | 内容 | 预期收益 |
|---|---|---|
| **P0(立即)** | 组件1 pre-pass + **组件1.5 主商品锚定/推荐位硬删** + 组件5 改 prompt | 消除大部分漏抓(数据终于进上下文了),同时不被推荐位价格污染 |
| **P1** | 组件3 召回校验 + 自愈重试 | 剩余漏抓被自动发现并修复 |
| **P2** | 组件2 抽取/分类拆分 | 解决三价混淆 / 归类错误 |
| **P3** | 组件4 多变体生成 + per-site hint 缓存 | parser 结构性覆盖全,跨页面类型稳定 |

P0 是性价比最高的一步:改动小,直接命中 80% 的漏抓根因。**组件1 与 1.5 必须同期上线**——只上 1 会把推荐位价格一起灌进上下文,需靠 1.5 剔除后才闭环。

---

## 6. 一句话总结

漏抓的本质不是"模型不会抽",而是"**你在把数据喂给模型之前,就已经用截断和 prompt 把价格证据丢掉 / 劝退了**"。先用确定性的 pre-pass 保证价格证据保真进上下文,并用主商品锚定(URL 商品 ID + canonical 标题 + DOM 容器)把推荐位价格硬删掉(P0);再用召回校验兜底(P1);最后用抽取/分类拆分解决混淆(P2)。**锚定与召回都是确定性的硬信号,LLM 只在存疑处兜底——这是整套方案稳定的关键。**
