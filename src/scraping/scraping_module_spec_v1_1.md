# Scraping 模块设计 Spec (Phase 0) — v1.1

> **文档性质**:设计决策与逻辑规则,不含文件结构与实现代码。实现由 Claude Code 对照本 spec 与现有 repo 执行。
> **Validation 约定**:本文只标注「此处需验证」锚点,说明什么该被验证;具体测试代码与命令由使用者在 Claude Code 中另行下达。
> **适用阶段**:Phase 0(原型/算法验证,纯 Python 本地运行,无 Docker/FastAPI/云服务)。
> **Phase 0 站点**:Tesco、Argos(自建 HTML 路线);Amazon UK(Bright Data 直接返回结构化 JSON,API 路线)。
>
> **v1.1 变更**(相对 v1.0):
> 1. 新增 **scraper 层 fallback**:一个 site 挂一个有序 scraper 列表,终态失败先换下一个 scraper,全挂才转人工(§5.13,D23);
> 2. 新增 **results 表**:只存合格结果,追加留全历史,五表结构(§6,D24);
> 3. **D10 修正**:DirectAPIScraper 获得受限 JSON 自愈(只准字段重映射,严禁凭空补数据)(§5.14,D25);
> 4. **数据流图修正**:两道门是两条 scraper 路线汇合后的**公共关卡**,不再画在 HTML 分支内部;门后失败分流按路线走(§1.2)。v1.0 此处图文矛盾(文字对、图错),已消除。

---

## 1. 系统架构总览

### 1.1 模块边界(输入 / 输出)

- **输入**:`(url, website)` —— 一个确切的商品页 URL 及其站点标识。
- **输出**:一个合法的 `ProductData`(Pydantic 模型);或抛出 `ScrapeFailed`(已完成人工升级的终态失败,携带失败签名与快照)。
- **明确不属于本模块的职责**:
  - 「商品名 → URL」的发现/召回(上游已有独立算法,Serper / google_shopping 属召回层);
  - 跨零售商的商品匹配(下游 matching 模块);
  - 数据业务合理性异常检测(如「价格暴涨 10 倍」)—— 交给下游分析师核对,scraping 只保证结构合法 + 语义通过两道门。

### 1.2 数据流(文字框图)

> **本图关键结构**:两道门是**公共关卡**,两条 scraper 路线的产出都必须经过;但**门后失败的分流按路线各走各的**(共享入口、分流出口)。

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
│     有序 parser 列表(按命中率,逐个尝试)→ 产出 dict      │
│                                                      │
│  B. DirectAPIScraper 路线(如 Amazon;或 Tesco 的       │
│     scraper_2 = Bright Data 成品 scraper)             │
│     API 调用 → JSON → 字段映射 → 产出 dict              │
│        │ API 报错/不返回数据 = 终态,不自愈(去◆)         │
└──────────────────────────────────────────────────────┘
      │ 两条路线的 dict 在此汇合
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

◆ 终态失败(任何路线、任何阶段):
      │
      ▼
   site 的 scraper 列表还有下一个?
      ├─ 有 → fallback 到下一个 scraper,从头走(scraper 层兜底)
      └─ 没有 → escalation(签名去重,记录哪个 scraper 哪个阶段挂)→ 人工

