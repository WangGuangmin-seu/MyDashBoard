"""Series storage — append-only with explicit revision records (spec §5.2).

Each series lives in ``data/series/<series_id>.json`` as a flat list of
Observation records, oldest first. History is never mutated in place: when the
upstream reports a new value for an ``observed_at`` we already hold, we append a
fresh record whose ``revision_of`` points at the superseded record's ``as_of``.
Git history (spec §5.4) is then the audit trail — no separate audit table.

The "current" value for an ``observed_at`` is always the record with the
greatest ``as_of``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..contract import Observation, SeriesMeta

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class Store:
    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)
        self.series_dir = self.data_dir / "series"

    # ---- paths -----------------------------------------------------------
    def _series_path(self, series_id: str) -> Path:
        return self.series_dir / f"{series_id}.json"

    # ---- read ------------------------------------------------------------
    def load_series(self, series_id: str) -> list[Observation]:
        path = self._series_path(series_id)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [Observation.model_validate(r) for r in raw]

    def load_series_ids(self) -> list[str]:
        if not self.series_dir.exists():
            return []
        return sorted(p.stem for p in self.series_dir.glob("*.json"))

    # ---- write -----------------------------------------------------------
    def append_observations(self, incoming: list[Observation]) -> dict[str, int]:
        """Merge ``incoming`` into stored series. Returns {series_id: n_written}.

        A record is written only when it is new for its ``observed_at`` or
        differs (value or status) from the current record — otherwise it is a
        no-op, keeping commits free of churn.
        """
        by_series: dict[str, list[Observation]] = {}
        for obs in incoming:
            by_series.setdefault(obs.series_id, []).append(obs)

        written: dict[str, int] = {}
        for series_id, new_obs in by_series.items():
            records = self.load_series(series_id)
            current = _current_by_observed_at(records)
            n = 0
            for obs in sorted(new_obs, key=lambda o: o.observed_at):
                existing = current.get(obs.observed_at)
                if existing is None:
                    records.append(obs)
                    current[obs.observed_at] = obs
                    n += 1
                elif (existing.value, existing.status) != (obs.value, obs.status):
                    revised = obs.model_copy(update={"revision_of": existing.as_of})
                    records.append(revised)
                    current[obs.observed_at] = revised
                    n += 1
                # else: unchanged -> skip
            if n:
                records.sort(key=lambda o: (o.observed_at, o.as_of))
                self._write_series(series_id, records)
            written[series_id] = n
        return written

    def _write_series(self, series_id: str, records: list[Observation]) -> None:
        self.series_dir.mkdir(parents=True, exist_ok=True)
        payload = [json.loads(r.model_dump_json()) for r in records]
        self._series_path(series_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---- meta ------------------------------------------------------------
    def write_meta(self, metas: list[SeriesMeta]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = [json.loads(m.model_dump_json()) for m in metas]
        (self.data_dir / "meta.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def current_series(self, series_id: str) -> list[Observation]:
        """The current (latest-as_of) record per observed_at, sorted by time."""
        records = self.load_series(series_id)
        current = _current_by_observed_at(records)
        return [current[k] for k in sorted(current)]


def _current_by_observed_at(records: list[Observation]) -> dict:
    """Map observed_at -> record with the greatest as_of."""
    current: dict = {}
    for r in records:
        prev = current.get(r.observed_at)
        if prev is None or r.as_of >= prev.as_of:
            current[r.observed_at] = r
    return current
