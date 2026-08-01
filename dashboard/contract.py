"""Data contract — the single source of truth for the whole system.

This module is frozen (spec §3). Both the Python backend and the JavaScript
frontend depend on these shapes. Because the contract crosses a language
boundary it cannot be guaranteed by a type system, so `pipeline.snapshot`
exports `model_json_schema()` to `data/schema.json` and CI validates every
generated `snapshot.json` against it. Renaming a field here without updating
the frontend must therefore fail CI, not silently blank the UI.

Do not add domain-specific fields (a chokepoint name, a treasury account, …).
The contract stays domain-agnostic; specificity lives in `series_id` and in
per-collector normalisation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator


class DataStatus(StrEnum):
    CONFIRMED = "confirmed"
    PROVISIONAL = "provisional"
    UNDER_REVIEW = "under_review"
    ESTIMATED = "estimated"


def _require_tz(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be timezone-aware (spec §3.1)")
    return value


class Observation(BaseModel):
    """A single data point.

    The two time axes are mandatory and distinct (spec §3.1):
      * ``observed_at`` — the instant the value describes.
      * ``as_of``       — the instant we learned the value.
    Upstreams revise history; storing only one axis makes charts change
    silently and injects look-ahead bias into any backtest.
    """

    model_config = ConfigDict(extra="forbid")

    series_id: str                      # globally unique, e.g. "portwatch.hormuz.transits"
    observed_at: datetime               # the point's own timestamp
    as_of: datetime                     # when we obtained this value
    value: float | None                 # None == upstream explicitly reported missing
    status: DataStatus
    source: str                         # collector id
    revision_of: datetime | None = None  # if a revision, the as_of of the record it supersedes

    @field_validator("observed_at", "as_of", "revision_of")
    @classmethod
    def _tz_aware(cls, v: datetime | None, info) -> datetime | None:
        if v is None:
            return v
        return _require_tz(v, info.field_name)


class SeriesMeta(BaseModel):
    """Self-describing metadata for one series. Drives the frontend entirely;
    the UI hard-codes no ``series_id`` (spec §8.1)."""

    model_config = ConfigDict(extra="forbid")

    series_id: str
    display_name: str
    unit: str
    source: str
    expected_interval: timedelta        # serialises to ISO-8601 duration (P1D / P7D). Not optional — heartbeat needs it.
    precision: int                      # decimal places for display
    direction_good: Literal["up", "down", "neutral"] = "neutral"
    description: str | None = None


class CollectorContext(BaseModel):
    """Ambient state handed to every collector's ``fetch``.

    ``now`` is injected (not read from the clock inside collectors) so runs are
    reproducible and tests can pin time. Secrets arrive here, never via globals.
    """

    model_config = ConfigDict(extra="forbid")

    now: datetime
    secrets: dict[str, str] = {}


@runtime_checkable
class Collector(Protocol):
    """Fetch + normalise only. No storage, no dedup, no alerting (spec §4.1).

    Contract for implementers:
      * a failed fetch MUST raise — never return ``[]`` or degrade silently;
      * every Observation MUST carry a truthful ``status`` (not blanket
        ``confirmed``);
      * ``series`` declares metadata for exactly the series this collector emits.
    """

    id: str
    series: list[SeriesMeta]

    async def fetch(self, ctx: CollectorContext) -> list[Observation]: ...
