"""Snapshot generation + the cross-language contract guard (spec §2.3, §5.3).

The snapshot is the *only* file the frontend reads. It is self-contained: per
series it carries the metadata, the last ``window`` current points, the latest
and previous values, and heartbeat health; plus per-collector run health.

Contract guard: ``Snapshot`` is a pydantic model. ``export_schema`` writes its
JSON Schema to ``data/schema.json`` — a **committed, frozen** artifact.
``validate_snapshot_file`` checks a generated ``snapshot.json`` against that
committed schema with jsonschema. If someone renames a contract field in Python
without regenerating & committing the schema, the generated snapshot stops
matching the frozen schema and CI fails (spec §12.9) — instead of the frontend
silently blanking.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import jsonschema
from pydantic import BaseModel, ConfigDict

from ..contract import DataStatus, SeriesMeta
from .store import DEFAULT_DATA_DIR, Store

DEFAULT_WINDOW = 180

# Dashboard categories: single source of truth for BOTH card order and the
# collapsible grouping the frontend renders. Each entry is (id, title, series_ids
# in display order). A series not listed in any category falls into the trailing
# "其他" group, so a newly added collector's cards still appear automatically.
CATEGORIES: list[tuple[str, str, list[str]]] = [
    ("core", "核心指标", [
        "portwatch.hormuz.transits",
        "portwatch.hormuz.trade_volume",
        "eia.crude.commercial_stocks",
        "eia.crude.spr_stocks",
        "eia.brent.spot",
        "treasury.yield.10y",
        "fx.dxy",
    ]),
    ("crude_freight", "原油货运辅助数据", [
        "portwatch.bab_el_mandeb.transits",
        "portwatch.bab_el_mandeb.trade_volume",
        "portwatch.cape_of_good_hope.transits",
        "portwatch.cape_of_good_hope.trade_volume",
    ]),
    ("fiscal", "财政类", [
        "treasury.tga.closing_balance",
        "treasury.mts.receipts",
        "treasury.mts.outlays",
        "treasury.mts.deficit",
    ]),
    ("freight_index", "运价指数", [
        "ctfi.composite",
        "ctfi.ct1",
        "ctfi.ct1.tce",
        "ctfi.ct2",
    ]),
    ("aluminum", "铝相关", [
        "metals.aluminum.price",
        "metals.alumina.price",
        "metals.aluminum.inventory",
        "manual.yunnan_market_price",
    ]),
]
_OTHER_CATEGORY = ("other", "其他")

# derived lookups
_FLAT_ORDER: list[str] = [sid for _, _, sids in CATEGORIES for sid in sids]
_CATEGORY_OF: dict[str, str] = {sid: cid for cid, _, sids in CATEGORIES for sid in sids}


def _order_key(series_id: str, fallback_index: int) -> tuple[int, int]:
    """Listed series first (in CATEGORIES flat order), then unlisted in original order."""
    try:
        return (0, _FLAT_ORDER.index(series_id))
    except ValueError:
        return (1, fallback_index)


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: datetime
    as_of: datetime
    value: float | None
    status: DataStatus


class SeriesHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    stale: bool
    last_observed_at: datetime | None
    reason: str | None = None


class CollectorHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    ok: bool
    error: str | None = None


class CategoryMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str


class SeriesSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: SeriesMeta
    category: str                       # category id; matches a Snapshot.categories entry
    points: list[Point]
    latest: Point | None
    previous: Point | None
    health: SeriesHealth


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: datetime
    window: int
    categories: list[CategoryMeta]      # display order of collapsible groups
    series: list[SeriesSnapshot]
    collectors: list[CollectorHealth]


def heartbeat_stale(now: datetime, last_observed_at: datetime | None, expected_interval) -> bool:
    """Spec §6.2: stale if now - last_observed_at > 2 × expected_interval."""
    if last_observed_at is None:
        return True
    return (now - last_observed_at) > (2 * expected_interval)


def build_snapshot(
    store: Store,
    metas: list[SeriesMeta],
    now: datetime,
    collector_health: list[CollectorHealth],
    window: int = DEFAULT_WINDOW,
) -> Snapshot:
    ordered_metas = [
        m for _, m in sorted(
            enumerate(metas), key=lambda t: _order_key(t[1].series_id, t[0])
        )
    ]
    series_snaps: list[SeriesSnapshot] = []
    for meta in ordered_metas:
        current = store.current_series(meta.series_id)  # sorted by observed_at
        points = [
            Point(observed_at=o.observed_at, as_of=o.as_of, value=o.value, status=o.status)
            for o in current[-window:]
        ]
        latest = points[-1] if points else None
        previous = points[-2] if len(points) >= 2 else None
        last_observed = latest.observed_at if latest else None
        stale = heartbeat_stale(now, last_observed, meta.expected_interval)
        health = SeriesHealth(
            ok=not stale and latest is not None,
            stale=stale,
            last_observed_at=last_observed,
            reason=(
                "no data collected yet"
                if latest is None
                else ("data stale — collection may be broken" if stale else None)
            ),
        )
        series_snaps.append(
            SeriesSnapshot(
                meta=meta,
                category=_CATEGORY_OF.get(meta.series_id, _OTHER_CATEGORY[0]),
                points=points, latest=latest, previous=previous, health=health,
            )
        )
    return Snapshot(
        generated_at=now,
        window=window,
        categories=_build_categories(series_snaps),
        series=series_snaps,
        collectors=collector_health,
    )


def _build_categories(series_snaps: list[SeriesSnapshot]) -> list[CategoryMeta]:
    """Categories in display order, including only those with ≥1 present series;
    append the trailing '其他' group if any series is uncategorised."""
    present = {s.category for s in series_snaps}
    cats = [CategoryMeta(id=cid, title=title) for cid, title, _ in CATEGORIES if cid in present]
    if _OTHER_CATEGORY[0] in present:
        cats.append(CategoryMeta(id=_OTHER_CATEGORY[0], title=_OTHER_CATEGORY[1]))
    return cats


def write_snapshot(snapshot: Snapshot, data_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    path = Path(data_dir) / "snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


def export_schema(data_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    """Write the frozen contract schema. Run intentionally on contract changes."""
    path = Path(data_dir) / "schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(Snapshot.model_json_schema(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def validate_snapshot_file(
    snapshot_path: Path | str, schema_path: Path | str
) -> None:
    """Validate a generated snapshot against the committed schema. Raises on mismatch."""
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    instance = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    jsonschema.validate(instance=instance, schema=schema)
