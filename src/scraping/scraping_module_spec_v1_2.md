# Scraping 模块设计 Spec (Phase 0) — v1.2

> **文档性质**:设计决策与逻辑规则,不含文件结构与实现代码。实现由 Claude Code 对照本 spec 与现有 repo 执行。
> **Validation 约定**:本文只标注「此处需验证」锚点,说明什么该被验证;具体测试代码与命令由使用者在 Claude Code 中另行下达。
> **适用阶段**:Phase 0(原型/算法验证,纯 Python 本地运行,无 Docker/FastAPI/云服务)。
> **Phase 0 站点**:Tesco、Argos(自建 HTML 路线);Amazon UK(Bright Data 直接返回结构化 JSON,API 路线)。
>
> **v1.2 变更**(相对 v1.1):新增**「无效目标(invalid_target)」检测**这一整类机制——处理"URL 技术上可达/可解析,但不对应一个当前在售的有效商品"的情况(错误链接、商品下架、404 类错误页,例如返回 200 却显示 "Oops, that didn't go to plan" 的页面)。核心设计:
> 1. **结构信号为主、关键词为辅**的前置检测工具(扩展自 D20 软墙工具),不依赖逐站维护的话术词典(D26);
> 2. `invalid_target` 是 **scrape_runs 的 outcome 分类**,不进 ProductData,避免与 `in_stock` 语义重叠(D27);
> 3. Phase 0 **不细分**下架/死链,合并一类(D28);
> 4. **单个静默记录,同 site 激增才告警**,复用签名去重机制(D29);
> 5. Agent 在 repair 阶梯中承担**兜底判定**职责,发现未知模式的无商品页时提前终止阶梯,并将新特征**回填**进前置检测的辅助短语库,防止同类页面反复烧 LLM 预算。
>
> v1.1 变更history见 v1.1 版本(scraper 层 fallback、results 表、DirectAPI 受限 JSON 自愈)。

---

## 1. 系统架构总览

### 1.1 模块边界(输入 / 输出)

- **输入**:`(url, website)` —— 一个确切的商品页 URL 及其站点标识。
- **输出**:一个合法的 `ProductData`(Pydantic 模型);或 `scrape_runs.outcome = invalid_target`(该 URL 不对应有效商品,不算故障);或抛出 `ScrapeFailed`(已完成人工升级的终态失败,携带失败签名与快照)。
- **明确不属于本模块的职责**:
  - 「商品名 → URL」的发现/召回(上游已有独立算法,Serper / google_shopping 属召回层);
  - 跨零售商的商品匹配(下游 matching 模块);
  - 数据业务合理性异常检测(如「价格暴涨 10 倍」)—— 交给下游分析师核对,scraping 只保证结构合法 + 语义通过两道门。

### 1.2 数据流(文字框图)

> **本图关键结构**:①无效目标检测在 extraction 之后、parser 之前拦截,命中即静默记录,不进两道门也不进 repair;②两道门仍是公共关卡;③门后失败按路线分流。

```
输入 (url, website)
      │
      ▼
┌─────────────┐  跳1: host→site(小 dict)
│   Router    │  跳2: site→有序 scraper 列表(decorator 注册,带顺序)
└─────────────┘
      │  取该 site 列表中第 1 个 scraper
      ▼
┌──────────────────────────────────────────────────────┐
│ 当前 scraper(实现 BaseScraper 契约)                    │
│                                                      │
│  A. HTMLScraper 路线(如 Tesco/Argos 的 scraper_1)     │
│     extraction(Bright Data 取 HTML)                  │
│        │ 非200/超时 → 2s 重试×2 → 仍失败 = 终态(去◆)   │
│        ▼                                             │
│     【无效目标检测】(结构信号为主,§5.15)                │
│        │ 命中 → outcome=invalid_target,静默记录(去★)   │
│        ▼ 未命中                                       │
│     有序 parser 列表(按命中率,逐个尝试)→ 产出 dict      │
│        │(若走到 Agent repair,Agent 亦可判定           │
│        │ no_product_on_page → 立即终止阶梯,回填        │
│        │ 短语库,去★;而非继续烧 pro)                    │
│                                                      │
│  B. DirectAPIScraper 路线(如 Amazon;或 Tesco 的       │
│     scraper_2 = Bright Data 成品 scraper)             │
│     API 调用 → JSON → 字段映射 → 产出 dict              │
│        │ API 报错/不返回数据/显式"未找到" = 终态(去◆)   │
└──────────────────────────────────────────────────────┘
      │ 两条路线的 dict 在此汇合(仅未被上面拦截的情况)
      ▼
┌──────────────────────────┐
│ 公共关卡(所有路线必过)      │
│   门1: Pydantic 类型校验    │
│   门2: feasible_check 语义 │
└──────────────────────────┘
      │
      ├─ 通过 ──→ 写 results 表(仅合格结果,追加)──→ 输出 ProductData
      │
      └─ 失败 ──→ 按路线分流:
           ├─ HTML 路线 → repair 阶梯(flash→flash→pro,共享3次预算,
           │              沙箱+golden考核+promote)→ 耗尽 = 终态(去◆)
           └─ API 路线 → 受限 JSON 自愈(仅字段重映射,禁止补数据)
                          → 修不了/源头缺失 = 终态(去◆)

★ invalid_target(无效目标,非故障):
      │
      ▼
   静默记入 scrape_runs(outcome=invalid_target),不触发 fallback、不触发 escalation
      │
      ▼
   同 site 短时间内 invalid_target 激增?(复用签名去重计数)
      ├─ 是 → escalation(mass_invalid_target,提示可能整站 URL 结构变更)
      └─ 否 → 结束

◆ 终态失败(任何路线、任何阶段,真实故障):
      │
      ▼
   site 的 scraper 列表还有下一个?
      ├─ 有 → fallback 到下一个 scraper,从头走(scraper 层兜底)
      └─ 没有 → escalation(签名去重,记录哪个 scraper 哪个阶段挂)→ 人工

侧路:
- 每次抓取(无论成败/invalid_target)→ scrape_runs 记录(可观测性,含 scraper 标识)
- HTML 路线抓取成功时按需 → golden_samples 播种/补样
- Bright Data 基础设施故障 → escalations(infra_failure)+ 即时告警,不重试不 fallback 掩盖
```

