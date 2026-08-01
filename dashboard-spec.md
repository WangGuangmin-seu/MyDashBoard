# 个人数据看板 — 需求与实现方案

> 本文档是交给 Claude Code 的实施规格。阅读者请先通读「关键环境知识」一节，其中包含数据源的非直觉行为，这些信息无法从模型的训练数据中获得，且直接决定实现的正确性。

---

## 1. 目标

构建一个个人用的多源数据看板，满足两条核心要求：

1. **打开即见最新数据** —— 在手机或桌面浏览器打开时，数据必须立即呈现，不能出现等待接口返回的加载过程。
2. **可持续扩展数据类型** —— 新增一个数据源的成本应当是「新增一个文件」，而不是修改前端或存储层。

当前首批要跟踪的是宏观与地缘数据（海峡通行量、石油库存、美国财政账户），但架构不得对这些领域做任何假设。

### 非目标

- 不做实时行情终端，不支持秒级数据
- 不支持下单、交易或任何写操作
- 不支持多用户、权限体系
- 不支持从客户端编辑或回写数据
- 不做移动端原生应用

---

## 2. 核心设计决策

### 2.1 预计算快照，而非请求时拉取

数据源更新频率为日更或周更，而查看频率远高于此。因此采集与展示必须解耦：

```
定时任务（每日）
  → 各 collector 抓取
  → 归一化并写入序列存储
  → 与上一版本 diff
  → 评估告警规则
  → 命中则推送飞书
  → 生成前端快照
  → commit & push
  → 站点数据更新
```

前端只读一个预生成的快照文件，不直接访问任何上游 API。

### 2.2 通用性来自数据契约，而非看板框架

前端不认识任何具体指标。它只认识一种统一的序列格式，渲染逻辑完全由数据自描述。新增数据源 = 新增一个 collector，前端零改动。

### 2.3 语言选型：Python（后端）+ 原生 JavaScript（前端）

采集层、存储层、告警层使用 Python 3.12+。前端为静态页面，使用原生 JavaScript，**不引入构建步骤**。

技术栈：

| 用途 | 选型 |
|---|---|
| 依赖管理 | `uv`（或 `requirements.txt`，二选一，不混用） |
| HTTP 客户端 | `httpx` |
| 数据契约 | `pydantic` v2 |
| 规则解析 | `PyYAML` |
| 前端图表 | 轻量库或手写 SVG，通过 CDN 引入 |

**契约一致性保障（必须实现）**

由于后端是 Python 而前端是 JavaScript，数据契约横跨两种语言，无法靠类型系统保证一致。因此：

1. 契约以 pydantic 模型为唯一定义源
2. 构建时通过 `model_json_schema()` 导出 `data/schema.json`
3. CI 中增加一步：用该 schema 校验生成的 `snapshot.json`，校验失败则整个 workflow 失败

这一步替代了静态类型检查的作用，不可省略。否则后端字段改名后前端会静默显示空白，而没有任何报错。

---

## 3. 数据契约

**这是整个系统唯一不允许后期重构的部分，必须首先实现并冻结。**

### 3.1 观测点

```python
from enum import StrEnum
from datetime import datetime
from pydantic import BaseModel

class DataStatus(StrEnum):
    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    UNDER_REVIEW = "under_review"
    ESTIMATED = "estimated"

class Observation(BaseModel):
    series_id: str                    # 全局唯一，形如 "portwatch.hormuz.transits"
    observed_at: datetime             # 数据点所属时间
    as_of: datetime                   # 我们获知该值的时刻
    value: float | None               # None 表示上游明确返回缺失
    status: DataStatus
    source: str                       # collector 标识
    revision_of: datetime | None = None   # 若为修订，指向被修订记录的 as_of
```

所有 datetime 必须带时区，统一以 UTC 存储，序列化为 ISO 8601。

**双时间轴是强制要求。** `observed_at` 与 `as_of` 必须分开存储。

原因：上游会回改历史数据。例如 IMF PortWatch 曾将霍尔木兹某几日的数据标记为「复核中」并在之后修订。若只存单时间轴，历史图表会在用户不知情的情况下静默改变，且任何回测都将引入前视偏差。

### 3.2 序列元数据

