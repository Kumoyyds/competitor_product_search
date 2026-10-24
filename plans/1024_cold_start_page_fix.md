# 冷启动修复循环改造：让它收敛（以 joybuy_de 为例）

## Context

`uv run python -m src.scraping.coldstart --site joybuy_de ...` 来回绕圈：第 3 轮修好会员价、弄坏折扣价，第 5 轮又反过来。读代码后确认是修复循环本身的问题，不是 joybuy 特有的：

1. **模型没见过出错的页面。** 首轮和所有修复轮都只喂 `_pick_representative_html` 选出的那一页（12 页里最大的一页，coldstart.py:123/491），而且是预处理压缩后的版本（正文摘录 ≤8000 字，跳过 `<script>`）。其余页面只用来跑 sandbox 给人审。
2. **反馈没有记忆。** `round_feedbacks` 每轮覆盖（:287），`previously_accepted` 每轮重置（:306）。“已修复勿回退”只列网址和字段名，没有值（prompts.py:452）。hint 只发一轮。
3. **新代码无条件替换旧代码。** 没有拿已知答案自动比对，也不保留最好的版本。更差的版本照样拿给人审，还成了下一轮的基础。
4. 小问题：修复时传的是 `https://dummy/{site}`，导致 prepass 的商品锚定失效（:573）；`hosts.yaml` 没有 joybuy.de 的映射。

已和用户确认的方向：
- 首轮每种 page_type 各取一页；
- 修复轮只针对出错网址，从它自己的 HTML 截取正确值附近的片段（script 里的也算），用确定性代码截取，不开 subagent；
- 记录每轮的代码改动和对应结果（改动日志）；
- 每个网址保存一份标准答案，程序自动打分，回退版本不被采用，两版各对一部分时让模型合并。

## 方案

### A. 标准答案表 + 改动日志 —— 新文件 `src/scraping/coldstart_ledger.py`
coldstart.py 已有 1069 行，状态逻辑单独拆出来。

- `CaseExpectation`：`url, page_type, fields: dict[str, Any]`（11 个比较字段，即 `golden._matches_expected` 的 compare_fields），`hints: list[str]`，`status`（`confirmed` / `rejected_unspecified`）。
- `ExpectationLedger`：整个会话内存活，**不重置**。
  - 人工 **y** → 写入该次产出的全部字段。
  - 人工 **N 并指定字段** → 产出值叠加人工修正值。没被选中的字段视为确认正确。修正值用 `golden._normalize` 统一（`3,31`→`3.31`，`-`/`none`→None）。
  - 人工 **N 但没选字段** → `rejected_unspecified`，下轮继续问。
  - 每次人工回答都覆盖旧值，最新的回答为准。hint 按网址累积，每轮都带上。
- `diff_case(product, expectation) -> dict[field, (expected, got)]`：复用 `golden._normalize` 做比较。
- `Score`：`(全对页数, 加权字段命中数)`，按元组比较。全对页数排第一，因为只有全对的页能存成 golden。字段权重：price / in_stock / currency = 3，list_price / membership_price = 2，title / availability_raw = 1，image_urls / brand / gtin / variant = 0.5。
- `RoundRecord`：`round, code, diff_vs_base`（unified diff，截断 3000 字），`per_case: url -> 错误字段集合`，`fixed`，`broke`，`adopted: bool`，`reviewer`（`human` / `auto`）。
- 回退判定：`regressions(base_result, cand_result)` = 在 base 里和标准答案一致、在 candidate 里不一致的 (url, field)。

### B. 证据截取 —— 新文件 `src/scraping/repair/evidence.py`
纯 Python，不调用大模型。

- `value_variants(value)`：给出 `3.31` / `3,31` / `13099.05` / `13.099,05` / `13,099.05` 等写法，带数字边界正则 `(?<![\d.,])…(?![\d])`。
- `locate_value_snippets(html, url, value, *, keywords, max_hits=3) -> list[Snippet]`，在原始 HTML（含 script）里查找：
  - **DOM 命中**（可见文本节点）：从命中节点往外层找，取第一个 outerHTML ≤600 字的外层元素，附带 CSS 路径（tag.class 链）。
  - **script 命中**：取前后各约 300 字，`\"`→`"`、`&`→`&`，标出 script 序号。
  - **排序**：前后 2000 字内出现 URL 商品 ID 的优先；含 keywords 的优先（来自 `sites.yaml` 的 hints，比如 `JoyPlus`，再加 `isDiscount`、`memberPromotion`、`crossLine`）；位置越靠前越优先。内容相同的片段去重。
