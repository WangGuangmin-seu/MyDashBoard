"""Pure parsing/normalisation locks for the collectors (no network)."""

from datetime import datetime, timezone

import pytest

from dashboard.collectors.portwatch import PortWatchCollector, _parse_date
from dashboard.collectors.treasury import TreasuryCollector, _num
from dashboard.contract import DataStatus


def test_treasury_num_handles_null_string_and_millions_scale():
    assert _num("null", 1e-3) is None
    assert _num(None, 1e-3) is None
    assert _num("970442", 1e-3) == 970.442      # millions -> billions
    assert _num("1022206833325.67", 1e-9) == pytest.approx(1022.206833, rel=1e-6)  # dollars -> billions


def test_portwatch_date_parses_iso_and_epoch():
    assert _parse_date("2026-07-23") == datetime(2026, 7, 23, tzinfo=timezone.utc)
    assert _parse_date(1690070400000) == datetime(2023, 7, 23, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _parse_date(None)


def test_portwatch_row_maps_to_two_series():
    c = PortWatchCollector()
    attrs = {"portid": "chokepoint6", "date": "2026-07-23", "n_total": 10, "capacity": 61241}
    obs = c._to_observations(attrs, datetime(2026, 7, 31, tzinfo=timezone.utc))
    ids = {o.series_id: o.value for o in obs}
    assert ids == {"portwatch.hormuz.transits": 10.0, "portwatch.hormuz.trade_volume": 61241.0}
    assert all(o.status is DataStatus.CONFIRMED for o in obs)


def test_collectors_declare_valid_meta():
    for c in (PortWatchCollector(), TreasuryCollector()):
        declared = {m.series_id for m in c.series}
        assert declared, f"{c.id} declares no series"