### 1.3 持久层

单个 SQLite 库,六张表:`parsers` / `golden_samples` / `scrape_runs` / `results` / `escalations` / `invalid_target_phrases`(轻量辅助表,详见 §6)。易变数据(parser 代码、样本、运行记录、结果)全部进库;静态结构(注册表、类层级)在代码中。

---

## 2. 术语表

多个 Claude Code session 间必须统一使用下列词汇:

| 术语 | 定义 |
|---|---|
| **host** | URL 中的域名,如 `amazon.co.uk`、`amazon.fr`、`tesco.com` |
| **site** | 零售商标识(与域名解耦),如 `amazon`、`tesco`、`argos`。parser、golden set、签名均按 site 组织 |
| **Router 两跳** | `host → site`(小 dict)+ `site → 有序 scraper 列表`(decorator 注册表,带顺序) |
| **scraper 层 fallback** | 一个 site 名下的有序 scraper 列表;任一 scraper 终态失败即换下一个,全挂才转人工。与 parser 层 fallback 是**嵌套**关系,勿混 |
| **parser 层 fallback** | 同一个 HTMLScraper **内部**的有序 parser 列表(解析方式的兜底) |
| **终态失败** | 一个 scraper 用尽自身全部手段后的**真实故障**:extraction 重试耗尽 / repair 预算耗尽 / API 报错或不返回数据 / 源头缺失判定。触发 scraper 层 fallback。**与 invalid_target 是不同性质**——前者是"能力没跟上",后者是"目标本来就不是商品" |
| **invalid_target(无效目标)** | URL 技术上可达/可解析,但**不对应一个当前在售的有效商品**(错误链接、下架页、404 类错误页,即使返回 HTTP 200)。**不是 scraper 故障**,不触发 repair/fallback/escalation(除非激增) |
| **结构信号检测** | 判定 invalid_target 的主要手段:JSON-LD Product schema 有无、HTTP 状态码、title/price/加购控件多重缺失、页面长度异常。语言无关、不随站点文案改版而失效 |
| **短语库回填(backfill)** | Agent 在 repair 阶梯中判定某未知模式页面为「无商品」后,把该页面的特征话术存入 `invalid_target_phrases` 表,作为后续同类页面的**辅助**命中信号,防止同类页面反复触发 Agent |
| **两道门** | 门1 = Pydantic 类型/结构校验(单字段);门2 = `feasible_check` 跨字段语义校验。**公共关卡**:HTML 与 API 两条路线的产出都必须过;门后失败分流按路线走 |
| **parser 单元** | 独立、自成一体的小解析单元:输入 HTML → 要么产出合法 dict,要么失败。selector 与清洗逻辑内含其中 |
| **有序 parser 列表** | 一个 site 名下按命中率排序的 parser 集合,运行时逐个尝试,第一个通过两道门者胜出 |
| **candidate** | Agent 新生成、尚未通过考核的 parser |
| **promote(转正)** | candidate 通过 golden set 考核后进入有序列表(置于最前) |
| **prune(退休)** | parser 因长期 0 命中或列表满员被移出(status 置 retired,不物理删除) |
| **golden set / golden sample** | 快照 HTML + 其「标准答案」(`expected_output`,当时现役 parser 解出的 ProductData)。考核 candidate 与检测样本腐烂的依据 |
| **page_type(页型)** | standard / out_of_stock / discounted / multipack,由 ProductData 字段组合自动判定(与 invalid_target 无关——invalid_target 根本不产出 ProductData) |
| **repair 预算** | 每次 scrape 的 Agent 修复尝试上限(3 次),parse-exception 与 feasible-fail **共享**,promote 失败同样消耗。**Agent 判定 no_product_on_page 时立即终止,不视为耗尽** |
| **repair 阶梯** | 三级模型升级:flash → flash+报错上下文 → pro+全部报错上下文。仅 HTML 路线 |
| **JSON 自愈** | DirectAPIScraper 的受限修复:仅允许对**已存在于 JSON 中**的字段做重映射(键名/结构变化);**严禁凭空补齐缺失字段**;API 报错或不返回数据时不触发 |
| **源头缺失** | 输入中根本不存在目标数据(HTML:软墙/captcha/结构性缺字段;JSON:字段压根没返回),任何模型也修不出 → 不修,走终态。**与 invalid_target 的区别**:源头缺失是"该有数据的地方没数据"(真实商品页但抓不全),invalid_target 是"这压根不是商品页" |
| **失败签名 (signature)** | `(site, 失败字段或feasible规则, parser_version)`,escalation 去重键;`mass_invalid_target` 复用同一去重机制,签名为 `(site, "invalid_target_surge")` |
| **腐烂 (staleness)** | golden 样本因商品下架/页面失效而过期。判据:现役 good parser 也跑不出其标准答案 |
| **冷启动** | 新 site 首次接入、parsers 表为空时的初始化路径(§5.9)。**使用者必须提供一批该 site 的商品 URL 作原料**;标准答案不需提供,由一次性人工确认自动生成 |
| **extraction** | 取数环节(Bright Data 获取 HTML / API 响应),与 parsing 环节相对 |

