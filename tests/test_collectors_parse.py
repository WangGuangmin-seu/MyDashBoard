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


def test_portwatch_hormuz_uses_total_scope():
    c = PortWatchCollector()
    attrs = {"portid": "chokepoint6", "date": "2026-07-23",
             "n_total": 10, "capacity": 61241, "n_tanker": 2, "capacity_tanker": 999}
    ids = {o.series_id: o.value for o in c._to_observations(attrs, datetime(2026, 7, 31, tzinfo=timezone.utc))}
    # Hormuz reports the all-vessel totals, not the tanker fields.
    assert ids == {"portwatch.hormuz.transits": 10.0, "portwatch.hormuz.trade_volume": 61241.0}


def test_portwatch_bab_el_mandeb_uses_tanker_scope():
    c = PortWatchCollector()
    attrs = {"portid": "chokepoint4", "date": "2026-07-26",
             "n_total": 20, "capacity": 814080, "n_tanker": 6, "capacity_tanker": 572869}
    obs = c._to_observations(attrs, datetime(2026, 7, 31, tzinfo=timezone.utc))
    ids = {o.series_id: o.value for o in obs}
    # Bab el-Mandeb reports the tanker scope (n_tanker/capacity_tanker), not totals.
    assert ids == {"portwatch.bab_el_mandeb.transits": 6.0,
                   "portwatch.bab_el_mandeb.trade_volume": 572869.0}
    assert all(o.status is DataStatus.CONFIRMED for o in obs)


def test_collectors_declare_valid_meta():
    for c in (PortWatchCollector(), TreasuryCollector()):
        declared = {m.series_id for m in c.series}
        assert declared, f"{c.id} declares no series"
