# Scraping 剩余人工测试清单

审视结论：核心离线逻辑覆盖不少，但上线前仍需补做真实服务与真实数据验证。当前保存的 M12 真实运行日志并非全绿：有 Argos 正常页失败、Tesco `unavailable` 被当成成功；Amazon 输出中还出现过 `GPB` 而非 `GBP`。这些都不应只靠 fixture 通过就放行。

## 必做（P0）

### 1. 真实 cold start

现有 M11 会调用真实 Qwen，但 BrightData 抓取是 mocked，因此一定要用真实 URL 跑一遍：

- 使用 8–15 个同站商品，覆盖普通价、折扣、会员价、缺货/预售、404/下架。
- 人工逐条对照网页：标题、价格、原价、会员价、库存、货币、图片、GTIN。
- 验证 `y / n / q`，以及抓取失败、LLM 无法生成 parser、所有候选拒绝时不会留下半成品。
- 注意：它并不是“随便一个新站点即可运行”；新站仍需先注册 HTML scraper 且配置 host 映射。

### 2. 重新跑真实端到端回归

用真实 BrightData + Qwen 跑 Tesco、Argos、Amazon 的固定样本，并抽查每一条字段，而非只看 `success`：

- 每站至少覆盖普通、折扣、会员、缺货、无效 URL。
- 断言金额和币种精确正确；折扣必须是 `price + list_price`，会员价必须是 `price + membership_price`。
- 特别复测 Argos 备用 DCA 路径与 Tesco unavailable 语义。

现有 M12 日志记录的是 18/22 成功，且最终有失败；代码在其后又改过，因此旧日志不能证明当前版本。

### 3. 真实 fallback / 故障演练

手动构造或临时使用一个会被 Web Unlocker 拦截的商品，确认：

- HTML route 失败后真的切到 DCA。
- DCA 也失败时才写 escalation，并包含每个 scraper 的失败阶段。
- 所有 BrightData 通道都失败时 reason 是 `infra_failure`。
- 单一路径失败不会误报整站故障。

M10 的离线测试仍按“直接 `BrightDataInfraError` 不 fallback”的旧表述测试，但实际 HTML/API scraper 已把它转换为 `ScrapeFailed` 再让 Router fallback，文档与运行语义有脱节。

### 4. 并发、长尾与取消

真实配置每站并发是 16，而 M12 只用 4。以 16 并发连续跑两轮，确认：

- SQLite 没有 `database is locked`，且 `scrape_runs`、`results`、`escalations` 都完整写入。
- 没有重复触发 Datasets/DCA snapshot。
- 轮询接近 300 秒、网络抖动、用户中断时，费用、超时和最终状态可接受。

历史日志已有单 URL 约 380 秒，建议把可接受的 p95 与超时预算写成明确验收线。

### 5. 真实页面的数据质量抽查

每站至少人工抽查 20 个当前在线商品，特别是促销页：

- 常规价、划线原价、会员价不互相覆盖。
- 多规格/多包装、单位价、预售、无货、地区价处理正确。
- 推荐商品、运费、购物篮金额不会被误当产品价。
- GB 代理下的结果与用户实际访问地区/登录态预期一致。

## 建议补做（P1）

- **升级旧数据库**：`membership` 约束迁移不是 `init_db()` 自动执行的。先备份生产 `scraping.db`，在副本执行迁移、启动服务、冷启动写入 membership golden，再验证原数据和索引仍在。
- **干净环境安装**：当前 `requirements.txt` 有未提交改动，建议用全新 Python 3.12 venv 安装目标依赖后，分别验证 macOS/Linux 与实际 Windows 部署环境；sandbox 在 Windows 有不同 fallback 行为。
- **持续漂移监控**：每站保留一组“金丝雀 URL”，每天或每周跑一次并记录字段差异与 fallback 比率，而不只是成功率。

## 测试资产缺口

- 文档声称有 `verify_m15.py`，但当前仓库只有 `verify_m15_output.log`，没有对应脚本，无法复跑。
- M16 没有独立回归测试，只宣称重跑 M14/M15；应加入至少一个 mocked-httpx 的 UTF-8/Latin-1 响应测试，再加一次真实 Tesco 验证。

本清单基于当前代码、测试脚本和保存的运行日志整理；未修改实现代码。