---

## 3. 关键设计决策 + 理由

> 本节是 spec 的核心资产。每条决策都附理由,防止后来者(包括未来的自己)好心改错。

| # | 决策 | 理由 |
|---|---|---|
| D1 | 价格字段用 `Decimal`,禁用 `float` | 浮点误差(0.1+0.2≠0.3)在价格对比中会累积成脏数据。无商量余地 |
| D2 | parser 更新采用**有序列表**,而非「完全替换」或「union 进同一函数」 | 替换会误杀仍在用旧模板的页面(A/B 改版、品类模板差异);union 使单函数无限膨胀难维护。有序列表 = union 的安全性 + 替换的干净度,每单元独立可剪枝 |
| D3 | `site→scraper` 注册表用**代码 decorator**,不用 YAML | 3 个站点规模下,代码注册 type-safe、IDE 可跳转、注册表与实现永不失同步;YAML 需要 string→import 间接层且改名不报错。YAML 的价值(非工程师改配置/百站热更新)Phase 0 不存在 |
| D4 | 易变数据(parser 代码、样本、运行记录、结果)进 SQLite;静态结构(注册表、类层级)在代码 | 「静态结构在代码,动态配置在数据」原则。parser 代码文本直接存库,不落文件系统 |
| D5 | selector 不单独建表,内含于 `parsers.code` | selector(定位地址)与使用它的清洗逻辑绑在一起,不会出现「选择器改了、处理逻辑忘改」的失配。原「裸 selector cache」设想废弃 |
| D6 | parser / golden / 签名按 **site** 组织,不按 host | 一个 scraper 可服务多个域名(Amazon 三国站)。scrape_runs 额外保留 host/url 供细粒度分析(site 用于聚合,host 用于细看) |
| D7 | extraction 失败与 parsing 失败**分层处理,预算独立** | extraction(非200/超时)是网络问题,廉价重试即可;parsing 失败是 parser 问题,需 Agent。两者性质不同,预算不互相挤占 |
| D8 | parse-exception 与 feasible-fail **共享同一个 repair 预算(3次)** | 二者本质都是「parser 不对,要叫 Agent」。分开计数会产生 ping-pong 死循环(parse 过→feasible 挂→修→parse 过→feasible 又挂→…) |
| D9 | `scrape` 接口为 **async** | orchestrator 是 LangGraph 异步;抓取是网络 I/O;需要批量并发 |
| D10 | DirectAPIScraper **不进** HTML 自愈体系(parser 列表/沙箱/golden/repair 阶梯),但拥有**受限 JSON 自愈**(见 D25) | Bright Data 直接返回结构化 JSON,无 selector 可修;但 JSON 键名/结构会变,这类映射问题 Agent 修起来比 HTML 容易一个量级,值得自动化 |
| D11 | gtin 定位为**匹配的可选短路键,非主键** | 页面不一定暴露 gtin(optional 是常态);EAN 粒度是「规格级」;ASIN 不是 gtin(Amazon 内部 id,不跨站)。规则:双方都有且相等→强信号短路;不等或缺失→交给匹配算法,**不做反向否决** |
| D12 | mpn / sku 字段砍掉 | gtin 已覆盖黄金匹配键需求;mpn 次优备胎、sku 仅本站有效,Phase 0 无消费方 |
| D13 | 「price 必填性」是条件规则,放**门2**而非类型系统 | 类型系统只能无条件判断:必填则误杀缺货商品,optional 则漏掉在售无价的真故障。「看情况」的跨字段规则只能用条件逻辑写 |
| D14 | 沙箱用标准库自建(subprocess+timeout+setrlimit+AST 白名单),不引入专门 infra | 被执行代码是纯 HTML 解析,合法权限本应≈0。要防的只有死循环/异常/内存/危险 import,标准库足够 |
| D15 | golden 样本存快照时**连标准答案一起存** | 考核从「能否解析」升级为「解析结果==标准答案」,防 candidate 抓错节点蒙混;同时腐烂可自动暴露,免去人工定期巡检 |
| D16 | scrape_runs **不存 HTML**;大文本只进 golden_samples 与 escalations | scrape_runs 每次抓取都写,存 HTML(单个可达 1.6MB,Argos 实测)会爆库。高频表保持轻,大文本限于低频表 |
| D17 | 命中率**不落表,实时从 scrape_runs 聚合** | 存了就会与真实记录脱节;`GROUP BY winning_parser_id` 算出来永远准 |
| D18 | 数据业务异常检测(价格突变等)**移出本模块** | 需要历史基线与跨商品视角,是分析层职责。scraping 只管结构与语义两道门 |
| D19 | repair 阶梯中加入**源头缺失粗判**(Phase 0 即实现) | HTML 里根本没有数据时,升级模型是纯烧钱。粗判(HTML 长度异常/captcha 关键词)→ 跳过 pro 直奔人工 |
| D20 | 软墙检测抽成**独立小工具**,extraction 边界检查与 repair 粗判共用;**v1.2 扩展为「无效页面检测工具」,统一承载软墙/captcha/invalid_target 三类判定** | 三者本质是同一件事:判「此页面无有效商品内容」。写多份会在反爬样式或错误页样式更新时改一漏一(DRY)。v1.2 起该工具是 §5.15 的落地主体 |
| D21 | Bright Data 基础设施失败(额度/代理/账户)**不重试,直接告警,不进 fallback 循环** | 重试解决不了额度耗尽,只是白烧;这类故障使全站抓取同时挂,是运维事件而非数据工单;备用 scraper 多半同账户,fallback 只会掩盖问题、延迟告警 |
| D22 | 冷启动需**一次性人工确认**,并借此播种 golden set;**URL 必须由使用者提供** | 全新 site 无标准答案,第一版 parser 无从自动验证;它又是后续所有 golden 的源头。URL 是原料,系统无「商品名→URL」能力;标准答案不需人写,人确认「对」的输出自动转为 golden |
| D23 | 一个 site 挂**有序 scraper 列表**;任何终态失败先 fallback 到下一个 scraper,全部挂掉才转人工 | 不同 scraper 走不同通道,一个通道卡住时另一通道很可能绕过;成品 scraper 的改版适配是供应商责任,也可能已适配。escalation 记录「哪个 scraper、哪个阶段」挂的,供人工判断是否调整顺序 |
| D24 | results 表只写**通过两道门且未 escalated** 的合格结果;**追加留全历史**,不覆盖 | results 是被下游复用、被分析师当真相的资产,混入脏数据会导致错误结论且难追溯。价格趋势是产品核心卖点,覆盖历史等于自废武功 |
| D25 | JSON 自愈**只允许重映射已存在字段,严禁凭空补齐缺失字段**;API 报错/不返回数据时直接 escalation,不触发自愈 | 不设红线时的危险失败模式:JSON 缺 price,Agent 为「修复成功」自作聪明从别的键凑值,制造出看似合法实则错误的静默脏数据——比直接失败更糟 |
| D26 (v1.2 新增) | invalid_target 检测采用**结构信号为主**(JSON-LD Product schema 有无 / HTTP 状态码 / title+price+加购控件多重缺失 / 页面长度异常),**关键词匹配降级为自动生长的辅助信号**,不做人工维护的逐站话术词典 | 下架/错误提示的文案因站而异且随时可能改版(Tesco "sorry this product is out" / Argos "Oops, that didn't go to plan" / Amazon 另一套),纯关键词法需要持续人工发现新话术、逐站维护,成本不收敛。JSON-LD Product schema 是电商网站为搜索引擎准备的机器可读数据,不随前端文案/语言变化,是更稳定的信号源 |
| D27 (v1.2 新增) | `invalid_target` 是 **scrape_runs 的 outcome 分类**,不写入 ProductData 字段 | `in_stock` 描述「一个真实商品当前能否购买」,`invalid_target` 描述「这个 URL 是否对应真实商品」,是更前置的一层判断,二者层级不同。硬塞进同一 schema(如加 status 字段)会与 in_stock 产生语义重叠且难以自洽。命中 invalid_target 时根本不会产出 ProductData,天然不会与 in_stock 同框出现 |
| D28 (v1.2 新增) | Phase 0 **不细分**「商品下架」与「链接失效」,统一归为 `invalid_target` 一类 | 细分需要额外判定逻辑(下架页通常仍留标题/品牌痕迹,死链完全空白),现阶段收益不足以证明这份复杂度合理。粗筛「这不是可抓的商品」已满足价格情报核心需求,细分留待有实际下架情报需求时再拆 |
| D29 (v1.2 新增) | 单个 `invalid_target` **静默记录,不触发 escalation**;同一 site **短时间内激增**才升级告警(`mass_invalid_target`),复用签名去重机制 | 商品下架是电商常态(100 个 URL 里有 1 个下架完全正常),逐个生成人工工单会持续骚扰人工且无意义。但激增(如同 site 当日 invalid_target 比例异常升高)大概率意味着系统性问题(整站 URL 结构变更),这才是真正值得人介入的信号 |

