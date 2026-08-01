"""Storage semantics: append-only, revisions, current-value selection (spec §5.2)."""

from datetime import datetime, timezone

from dashboard.contract import DataStatus, Observation
from dashboard.pipeline.store import Store


def _obs(series, observed, as_of, value, status=DataStatus.CONFIRMED):
    return Observation(
        series_id=series,
        observed_at=datetime(*observed, tzinfo=timezone.utc),
        as_of=datetime(*as_of, tzinfo=timezone.utc),
        value=value,
        status=status,
        source="test",
    )


def test_append_is_idempotent(tmp_path):
    store = Store(tmp_path)
    obs = [_obs("s.a", (2026, 1, 1), (2026, 1, 2), 10.0)]
    assert store.append_observations(obs)["s.a"] == 1
    # same value again -> no new record
    assert store.append_observations(obs)["s.a"] == 0
    assert len(store.load_series("s.a")) == 1


def test_revision_appends_not_overwrites(tmp_path):
    store = Store(tmp_path)
    store.append_observations([_obs("s.a", (2026, 1, 1), (2026, 1, 2), 10.0)])
    # upstream revises the same observed_at with a new value on a later as_of
    store.append_observations([_obs("s.a", (2026, 1, 1), (2026, 1, 9), 15.0)])

    records = store.load_series("s.a")
    assert len(records) == 2, "revision must append, never overwrite"
    revised = [r for r in records if r.value == 15.0][0]
    assert revised.revision_of == datetime(2026, 1, 2, tzinfo=timezone.utc)

    # current value = greatest as_of
    current = store.current_series("s.a")
    assert len(current) == 1
    assert current[0].value == 15.0


def test_current_picks_latest_as_of(tmp_path):
    store = Store(tmp_path)
    store.append_observations([
        _obs("s.a", (2026, 1, 1), (2026, 1, 2), 1.0),
        _obs("s.a", (2026, 1, 2), (2026, 1, 3), 2.0),
    ])
    current = store.current_series("s.a")
    assert [c.value for c in current] == [1.0, 2.0]