```python
from datetime import timedelta
from typing import Literal

class SeriesMeta(BaseModel):
    series_id: str
    display_name: str
    unit: str
    source: str
    expected_interval: timedelta          # 序列化为 ISO 8601 duration，如 P1D / P7D
    precision: int                        # 展示时保留的小数位
    direction_good: Literal["up", "down", "neutral"] = "neutral"
    description: str | None = None
```

`expected_interval` 用于心跳检测，非可选字段。

---

## 4. 采集层

### 4.1 Collector 接口

```python
from typing import Protocol

class Collector(Protocol):
    id: str
    series: list[SeriesMeta]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]: ...
```

使用 `asyncio.gather(..., return_exceptions=True)` 并发执行各 collector，以保证单个失败不影响其他。

约束：

- collector 只负责「抓取 + 归一化」，不负责存储、不负责去重、不负责告警
- 抓取失败必须抛异常，**禁止返回空数组或静默降级**
- 返回的每条 observation 必须带正确的 `status`，不得一律标为 `confirmed`
- 单个 collector 失败不得中断其他 collector

### 4.2 首批 collector

#### `portwatch` — IMF PortWatch 咽喉点通行量

- 平台：ArcGIS Hub，数据集 `Daily Chokepoint Transit Calls and Trade Volume Estimates`
- 入口：`https://portwatch.imf.org/datasets/42132aa4e2fc4d41bdaf9a445f688931_0/about`
- 接口：FeatureServer `/query`，标准 ArcGIS 参数（`where` / `outFields` / `resultOffset` / `f=json`）
- 需要的咽喉点：霍尔木兹（`chokepoint6`）、曼德海峡、好望角
- 产出序列：各咽喉点的日通行船数与通行贸易量

#### `treasury` — 美国财政部 Fiscal Data

- 根路径：`https://api.fiscaldata.treasury.gov/services/api/fiscal_service/`
- **无需 API key**
- 需要的数据：Daily Treasury Statement 中的 TGA 现金余额（日频）；Monthly Treasury Statement 收支（月频）

#### `eia` — 美国能源信息署

- 根路径：`https://api.eia.gov/v2/`
- 需要免费注册 API key，存入 secrets
- 需要的数据：SPR 库存、商业原油库存（周频，周三发布）

> 上述所有端点的确切路径、字段名与分页行为，**实现时必须先实际请求一次并根据真实响应编写解析逻辑**，不得依据本文档或模型记忆推断字段名。

---

## 5. 存储层

### 5.1 布局

```
data/
  series/<series_id>.json    # 全量历史，含所有修订记录
  meta.json                  # 所有 SeriesMeta 汇总
  snapshot.json              # 前端唯一读取的文件
  alert_state.json           # 告警去重状态
```

### 5.2 写入语义

- **追加而非覆写。** 当上游对同一 `observed_at` 给出新值时，追加一条新记录，`as_of` 为当前时刻，`revision_of` 指向旧记录。不得原地修改历史记录。
- 快照生成时，对每个 `observed_at` 取 `as_of` 最大的那条作为当前值。

### 5.3 快照格式

快照应为自包含的单文件，包含前端渲染所需的全部内容：各序列的元数据、最近 N 个点的时间序列（N 可配置，建议 180）、最新值、上一值、采集健康状态。

前端不得为渲染而发起第二次请求。

### 5.4 版本控制即历史存储

序列文件随每次采集 commit 回仓库。git 历史天然提供了完整的修订追溯，无需额外实现审计表。

---

## 6. 告警层

### 6.1 规则以配置表达，不以代码表达

```yaml
# rules/alerts.yaml
- id: hormuz_collapse
  series: portwatch.hormuz.transits
  condition: "value < 20"
  cooldown: 7d
  skip_if_status: [under_review, provisional]
  severity: critical
  message: "霍尔木兹日通过 {value} 艘（阈值 20）"
```

新增告警不应需要修改任何 TypeScript 文件。

### 6.2 心跳检测（必须实现）

**这是本系统最重要的告警类型。**

对每个序列，若 `now - 最新 observed_at > 2 × expected_interval`，触发采集中断告警。

理由：上游改字段名会导致解析出空结果，看板上的曲线会停在最后一个值不动。使用者会将其误读为「数据没有变化」，而实际上管道已中断。**「数据没来」比「数据越界」更需要立即通知。**