---

## 4. 功能模块划分与职责边界

### 4.1 Router
- 职责:`host → site → 有序 scraper 列表` 两跳分发,并驱动 scraper 层 fallback(取列表第 1 个执行;终态失败取下一个;耗尽列表 → escalation)。
- host→site:简单 dict。site→scrapers:decorator 注册表,支持一个 site 注册多个 scraper 并声明顺序。
- **不负责**:任何抓取、解析、失败处理、invalid_target 判定的内部逻辑。

### 4.2 BaseScraper(抽象契约)
- 类属性:`site`、`source_type`(`"html"` / `"api"`)。
- 唯一抽象方法:`async scrape(url) -> ProductData`;
  - invalid_target 情况返回一个明确的哨兵结果(而非抛异常,因为它不是错误);
  - 终态失败抛 `ScrapeFailed`(携带 signature + snapshot + 哪个阶段挂的)。

### 4.3 HTMLScraper(自愈骨架,Template Method)
- 复用逻辑全部在此:extraction 重试、**无效目标前置检测**、有序 parser 列表、repair 阶梯(含 Agent 兜底判定)、沙箱、golden 播种、escalation 上报。
- 站点子类(TescoScraper / ArgosScraper)只填:site 标识、extraction 配置。

### 4.4 DirectAPIScraper(API 路线)
- Bright Data(或其他成品)API → JSON → 字段映射 → 产出 dict → **公共两道门**。
- API 若显式返回"产品未找到/已下架"类响应(而非报错),同样归入 `invalid_target`,不进 JSON 自愈、不进 escalation。
- 拥有**受限 JSON 自愈**(§5.14):仅当「数据在、键变了」时重映射;API 报错/不返回数据 → 直接 escalation。

