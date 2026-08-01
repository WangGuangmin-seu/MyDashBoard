"""Snapshot heartbeat + the frozen-contract guard (spec §2.3, §6.2, §12.9)."""

import json
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from dashboard.contract import DataStatus, Observation, SeriesMeta
from dashboard.pipeline.snapshot import (
    CollectorHealth,
    build_snapshot,
    export_schema,
    heartbeat_stale,
    validate_snapshot_file,
    write_snapshot,
)
from dashboard.pipeline.store import Store

NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def _meta(series_id, interval_days):
    return SeriesMeta(
        series_id=series_id,
        display_name=series_id,
        unit="x",
        source="test",
        expected_interval=timedelta(days=interval_days),
        precision=0,
    )


def test_heartbeat_stale_threshold():
    daily = timedelta(days=1)
    assert heartbeat_stale(NOW, NOW - timedelta(days=1), daily) is False
    assert heartbeat_stale(NOW, NOW - timedelta(days=3), daily) is True
    assert heartbeat_stale(NOW, None, daily) is True  # no data == stale


def test_build_snapshot_flags_stale_series(tmp_path):
    store = Store(tmp_path)
    store.append_observations([
        Observation(series_id="fresh", observed_at=NOW - timedelta(days=1), as_of=NOW,
                    value=5.0, status=DataStatus.CONFIRMED, source="test"),
        Observation(series_id="old", observed_at=NOW - timedelta(days=30), as_of=NOW,
                    value=5.0, status=DataStatus.CONFIRMED, source="test"),
    ])
    metas = [_meta("fresh", 7), _meta("old", 1)]
    snap = build_snapshot(store, metas, NOW, [CollectorHealth(id="test", ok=True)])
    by_id = {s.meta.series_id: s for s in snap.series}
    assert by_id["fresh"].health.stale is False
    assert by_id["old"].health.stale is True


def test_snapshot_conforms_and_rename_breaks_schema(tmp_path):
    store = Store(tmp_path)
    store.append_observations([
        Observation(series_id="fresh", observed_at=NOW - timedelta(days=1), as_of=NOW,
                    value=5.0, status=DataStatus.CONFIRMED, source="test"),
    ])
    snap = build_snapshot(store, [_meta("fresh", 7)], NOW, [CollectorHealth(id="test", ok=True)])
    snap_path = write_snapshot(snap, tmp_path)
    schema_path = export_schema(tmp_path)

    # valid snapshot passes
    validate_snapshot_file(snap_path, schema_path)

    # simulate a backend field rename without regenerating the frozen schema:
    # the generated snapshot no longer matches -> validation must fail (spec §12.9)
    raw = json.loads(snap_path.read_text(encoding="utf-8"))
    raw["series"][0]["meta"]["displayName"] = raw["series"][0]["meta"].pop("display_name")
    snap_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(jsonschema.ValidationError):
        validate_snapshot_file(snap_path, schema_path)