侧路:
- 每次抓取(无论成败)→ scrape_runs 记录(可观测性,含 scraper 标识)
- HTML 路线抓取成功时按需 → golden_samples 播种/补样
- Bright Data 基础设施故障 → escalations(infra_failure)+ 即时告警,不重试不 fallback 掩盖
```

### 1.3 持久层

单个 SQLite 库,五张表:`parsers` / `golden_samples` / `scrape_runs` / `results` / `escalations`(详见 §6)。易变数据(parser 代码、样本、运行记录、结果)全部进库;静态结构(注册表、类层级)在代码中。

---

## 2. 术语表

多个 Claude Code session 间必须统一使用下列词汇:

| 术语 | 定义 |
|---|---|
| **host** | URL 中的域名,如 `amazon.co.uk`、`amazon.fr`、`tesco.com` |
| **site** | 零售商标识(与域名解耦),如 `amazon`、`tesco`、`argos`。parser、golden set、签名均按 site 组织 |
| **Router 两跳** | `host → site`(小 dict)+ `site → 有序 scraper 列表`(decorator 注册表,带顺序) |
| **scraper 层 fallback** | 一个 site 名下的有序 scraper 列表(如 tesco → [自建HTML方案, Bright Data 成品方案]);任一 scraper 终态失败即换下一个,全挂才转人工。与 parser 层 fallback 是**嵌套**关系,勿混 |
| **parser 层 fallback** | 同一个 HTMLScraper **内部**的有序 parser 列表(解析方式的兜底) |
| **终态失败** | 一个 scraper 用尽自身全部手段后的失败:extraction 重试耗尽 / repair 预算耗尽 / API 报错或不返回数据 / 源头缺失判定。终态失败触发 scraper 层 fallback,而非直接转人工 |
| **两道门** | 门1 = Pydantic 类型/结构校验(单字段);门2 = `feasible_check` 跨字段语义校验。**公共关卡**:HTML 与 API 两条路线的产出都必须过;门后失败分流按路线走 |
| **parser 单元** | 独立、自成一体的小解析单元:输入 HTML → 要么产出合法 dict,要么失败。selector 与清洗逻辑内含其中 |
| **有序 parser 列表** | 一个 site 名下按命中率排序的 parser 集合,运行时逐个尝试,第一个通过两道门者胜出 |
| **candidate** | Agent 新生成、尚未通过考核的 parser |
| **promote(转正)** | candidate 通过 golden set 考核后进入有序列表(置于最前) |
| **prune(退休)** | parser 因长期 0 命中或列表满员被移出(status 置 retired,不物理删除) |
| **golden set / golden sample** | 快照 HTML + 其「标准答案」(`expected_output`,当时现役 parser 解出的 ProductData)。考核 candidate 与检测样本腐烂的依据 |
| **page_type(页型)** | standard / out_of_stock / discounted / multipack,由 ProductData 字段组合自动判定 |
| **repair 预算** | 每次 scrape 的 Agent 修复尝试上限(3 次),parse-exception 与 feasible-fail **共享**,promote 失败同样消耗 |
| **repair 阶梯** | 三级模型升级:flash → flash+报错上下文 → pro+全部报错上下文。仅 HTML 路线 |
| **JSON 自愈** | DirectAPIScraper 的受限修复:仅允许对**已存在于 JSON 中**的字段做重映射(键名/结构变化);**严禁凭空补齐缺失字段**;API 报错或不返回数据时不触发 |
| **源头缺失** | 输入中根本不存在目标数据(HTML:软墙/captcha/结构性缺字段;JSON:字段压根没返回),任何模型也修不出 → 不修,走终态 |
| **失败签名 (signature)** | `(site, 失败字段或feasible规则, parser_version)`,escalation 去重键 |
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
| D10 (v1.1 修正) | DirectAPIScraper **不进** HTML 自愈体系(parser 列表/沙箱/golden/repair 阶梯),但拥有**受限 JSON 自愈**(见 D25) | Bright Data 直接返回结构化 JSON,无 selector 可修,HTML 那套对它无意义;但 JSON 键名/结构会变,这类映射问题 Agent 修起来比 HTML 容易一个量级,值得自动化。v1.0 的「完全无自愈」表述废止 |
| D11 | gtin 定位为**匹配的可选短路键,非主键** | 页面不一定暴露 gtin(optional 是常态);EAN 粒度是「规格级」(净含量/多件装变了才换码,纯营销改包装不换);ASIN 不是 gtin(Amazon 内部 id,不跨站)。规则:双方都有且相等→强信号短路;不等或缺失→交给匹配算法,**不做反向否决** |
| D12 | mpn / sku 字段砍掉 | gtin 已覆盖黄金匹配键需求;mpn 次优备胎、sku 仅本站有效,Phase 0 无消费方 |
| D13 | 「price 必填性」是条件规则,放**门2**而非类型系统 | 类型系统只能无条件判断:必填则误杀缺货商品,optional 则漏掉在售无价的真故障。「看情况」的跨字段规则只能用条件逻辑写 |
| D14 | 沙箱用标准库自建(subprocess+timeout+setrlimit+AST 白名单),不引入专门 infra | 被执行代码是纯 HTML 解析,合法权限本应≈0。要防的只有死循环/异常/内存/危险 import,标准库足够。gVisor 等重武器留给 Phase 1+ |
| D15 | golden 样本存快照时**连标准答案一起存** | 考核从「能否解析」升级为「解析结果==标准答案」,防 candidate 抓错节点蒙混;同时腐烂可自动暴露(good parser 也跑不出标准答案→样本过期),免去人工定期巡检 |
| D16 | scrape_runs **不存 HTML**;大文本只进 golden_samples 与 escalations | scrape_runs 每次抓取都写,存 HTML(单个可达 1.6MB,Argos 实测)会爆库。高频表保持轻,大文本限于低频表 |
| D17 | 命中率**不落表,实时从 scrape_runs 聚合** | 存了就会与真实记录脱节;`GROUP BY winning_parser_id` 算出来永远准 |
| D18 | 数据业务异常检测(价格突变等)**移出本模块** | 需要历史基线与跨商品视角,是分析层职责。scraping 只管结构与语义两道门 |
| D19 | repair 阶梯中加入**源头缺失粗判**(Phase 0 即实现) | HTML 里根本没有数据时,升级模型是纯烧钱。粗判(HTML 长度异常/captcha 关键词)→ 跳过 pro 直奔人工 |
| D20 | 软墙检测抽成**独立小工具**,extraction 边界检查与 repair 粗判共用 | 二者本质是同一件事(判「此页面无有效商品内容」)。写两份会在反爬样式更新时改一漏一(DRY) |
| D21 | Bright Data 基础设施失败(额度/代理/账户)**不重试,直接告警** | 重试解决不了额度耗尽,只是白烧;且这类故障使全站抓取同时挂,是运维事件而非数据工单,需要即时通知而非安静排队。**也不做 scraper fallback 掩盖**(备用 scraper 多半同样依赖 Bright Data 账户) |
| D22 | 冷启动需**一次性人工确认**,并借此播种 golden set;**URL 必须由使用者提供** | 全新 site 无标准答案,第一版 parser 无从自动验证;它又是后续所有 golden 的源头,源头错会污染整条链。URL 是原料:系统无「商品名→URL」能力(归上游),没有 URL 则 Agent 无 HTML 可学。标准答案不需人写:人确认「对」的输出自动转为 golden。此后该 site 全自动 |
| D23 (v1.1 新增) | 一个 site 挂**有序 scraper 列表**;任何终态失败先 fallback 到下一个 scraper,全部挂掉才转人工 | 不同 scraper 走不同通道(自建 HTML 抽取 vs Bright Data 成品),一个通道卡住时另一通道很可能绕过(尤其 extraction 阶段故障);parsing 阶段故障时,成品 scraper 的改版适配是 Bright Data 的责任,也可能已适配。escalation 必须记录「哪个 scraper、哪个阶段」挂的:若「成品救自建」频繁发生,说明自建方案对该站性价比低,应人工调整列表顺序 |
| D24 (v1.1 新增) | results 表只写**通过两道门且未 escalated** 的合格结果;**追加留全历史**,不覆盖 | results 是被下游复用、被分析师当真相的资产,混入「抓错但结构恰好合法」的脏数据会导致错误结论且难追溯——失败/升级记录归 scrape_runs 与 escalations,职责三分。价格趋势是产品核心卖点,同一 URL 多时间点多行,覆盖历史等于自废武功 |
| D25 (v1.1 新增) | JSON 自愈**只允许重映射已存在字段,严禁凭空补齐缺失字段**;API 报错/不返回数据时直接 escalation,不触发自愈 | 不设红线时的危险失败模式:JSON 缺 price,Agent 为「修复成功」自作聪明从别的键凑值(如把 list_price 填进 price),制造出看似合法实则错误的静默脏数据——比直接失败更糟。「字段改名 → 修;字段缺失 → 源头缺失,不修」,判定复用 D19/D20 的源头缺失思路 |

---

## 4. 功能模块划分与职责边界

### 4.1 Router
- 职责:`host → site → 有序 scraper 列表` 两跳分发,并驱动 scraper 层 fallback(取列表第 1 个执行;终态失败取下一个;耗尽列表 → escalation)。
- host→site:简单 dict(如 `amazon.co.uk / amazon.fr / amazon.de → amazon`)。
- site→scrapers:decorator 注册表,scraper 类自行「报到」,**支持一个 site 注册多个 scraper 并声明顺序**。
- **不负责**:任何抓取、解析、失败处理的内部逻辑。

### 4.2 BaseScraper(抽象契约)
- 类属性:`site`、`source_type`(`"html"` / `"api"`)。
- 唯一抽象方法:`async scrape(url) -> ProductData`;终态失败抛 `ScrapeFailed`(携带 signature + snapshot + **哪个阶段挂的**,供 Router 决定 fallback 及 escalation 记录)。
- 契约极薄:所有自愈复杂度藏在实现层之后。

### 4.3 HTMLScraper(自愈骨架,Template Method)
- 复用逻辑全部在此:extraction 重试、有序 parser 列表、repair 阶梯、沙箱、golden 播种、escalation 上报。
- 站点子类(TescoScraper / ArgosScraper)只填:site 标识、extraction 配置(Tesco 用 Scraping Browser/CDP,因 Akamai TLS 指纹;Argos 用普通 Unlocker)。

### 4.4 DirectAPIScraper(API 路线)
- Bright Data(或其他成品)API → JSON → 字段映射 → 产出 dict → **公共两道门**。
- 无 parser 列表、无沙箱、无 golden set、无 repair 阶梯。
- 拥有**受限 JSON 自愈**(§5.14):仅当「数据在、键变了」时重映射;API 报错/不返回数据 → 直接 escalation(`api_malformed` 或 `infra_failure`),不自愈。
- 同一个类既服务 Amazon 主方案,也服务其他 site 的备用成品 scraper(如 Tesco 的 scraper_2)。

### 4.5 校验(两道门 —— 公共关卡)
- **所有 scraper 路线的产出都必须经过两道门**,无例外。门1:Pydantic 类型/结构;门2:`feasible_check` 跨字段语义(站点通用规则 + 允许 per-site 附加规则)。
- **门的判定公共,门后失败分流按路线**:HTML 路线 → repair 阶梯;API 路线 → 受限 JSON 自愈或 escalation。

### 4.6 Agent(repair)
- HTML 路线:输入失败 HTML + schema 要求 + 累积报错上下文,输出 candidate parser 代码(走沙箱+golden 考核)。
- API 路线:输入失败 JSON + schema 要求,输出**字段映射修正**(受 D25 红线约束)。
- Agent 本身不需限权(只生成字符串);限权对象是执行 candidate 的沙箱子进程。

### 4.7 存储层
- SQLite 五表(§6)。上游模块通过 orchestrator state 通信,不横向 import(import 流向严格向下)。

---

## 5. 详细机制规格

### 5.1 ProductData 契约

字段清单(最终版,mpn/sku 已砍):

| 分组 | 字段 | 类型 | 必填性 | 说明 |
|---|---|---|---|---|
| 溯源 | `url` | str | 必填 | 规范商品 URL |
| | `website` | str | 必填 | scraper 自填(它知道自己是谁)。值为 site 标识 |
| | `scraped_at` | datetime | 必填 | 价格情报是时间序列,不可省 |
| | `source_type` | Literal["html","api"] | 必填 | |
| | `parser_version` | str | optional | 哪个 parser 产出 → 签名/可观测性用。API 路线为 None |
| 识别 | `title` | str | 必填 | |
| | `brand` | str | optional | 有就截取,没就算了 |
| | `gtin` | str | optional | EAN/UPC。定位见 D11:可选短路键,非主键 |
| | `image_urls` | list[str] | 必填但**可为空列表** | 复数;缺货页/极简页可无图,不因无图判 parser 挂 |
| | `variant` | dict | optional | {size, color, pack_qty},服务下游区分性匹配 |
| 价格 | `price` | Decimal | 条件必填(见门2) | |
| | `currency` | str | optional | ISO-4217("GBP"/"EUR"),**不是**符号"£" |
| | `list_price` | Decimal | optional | 原价/RRP,判折扣 |
| | `unit_price` | Decimal | optional | £/kg,Tesco 生鲜关键但不一定有 |
| | `unit` | str | optional | 归一化留给下游匹配算法 |
| 库存 | `in_stock` | bool | 必填 | |
| | `availability_raw` | str | optional | 原始库存文案 |
| 调试 | `raw` | dict | optional | 归一化前原始串("£12.99"/"In stock"),escalation 快照直接可读 |

> 此处需验证:schema 实例化对各字段类型的拒收行为;`image_urls=[]` 应通过。

### 5.2 两道门校验(公共关卡)

**适用范围:所有 scraper 路线(HTML 与 API)的产出,无例外。**

**门1(Pydantic,免费送的)**:单字段类型/结构。`price` 在此层为 optional,**故意放缺货商品通过**。

**门2(feasible_check,手写)**:跨字段语义。核心规则:

| in_stock | price | 判定 |
|---|---|---|
| True | None | **故障** → 按路线分流(HTML→repair;API→JSON自愈/escalation) |
| False | None | 合法(缺货本就无价) |
| False | 有值 | 合法(有些站缺货仍挂末价) |

- 允许 per-site 附加规则(如价格合理区间,Tesco 与 Argos 不同)。
- **HTML 路线**:两门失败汇入同一个 repair 预算池(D8)。
- **API 路线**:两门失败 → 受限 JSON 自愈判定(§5.14),不进 repair 阶梯。

> 此处需验证:上表三种组合的判定;HTML 路线两门失败共同消耗预算的计数行为;API 路线产出确实经过两道门(防止实现时只给 HTML 路线挂校验)。

### 5.3 类层级与 Router

```
BaseScraper (ABC)              # 只有 scrape() 契约 + ScrapeFailed(带阶段信息)
  ├── HTMLScraper              # 自愈骨架(§5.4~5.7 全部逻辑)
  │     ├── TescoScraper       # site + Scraping Browser 配置
  │     └── ArgosScraper       # site + Unlocker 配置
  └── DirectAPIScraper         # JSON 映射 + 受限 JSON 自愈(§5.14)
        ├── AmazonUKScraper    # Amazon 主方案
        └── (各 site 的成品备用 scraper,如 TescoBrightDataScraper)