### 4.5 校验(两道门 —— 公共关卡)
- **仅未被 invalid_target 拦截的产出**需要经过两道门。门1:Pydantic 类型/结构;门2:`feasible_check` 跨字段语义。
- 门的判定公共,门后失败分流按路线。

### 4.6 无效目标检测(v1.2 新增模块职责)
- 一个独立小工具(扩展自软墙检测工具,D20),在两处被复用:
  - **前置检测**:extraction 之后、parser 之前,主力拦截;
  - **Agent 兜底判定**:repair 阶梯中,Agent 判断"这是解析困难还是压根没商品",命中后终止阶梯并回填短语库。
- 判定结果不是"失败",是一个平行于 ProductData 的**合法结果分类**。

### 4.7 Agent(repair)
- HTML 路线:输入失败 HTML + schema 要求 + 累积报错上下文,输出 candidate parser 代码;**新增职责**:先判断该页面是否属于 no_product_on_page,是则不生成 parser、直接终止阶梯并触发回填。
- API 路线:输入失败 JSON + schema 要求,输出字段映射修正(受 D25 红线约束)。

### 4.8 存储层
- SQLite 六表(§6)。上游模块通过 orchestrator state 通信,不横向 import。

---

## 5. 详细机制规格

### 5.1 ProductData 契约

字段清单(未因 invalid_target 变动——这是 D27 的直接体现:invalid_target 不进此 schema):

| 分组 | 字段 | 类型 | 必填性 | 说明 |
|---|---|---|---|---|
| 溯源 | `url` | str | 必填 | 规范商品 URL |
| | `website` | str | 必填 | scraper 自填,值为 site 标识 |
| | `scraped_at` | datetime | 必填 | 价格情报是时间序列,不可省 |
| | `source_type` | Literal["html","api"] | 必填 | |
| | `parser_version` | str | optional | 哪个 parser 产出。API 路线为 None |
| 识别 | `title` | str | 必填 | |
| | `brand` | str | optional | |
| | `gtin` | str | optional | 可选短路键,非主键(D11) |
| | `image_urls` | list[str] | 必填但**可为空列表** | |
| | `variant` | dict | optional | {size, color, pack_qty} |
| 价格 | `price` | Decimal | 条件必填(见门2) | |
| | `currency` | str | optional | ISO-4217,非符号 |
| | `list_price` | Decimal | optional | |
| | `membership_price` | Decimal | optional | 会员/忠诚度计划优惠价,如 Tesco Clubcard 价、Amazon Prime 会员价 |
| 库存 | `in_stock` | bool | 必填 | **仅描述真实商品当前能否购买,与 invalid_target 无关** |
| | `availability_raw` | str | optional | |
| 调试 | `raw` | dict | optional | |

> 此处需验证:schema 实例化对各字段类型的拒收行为;`image_urls=[]` 应通过。

### 5.2 两道门校验(公共关卡)

**适用范围:所有 scraper 路线中,未被 invalid_target 拦截的产出。**

门1(Pydantic):单字段类型/结构,`price` optional。
门2(feasible_check):跨字段语义。

| in_stock | price | 判定 |
|---|---|---|
| True | 缺失或 `<= 0` | **故障** → 按路线分流 |
| True | `> 0` | 合法；可再带折扣/会员价格 |
| False | 任意 | 合法，但必须保留图片或任一价格等商品信号 |

价格字段遵守固定的客户语义：

- standard：仅 `price`；
- discounted：`price + list_price`，且 `list_price > price`；
- membership：`price + membership_price`，且 `membership_price < price`；只有页面另行展示更高 Was/RRP 时才可附带 `list_price`；
- 三价同时出现时：`list_price > price > membership_price`。

- HTML 路线两门失败共享 repair 预算(D8);API 路线两门失败进受限 JSON 自愈判定(§5.14)。

> 此处需验证:上表三种组合判定;invalid_target 命中的记录确实不会走到两道门(避免误产出一个"假冒合法"的 ProductData)。

### 5.3 类层级与 Router

