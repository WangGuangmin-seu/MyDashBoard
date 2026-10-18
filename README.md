# 个人数据看板

多源数据看板：预计算快照 + 配置化告警 + 纯静态 PWA 前端。打开即见最新数据，
新增数据源只需加一个 collector，前端零改动。

规格：[`dashboard-spec.md`](dashboard-spec.md) ｜ 环境知识与运维：[`CLAUDE.md`](CLAUDE.md)

## 快速开始

```bash
uv sync --extra dev                       # 安装依赖（含开发/测试）
cp .env.example .env                       # 填入 EIA/飞书凭证（可留空先跑）
uv run python -m dashboard --no-notify     # 抓取 → 存储 → 快照 → 校验 → 告警
uv run python -m dashboard digest --dry-run # 预览每日摘要卡片（不发送）
uv run pytest -q                           # 测试
```

生成的快照在 `web/data/snapshot.json`。本地预览前端：

```bash
python -m http.server 8137 --directory web
```

## 每日摘要（飞书）

`digest.yml` 每天 09:30（北京时间）读取 `web/data/snapshot.json`，发一张汇总卡片：全部序列
按分类分组，每条含最新值、涨跌、状态、最后日期和文字迷你走势；有采集器失败或序列 stale 时
卡片变橙并标注。看板地址默认 `https://mydashboard.wangguangmin1995.workers.dev/`
（`digest.yml` 里的兜底值），要更换就设仓库变量 `DASHBOARD_SITE_URL`，代码不用动。

> 自定义机器人 webhook **不能上传图片**（`image_key` 需要飞书自建应用的 tenant token），
> 所以走势用 `▁▂▃▄▅▆▇█` 文字渲染，不发 PNG。要发真图片需接自建应用，见 [`CLAUDE.md`](CLAUDE.md)。

## 结构

```
dashboard/          后端（Python 3.12+）
  contract.py         数据契约——唯一定义源，前后端共用
  collectors/         portwatch / treasury / eia / ctfi / fx / metals + registry
  pipeline/           store（追加写+修订）/ snapshot（生成+schema）/ alerts（规则+心跳）
  notify/feishu.py    飞书自定义机器人（签名 + code 校验）
  notify/digest.py    每日摘要卡片（快照 → 一张卡，文字走势）
  __main__.py         入口
rules/alerts.yaml   告警规则（纯配置）
data/               series/ + meta.json + schema.json + alert_state.json（git 即历史）
web/                纯静态站点（无构建步骤），发布目录
  index.html app.js sw.js manifest.json  data/snapshot.json
tests/              pytest
.github/workflows/  collect.yml（每日采集+推送）/ digest.yml（09:30 摘要）/ ci.yml（测试+schema 漂移）
```

## 核心不变量

- **契约冻结**：改 `contract.py` 字段必须 `python -m dashboard export-schema` 并提交
  `data/schema.json`，否则 CI 阻断（防止后端改名 → 前端静默空白）。
- **双时间轴**：`observed_at` 与 `as_of` 分开存储，修订追加不覆写。
- **心跳**：`expected_interval` 反映真实交付节奏（PortWatch 周更 → 7d，非日更），
  超 2× 未更新即告警——「数据没来」比「数据越界」更需通知。
- **前端数据驱动**：无任何 `series_id` 硬编码；加 collector 自动出卡片。

详见 [`CLAUDE.md`](CLAUDE.md)。