```

Router 两跳:`host → site`(dict)→ `site → 有序 scraper 列表`(decorator 注册表,带顺序,D3/D23)。

**scraper 层 fallback 驱动逻辑**(Router 职责):
```
for scraper in site_scraper_list:      # 按声明顺序
    结果 = await scraper.scrape(url)
    成功 → 返回
    终态失败 → 记录(哪个 scraper、哪个阶段),继续下一个
全部终态失败 → escalation(汇总各 scraper 的失败信息)→ 人工
例外:infra_failure(D21)不进入 fallback 循环,直接告警
```

### 5.4 parser 生命周期(四阶段,仅 HTML 路线)

**① match(运行)**
- 取该 site 全部 `active` parser,按命中率降序(实时聚合,D17)逐个尝试。
- 第一个通过两道门者胜出;记录 `winning_parser_id`。
- 列表为空 → 冷启动路径(§5.9)。

**② candidate(诞生)**
- 全部 parser 未通过 → 触发 Agent(repair 阶梯 §5.5)。
- Agent 产物 = candidate,无上岗资格。

**③ promote(转正)** —— candidate 的完整晋级链,顺序固定:
```
candidate → 沙箱(§5.6:能不能安全跑) → 两道门(产出合不合法)
          → golden set 考核(§5.7:抓得对不对,多页型全过)
          → promote 进列表最前
