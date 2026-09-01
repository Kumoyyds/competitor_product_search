# Matching / New-Input / Rerun 设计理解（Claude, 2026-08-29）

来源：用户提供的三张设计图（New Input 流程、matching 机制、rerun 流程）。
本文最初是理解复述；文末问题已于 2026-08-31 决定，并已落实到实现。当前操作说明以模块 README 和 `docs/orchestrator_storage.md` 为准。

范围：已实现的 `src/matching/`、`src/orchestrator/` 和单一 `orchestrator.db`。
`src/search/` 与 `src/scraping/` 已实现，本文只描述与它们的对接点。

---

## 1. 两个操作入口

系统对外只有两个操作：

| 操作 | 入口参数 | 是否跑 search | 是否跑 scraping |
|---|---|---|---|
| **operation 1 — New Input** | 商品清单（xlsx/csv/json） | 是 | 是 |
| **operation 2 — Rerun** | `batch_id`（+ 可选 `search_title` 列表） | 否（复用已存 URL） | 是 |

`batch_id` 是贯穿两个操作的唯一句柄：operation 1 分配它，operation 2 靠它回查。

---

## 2. operation 1 — New Input 全流程

```
  用户文件                      归一化层
  xlsx / csv / json  ────────▶  input_whateverformat → JSON
                                分配 batch_id
                                      │
              ┌───────────────────────┴───────────────────────┐
              │  必填: title, country/region, site_name       │
              │        （触发 search 的最小集）               │
              │  选填: image_urls: list[url], gtin            │
              │        （不参与 search，只在 matching 用）    │
              └───────────────────────┬───────────────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    │                                   │
                    │  ①  New Input（原样保留到最后）   │
                    │                                   │
                    ▼                                   │
              ┌───────────┐                             │
              │  search   │                             │
              │  module   │                             │
              └─────┬─────┘                             │
                    │  ②  Search Output                 │
                    │     title / URL                   │
                    │                                   │
                    │  ── 只有 URL 往下传 ──▶           │
                    ▼                                   │
              ┌───────────┐                             │
              │ scraping  │                             │
              │  module   │                             │
              └─────┬─────┘                             │
                    │  ③  Scraping Output               │
                    │     必填: title                   │
                    │     选填: brand, image_urls,      │
                    │           gtin, variant           │
                    │     （= scraping.db results 表    │
                    │        里的 product_data）        │
                    ▼                                   ▼
              ┌─────────────────────────────────────────────┐
              │            Comparison  =  matching           │
              │       比对的两端是 ① New Input 和 ③         │
              │       Scraping Output —— ② 只是拿 URL 的桥  │
              └──────────────────┬──────────────────────────┘
                                 │
                 yes / success   │   no / fail
              ┌──────────────────┴──────────────────┐
              ▼                                     ▼
      ┌───────────────┐                    ┌─────────────────┐
      │ Valid Database│                    │ Failure Database│
      └───────────────┘                    └─────────────────┘
```

**关键点**：比对的是"用户原始输入"对"抓回来的页面数据"，不是"搜索候选"对"页面数据"。
search 的产出在这条链上只有 URL 是必需的。

---

## 3. matching 机制（核心）

判定按"越便宜越先跑"排列，任何一层拿到结论就短路：

```
   ┌──────────────┐
   │  gtin same?  │
   └──┬────────┬──┘
 yes  │        │  no
      │        │        证据写入 context
      │        ▼
      │  ┌────────────────┐
      │  │ variants same? │──── no ────▶ ┌──────┐
      │  └────────┬───────┘              │ FAIL │
      │       yes │                      └──────┘
      │           │   证据写入 context      ▲
      │           ▼                         │
      │  ┌──────────────────────────┐       │
      │  │  是否走图片比对?         │       │
      │  │  1. 用户 toggle 打开     │       │
      │  │  2. 两边都有 image_urls  │       │
      │  └────┬─────────────────┬───┘       │
      │   yes │                 │ no        │
      │       ▼                 │           │
      │  ┌──────────────────┐   │           │
      │  │  vision 模型比对 │   │           │
      │  └────────┬─────────┘   │           │
      │           │ 视觉证据    │           │
      │           ▼             │           │
      │      [  context  ]      │           │
      │       累积证据袋 ◀──────┘           │
      │           │                         │
      │           ▼                         │
      │     ┌───────────┐                   │
      │     │ LLM 判断  │───── no ──────────┘
      │     └─────┬─────┘
      │       yes │
      ▼           ▼
   ┌─────────────────┐
   │     SUCCESS     │
   └─────────────────┘
```

> 原图里画了**两个** `LLM 判断` 方框（走图片分支的和不走图片分支的）。
> 我理解它们是同一个判断节点，区别只是 context 里有没有视觉证据这一条，
> 所以上图合并成一个。如果你的本意是两个 prompt 不同的独立节点，需要更正。

### 各节点语义

| 节点 | 判定内容 | 备注 |
|---|---|---|
| `gtin same?` | 全局唯一码相等 | 相等即直接成立，不再往下走 |
| `variants same?` | brand + 规格参数（原图注：「Variants 是 brand，规则参数这种」） | 唯一一个可以**单独判 FAIL** 的规则层 |
| 图片开关 | ①用户 toggle ②两边都有图，**两个条件都满足**才走 vision | 任一不满足 → 跳过 vision，直接 LLM |
| `vision 模型比对` | 视觉证据，不出结论 | 见下 |
| `context` | 逐步累积的证据袋，不是一次性快照 | gtin / variants / vision 三处都往里写 |
| `LLM 判断` | 读 context 做最终裁决 | yes → SUCCESS，no → FAIL |

