# CLAUDE.md — 个人数据看板

系统实现规格见 [`dashboard-spec.md`](dashboard-spec.md)。本文件沉淀**环境特有知识**：
数据源的非直觉行为、契约不可变约定、配额用量。修改采集逻辑前先读这里。

## 架构一句话

定时任务抓取各源 → 归一化为统一 `Observation` → 追加写入 `data/series/` →
生成 `web/data/snapshot.json` → 校验契约 → 评估告警 → 命中推飞书 → commit & push。
前端只读快照，零硬编码 `series_id`。

```
python -m dashboard              # 全流程
python -m dashboard --no-notify  # 不发飞书
python -m dashboard export-schema # 契约变更后重新冻结 data/schema.json（需提交）
```

## 数据契约（不可变）

- `dashboard/contract.py` 是**唯一定义源**，横跨 Python 后端与 JS 前端。
- 双时间轴强制：`observed_at`（数据点时刻）与 `as_of`（获知时刻）必须分开存储。
  上游会回改历史；只存单轴会让图表静默变化并引入前视偏差。
- 所有 datetime 带时区、UTC 存储、ISO 8601 序列化（contract 层强制校验）。
- 改动任一契约字段后**必须** `export-schema` 并提交 `data/schema.json`，否则：
  - CI `ci.yml` 的 schema-drift 步骤在 PR 阶段失败；
  - `collect.yml` 运行时 snapshot 不匹配冻结 schema → 退出码 2 → **阻断部署**。

## 数据源

### PortWatch（`dashboard/collectors/portwatch.py`）

- **真实端点**（实测得到，非文档推断）：
  `https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query`
- 字段：`date`(dateOnly，返回 `"YYYY-MM-DD"` 字符串)、`portid`、`n_total`(船数)、`capacity`(估计贸易量)。
- 咽喉点 id：霍尔木兹 `chokepoint6`、曼德海峡 `chokepoint4`、好望角 `chokepoint7`。
- **更新时点**：标称日频，但每周二美东 09:00 才刷新一次，最新数据可滞后 ~7 天。
  因此 `expected_interval=7d`（心跳容忍 = 2× = 14 天），**不是** 1 天，否则每天误报采集中断。
- **数据质量**：官方公告该区域有 GPS 干扰 / AIS 欺骗，部分日期数据处于复核状态。
  但该 FeatureServer **无逐行 status 列**，故本采集器一律标 `confirmed`；
  规格中 `under_review` 传递在此源无法实现，若换用带状态的源需在此处补。
- ArcGIS 在 HTTP 200 下用响应体 `error` 字段报错；分页看 `exceededTransferLimit`。

### Treasury（`dashboard/collectors/treasury.py`）

- **无需 API key**（不要为其预留 secret）。
- TGA 现金余额（日频）：`v1/accounting/dts/operating_cash_balance`，取
  `account_type == "Treasury General Account (TGA) Closing Balance"` 行，值在
  **`open_today_bal`**（单位百万美元；`close_today_bal` 现恒为字符串 `"null"`）。
- MTS 收支（月频）：`v1/accounting/mts/mts_table_1`。该表按财年布局，每期报告
  重复列出各月 + YTD/FY 小计；**某月权威值 = `classification_desc` 等于该报告自身
  日历月名的那一行**。`current_month_dfct_sur_amt` 正=赤字、负=盈余。
- 数值以字符串返回，`"null"` 是字符串。所有金额归一化为**十亿美元**统一单位。
- 更新：DTS 工作日日更且有 ~1 天发布滞后 → `expected_interval=3d`（容忍周末/假期）。

### EIA（`dashboard/collectors/eia.py`）

- **需免费 API key**，存入 `EIA_API_KEY` secret / 本地 `.env`。
- **已用真实 key 实测**（2026-07-31）。
- **真实端点**（实测得到）：`https://api.eia.gov/v2/petroleum/stoc/wstk/data/`，
  参数 `frequency=weekly&data[0]=value&facets[series][]=WCESTUS1&facets[series][]=WCSSTUS1`。
  ⚠️ v2 `seriesid` 兼容路由**不认**这两个 id（返回 404），必须用上面这个带 facet
  过滤的 data 端点。
- 系列：`WCESTUS1`（商业原油库存，不含 SPR）、`WCSSTUS1`（SPR 原油库存），周频（周三发布）。
- 返回行字段：`period`(YYYY-MM-DD)、`series`(EIA id)、`value`(字符串，单位 MBBL=千桶)。
  按 `series` 字段映射回本地 series_id；分页用 `offset`/`length` 循环到 `response.total`。
  实测基线：商业原油 ~404508 千桶、SPR ~307650 千桶。

## 告警

- 规则在 `rules/alerts.yaml`，纯配置，新增告警不改代码。条件表达式只允许
  `value`、数字、比较、and/or/not（AST 白名单，非 eval）。
- **心跳**（`now - 最新 observed_at > 2×expected_interval`）与**采集器失败**自动触发，
  无需规则，走灰色卡片；阈值突破走红色卡片。
- 去重：`data/alert_state.json` 记录每键 `last_fired_at`，cooldown 内不重复推。
- `hormuz_collapse` 阈值：实测基线 ~9–12 船/日，故设 `value < 5`（规格例中的 `< 20`
  对此数据会天天触发）。上游口径若变，重新校准。

## 飞书（`dashboard/notify/feishu.py`）

- 签名反直觉：HMAC-SHA256 的 **key = `f"{timestamp}\n{secret}"`，待签消息为空串**
  （`hmac.new(...)` 不调用 `.update()`）。照抄通用模板会失败。timestamp 1 小时有效。
- **响应校验**：消息被安全策略拒绝时仍可能返回 HTTP 200，成功仅以响应体 `code == 0`
  为准。只看状态码会让告警静默失效。
- 自定义机器人为单向，无法接收交互回调（不支持「点击静音」）。
- 安全设置用**签名校验**，不要用 IP 白名单（GitHub Actions 出口 IP 不固定）。

## 配额与运行环境

| 项 | 事实 | 影响 |
|---|---|---|
| Cloudflare Pages | 免费约 500 次构建/月 | 数据更新**不得**触发构建；站点仅在代码变更时构建。当前日频 commit ≈ 30/月，安全。若转小时级需把快照迁出仓库（R2/KV）。 |
| GitHub Actions | 定时触发有数分钟~半小时延迟；仓库 60 天无提交自动停用定时任务 | 对日频无影响；每次运行都 commit，不会被停用。 |
| 本地 TLS | 本机网络有 SSL 拦截（自签根证书） | 本地跑设 `DASHBOARD_INSECURE_TLS=1` 关闭校验；CI 从不设置，始终校验。 |
| 部署目录 | 站点发布目录 = `web/`，前端 fetch `./data/snapshot.json` | 管道把快照写到 `web/data/snapshot.json`；`data/series|meta|schema|alert_state` 留在根 `data/`。 |

## Secrets

`EIA_API_KEY`、`FEISHU_WEBHOOK_URL`、`FEISHU_SECRET` 存 GitHub Actions secrets，
本地放 `.env`（已 gitignore）。webhook URL 本身即完整凭证，禁止提交。