```
- 沙箱只判「安全与合法结构」,**不判「抓得对」**——后者是 golden 考核的职责,两关不可互相替代;
- golden 考核:该 site 每个 page_type 桶各抽非 stale 样本,**每类至少 1 个,结果必须 == expected_output,全过才转正**(严格策略:价格数据静默错误代价高,宁可 promote 偶尔被卡);
- 通过 → 写入 parsers 表,置于列表最前;旧 parser 保留做 fallback,不硬删;
- 考核失败 = repair 失败的一种,同样消耗预算。

**④ prune(退休)**
- 双保险:
  - 自然退休:**最近 50 次抓取滑动窗口内 0 命中**。按 parser **独立计数**,不用全局计数器——防止低频但有效的 parser(专治罕见页型)被高频页型的抓取量冲刷误杀;
  - 硬上限:每 site 最多 **4** 个存活 parser;满员要加新的,强制退休命中率最低者。
- 退休 = status 置 `retired`,不物理删除。

> 此处需验证:promote 的分桶全覆盖逻辑;沙箱通过但 golden 不过的 candidate 确实被拒;prune 的独立滑动窗口计数;硬上限触发时的强制退休选择。

### 5.5 repair 阶梯(仅 HTML 路线)

预算 3 次,parse-exception / feasible-fail / promote-fail 共享:

```
尝试1: deepseek-v4-flash
尝试2: deepseek-v4-flash + 尝试1的报错做上下文
       └─ 同时做「源头缺失」粗判(共用软墙检测工具,D20):
          HTML 长度异常 / captcha 关键词 / 结构性无商品内容
          → 判定源头缺失:跳过尝试3,记终态失败(触发 scraper fallback)
