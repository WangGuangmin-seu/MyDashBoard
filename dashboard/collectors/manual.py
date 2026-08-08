"""人工录入的序列 — 用于没有干净公开接口、只能手动维护的数据。

有些指标（如云南电力市场化交易平均成交价）只在月度公告的文字里披露，无结构化源。
此采集器从 `data/manual/<name>.csv` 读取人工录入的值（无网络），生成序列，让这类数据
也能进看板。CSV 格式：忽略 `#` 注释与表头，数据行为 `YYYY-MM,数值` 或 `YYYY-MM-DD,数值`。

维护：编辑对应 CSV，加一行即可；下次采集自动纳入。`expected_interval` 放宽到月频 +
余量，若长期不更新会触发心跳（提醒补录），这是刻意的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..contract import CollectorContext, Collector, DataStatus, Observation, SeriesMeta

MANUAL_DIR = Path(__file__).resolve().parents[2] / "data" / "manual"

# (csv filename without dir, SeriesMeta). Add an entry to expose a new manual series.
_MANUAL: list[tuple[str, SeriesMeta]] = [
    (
        "yunnan_market_price.csv",
        SeriesMeta(
            series_id="manual.yunnan_market_price",
            display_name="云南电力市场成交均价",
            unit="元/千瓦时",
            source="manual",
            # Monthly figure; 45d SLA (90d tolerance) alerts if not topped up for ~3 months.
            expected_interval=timedelta(days=45),
            precision=4,
            direction_good="neutral",
            description="云南省内市场化交易平均成交价（昆明电力交易中心月度公告，人工录入）。",
        ),
    ),
]


class ManualCollector:
    id = "manual"
    series = [meta for _, meta in _MANUAL]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]:
        obs: list[Observation] = []
        for filename, meta in _MANUAL:
            obs += _read_csv(MANUAL_DIR / filename, meta.series_id, ctx.now)
        if not obs:
            raise RuntimeError("manual collector produced no observations — check data/manual CSVs")
        return obs


def _read_csv(path: Path, series_id: str, now: datetime) -> list[Observation]:
    if not path.exists():
        raise RuntimeError(f"manual CSV missing: {path}")
    out: list[Observation] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[1] or parts[1].lower().startswith("price"):
            continue  # header or malformed row
        try:
            value = float(parts[1])
        except ValueError:
            continue  # header like "month,price..."
        out.append(
            Observation(
                series_id=series_id,
                observed_at=_day(parts[0]),
                as_of=now,
                value=value,
                status=DataStatus.CONFIRMED,
                source="manual",
            )
        )
    return out


def _day(raw: str) -> datetime:
    """'YYYY-MM' → first of month; 'YYYY-MM-DD' as-is. UTC midnight."""
    s = raw if len(raw) > 7 else raw + "-01"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


_: Collector = ManualCollector()