- 同一个函数也用于定位**当前的错值**是从哪取的（比如 13.49 来自 `downPrice`），在 prompt 里和正确值的位置并排展示。
- `found == []` 表示整页找不到正确值，在人工输入时用到（见 D）。

### C. Prompt 改造 —— `src/scraping/repair/prompts.py`、coldstart.py 里的 `_gen_initial_parser` / `_gen_repaired_parser`
- **首轮多页面**：用新的 `_pick_representative_pages(fetched, rows) -> list[(page_type, url, html)]` 替换 `_pick_representative_html`。每个 page_type 取一页，优先取 HTML 最大的那一页。
  - `initial_parser_gen_prompt` 改为接收多个 `(page_type, url, PriceContext)`。每页用真实 url 调用 `build_price_aware_context`，预算 `max(6000, 24000 // 页数)`。
  - `_render_price_context` 增加 `main_cap` 参数（默认 8000，多页时按比例缩小），每页加标题 `=== PAGE (discounted) <url> ===`。
- **修复轮** `coldstart_repair_prompt` 的新输入结构（替换现在的 feedbacks / resolved_ledger / regressions 三段）：
  1. `[EXPECTED OUTPUT TABLE]`：每个网址一行，包括商品 id、page_type、price、list_price、membership_price、in_stock、currency，以及 PASS/FAIL 状态。
  2. `[FAILING CASE EVIDENCE]`：先按 (page_type, 错误字段) 分组，每组最多 2 个网址。每个网址写明期望值和实际值，附正确值片段（≤3 处）和错值片段（≤1 处），再附该网址累积的 hint。总上限 16000 字。
  3. `[CHANGE LOG]`：最近 3 条 `RoundRecord`，每条包括 diff、fixed、broke、是否被采用。
  4. **合并指令**（仅在上一个候选有回退时出现）：“候选版修好了 X、弄坏了 Y。基础版里负责 Y 的是下面这段代码，请保留候选版对 X 的修复，并恢复 Y 的逻辑。”同时附 base 和候选版相关部分的 diff。
  5. 当前 base 代码，加上按页面类型挑的页面上下文（同首轮，预算减半）。
- 在系统规则里补一句：`[FAILING CASE EVIDENCE]` 里的值是人工确认的标准答案；片段即使在 `<script>` 里也可以用来定位。这条用来抵消现有那条“script 价格默认不可信”的规则。
- 每轮打印 prompt 字数，例如 `prompt ≈ 41k chars`，方便以后排查超长问题。

### D. 主循环 —— `src/scraping/coldstart.py` `run_coldstart`
状态：`ledger`、`base_code`、`base_result`、`best_code` / `best_score`、`records`、`auto_streak`。

每轮流程：
1. 生成或修复，得到 `cand_code`。
2. 在全部 HTML 上跑 sandbox，用 `ledger` 打分，得到 `cand_result` / `cand_score`。
3. **采用判定**：
   - 首轮，或者没有回退且 `cand_score >= base_score` → 采用，`base = cand`。
   - 有回退且 `auto_streak < 2` → **不采用、不打扰人工**，记一条 `RoundRecord(adopted=False, reviewer=auto)`，`auto_streak += 1`，直接进入下一轮。下一轮 prompt 带合并指令。
   - 有回退且 `auto_streak` 已到 2 → `base` 取 {base, 这几轮候选} 中得分最高的那个，然后进入人工审核。
4. **人工审核**（只针对 `base`）：
   - 和标准答案完全一致的页 → 自动通过，打印 `matches confirmed expectation; accepted automatically`。这条取代原来的 `previously_accepted` 逻辑。
   - 不一致的页 → 在面板下打印 `!! expected 2.84, got —`，然后照常问 y/N。
   - **y** → 更新标准答案（视为页面合理变化）。**N** → 走 `_collect_feedback`。