```
BaseScraper (ABC)
  ├── HTMLScraper              # extraction + 无效目标检测 + parser自愈(§5.4~5.7, 5.15)
  │     ├── TescoScraper
  │     └── ArgosScraper
  └── DirectAPIScraper         # JSON 映射 + 受限 JSON 自愈(§5.14)
        ├── AmazonUKScraper
        └── (各 site 的成品备用 scraper)
```

Router 两跳 + scraper 层 fallback 逻辑同 v1.1(§5.13),不因 invalid_target 变化——invalid_target 不触发 fallback(它不是故障)。

### 5.4 parser 生命周期(四阶段,仅 HTML 路线,未变)

match → candidate → promote → prune,规则与 v1.1 一致。**唯一新增**:match 阶段之前先过无效目标检测(§5.15),命中则根本不进入 parser 列表尝试。

### 5.5 repair 阶梯(v1.2 有调整:新增提前终止分支)

```
尝试1: deepseek-v4-flash
       └─ Agent 先判断:这是「解析困难」还是「页面根本无商品」(no_product_on_page)?
          └─ 判定无商品 → 立即终止阶梯(不算预算耗尽)→ 记 invalid_target(去★)
                          → 回填新特征进 invalid_target_phrases(§5.15)
尝试2: deepseek-v4-flash + 尝试1的报错做上下文
       └─ 同时做「源头缺失」粗判(D20,与 invalid_target 判断共用同一工具但不同分支):
          源头缺失(真实商品页但反爬/数据不全)→ 跳过尝试3,记终态失败(触发 fallback)
          no_product_on_page(压根没商品)→ 同上,终止阶梯,记 invalid_target
尝试3: deepseek-v4-pro + 前两次全部报错做上下文
仍失败 → 终态失败(触发 scraper fallback;列表已尽 → escalation)
```

- **关键区分**:no_product_on_page(→invalid_target,静默,不消耗告警资源)与源头缺失(→终态失败,是真实商品页但抓不到数据,需要 fallback/escalation)是两条不同的出口,不可混淆。判断依据:前者是"这个 URL 不该有商品数据",后者是"这个 URL 该有商品数据但没抓到"。

### 5.6 沙箱(candidate 执行环境,未变)

同 v1.1 §5.6,不受 invalid_target 影响——沙箱只服务真正生成 parser 的场景;no_product_on_page 判定发生在生成 parser 之前,不涉及沙箱。

### 5.7 golden set(样本池,未变)

同 v1.1 §5.7。**一点关联提醒**:invalid_target 页面不会进入 golden set(它们在成功路径判定阶段就被分流,不产出 ProductData,自然没有 expected_output 可存)。

### 5.8 失败类型完整映射表(v1.2 更新)

> Claude Code 遇到任何失败,先查此表归口;表外新失败类型必须先补进此表再实现。
> **v1.2 新增**:invalid_target 相关行,注意它们**不是"失败处理路径"而是"非故障分类路径"**,单独列出以示区分。

| 类型 | 检测方式 | 处理路径 |
|---|---|---|
| 目标站非 200 / 超时 / 网络异常 | extraction 层状态码/异常 | 暂停 2s 重试×2 → 仍失败 = 终态 → scraper fallback → 列表尽则 escalation |
| Bright Data 基础设施失败 | Bright Data 错误码 | 不重试不 fallback,escalation(`infra_failure`)+ 即时告警 |
| **前置结构信号命中 invalid_target**(JSON-LD 缺失+状态码+多重字段缺失+页面异常短) | 无效页面检测工具(§5.15) | **不进 parser、不进两道门、不进 repair**。`scrape_runs.outcome=invalid_target`,静默记录 |
| 返回 200 但软墙/captcha(反爬,非无商品) | 无效页面检测工具(判定分支不同) | 落到门2或 repair 粗判 → 源头缺失 = 终态 → scraper fallback |
| parse 抛异常 | parser 执行异常 | repair 预算池 |
| 门1/门2 失败 | Pydantic / feasible_check | HTML→repair 预算;API→JSON 自愈判定 |
| promote 考核失败 | golden 比对不等 | 消耗 repair 预算 |
| repair 预算耗尽 | 计数器 | 终态 → scraper fallback → 列表尽则 escalation(`parser_broken`) |
| 源头缺失(粗判命中,真实商品页但抓不全) | 软墙工具 | 跳过 pro,终态 → scraper fallback |
| **Agent 判定 no_product_on_page**(前置未拦截的新模式) | Agent 在 repair 阶梯中判断 | **立即终止阶梯**(不算预算耗尽)→ `invalid_target`,**回填短语库** |
| API 报错 / 不返回数据 | DirectAPI 调用层 | 终态 → scraper fallback → 列表尽则 escalation |
| **API 显式返回"未找到/已下架"**(数据结构正常,内容表明无商品) | DirectAPI 映射层 | `invalid_target`,静默记录,不进自愈不进 escalation |
| API 返回 JSON 但键名/结构变化(数据在) | 门失败 + 自愈判定 | 受限 JSON 自愈 |
| API JSON 中字段缺失(数据不在) | 自愈判定(源头缺失) | 禁止自愈补数据,终态 → scraper fallback |
| **同 site invalid_target 短时间激增** | 签名去重计数(复用) | escalation(`mass_invalid_target`),提示可能整站 URL 结构变更 |
| parsers 表为空(新 site) | 列表查询 | 冷启动路径(§5.9) |
| 数据合法但业务可疑 | —— | 不在本模块处理(D18) |

