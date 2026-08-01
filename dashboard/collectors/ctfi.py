"""上海航运交易所 · 中国进口原油运价指数 CTFI（`dashboard/collectors/ctfi.py`）。

数据源调研（遵循 §4.2「先实测再写解析」）：
  * AkShare **不封装** CTFI —— `macro_china_freight_index` 只给波罗的海指数
    (BDI/BCTI/BDTI 等)，不是上海航交所的 CTFI，故自行解析。
  * SSE 历史接口 `/index/mutipleIndex`、`/index/ctfilist` **需登录**
    （返回 `{"success":false,"message":"对不起你没有登陆!"}`）。
  * 可用公开源 = `singleIndex?indexType=ctfi` 落地页：服务端直接渲染「本期」四个
    分量 + 发布日期（`<div class="title2">` 内的 YYYY-MM-DD），**无需登录**。
    历史需登录，故本采集器每个发布日取一个点，由 store 逐日累积历史；
    值未变则去重不重复写。

页面结构：`title2` 里含发布日期；`table.lb1` 表头
`航线|载货量|船型|单位|权重|本期|与上期比涨跌`，「本期」恒为倒数第二个非空单元格。
综合指数一行；CT1/CT2 各有主行(点) + 子行(WS / 美元/吨 / 美元/天×2)。

⚠️ **页面结构变更风险高**（§4.2 备注）：任一预期值缺失即 `raise`，绝不返回部分结果，
让心跳/采集失败告警立刻暴露解析失效——这正是心跳在此源尤其关键的原因（§6.2）。

WS 点数（运价率）与 TCE 美元/天（船东实际收益）是两个独立序列，油价剧烈波动时会
背离，故不合并（§4.2 备注）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta
from .base import make_client

SINGLE_INDEX_URL = "https://www.sse.net.cn/index/singleIndex?indexType=ctfi"

# CTFI 名义日频（工作日），但周末/法定节假日不发布。expected_interval 是心跳 SLA
# （容忍 = 2×），须按交易日历放宽，否则春节长假必然误报（§4.2 备注）。取 7d → 14 天
# 容忍，可覆盖春节/国庆连假；真实中断超两周才告警，是对该源的合理取舍。
_INTERVAL = timedelta(days=7)


def _meta(series_id, name, unit, precision):
    return SeriesMeta(
        series_id=series_id,
        display_name=name,
        unit=unit,
        source="ctfi",
        expected_interval=_INTERVAL,
        precision=precision,
        direction_good="neutral",
        description="上海航运交易所 中国进口原油运价指数 CTFI（基期 2012-11-28 = 1000）。",
    )


class CTFICollector:
    id = "ctfi"
    series = [
        _meta("ctfi.composite", "中国进口原油综合运价指数 (CTFI)", "点", 2),
        _meta("ctfi.ct1", "CTFI · CT1 中东湾→宁波 (WS)", "WS点数", 2),
        _meta("ctfi.ct1.tce", "CTFI · CT1 等价期租水平 (TCE)", "美元/天", 0),
        _meta("ctfi.ct2", "CTFI · CT2 西非→宁波 (WS)", "WS点数", 2),
    ]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        async with make_client() as client:
            resp = await client.get(SINGLE_INDEX_URL)
            resp.raise_for_status()
            html = resp.text
        observed_at, values = parse_ctfi(html)  # raises on structure change
        return [
            Observation(
                series_id=sid,
                observed_at=observed_at,
                as_of=ctx.now,
                value=values[sid],
                status=DataStatus.CONFIRMED,
                source=self.id,
            )
            for sid in ("ctfi.composite", "ctfi.ct1", "ctfi.ct1.tce", "ctfi.ct2")
        ]


_EXPECTED = ("ctfi.composite", "ctfi.ct1", "ctfi.ct1.tce", "ctfi.ct2")


def parse_ctfi(html: str) -> tuple[datetime, dict[str, float]]:
    """Parse the SSE CTFI landing page. Raises if the date or any of the four
    series cannot be found (structure change → surface via heartbeat, §6.2)."""
    # publish date, inside the title2 block
    tm = re.search(r'class="title2".*?</div>', html, re.S)
    dm = re.search(r"(\d{4}-\d{2}-\d{2})", tm.group(0)) if tm else None
    if not dm:
        raise RuntimeError("CTFI parse: publish date not found (page structure changed?)")
    observed_at = datetime.fromisoformat(dm.group(1)).replace(tzinfo=timezone.utc)

    table = re.search(r'<table[^>]*class="lb1".*?</table>', html, re.S)
    if not table:
        raise RuntimeError("CTFI parse: data table (class=lb1) not found")

    out: dict[str, float] = {}
    ctx_route: str | None = None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(0), re.S):
        cells = _cells(row)
        if not cells:
            continue
        head = cells[0]
        if "综合指数" in head:
            out["ctfi.composite"] = _num(cells[-2])
        elif "CT1" in head or "拉斯坦努拉" in head:
            ctx_route = "ct1"
        elif "CT2" in head or "西非" in head:
            ctx_route = "ct2"
        elif head == "WS" and ctx_route in ("ct1", "ct2"):
            out[f"ctfi.{ctx_route}"] = _num(cells[-2])
        elif "美元/天" in head and "标准航速" in head and ctx_route == "ct1":
            out["ctfi.ct1.tce"] = _num(cells[-2])

    missing = [s for s in _EXPECTED if s not in out]
    if missing:
        raise RuntimeError(f"CTFI parse: missing series {missing} — page structure changed?")
    return observed_at, out


def _cells(row_html: str) -> list[str]:
    cs = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    cs = [re.sub(r"<[^>]+>", " ", c).replace("&nbsp;", " ").strip() for c in cs]
    return [re.sub(r"\s+", " ", c) for c in cs if c.strip()]


def _num(raw: str) -> float:
    return float(raw.replace(",", "").strip())


_: Collector = CTFICollector()