5. `_collect_feedback` 新增：
   - 每输入一个数值修正，就用 `locate_value_snippets` 在该页 HTML 里查找。
   - 找不到时提示：`!! 17.99 不在抓取到的 HTML 中（可能是地区/登录差异），仍然保留？[y/N]`。只有保留的修正才写入 ledger。
   - 函数需要接收 `case.html`。
6. 刷新 `best_code` / `best_score`，追加 `RoundRecord`，`auto_streak` 在人工审核后清零。
7. 按 `s` 或到达轮数上限时，用 `best_code` 调用 `_seed`，并且只存它全对的那些页。今天存的是“最后一版 + 本轮通过的页”。
8. 删除 `_update_review_ledger`、`reported` / `resolved` / `regressions` / `previously_accepted` 和 `_sorted_ledger`，由 ledger 和 records 取代。`_print_failure_summary` 改为从 ledger diff 生成：`[REJECTED]` 显示期望值和实际值，`[REGRESSION]` 取自 records。

`cold_start_max_repair_rounds` 继续作为总轮数上限，自动轮也计入。“连续两次没产出代码就中止”的逻辑保留。

### E. 零碎
- 首轮和修复轮都把 `https://dummy/{site}` 换成真实 url。
- `src/scraping/hosts.yaml`：加 `joybuy.de: joybuy_de` 和 `www.joybuy.de: joybuy_de`。
- 文档（Documentation Discipline，审核交互有变化）：
  - `src/scraping/README.md` 冷启动一节：说明自动通过的规则、`expected X, got Y` 提示、“值不在 HTML 中”的确认，以及“回退版本会自动重试，不会拿给你审”。
  - `src/scraping/CLAUDE.md` 冷启动架构段：更新 ledger、证据截取、选优这几块。AGENTS.md 由 hook 同步。

## 关键文件
- 修改：`src/scraping/coldstart.py`、`src/scraping/repair/prompts.py`、`src/scraping/hosts.yaml`、`src/scraping/README.md`、`src/scraping/CLAUDE.md`
- 新增：`src/scraping/coldstart_ledger.py`、`src/scraping/repair/evidence.py`、`tests/unit/scraping/test_coldstart_ledger.py`、`tests/unit/scraping/test_value_evidence.py`
- 复用：`repair/golden.py` 里的 `_matches_expected` / `_normalize`；`repair/prepass.py` 里的 `build_price_aware_context`、`_extract_url_product_id`、`_CURRENCY_RE`；`repair/sandbox.py` 里的 `run_in_sandbox`；`site_profile` 里的 hints

## 验证
1. 单元测试：
   - **ledger**：覆盖 accept、reject+修正、reject 未选字段、后来覆盖旧值、hint 累积。
   - **打分和回退**：用 A 版（membership 错）对 B 版（list_price 错）的例子，验证得分排序和回退检测。
   - **主循环**：用桩 LLM 依次返回 好版 → 回退版 → 合并版，配合桩 `input_fn`，断言：
     - 回退版不进人工审核；
     - 和标准答案一致的页不调用 `input_fn`；
     - `s` 存的是最佳版本。
   - **evidence**：从 `src/scraping/data/joybuy_de_standard_page.html` 截取 `self.__next_f.push` 片段作 fixture，验证 `3,31` / `3.31` 都能命中 script 和 DOM，商品 ID 附近的命中排在前面，找不到时返回空。
   - **prompt**：断言包含标准答案表和证据段，总长度不超过上限。
2. `uv run pytest tests/unit/scraping -q`
3. 实跑 `uv run python -m src.scraping.coldstart --site joybuy_de --input src/scraping/data/cold_start/joybuy_de.xlsx`，观察：
   - 已确认的页自动通过；
   - 回退版本被自动重试，没有拿给人审；
   - 会员价和折扣价不再来回跳；
   - 每轮的 prompt 字数在预期范围内（约 30–50k 字）。