### variants 比对的陷阱（原图批注）

```
  只有「同单位、不同值」才构成 FAIL 证据：
      15ml   vs  30ml            ──▶  确实不同
  多件装声明要小心，同一个东西可能有两种写法：
      15ml × 20                  ──┐
      20 件各 15ml，共 300ml     ──┴──▶  同一个东西，不能判不同
```

### vision 那一步用 `image-load-compression`

该包**已经装好**，不用再加依赖：

- 声明在 `pyproject.toml:12`，editable 指向 `../image-load-compression`（`pyproject.toml:36`），`uv.lock` 已锁。

```python
from image_load_compression import compare_batch, load_compare_config

results = await compare_batch([(input_image_urls, scraped_image_urls)], cfg)
results[0].comment   # 英文证据描述（agreements / conflicts）
results[0].status    # OK / 无可用图 / 请求失败
```

它**故意不给 verdict、不给 score，只给 evidence** —— 正好就是图上
`vision 模型比对 → context → LLM 判断` 的形状：视觉证据进袋子，裁决权留给 LLM。

---

## 4. operation 2 — Rerun 流程

```
   ┌───────────────────────────────┐
   │  rerun input                  │
   │  必填: batch_id               │
   │  选填: search_title 列表      │
   └───────────────┬───────────────┘
                   │
        ┌──────────┴──────────────────────────────────┐
        │  只给 batch_id      → 重跑该批全部商品      │
        │  给了 search_title  → 只重跑这批里的这几个  │
        │  同一 title 多条记录 → 取最新那条           │
        └──────────┬──────────────────────────────────┘
                   │  using the stored URLs（跳过 search）
                   ▼
             ┌───────────┐
             │ scraping  │
             │  module   │
             └──┬─────┬──┘
       succeed  │     │  fail
                ▼     ▼
      ┌───────────────┐   ┌────────────────────────────────┐
      │ Valid Database│   │ 回落到 new input 全流程        │
      └───────────────┘   │ 换一个 batch_id 重跑           │
                          │ （含 search，读出来是重新跑的）│
                          └────────────────────────────────┘
```

rerun 便宜的原因就是跳过了 search；一旦 scraping 失败，说明存的 URL 已经不可信，
必须退回完整流程重新找 URL。

---

## 5. 两个结果库

### Valid Database

| 字段 | 含义 |
|---|---|
| `batch_id` | 批次号 |
| `input_title` | 用户原始输入的 title |
| `search_title` | Search 模块成功选中 URL 时同时输出的候选 title；Scraping title 单独保留在 ProductData |
| `timestamp` | 时间戳 |
| `result` | scraping 结果（ProductData） |

### Failure Database

| 字段 | 含义 |
|---|---|
| `batch_id` | 批次号 |
| `input_title` | 用户原始输入的 title |
| `search_title` | if any —— 找到了才有 |
| `timestamp` | 时间戳 |
| `fail_node` | `search`（没找到）/ `scraping`（抓失败）/ `match`（没匹配成功）/ `rerun` |
| `reasoning` | if any，most likely from the output of LLM |

失败节点与流程的对应：

```
  search 失败  ─────────────▶  fail_node = search    , search_title = NULL
  scraping 失败 ────────────▶  fail_node = scraping  , search_title 有值
  matching 判 FAIL ─────────▶  fail_node = match     , reasoning = LLM 理由
  rerun 阶段失败 ───────────▶  fail_node = rerun
```

---

## 6. 与现有代码的对接

### 已经具备的

| 能力 | 位置 | 说明 |
|---|---|---|
| search 三参数入口 | `src/search/pipeline.py::match_product(product_name, website, country)` | 正好对上必填的 title / site_name / country |
| scraping 全部比对字段 | `src/scraping/models/product_data.py::ProductData` | `title` / `brand` / `gtin` / `image_urls` / `variant` 全都有 |
| 抓取结果落库 | `scraping.db` → `results.product_data`（JSON） | 即图 3 标注的数据源 |
| vision 比对 | `image_load_compression.compare_batch` | 已是本项目依赖 |

### 已补齐的钩子

```
  1. Search 新增 typed in-memory batch API，成功结果仍是 title + URL
  2. Orchestrator 入口支持 xlsx / csv / JSON / Sequence[InputItem]
  3. Matching、共享模型和 orchestrator.db 已实现
  4. 原 src/storage 空壳已删除
```

---

## 7. 已决定语义（2026-08-31）

1. 任一侧 GTIN 缺失/无效为 Unknown；双边不同是强负证据但不直接失败；相同有效 GTIN 直接 Match。
2. Valid 与 Failure 同存 `orchestrator.db`；旧 `src/storage`、temp/main/trash 空壳删除。
3. 每次 Rerun 创建 `<root>-rN`，并显式保存 root/parent 血缘。
4. `search_title` 严格来自 Search 成功输出；Search 失败一律 NULL。Scraping title 留在 ProductData。
5. Matching 只有一个最终 LLM prompt；Vision 只改变 context。
6. Variant 硬冲突直接 No Match；multipack 通过 per-item/count/total 槽位归一化，连续量沿用 ±10%。
7. Rerun 使用血缘中的最新 Valid URL，身份字段变化时复核，stored URL 失败或复核 No Match 时只 fallback 一次。