### 5.9 冷启动(未变,同 v1.1 §5.9)

一个新的实务提醒:使用者提供冷启动 URL 时,**应确保这批 URL 对应真实在售商品**,否则第一版 parser 会在无效页面上"学习"、人工确认阶段也难以判断对错。这不是新机制,只是操作提醒。

### 5.10 幂等与去重(v1.2 补充)

- 新增:**invalid_target 判定也走 scrape_runs 的去重逻辑**——同一 URL 时间窗口内重复判定 invalid_target,不重复计数,避免污染"激增"判断的分母。
- 其余同 v1.1。

### 5.11 并发与批量执行模型(未变)

### 5.12 escalation(人工升级,v1.2 新增一类 reason)

- `reason` 四类(新增第四类):
  - `parser_broken`
  - `infra_failure`
  - `api_malformed`
  - **`mass_invalid_target`**(v1.2 新增)—— 同 site 短时间 invalid_target 激增。快照内容:样本 URL 若干、当前 invalid_target 占比/计数、时间窗口。**这是唯一一类不代表"某个 scraper 挂了"的 escalation**,提示的是上游数据(URL 列表)可能需要刷新。

### 5.13 scraper 层 fallback(未变,同 v1.1 §5.13)

明确一点:**invalid_target 不触发 scraper 层 fallback**——换一个 scraper 抓同一个失效 URL 没有意义,该 URL 本身就是问题所在,不是某个 scraper 能力不足。

### 5.14 受限 JSON 自愈(v1.2 微调)

在 v1.1 基础上新增边界:API 若明确返回"该商品未找到/已下架"的结构化响应(不是报错,是正常响应但语义为无商品),判定为 `invalid_target`,**不进入自愈判定流程**(因为它不是"键名变了"，是"这个商品确实没有")。自愈判定流程只处理"数据结构对不上预期字段名"的情况。

### 5.15 无效目标检测(§5.15,v1.2 新增核心机制)

**工具定位**:D20 软墙检测工具的扩展体,统一承载三类判定:软墙/captcha(反爬)、no_product_on_page(错误页/下架页)、以及为 repair 阶梯提供的源头缺失粗判。三者共用同一套"这个内容里有没有真实商品信息"的底层判断,只是触发场景和下游出口不同。

**信号优先级(结构信号为主,D26)**:

1. **JSON-LD `Product` schema 有无**(最强信号)—— 绝大多数电商页面为 SEO 目的嵌入结构化数据,语言无关、话术无关、不随前端改版而失效。有则大概率是真实商品页,不拦截;无则进入下一层判断。
2. **HTTP 状态码**——404/410 等直接判定,不需要内容层面的判断。
3. **多重结构缺失组合**——同时缺失 title 提取结果 / 任何 price-like 数字 / 加购物车类表单控件,**多项同时缺失才判定**(单项缺失可能只是页面样式特殊,不足以下结论)。
4. **页面长度异常**(复用 D19 的粗判逻辑,错误页通常远短于正常商品页)。
5. **关键词匹配**(辅助信号,权重最低)—— 来自 `invalid_target_phrases` 表(按 site 维护),不需要人工预先收集,由 Agent 兜底判定后自动回填(见下)。

**两处复用**:

- **前置检测**(主力):extraction 拿到 HTML 后、进入 parser 列表尝试之前运行。命中 → 直接记 `invalid_target`,不消耗任何 parser/repair 资源。
- **Agent 兜底判定**(补漏):当前置检测未命中(说明是前置规则没见过的新模式),流程正常进入 repair 阶梯。Agent 在生成 parser 之前,先对 HTML 做一次"这是解析困难还是压根没商品"的判断。判定为无商品 → **不生成 parser、不消耗后续尝试**,直接终止阶梯,记 `invalid_target`,并将该页面的关键特征(如新出现的错误提示短语)**回填**进 `invalid_target_phrases` 表。

**回填的效果**:下一次同一 site 出现相同话术的页面,前置检测第 5 层(关键词)就能直接命中,不需要再次走到 Agent 才能识别——**系统对未知模式的识别成本只在第一次出现时支付一次**。

**判定阈值(需在 config 中明确,§7)**:多重结构缺失需要"至少几项同时缺失"才判定,建议默认 2 项(如同时缺 title 与加购控件)。阈值过低会误伤特殊样式的正常页面,过高会漏判。

> 此处需验证:JSON-LD 存在时不会被误判为 invalid_target;404 状态码正确触发;多重缺失阈值的边界情况(恰好 1 项缺失应放行,2 项缺失应拦截);Agent 兜底判定后短语库确实被写入,且下次前置检测能命中该短语。

---

## 6. SQLite 表结构(六表)

> 按变化频率分组组织;`website` 字段全部正名为 `site`。
> **四类记录职责划清**:results = 合格结果(复用资产);scrape_runs = 过程记录(含成功/失败/invalid_target);escalations = 需人工介入的工单;invalid_target_phrases = 轻量辅助学习库。互不重叠。

**parsers(慢)** —— 同 v1.1,未变。

**golden_samples(中)** —— 同 v1.1,未变。invalid_target 页面不会进入此表(见 §5.7)。

**scrape_runs(快;v1.2 outcome 新增枚举值)**

