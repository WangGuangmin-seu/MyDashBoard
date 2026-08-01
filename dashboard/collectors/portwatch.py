"""IMF PortWatch — daily chokepoint transit calls & trade-volume estimates.

Live-verified endpoint (spec §4.2 requires real inspection, not memory):

    https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services
        /Daily_Chokepoints_Data/FeatureServer/0/query

Real fields on that layer: date (esriFieldTypeDateOnly, returned as "YYYY-MM-DD"
string), portid, portname, n_total (vessel count), capacity (estimated trade
volume). There is NO per-row status column, so every row is emitted as
CONFIRMED; the `under_review` handling described in the spec cannot be sourced
from this layer (documented in CLAUDE.md).

Update cadence: nominally daily, but the layer refreshes only ~Tuesday 09:00 ET
(spec §11), so the newest observed_at can lag ~7 days. That is expected and is
NOT a heartbeat failure on its own — expected_interval is set to accommodate it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta
from .base import get_json, make_client

FEATURE_LAYER = (
    "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
    "/Daily_Chokepoints_Data/FeatureServer/0/query"
)

# portid -> (slug, human name). Only the chokepoints the spec asks for.
CHOKEPOINTS: dict[str, tuple[str, str]] = {
    "chokepoint6": ("hormuz", "霍尔木兹海峡"),
    "chokepoint4": ("bab_el_mandeb", "曼德海峡"),
    "chokepoint7": ("cape_of_good_hope", "好望角"),
}

# ArcGIS default page size is 2000; page explicitly to be safe.
_PAGE = 2000


def _build_series() -> list[SeriesMeta]:
    from datetime import timedelta

    metas: list[SeriesMeta] = []
    for _pid, (slug, name) in CHOKEPOINTS.items():
        metas.append(
            SeriesMeta(
                series_id=f"portwatch.{slug}.transits",
                display_name=f"{name} · 日通行船数",
                unit="艘/日",
                source="portwatch",
                # Points are daily, but the layer only refreshes ~weekly and can
                # lag ~7 days (spec §11). expected_interval is the heartbeat SLA
                # (staleness tolerance = 2×), so it tracks delivery cadence, not
                # the nominal daily spacing — else every series false-alarms.
                expected_interval=timedelta(days=7),
                precision=0,
                direction_good="neutral",
                description="Daily vessel transit calls (IMF PortWatch). Layer refreshes weekly.",
            )
        )
        metas.append(
            SeriesMeta(
                series_id=f"portwatch.{slug}.trade_volume",
                display_name=f"{name} · 日通行贸易量",
                unit="估计吨/日",
                source="portwatch",
                expected_interval=timedelta(days=7),  # weekly delivery; see note above
                precision=0,
                direction_good="neutral",
                description="Estimated daily trade volume transiting the chokepoint (IMF PortWatch).",
            )
        )
    return metas


class PortWatchCollector:
    id = "portwatch"
    series = _build_series()

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        ids = ",".join(f"'{pid}'" for pid in CHOKEPOINTS)
        where = f"portid IN ({ids})"
        obs: list[Observation] = []
        async with make_client() as client:
            offset = 0
            while True:
                data = await get_json(
                    client,
                    FEATURE_LAYER,
                    params={
                        "where": where,
                        "outFields": "date,portid,n_total,capacity",
                        "orderByFields": "date ASC",
                        "resultOffset": offset,
                        "resultRecordCount": _PAGE,
                        "f": "json",
                    },
                )
                if "error" in data:  # ArcGIS reports errors in-body with HTTP 200
                    raise RuntimeError(f"PortWatch ArcGIS error: {data['error']}")
                feats = data.get("features", [])
                for feat in feats:
                    obs.extend(self._to_observations(feat["attributes"], ctx.now))
                if not data.get("exceededTransferLimit") or not feats:
                    break
                offset += len(feats)
        if not obs:
            raise RuntimeError("PortWatch returned zero observations — refusing to report success")
        return obs

    def _to_observations(self, attrs: dict, now: datetime) -> list[Observation]:
        pid = attrs.get("portid")
        slug = CHOKEPOINTS[pid][0]
        observed_at = _parse_date(attrs.get("date"))
        out: list[Observation] = []
        for field, kind in (("n_total", "transits"), ("capacity", "trade_volume")):
            raw = attrs.get(field)
            out.append(
                Observation(
                    series_id=f"portwatch.{slug}.{kind}",
                    observed_at=observed_at,
                    as_of=now,
                    value=None if raw is None else float(raw),
                    status=DataStatus.CONFIRMED,
                    source=self.id,
                )
            )
        return out


def _parse_date(raw: object) -> datetime:
    """PortWatch's dateOnly field comes back as 'YYYY-MM-DD'. Anchor at UTC midnight.
    (Guard against epoch-ms in case ESRI changes serialisation.)"""
    if isinstance(raw, str):
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    raise ValueError(f"unparseable PortWatch date: {raw!r}")


# Static Protocol conformance check.
_: Collector = PortWatchCollector()