### 6.3 去重

`alert_state.json` 记录每条规则的 `last_fired_at`，在 `cooldown` 内不重复推送。

未实现去重的后果：阈值一旦突破，每次运行都推送一条，使用者会在数日内将通知静音，进而错过真正重要的告警。

### 6.4 修订数据的误报防护

被标记为 `under_review` 或 `provisional` 的数据点可能是上游失真（例如 AIS 信号被干扰导致的假低值）。规则必须支持 `skip_if_status`；即使不跳过，推送消息中也必须显式标注该状态。

---

## 7. 通知：飞书自定义机器人

### 7.1 配置

群设置 → 群机器人 → 添加自定义机器人 → 获取 webhook URL。

安全设置选择**签名校验**。**不要选 IP 白名单**，GitHub Actions 的出口 IP 不固定。

### 7.2 签名算法（反直觉，注意）

签名以 `{timestamp}\n{secret}` 作为 HMAC 的 **key**，而待签名消息体为**空字符串**：

```python
import base64, hashlib, hmac, time

timestamp = str(int(time.time()))
string_to_sign = f"{timestamp}\n{secret}"
sign = base64.b64encode(
    hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
).decode("utf-8")
```

注意 `hmac.new()` 未调用 `.update()`，待签名消息即为空——这是该算法的正确形态，不是遗漏。

timestamp 需在 1 小时有效期内。

> 实现前请对照飞书开放平台当前官方文档确认该算法未发生变更。

### 7.3 消息格式

使用交互式卡片（`msg_type: "interactive"`），按严重度着色 `header.template`：

| template | 语义 |
|---|---|
| `red` | 数值突破阈值 |
| `grey` | 采集器心跳失败 |

这两类必须视觉可区分：前者是外部世界发生变化，后者是本系统故障，处理方式完全不同。

### 7.4 响应校验（必须实现）

**飞书在消息被安全策略拒绝时仍可能返回 HTTP 200。** 必须解析响应体并检查 `code` 字段，非 0 视为发送失败并记入日志。

仅检查 HTTP 状态码会导致告警系统静默失效，这比没有告警更危险。

### 7.5 已知限制

自定义机器人 webhook 为单向，无法接收交互回调，因此不支持「点击按钮静音告警」一类功能。如需交互需改用飞书应用，要求企业管理员权限，当前不在范围内。

---

## 8. 前端

### 8.1 形态

单页静态站点。布局为纵向卡片列表，每张卡片对应一个序列：显示名、当前值、单位、迷你折线图、数据状态徽标、最后更新时间。采集中断的序列以降饱和度呈现并显示中断提示。

点击卡片展开大图，支持切换时间范围。

**卡片与图表必须由 `snapshot.json` 完全驱动**，不得存在任何针对特定 `series_id` 的硬编码分支。

### 8.2 PWA

- `manifest.json` + service worker
- 支持添加到主屏，独立窗口启动
- **缓存优先策略**：service worker 先返回缓存的快照使界面立即呈现，随后后台请求新快照并静默替换

这是满足「打开即见最新数据」的具体实现手段：先毫秒级显示上次数据，再无感更新。

### 8.3 数据获取方式（关键约束）

前端必须在**运行时** fetch 快照：

```typescript
const snap = await fetch(`/data/snapshot.json?v=${Date.now()}`);
```

**禁止在构建期将数据内联进 HTML 或打包产物。**

理由见 §11。此约束是未来迁移到小时级频率的唯一前提，现在遵守的成本为零，事后修改需重写前端。

---

## 9. CI/CD

### 9.1 GitHub Actions

```yaml
on:
  schedule:
    - cron: '0 1 * * *'   # UTC
  workflow_dispatch:       # 必须保留手动触发
```

流程：抓取 → 写入 → 告警 → 生成快照 → commit → push。

即使某个 collector 失败也要完成后续步骤并提交，同时把失败作为心跳告警推送。

### 9.2 部署

静态站点部署至 Cloudflare Pages（或同类）。

**数据更新不得触发站点重新构建。** 站点仅在代码变更时构建。

### 9.3 Secrets

`EIA_API_KEY`、`FEISHU_WEBHOOK_URL`、`FEISHU_SECRET` 均存入 GitHub Actions secrets。webhook URL 本身即完整凭证，禁止提交进仓库。