| 字段 | 说明 |
|---|---|
| id | |
| url | |
| host | |
| site | |
| scraper | 哪个 scraper 执行的 |
| scraped_at | |
| outcome | success / escalated / **invalid_target**(v1.2 新增) |
| path | fast / retried / agent_repaired / fallback_scraper / escalated / **invalid_target**(v1.2 新增) |
| winning_parser_id | invalid_target 时为 null |
| attempts | |
| model_used | |
| latency_ms | |
| cost | |

**results(快)** —— 同 v1.1,未变。invalid_target 不产出 ProductData,自然不写此表(D27)。

**escalations(低频;reason 新增枚举值)**

| 字段 | 说明 |
|---|---|
| id | |
| signature | 唯一约束;mass_invalid_target 的签名为 `(site, "invalid_target_surge")` |
| reason | parser_broken / infra_failure / api_malformed / **mass_invalid_target**(v1.2 新增) |
| affected_count | |
| snapshot | |
| status | |
| created_at | |

**invalid_target_phrases(轻量辅助表,v1.2 新增)**

| 字段 | 说明 |
|---|---|
| id | |
| site | |
| phrase | Agent 回填的特征短语/片段 |
| source | 恒为 `agent_backfill`(Phase 0 无其他来源) |
| added_at | |

- 定位:**辅助信号源**,不是主力判定依据(D26)。前置检测第 5 层按 site 查询此表做关键词匹配。
- 数据量小,增长缓慢(只在 Agent 兜底命中新模式时才新增一行),无需归档策略。

> 此处需验证:invalid_target 记录不出现在 results 表;mass_invalid_target 的 affected_count 递增逻辑;invalid_target_phrases 的回填写入与后续前置检测命中的联动。

---

## 7. 配置项集中(v1.2 新增两项)

| 配置项 | 默认值 |
|---|---|
| 每 site 并发上限 | 16 |
| extraction 重试次数 | 2 |
| extraction 重试间隔 | 2s |
| repair 预算(HTML 路线) | 3 |
| repair 模型阶梯 | flash / flash / pro |
| JSON 自愈预算(API 路线) | 1 |
| prune 滑动窗口 | 最近 50 次 |
| 每 site parser 硬上限 | 4 |
| 沙箱 timeout | 10s |
| 沙箱 import 白名单 | bs4, lxml, re, json |
| promote 考核 | 每 page_type ≥1 样本,全过 |
| scrape_runs 去重时间窗口 | 待定,建议先设 1h |
| 每 site scraper 列表 | 代码注册声明顺序 |
| **invalid_target 多重缺失判定阈值**(v1.2) | 同时缺失 ≥2 项(title/price/加购控件) |
| **mass_invalid_target 激增阈值**(v1.2) | 待定,建议先设"同 site 当日 invalid_target 占比 > 30% 或绝对数 > 20" |

---

## 8. 可观测性(v1.2 补充一条)

- 数据源同 v1.1,新增:**invalid_target 占比趋势**(按 site 按日聚合)—— 这是 mass_invalid_target 告警的判断基础,也是观察"某 site 的 URL 列表新鲜度"的直接信号,可反馈给上游召回模块。

---

## 9. Phase 2 钩子(留话不留码,未变)

1. 语言无关 parser。
2. 多域名 × 自写 parser 的组合。
3. 沙箱强隔离。
4. **(v1.2 新增)invalid_target 细分**:若未来需要区分「下架」与「链接失效」以支撑下架情报类产品功能,可在 D28 基础上拆分,当前合并的设计已为拆分留了空间(拆分只需给 scrape_runs.outcome 细化枚举值,不影响其他机制)。

---

## 10. 附录:实施顺序与里程碑(v1.2 微调)

```
M1: ProductData schema + 两道门(公共关卡)
     ▼
M2: BaseScraper 契约 + Router 两跳 + 多 scraper 注册表
     │
     ├────────────────────────┐
     ▼                        ▼
M3: SQLite 六表 + config    M4: DirectAPIScraper(Amazon)+ 受限 JSON 自愈
     │
     ▼
M5: HTMLScraper extraction 层 + 【无效页面检测工具雏形】(结构信号:JSON-LD/状态码/字段缺失/长度)
     │  (提前到此处实现基础版,因为它要在 M6 之前拦截,且逻辑相对独立)
     ▼
M6: 有序 parser 列表 match 逻辑 + scrape_runs / results 写入
     │
     ▼
M7: 沙箱 runner
     ▼
M8: Agent repair 阶梯 + 【no_product_on_page 判定分支 + 短语库回填】
     ▼
M9: golden set + promote/prune
     ▼
M10: scraper 层 fallback 驱动 + escalation(四类 reason,含 mass_invalid_target)
     ▼
M11: 冷启动路径打通
```

新增里程碑检验点:
- **M5 完成**:用一个已知 404/下架样例验证前置检测正确拦截,不进入 parser 尝试;
- **M8 完成**:用一个前置检测漏过的模拟样例验证 Agent 能正确判定 no_product_on_page 并终止阶梯,同时验证短语库被写入、下次同类样例被前置检测命中。

---

*Spec 版本:v1.2 — 新增无效目标检测机制,变更清单见文档头部。修改本 spec 中任何 D 编号决策前,先读其理由列。*
