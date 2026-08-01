"""U.S. Energy Information Administration — weekly petroleum stocks.

Requires a free EIA API key (spec §4.2), read from ctx.secrets["EIA_API_KEY"].

Live-verified route (spec §4.2 — the v2 `seriesid` shortcut does NOT recognise
these ids; the facet-filtered data endpoint does):

    https://api.eia.gov/v2/petroleum/stoc/wstk/data/
      ?frequency=weekly&data[0]=value&facets[series][]=WCESTUS1&facets[series][]=WCSSTUS1

Rows carry: period ("YYYY-MM-DD"), series (the EIA id), value (string,
thousand barrels / "MBBL"), units. Verified value example: WCESTUS1 = 404508.
Weekly, released Wednesdays.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta
from .base import get_json, make_client

DATA_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
_PAGE = 5000

# EIA series id -> our series_id + display metadata
_SPECS = {
    "WCESTUS1": dict(
        series_id="eia.crude.commercial_stocks",
        display_name="U.S. commercial crude oil stocks (excl. SPR)",
        direction_good="neutral",
        description="Weekly U.S. ending stocks of crude oil excluding the SPR (EIA).",
    ),
    "WCSSTUS1": dict(
        series_id="eia.crude.spr_stocks",
        display_name="U.S. Strategic Petroleum Reserve crude stocks",
        direction_good="up",
        description="Weekly U.S. ending stocks of crude oil in the SPR (EIA).",
    ),
}
_BY_EIA_ID = {eia_id: spec["series_id"] for eia_id, spec in _SPECS.items()}


class EIACollector:
    id = "eia"
    series = [
        SeriesMeta(
            series_id=spec["series_id"],
            display_name=spec["display_name"],
            unit="thousand barrels",
            source="eia",
            expected_interval=timedelta(days=7),
            precision=0,
            direction_good=spec["direction_good"],
            description=spec["description"],
        )
        for spec in _SPECS.values()
    ]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        key = ctx.secrets.get("EIA_API_KEY")
        if not key:
            raise RuntimeError("EIA_API_KEY missing from secrets — cannot fetch")
        obs: list[Observation] = []
        async with make_client() as client:
            offset = 0
            while True:
                params = [
                    ("api_key", key),
                    ("frequency", "weekly"),
                    ("data[0]", "value"),
                    ("sort[0][column]", "period"),
                    ("sort[0][direction]", "asc"),
                    ("offset", str(offset)),
                    ("length", str(_PAGE)),
                ]
                params += [("facets[series][]", sid) for sid in _SPECS]
                data = await get_json(client, DATA_URL, params=params)
                resp = data.get("response")
                if resp is None:
                    raise ValueError(f"unexpected EIA envelope: {list(data)[:5]}")
                rows = resp.get("data", [])
                for row in rows:
                    obs.append(self._to_observation(row, ctx.now))
                total = int(resp.get("total", 0))
                offset += len(rows)
                if not rows or offset >= total:
                    break
        if not obs:
            raise RuntimeError("EIA returned zero observations — refusing to report success")
        return obs

    def _to_observation(self, row: dict, now: datetime) -> Observation:
        eia_id = row["series"]
        series_id = _BY_EIA_ID.get(eia_id)
        if series_id is None:
            raise ValueError(f"unexpected EIA series id in response: {eia_id!r}")
        raw = row.get("value")
        return Observation(
            series_id=series_id,
            observed_at=_period(row["period"]),
            as_of=now,
            value=None if raw is None else float(raw),
            status=DataStatus.CONFIRMED,
            source=self.id,
        )


def _period(raw: str) -> datetime:
    """EIA weekly period is 'YYYY-MM-DD'. Anchor at UTC midnight."""
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


_: Collector = EIACollector()
