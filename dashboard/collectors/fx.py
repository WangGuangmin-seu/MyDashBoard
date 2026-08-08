"""外汇 — 美元指数 (DXY)。

⚠️ 数据源现实（遵循 §4.2 先实测）：美元指数（ICE DXY）由 ICE 授权，**没有官方免费
API**。可用的公开源是行情站点。实测 Stooq 已加 JS 反爬墙（返回验证页非 CSV）；
**Yahoo Finance 图表 API 可用且无 key**：

    https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?range=1y&interval=1d

响应：chart.result[0].timestamp（epoch 秒）+ indicators.quote[0].close（收盘）。
实测 DX-Y.NYB ≈ 99.6，currency USD。属**非官方行情**，页面/接口结构可能变；解析不到
任何点即 raise，由采集失败/心跳告警暴露（§6.2）。Yahoo 对非浏览器 UA 可能限流，故带
浏览器 UA。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta
from .base import make_client

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class FXCollector:
    id = "fx"
    series = [
        SeriesMeta(
            series_id="fx.dxy",
            display_name="美元指数 (DXY)",
            unit="点",
            source="fx",
            # Business-daily close with ~1 day lag; 3d SLA (6d tolerance) covers weekends.
            expected_interval=timedelta(days=3),
            precision=2,
            direction_good="neutral",
            description="ICE 美元指数 DXY（Yahoo Finance DX-Y.NYB 日收盘，非官方行情）。",
        )
    ]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        async with make_client() as client:
            resp = await client.get(
                CHART_URL,
                params={"range": "1y", "interval": "1d"},
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()
        obs = _parse_chart(data, ctx.now)
        if not obs:
            raise RuntimeError("DXY returned zero points — Yahoo chart structure changed?")
        return obs


def _parse_chart(data: dict, now: datetime) -> list[Observation]:
    try:
        result = data["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"unexpected Yahoo chart envelope: {str(data)[:200]}") from exc

    out: list[Observation] = []
    for ts, close in zip(stamps, closes):
        if close is None:  # non-trading day within the range
            continue
        observed = datetime.fromtimestamp(ts, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        out.append(
            Observation(
                series_id="fx.dxy",
                observed_at=observed,
                as_of=now,
                value=float(close),
                status=DataStatus.CONFIRMED,
                source="fx",
            )
        )
    return out


_: Collector = FXCollector()
