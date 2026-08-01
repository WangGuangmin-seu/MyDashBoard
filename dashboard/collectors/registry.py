"""Collector registry — the one place that knows which collectors exist.

Adding a data source = add one import + one entry here (spec §2.2). Nothing
downstream (store, snapshot, frontend) needs to change.
"""

from __future__ import annotations

from ..contract import Collector
from .ctfi import CTFICollector
from .eia import EIACollector
from .portwatch import PortWatchCollector
from .treasury import TreasuryCollector

ALL_COLLECTORS: list[Collector] = [
    PortWatchCollector(),
    TreasuryCollector(),
    EIACollector(),
    CTFICollector(),
]


def all_series_meta():
    """Flatten every collector's declared SeriesMeta."""
    metas = []
    for c in ALL_COLLECTORS:
        metas.extend(c.series)
    return metas
