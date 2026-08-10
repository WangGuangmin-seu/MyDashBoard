"""铝相关 — 铝价、氧化铝价、铝锭库存、电解铝模拟冶炼毛利。

数据源调研（遵循 §4.2 先实测；用 AkShare 定位到底层接口后**直连解析**，不引入 akshare
重依赖，与项目一贯做法一致）：

* 铝价 / 氧化铝价：新浪财经期货日 K 线（JSONP，无 key）
    …/InnerFuturesNewService.getDailyKLine  symbol=AL0（沪铝主力）/ AO0（氧化铝主力），取收盘 c。
* 铝锭库存：东方财富 datacenter（RPT_FUTU_STOCKDATA，SECURITY_CODE=AL，ON_WARRANT_NUM，交易所库存）。
* 电解铝模拟冶炼毛利（sim_margin）：把"拿不到的电价/成本"变成从财报反推的残差 C：
    C = 铝价(报告期均) × (1 − 毛利率) − 1.93 × 氧化铝价(报告期均)
    模拟单吨毛利(日) = 铝价(日) − 1.93 × 氧化铝价(日) − C
  其中 1.93 = 氧化铝单耗(吨/吨电解铝)；毛利率取云铝(000807)"有色金属冶炼业"分部毛利率
  （东方财富 F10 BusinessAnalysis，结构化，半年报/年报披露，无 key）。
  用 毛利率×铝价(期均) 近似"实际单吨毛利"，从而**无需产量数据**（产量不结构化）。
  ⚠️ 口径说明：这是"电解铝冶炼毛利"而非电价；C 含电力+阳极+辅料+折旧+制造费用，
  C 漂移反映的是**综合转换成本**（阳极/石油焦波动也在内），非单纯电价。C 只定水平，
  日频变动完全由两个日价驱动，故信号干净。C 随财报半年更新一次（慢变量）。

⚠️ 均为**非官方行情**接口，结构可能变；解析不到关键值即 raise，交心跳/采集失败告警暴露。
价格/毛利只取近端窗口，历史由 store 逐日累积。`expected_interval=7d` 按交易日历放宽避免长假误报。
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
EM_BIZ = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
_WINDOW = 500          # trailing daily points to seed charts; store accumulates
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_INTERVAL = timedelta(days=7)  # SHFE trading days + holiday tolerance (see note)
_ALUMINA_RATIO = 1.93  # 氧化铝单耗（吨氧化铝/吨电解铝）
_YUNLU = "SZ000807"    # 云铝股份


class MetalsCollector:
    id = "metals"
    series = [
        SeriesMeta(
            series_id="metals.aluminum.price",
            display_name="铝价（沪铝主力）",
            unit="元/吨", source="metals", expected_interval=_INTERVAL, precision=0,
            direction_good="neutral",
            description="上期所沪铝主力合约收盘价（新浪财经，日频）。",
        ),
        SeriesMeta(
            series_id="metals.alumina.price",
            display_name="氧化铝价（主力）",
            unit="元/吨", source="metals", expected_interval=_INTERVAL, precision=0,
            direction_good="neutral",
            description="上期所氧化铝主力合约收盘价（新浪财经，日频，2023 年上市）。",
        ),
        SeriesMeta(
            series_id="metals.aluminum.inventory",
            display_name="铝锭库存（交易所）",
            unit="吨", source="metals", expected_interval=_INTERVAL, precision=0,
            direction_good="neutral",
            description="沪铝交易所库存/仓单（东方财富，非社会库存）。",
        ),
        SeriesMeta(
            series_id="metals.aluminum.sim_margin",
            display_name="电解铝模拟冶炼毛利",
            unit="元/吨", source="metals", expected_interval=_INTERVAL, precision=0,
            direction_good="up",
            description=(
                "模拟单吨毛利 = 铝价 − 1.93×氧化铝价 − C；C 由云铝冶炼分部毛利率反推的"
                "综合转换成本（含电力，半年更新）。反映电解铝冶炼盈利，非电价。"
            ),
        ),
    ]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        async with make_client() as client:
            al = await self._kline(client, "AL0")   # {date_str: close}
            ao = await self._kline(client, "AO0")
            obs: list[Observation] = []
            obs += self._price_obs(al, "metals.aluminum.price", ctx.now)
            obs += self._price_obs(ao, "metals.alumina.price", ctx.now)
            obs += await self._fetch_inventory(client, ctx.now)
            obs += await self._sim_margin(client, al, ao, ctx.now)
        if not obs:
            raise RuntimeError("metals returned zero observations — refusing to report success")
        return obs

    async def _kline(self, client, symbol: str) -> dict[str, float]:
        resp = await client.get(
            SINA_KLINE, params={"symbol": symbol, "type": "2021_04_12"},
            headers={"User-Agent": _UA},
        )
        resp.raise_for_status()
        rows = _parse_sina_jsonp(resp.text)
        out = {r["d"]: float(r["c"]) for r in rows if r.get("c") not in (None, "", "0.000")}
        if not out:
            raise RuntimeError(f"metals kline {symbol} returned no rows — sina structure changed?")
        return out

    def _price_obs(self, kline: dict[str, float], series_id: str, now: datetime) -> list[Observation]:
        dates = sorted(kline)[-_WINDOW:]
        return [
            Observation(series_id=series_id, observed_at=_day(d), as_of=now,
                        value=kline[d], status=DataStatus.CONFIRMED, source=self.id)
            for d in dates
        ]

    async def _fetch_inventory(self, client, now: datetime) -> list[Observation]:
        params = {
            "reportName": "RPT_FUTU_STOCKDATA",
            "columns": "SECURITY_CODE,TRADE_DATE,ON_WARRANT_NUM,ADDCHANGE",
            "filter": '(SECURITY_CODE="AL")(TRADE_DATE>=\'2020-10-28\')',
            "pageNumber": "1", "pageSize": str(_WINDOW),
            "sortTypes": "-1", "sortColumns": "TRADE_DATE", "source": "WEB", "client": "WEB",
        }
        resp = await client.get(EM_DATA, params=params, headers={"User-Agent": _UA})
        resp.raise_for_status()
        data = resp.json()
        try:
            rows = data["result"]["data"]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"unexpected EM inventory envelope: {str(data)[:150]}") from exc
        out = [
            Observation(series_id="metals.aluminum.inventory", observed_at=_day(r["TRADE_DATE"]),
                        as_of=now, value=None if r.get("ON_WARRANT_NUM") is None else float(r["ON_WARRANT_NUM"]),
                        status=DataStatus.CONFIRMED, source=self.id)
            for r in rows
        ]
        if not out:
            raise RuntimeError("metals inventory returned no rows — EM structure changed?")
        return out

    async def _sim_margin(self, client, al: dict[str, float], ao: dict[str, float],
                          now: datetime) -> list[Observation]:
        gm, report_end = await self._smelting_gross_margin(client)
        start = f"{report_end[:4]}-01-01"  # report period: Jan 1 .. report_end (H1 or FY)
        al_avg = _avg(al, start, report_end)
        ao_avg = _avg(ao, start, report_end)
        if al_avg is None or ao_avg is None:
            raise RuntimeError(f"metals sim_margin: no price data in report period {start}..{report_end}")
        c = al_avg * (1 - gm) - _ALUMINA_RATIO * ao_avg  # 综合转换成本残差 (元/吨)
        common = sorted(set(al) & set(ao))[-_WINDOW:]
        out = [
            Observation(series_id="metals.aluminum.sim_margin", observed_at=_day(d), as_of=now,
                        value=al[d] - _ALUMINA_RATIO * ao[d] - c,
                        status=DataStatus.CONFIRMED, source=self.id)
            for d in common
        ]
        if not out:
            raise RuntimeError("metals sim_margin produced no points — no overlapping AL/AO dates?")
        return out

    async def _smelting_gross_margin(self, client) -> tuple[float, str]:
        """(毛利率, 报告期末 'YYYY-MM-DD') for 云铝 有色金属冶炼业 latest report."""
        resp = await client.get(EM_BIZ, params={"code": _YUNLU}, headers={"User-Agent": _UA})
        resp.raise_for_status()
        rows = (resp.json() or {}).get("zygcfx") or []
        smelt = [r for r in rows if r.get("MAINOP_TYPE") == "1" and "冶炼" in (r.get("ITEM_NAME") or "")]
        if not smelt:
            raise RuntimeError("metals: 云铝冶炼分部毛利率未找到 — EM F10 结构变更?")
        latest = max(smelt, key=lambda r: r["REPORT_DATE"])
        gm = latest.get("GROSS_RPOFIT_RATIO")
        if gm is None:
            raise RuntimeError("metals: 冶炼分部缺 GROSS_RPOFIT_RATIO")
        return float(gm), latest["REPORT_DATE"][:10]


def _avg(kline: dict[str, float], start: str, end: str) -> float | None:
    vals = [v for d, v in kline.items() if start <= d <= end]
    return sum(vals) / len(vals) if vals else None


def _parse_sina_jsonp(text: str) -> list[dict]:
    try:
        return json.loads(text.split("=(", 1)[1].rsplit(");", 1)[0])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"unparseable sina JSONP: {text[:120]!r}") from exc


def _day(raw: str) -> datetime:
    """'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS' → UTC midnight."""
    return datetime.fromisoformat(raw[:10]).replace(tzinfo=timezone.utc)


_: Collector = MetalsCollector()
