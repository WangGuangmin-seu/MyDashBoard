"""Alert engine: condition eval, thresholds, cooldown, status guard, heartbeat,
collector-failure (spec §6, §12.5, §12.7)."""

from datetime import datetime, timedelta, timezone

from dashboard.contract import DataStatus, SeriesMeta
from dashboard.pipeline.alerts import (
    AlertRule,
    AlertState,
    _eval_condition,
    evaluate,
)
from dashboard.pipeline.snapshot import (
    CollectorHealth,
    Point,
    SeriesHealth,
    SeriesSnapshot,
    Snapshot,
)

NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def _series(series_id, value, status=DataStatus.CONFIRMED, stale=False, interval_days=1):
    meta = SeriesMeta(series_id=series_id, display_name=series_id, unit="x",
                      source="test", expected_interval=timedelta(days=interval_days), precision=0)
    pt = None if value is None else Point(observed_at=NOW, as_of=NOW, value=value, status=status)
    return SeriesSnapshot(
        meta=meta, category="test", points=[pt] if pt else [], latest=pt, previous=None,
        health=SeriesHealth(ok=not stale, stale=stale, last_observed_at=(NOW if pt else None)),
    )


def _snap(series, collectors=None):
    return Snapshot(generated_at=NOW, window=180, categories=[], series=series,
                    collectors=collectors or [CollectorHealth(id="test", ok=True)])


def test_condition_eval_safe_and_correct():
    assert _eval_condition("value < 20", 10) is True
    assert _eval_condition("value < 20", 25) is False
    assert _eval_condition("value < 20 and value > 5", 10) is True
    # only 'value' is allowed
    import pytest
    with pytest.raises(ValueError):
        _eval_condition("__import__('os')", 1)


def test_threshold_fires_once_then_cooldown(tmp_path):
    rule = AlertRule(id="r", series="s", condition="value < 20", cooldown="7d",
                     message="{series}={value}")
    state = AlertState(tmp_path / "state.json")
    snap = _snap([_series("s", 10.0)])

    first = evaluate(snap, [rule], state, NOW)
    assert len(first) == 1 and first[0].template == "red"

    # second run within cooldown -> suppressed (spec §12.7)
    second = evaluate(snap, [rule], state, NOW + timedelta(days=1))
    assert second == []

    # after cooldown -> fires again
    third = evaluate(snap, [rule], state, NOW + timedelta(days=8))
    assert len(third) == 1


def test_skip_if_status(tmp_path):
    rule = AlertRule(id="r", series="s", condition="value < 20", cooldown="7d",
                     skip_if_status=[DataStatus.UNDER_REVIEW])
    state = AlertState(tmp_path / "state.json")
    snap = _snap([_series("s", 10.0, status=DataStatus.UNDER_REVIEW)])
    assert evaluate(snap, [rule], state, NOW) == []


def test_provisional_status_annotated_when_not_skipped(tmp_path):
    rule = AlertRule(id="r", series="s", condition="value < 20", cooldown="7d",
                     message="v={value}")
    state = AlertState(tmp_path / "state.json")
    snap = _snap([_series("s", 10.0, status=DataStatus.PROVISIONAL)])
    alerts = evaluate(snap, [rule], state, NOW)
    assert "provisional" in alerts[0].message  # §6.4


def test_heartbeat_alert_on_stale_series(tmp_path):
    state = AlertState(tmp_path / "state.json")
    snap = _snap([_series("s", 5.0, stale=True)])
    alerts = evaluate(snap, [], state, NOW)
    assert len(alerts) == 1
    assert alerts[0].kind == "heartbeat" and alerts[0].template == "grey"


def test_collector_failure_alerts(tmp_path):
    state = AlertState(tmp_path / "state.json")
    snap = _snap([_series("s", 5.0)], collectors=[CollectorHealth(id="eia", ok=False, error="boom")])
    alerts = evaluate(snap, [], state, NOW)
    assert any(a.kind == "heartbeat" and "eia" in a.series_id for a in alerts)  # §12.5