尝试3: deepseek-v4-pro + 前两次全部报错做上下文
仍失败 → 终态失败(触发 scraper fallback;若列表已尽 → escalation,
        携带全部 3 次报错 + 快照 + 试过的 candidate)
```

- 每次失败的报错累积传递,让重试真的「学到东西」而非换模型瞎试。
- 转人工时保留全部挣扎记录,人不必从零复现。

### 5.6 沙箱(candidate 执行环境)

- 目标:防死循环 / 异常 / 内存爆 / 危险 import。纯本地,零新依赖(D14)。
- **职责边界**:沙箱判定「这段代码能不能安全跑、产出结构是否合法」;它**不判定「抓得对不对」**(golden 考核的职责)。沙箱通过 ≠ parser 正确。
- 流程:
  1. **执行前**:AST 扫描 candidate 代码,import 白名单仅 `bs4 / lxml / re / json`;出现 `os / socket / open / eval / subprocess` 等直接拒,不执行;
  2. **执行**:subprocess 运行,timeout(默认 10s),HTML 走 stdin,结果 JSON 走 stdout;子进程内 `resource.setrlimit` 限 CPU/内存;
  3. **执行后**:stdout 结果过两道门,全过才有资格进 golden 考核。
- 未经「沙箱 + golden 考核」完整晋级链验证的生成代码,**永不接触**生产输出路径。

> 此处需验证:白名单外 import 的拒收;超时/内存超限时主进程无损;恶意样例(死循环、大内存分配)的隔离效果。

### 5.7 golden set(样本池,仅 HTML 路线)

**播种与补样**
- 正常抓取成功时顺手存:HTML 快照 + 现役 parser 解出的 ProductData(= `expected_output`,标准答案,D15)。
- 存前查重(§5.10),按 page_type 归桶。
- 冷启动的第一批 golden 来自人工确认(§5.9)。

**page_type 自动分类(无需额外算法)**
- 页型 = ProductData 字段布尔组合,if/elif 即可:
  - `in_stock == False` → out_of_stock
  - `list_price` 存在且 > price → discounted
  - `variant.pack_qty > 1` → multipack
  - 其余 → standard
- 边界:分类依赖「现役 parser 当时是好的」。改版当天解析失败的页面走修复流程,本就不进样本池——存样本永远发生在成功路径上,不冲突。

**腐烂自动暴露**
- 判据:现役 good parser 也跑不出某快照的标准答案 → 该快照 `is_stale = true`,踢出考核池,提示补样。
- 人只被动响应提示,不做定期巡检。

**容量参考**:每 site 4 页型 × 每型 ~3 样本 ≈ 12 个 × ~1.6MB ≈ 20MB/站,两个 HTML 站 ≈ 40MB,SQLite 无压力(单值上限 ~1GB)。

### 5.8 失败类型完整映射表(自愈逻辑总闸)

> Claude Code 遇到任何失败,先查此表归口;表外新失败类型必须先补进此表再实现。
> **v1.1 关键变化**:多数「转人工」改为「终态失败 → 先 scraper fallback → 列表耗尽才 escalation」。

| 失败类型 | 检测方式 | 处理路径 |
|---|---|---|
| 目标站非 200 / 超时 / 网络异常 | extraction 层状态码/异常 | 暂停 2s 重试,最多 2 次 → 仍失败 = **终态** → scraper fallback → 列表尽则 escalation |
| **Bright Data 基础设施失败**(407/额度耗尽/代理/账户) | Bright Data 错误码 | **不重试、不 fallback**(备用 scraper 多半同账户,fallback 只会掩盖运维事件),escalation(`infra_failure`)+ **即时告警** |
| 返回 200 但软墙/captcha/无有效内容 | 软墙检测工具(D20) | extraction 层抓不到(只看状态码),落到门2或 repair 粗判兜住 → 源头缺失 = **终态** → scraper fallback |
| parse 抛异常 | parser 执行异常 | repair 预算池(3 次阶梯) |
| 门1 ValidationError | Pydantic | HTML 路线:repair 预算池(共享);API 路线:JSON 自愈判定(§5.14) |
| 门2 feasible 失败 | feasible_check | 同上,按路线分流 |
| promote 考核失败 | golden 比对不等 | 消耗 repair 预算,预算未尽则再生成 |
| repair 预算耗尽 | 计数器 | **终态** → scraper fallback → 列表尽则 escalation(`parser_broken`,带全部报错+快照+candidate) |
| 源头缺失(粗判命中) | 软墙检测工具 | 跳过 pro,**终态** → scraper fallback |
| **API 报错 / 不返回数据** | DirectAPI 调用层 | **不触发 JSON 自愈**(D25),= **终态** → scraper fallback → 列表尽则 escalation(`api_malformed` 或 `infra_failure`) |
| **API 返回了 JSON 但键名/结构变化**(数据在) | 门失败 + 自愈判定 | 受限 JSON 自愈(§5.14),修不了 = **终态** → scraper fallback |
| **API JSON 中字段缺失**(数据不在) | 自愈判定(源头缺失) | **禁止自愈补数据**(D25),= **终态** → scraper fallback |
| parsers 表为空(新 site) | 列表查询 | 冷启动路径(§5.9),不算失败 |
| 数据合法但业务可疑(价格突变等) | —— | **不在本模块处理**(D18),下游分析师核对 |

### 5.9 冷启动(新 site 从零到一)

- 触发:该 site `active` parser 为空集 = 一种特殊的「全部未命中」,自然落入 Agent 流程,**无需独立分支逻辑**。
- **使用者必须提供的原料:一批该 site 的商品 URL**(系统无「商品名→URL」能力,归上游;没有 URL 则 Agent 无 HTML 可学)。标准答案**不需**使用者手写。
- 先有鸡还是先有蛋:promote 靠 golden 比对,但新 site 没有 golden。受限初始化路径:
  1. 使用者提供一批该 site 的商品 URL;
  2. 实时抓取这批页面,Agent 生成第一版 parser;
  3. **一次性人工确认**:第一版 parser 在这批页面上的输出,人眼核对对错(机器无基线可比,D22);
  4. 确认「对」的输出**当场自动成为该 site 第一批 golden 标准答案**;
  5. 此后该 site 回归全自动 promote,不再需要人。
- 这是唯一需要人介入 promote 的时刻。

### 5.10 幂等与去重

- **scrape_runs**:同一 URL 在时间窗口内(配置项)重复抓取 → 去重,防止命中率统计被重复记录污染。
- **results**:同一 URL 不同时间点为**不同行**(追加,D24);时间窗口内的重复抓取因 scrape_runs 去重而自然不产生重复 result。
- **golden_samples**:存样本前查重,避免同页重复快照。
- **escalations**:signature 唯一约束,同签名重复到达 → `affected_count += 1`,不新建行(50 个失败塌成 1 张工单)。

### 5.11 并发与批量执行模型

- 每 site 一个 semaphore,**默认并发上限 16**,允许 per-site 覆盖(Tesco 的 Scraping Browser 重、Argos 的 Unlocker 轻,承压不同)。
- 批量任务中各 URL 独立成败,互不阻塞;禁止无界 `asyncio.gather` 全量并发(会触发限流/反爬)。

### 5.12 escalation(人工升级)

- 触发前提(v1.1):该 site 的 scraper 列表**全部**终态失败(infra_failure 例外,直达)。
- 签名:`(site, 失败字段或feasible规则, parser_version)`,唯一约束去重。
- `reason` 三类:
  - `parser_broken` —— repair 耗尽,带 Agent 全部尝试记录;
  - `infra_failure` —— Bright Data 基础设施,**高优先级即时告警**(邮件/IM),不安静排队,不被 fallback 掩盖;
  - `api_malformed` —— API 报错/JSON 坏/字段缺失。
- 快照内容:1 份 raw HTML(或 JSON)+ 1 个样本 URL + 全部报错 + 试过的 candidate 版本 + **每个 scraper 在哪个阶段挂的** + expected schema。1 份即可,人能定位,不塞 N 份。
- 「哪个 scraper 哪个阶段」的记录还有第二用途:若「成品 scraper 频繁救活自建方案」,提示自建方案对该站性价比低,应人工调整 scraper 列表顺序(D23)。

### 5.13 scraper 层 fallback(v1.1 新增)

- 一个 site 注册**有序 scraper 列表**,如 `tesco → [TescoScraper(自建HTML), TescoBrightDataScraper(成品JSON)]`。
- **两层 fallback 的嵌套关系,勿混**:
  - parser 层 fallback = 同一个 HTMLScraper **内部**换解析方式(有序 parser 列表);
  - scraper 层 fallback = 整个 scraper 方案**换一套**(不同抽取通道)。
- 触发:当前 scraper 的**任何终态失败**(extraction 重试耗尽 / repair 预算耗尽 / 源头缺失 / API 报错或字段缺失)→ Router 取列表下一个 scraper 从头执行。
- 终点:列表耗尽 → escalation(汇总各 scraper 失败信息)。
- 例外:`infra_failure` 直达告警,不进 fallback 循环(D21)。
- 备用 scraper(成品 JSON 类)本身走 DirectAPIScraper 路线:同样过公共两道门,同样受 D25 红线约束。

> 此处需验证:终态失败正确触发列表推进;infra_failure 不触发 fallback;escalation 汇总包含每个 scraper 的阶段信息。

### 5.14 受限 JSON 自愈(v1.1 新增,仅 API 路线)

- **触发条件**:API 调用成功、返回了 JSON,但产出 dict 未通过两道门(典型:上游改了键名/嵌套结构)。
- **允许**:Agent 重新生成「JSON → ProductData 字段」的映射,**仅限映射到 JSON 中已存在的数据**。
- **严禁(D25 红线)**:
  - 凭空补齐 JSON 中不存在的字段(如缺 price 时从 list_price 凑值填入);
  - API 报错 / 不返回数据 / 返回空时触发自愈(此时直接终态,走 fallback/escalation)。
- **判定「字段改名 vs 字段缺失」**:复用源头缺失思路(D19/D20)——Agent 修复前先回答「目标数据在 JSON 里存在吗」;不存在 → 不修,终态。
- 自愈成功的新映射持久化(替换旧映射即可,JSON 映射无需 HTML 那套列表/golden 机制——结构远稳定于 HTML,且映射对错由两道门 + 下一次抓取即时暴露)。
- 修复预算:1 次(JSON 映射问题一次修不好,大概率是源头问题,不值得阶梯)。

> 此处需验证:「键改名」样例被修复;「字段缺失」样例被拒绝修复并走终态;API 报错不触发自愈。

---

## 6. SQLite 表结构(五表)

> 按变化频率分组组织;`website` 字段全部正名为 `site`。
> **三类记录职责划清(D24)**:results = 合格结果(复用资产);scrape_runs = 过程记录(可观测性,含失败);escalations = 失败工单。互不重叠。

**parsers(慢:仅改版时变)**

| 字段 | 说明 |
|---|---|
| id | |
| site | `tesco` / `argos`(纯 API 站不入此表) |
| version | 如 `v3`,签名要用 |
| code | parser 代码文本本体(selector 内含,D5) |
| page_type_scope | 可选,调试用 |
| status | `active` / `retired` |
| created_at | |
| created_by | `initial` / `agent` |

**golden_samples(中:补样时变)**

| 字段 | 说明 |
|---|---|
| id | |
| site | |
| page_type | standard / out_of_stock / discounted / multipack |
| html_snapshot | 完整 HTML(存库,单值远低于 SQLite 1GB 上限) |
| expected_output | 标准答案 ProductData(JSON) |
| captured_at | |
| is_stale | 腐烂标记 |

**scrape_runs(快:每次抓取写,含失败;不存 HTML,D16)**

| 字段 | 说明 |
|---|---|
| id | |
| url | |
| host | 保留细粒度(如区分 amazon.fr 与 amazon.co.uk 的失败率) |
| site | 聚合用 |
| scraper | 哪个 scraper 执行的(fallback 分析用,v1.1) |
| scraped_at | |
| outcome | success / escalated |
| path | fast / retried / agent_repaired / fallback_scraper / escalated |
| winning_parser_id | 命中率聚合键(D17) |
| attempts | |
| model_used | flash / pro / null |
| latency_ms | |
| cost | |

**results(快:每次合格抓取写;v1.1 新增,D24)**

| 字段 | 说明 |
|---|---|
| id | |
| url | |
| site | |
| scraped_at | 时间戳,时间序列的键 |
| product_data | 完整 ProductData(JSON) |

- **准入**:仅通过两道门、且未 escalated 的结果。失败不入此表。
- **写入模式**:追加(同一 URL 多时间点多行),永不覆盖——价格历史是核心资产。
- 常用查询:某 URL 的价格时间序列;某 site 某日全量快照。

**escalations(低频)**

| 字段 | 说明 |
|---|---|
| id | |
| signature | 唯一约束 |
| reason | parser_broken / infra_failure / api_malformed |
| affected_count | 同签名计数 |
| snapshot | HTML/JSON 只在此表与 golden 出现;含各 scraper 失败阶段 |
| status | open / resolved |
| created_at | |

> 此处需验证:signature 唯一约束下重复到达的 affected_count 递增;scrape_runs 时间窗口去重;results 只接收合格结果(escalated/失败被拒);results 追加不覆盖。

---

## 7. 配置项集中

所有魔法数字收进单一 config,禁止散落硬编码:

| 配置项 | 默认值 |
|---|---|
| 每 site 并发上限 | 16(允许 per-site 覆盖) |
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
| 每 site scraper 列表 | 代码注册声明顺序(如 tesco: [自建, BrightData成品]) |

---

## 8. 可观测性

- 数据源:`scrape_runs` 每次抓取记录 scraper / path / attempts / model_used / latency / cost / winning_parser_id。
- 这不只是监控,是**机制的输入**:
  - parser 命中率(GROUP BY winning_parser_id)→ 列表排序 + prune 判断;
  - path 分布 → 某站是否频繁触发 Agent(改版频率信号);
  - `fallback_scraper` 频率 → 「成品救自建」是否频繁,自建方案性价比信号(D23);
  - model_used + cost → repair 阶梯的成本核算。
- Phase 1 需考虑 scrape_runs / results 归档;Phase 0 不管。

---

## 9. Phase 2 钩子(留话不留码)

1. **语言无关 parser**:当前所有自写 parser 的站点(Tesco/Argos)均为单域名单语言;唯一多域名站 Amazon 走 API 无 parser。**若未来引入跨语言自写 parser 站点**(如法国/德国零售商),parser 的库存/货币判断需改为语言无关:不依赖页面文案("In stock"/"En stock"),改用加购按钮存在性 / structured data availability 字段;价格只认数字不认符号;货币从域名推导。现在不设计,但坑在这。
2. **多域名 × 自写 parser 的组合**:host→site 两跳已为此留好位置(Router 一个 dict),届时 parsers 表按 site 组织的设计可直接承接,无需迁移。
3. **沙箱强隔离**:Phase 1+ 若需要,把现有 runner 塞进容器即可,接口不变。

---

## 10. 附录:实施顺序与里程碑

> 施工顺序建议,供人 + Claude Code 协作参考。箭头 = 依赖。

```
M1: ProductData schema + 两道门(公共关卡)
     │(一切分支的收口点,必须最先钉死)
     ▼
