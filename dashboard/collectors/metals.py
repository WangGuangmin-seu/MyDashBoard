"""铝相关 — 铝价、氧化铝价、铝锭库存。

数据源调研（遵循 §4.2 先实测；用 AkShare 定位到底层接口后**直连解析**，不引入 akshare
重依赖，与项目一贯做法一致）：

* 铝价 / 氧化铝价：新浪财经期货日 K 线（JSONP，无 key）
    https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20x=/InnerFuturesNewService.getDailyKLine
  symbol=AL0（沪铝主力）/ AO0（氧化铝主力）。返回 `var x=([{d,o,h,l,c,v,p,s},...]);`，
  取收盘 c。实测 AL0≈24040、AO0≈2698 元/吨。氧化铝 2023 年上市，历史较短。
* 铝锭库存：东方财富期货库存 datacenter API（无 key）
    https://datacenter-web.eastmoney.com/api/data/v1/get  reportName=RPT_FUTU_STOCKDATA
  SECURITY_CODE=AL（沪铝），取 ON_WARRANT_NUM（交易所库存/仓单）。实测 ≈307008 吨。
  注意：这是**交易所库存**，非 SMM/我的有色的电解铝**社会库存**（社会库存无干净公开源）。

⚠️ 均为**非官方行情**接口，结构可能变；解析不到任何点即 raise，交心跳/采集失败告警暴露。
只取近端窗口，历史由 store 逐日累积。SHFE 交易日频但逢节假日休市，`expected_interval=7d`
（心跳容忍 14 天）按交易日历放宽，避免春节/国庆长假误报。

（云南综合电价：无干净公开源，未纳入——详见对话与 CLAUDE.md。）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta
from .base import make_client

SINA_KLINE = (
    "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20k="
    "/InnerFuturesNewService.getDailyKLine"
)
EM_DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_WINDOW = 500  # trailing daily points to seed charts; store accumulates history
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_INTERVAL = timedelta(days=7)  # SHFE trading days + holiday tolerance (see note)


class MetalsCollector:
    id = "metals"
    series = [
        SeriesMeta(
            series_id="metals.aluminum.price",
            display_name="铝价（沪铝主力）",
            unit="元/吨",
            source="metals",
            expected_interval=_INTERVAL,
            precision=0,
            direction_good="neutral",
            description="上期所沪铝主力合约收盘价（新浪财经，日频）。",
        ),
        SeriesMeta(
            series_id="metals.alumina.price",
            display_name="氧化铝价（主力）",
            unit="元/吨",
            source="metals",
            expected_interval=_INTERVAL,
            precision=0,
            direction_good="neutral",
            description="上期所氧化铝主力合约收盘价（新浪财经，日频，2023 年上市）。",
        ),
        SeriesMeta(
            series_id="metals.aluminum.inventory",
            display_name="铝锭库存（交易所）",
            unit="吨",
            source="metals",
            expected_interval=_INTERVAL,
            precision=0,
            direction_good="neutral",
            description="沪铝交易所库存/仓单（东方财富，非社会库存）。",
        ),
    ]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        obs: list[Observation] = []
        async with make_client() as client:
            obs += await self._fetch_kline(client, "AL0", "metals.aluminum.price", ctx.now)
            obs += await self._fetch_kline(client, "AO0", "metals.alumina.price", ctx.now)
            obs += await self._fetch_inventory(client, ctx.now)
        if not obs:
            raise RuntimeError("metals returned zero observations — refusing to report success")
        return obs

    async def _fetch_kline(self, client, symbol: str, series_id: str, now: datetime) -> list[Observation]:
        resp = await client.get(
            SINA_KLINE, params={"symbol": symbol, "type": "2021_04_12"},
            headers={"User-Agent": _UA},
        )
        resp.raise_for_status()
        rows = _parse_sina_jsonp(resp.text)
        out: list[Observation] = []
        for row in rows[-_WINDOW:]:
            close = row.get("c")
            out.append(
                Observation(
                    series_id=series_id,
                    observed_at=_day(row["d"]),
                    as_of=now,
                    value=float(close) if close not in (None, "", "0.000") else None,
                    status=DataStatus.CONFIRMED,
                    source=self.id,
                )
            )
        if not out:
            raise RuntimeError(f"metals kline {symbol} returned no rows — sina structure changed?")
        return out

    async def _fetch_inventory(self, client, now: datetime) -> list[Observation]:
        params = {
            "reportName": "RPT_FUTU_STOCKDATA",
            "columns": "SECURITY_CODE,TRADE_DATE,ON_WARRANT_NUM,ADDCHANGE",
            "filter": '(SECURITY_CODE="AL")(TRADE_DATE>=\'2020-10-28\')',
            "pageNumber": "1",
            "pageSize": str(_WINDOW),
            "sortTypes": "-1",
            "sortColumns": "TRADE_DATE",
            "source": "WEB",
            "client": "WEB",
        }
        resp = await client.get(EM_DATA, params=params, headers={"User-Agent": _UA})
        resp.raise_for_status()
        data = resp.json()
        try:
            rows = data["result"]["data"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unexpected EM inventory envelope: {str(data)[:150]}") from exc
        out: list[Observation] = []
        for row in rows:
            num = row.get("ON_WARRANT_NUM")
            out.append(
                Observation(
                    series_id="metals.aluminum.inventory",
                    observed_at=_day(row["TRADE_DATE"]),
                    as_of=now,
                    value=None if num is None else float(num),
                    status=DataStatus.CONFIRMED,
                    source=self.id,
                )
            )
        if not out:
            raise RuntimeError("metals inventory returned no rows — EM structure changed?")
        return out


def _parse_sina_jsonp(text: str) -> list[dict]:
    try:
        return json.loads(text.split("=(", 1)[1].rsplit(");", 1)[0])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"unparseable sina JSONP: {text[:120]!r}") from exc


def _day(raw: str) -> datetime:
    """Accepts 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'. Anchor at UTC midnight."""
    return datetime.fromisoformat(raw[:10]).replace(tzinfo=timezone.utc)


_: Collector = MetalsCollector()