---

## 10. 目录结构

```
dashboard/
  contract.py         # Observation / SeriesMeta / Collector，唯一契约定义源
  collectors/
    base.py           # 通用 HTTP、重试、错误处理
    portwatch.py
    treasury.py
    eia.py
    registry.py       # 注册表
  pipeline/
    store.py          # 序列读写、修订处理
    snapshot.py       # 快照生成 + schema 导出
    alerts.py         # 规则引擎 + 心跳
  notify/
    feishu.py
  __main__.py         # 入口：python -m dashboard
rules/
  alerts.yaml
data/                 # 见 §5.1，含 schema.json
web/                  # 纯静态，无构建步骤
  index.html
  app.js
  sw.js
  manifest.json
tests/
pyproject.toml
.github/workflows/collect.yml
CLAUDE.md
```

`web/` 目录下不得出现需要编译的文件。这样 Cloudflare Pages 的「构建」实际上只是复制静态文件，配额消耗与失败面都最小。

---

## 11. 关键环境知识

以下内容是实现正确性的前提，且不存在于模型的常识中。

| 项 | 事实 | 影响 |
|---|---|---|
| PortWatch 更新时点 | 数据标称日频，但**每周二美东 9:00 才更新一次** | 最新数据可能滞后 7 天。回测中必须按 `as_of` 而非 `observed_at` 切片 |
| PortWatch 数据质量 | 官方公告该区域存在 GPS 干扰、AIS 欺骗、船舶关闭信号，部分日期数据处于复核状态 | 必须实现 `under_review` 状态传递与告警跳过 |
| Cloudflare Pages 配额 | 免费额度约 500 次构建/月 | 若数据更新触发构建，改为每小时采集后（720 次/月）即超限。这是 §8.3 运行时 fetch 约束的根本原因 |
| GitHub Actions 调度 | 定时触发存在数分钟至半小时延迟；仓库 60 天无提交则自动停用定时任务 | 延迟对日频数据无影响；因每次运行都会 commit，不会触发停用 |
| 飞书响应 | 消息被安全策略拒绝时仍可能返回 HTTP 200 | 必须检查响应体 `code` 字段 |
| 飞书签名 | secret 作为 HMAC key，待签名消息为空串 | 与常见签名实现相反，照抄通用模板会失败 |
| Treasury API | 无需 API key | 不要为其预留 secret 配置 |

---

## 12. 验收标准

1. 手动触发 workflow，三个 collector 均成功产出数据并 commit
2. 打开站点，卡片正确渲染，数值与单位与上游一致
3. 手机添加到主屏，断网状态下打开仍能显示上次数据
4. 人为构造一次阈值突破，飞书收到红色卡片
5. 人为将某 collector 指向错误 URL，飞书收到灰色心跳告警，且**站点其余部分正常更新**
6. 人为修改某历史点的上游值，验证序列文件中产生新记录而非覆写，且快照取到新值
7. 连续两次运行且阈值持续突破，第二次不重复推送
8. 新增一个任意 collector（可用固定值 mock），验证前端自动出现新卡片且未修改任何前端代码
9. 人为在 pydantic 模型中改动一个字段名，验证 CI 的 schema 校验步骤失败并阻断部署

第 5、6、8、9 条是本项目的核心价值所在，不得省略。

---

## 13. 未来扩展

若后续需要小时级频率：

| 组件 | 改动 |
|---|---|
| 数据契约 | 无 |
| collector | 无 |
| 告警规则 | 无 |
| 前端 | 无 |
| cron 表达式 | 改一行 |
| 存储 | 序列数量大时由 git 迁至对象存储（R2 / KV） |

前提是 §8.3 的运行时 fetch 约束已被遵守。若未遵守，前端需要重写。

秒级频率或需要在数据到达瞬间响应的场景，本架构不适用，需引入常驻进程与时序数据库。

---

## 14. CLAUDE.md 要求

在仓库根目录维护 `CLAUDE.md`，至少记录：

- 每个数据源的更新时点、字段含义、已知失真模式、修订行为
- 数据契约的不可变约定
- 各项配额限制与当前用量

§11 表格的内容应作为初始内容写入。此文件的价值在于沉淀环境特有知识，避免每次修改采集逻辑时重新解释上下文。