M2: BaseScraper 契约 + Router 两跳 + 多 scraper 注册表(含顺序)
     │
     ├────────────────────────┐
     ▼                        ▼
M3: SQLite 五表 + config    M4: DirectAPIScraper(Amazon)
     │                        + 受限 JSON 自愈(§5.14)
     ▼                        (只依赖 M1/M2,可与 M3 并行)
M5: HTMLScraper extraction 层(Bright Data 接入 + 重试 + 软墙检测工具)
     │
     ▼
M6: 有序 parser 列表 match 逻辑 + scrape_runs / results 写入
     │
     ▼
M7: 沙箱 runner
     │
     ▼
M8: Agent repair 阶梯(依赖 M7 沙箱、M6 的失败出口)
     │
     ▼
M9: golden set(播种/分类/腐烂检测)+ promote/prune
     │(依赖 M6 的成功路径播种、M8 产 candidate)
     ▼
M10: scraper 层 fallback 驱动(Router 循环)+ escalation(签名去重 + 三类 reason + infra 告警)
     │
     ▼
M11: 冷启动路径打通(使用者提供 URL → Agent 生成 → 人工确认 → 播种 golden)
```

里程碑检验点:
- **M1-M2 完成**:能用假数据实例化 ProductData、Router 能对三个 site 正确取出 scraper 列表并按序分发;
- **M4 完成**:Amazon 真实 URL 端到端出 ProductData,且产出确实经过两道门;
- **M6 完成**:Tesco/Argos 用手写初始 parser 端到端出 ProductData,results 表正确落库;
- **M9 完成**:人为破坏一个 parser,系统能自动修复并 promote(全链路自愈演习);
- **M10 完成**:人为掐死 scraper_1 的 extraction,系统 fallback 到 scraper_2 成功出数;
- **M11 完成**:模拟新 site 接入,冷启动全流程走通。

---

*Spec 版本:v1.1 — 变更清单见文档头部。修改本 spec 中任何 D 编号决策前,先读其理由列。*
