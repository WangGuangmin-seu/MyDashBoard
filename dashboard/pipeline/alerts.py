"""Alert engine (spec §6): config-driven threshold rules + mandatory heartbeat,
with cooldown dedup and revision-status guards.

Rules come from YAML (``rules/alerts.yaml``); adding an alert never touches code.
Two alert kinds map to two Feishu card colours (spec §7.3):

  * threshold  -> red   : the outside world crossed a threshold.
  * heartbeat  -> grey  : our own collection stalled (spec §6.2, the most
                          important alert — "no data" beats "out of range").

Conditions are evaluated by a tiny AST-restricted evaluator: only the name
``value``, numeric literals, comparisons and and/or/not are allowed. No eval().
"""

from __future__ import annotations

import ast
import json
import operator
import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from ..contract import DataStatus
from .snapshot import Snapshot
from .store import DEFAULT_DATA_DIR

# ---- rule model ----------------------------------------------------------


class AlertRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    series: str
    condition: str
    cooldown: timedelta
    severity: str = "warning"
    message: str = "{series} = {value}"
    skip_if_status: list[DataStatus] = []

    @field_validator("cooldown", mode="before")
    @classmethod
    def _parse_cooldown(cls, v):
        return _parse_duration(v) if isinstance(v, str) else v


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    kind: str            # "threshold" | "heartbeat"
    template: str        # "red" | "grey"
    severity: str
    series_id: str
    message: str
    value: float | None = None
    status: DataStatus | None = None


# ---- loading -------------------------------------------------------------


def load_rules(path: Path | str) -> list[AlertRule]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    return [AlertRule.model_validate(r) for r in raw]


# ---- state (cooldown dedup) ---------------------------------------------


class AlertState:
    """Persists last_fired_at per alert key so cooldowns survive across runs."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._fired: dict[str, datetime] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._fired = {k: datetime.fromisoformat(v) for k, v in raw.items()}

    def last_fired(self, key: str) -> datetime | None:
        return self._fired.get(key)

    def mark_fired(self, key: str, when: datetime) -> None:
        self._fired[key] = when

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v.isoformat() for k, v in self._fired.items()}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- evaluation ----------------------------------------------------------


def evaluate(
    snapshot: Snapshot,
    rules: list[AlertRule],
    state: AlertState,
    now: datetime,
) -> list[Alert]:
    """Return the alerts that should actually be sent now (cooldown-filtered).

    Mutates ``state`` to record firings; caller is responsible for ``state.save()``
    after the notifications succeed.
    """
    by_id = {s.meta.series_id: s for s in snapshot.series}
    alerts: list[Alert] = []

    # 1) Heartbeat — one per stale series (spec §6.2).
    for s in snapshot.series:
        if not s.health.stale:
            continue
        key = f"heartbeat:{s.meta.series_id}"
        if _in_cooldown(state, key, now, _HEARTBEAT_COOLDOWN):
            continue
        alerts.append(
            Alert(
                rule_id=key,
                kind="heartbeat",
                template="grey",
                severity="critical",
                series_id=s.meta.series_id,
                message=(
                    f"采集中断：{s.meta.display_name} 最新数据 "
                    f"{_fmt_dt(s.health.last_observed_at)}，已超过 "
                    f"2×{s.meta.expected_interval} 未更新"
                ),
            )
        )
        state.mark_fired(key, now)

    # 1b) Collector failure — an outright fetch failure is a collection fault
    #     too, and must alert immediately rather than waiting for staleness
    #     to accrue (spec §9.1, acceptance §12.5).
    for c in snapshot.collectors:
        if c.ok:
            continue
        key = f"collector:{c.id}"
        if _in_cooldown(state, key, now, _HEARTBEAT_COOLDOWN):
            continue
        alerts.append(
            Alert(
                rule_id=key,
                kind="heartbeat",
                template="grey",
                severity="critical",
                series_id=c.id,
                message=f"采集器 {c.id} 运行失败：{c.error or '未知错误'}",
            )
        )
        state.mark_fired(key, now)

    # 2) Threshold rules (spec §6.1, §6.4).
    for rule in rules:
        s = by_id.get(rule.series)
        if s is None or s.latest is None or s.latest.value is None:
            continue
        latest = s.latest
        if latest.status in rule.skip_if_status:
            continue
        if not _eval_condition(rule.condition, latest.value):
            continue
        if _in_cooldown(state, rule.id, now, rule.cooldown):
            continue
        msg = rule.message.format(
            value=latest.value, series=rule.series, status=latest.status.value
        )
        # Even when not skipped, revised/provisional status must be surfaced (§6.4).
        if latest.status != DataStatus.CONFIRMED:
            msg += f"（数据状态：{latest.status.value}）"
        alerts.append(
            Alert(
                rule_id=rule.id,
                kind="threshold",
                template="red",
                severity=rule.severity,
                series_id=rule.series,
                message=msg,
                value=latest.value,
                status=latest.status,
            )
        )
        state.mark_fired(rule.id, now)

    return alerts


_HEARTBEAT_COOLDOWN = timedelta(days=1)


def _in_cooldown(state: AlertState, key: str, now: datetime, cooldown: timedelta) -> bool:
    last = state.last_fired(key)
    return last is not None and (now - last) < cooldown


# ---- safe condition evaluator -------------------------------------------

_CMP = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
    ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


def _eval_condition(expr: str, value: float) -> bool:
    tree = ast.parse(expr, mode="eval")
    return bool(_ev(tree.body, value))


def _ev(node: ast.AST, value: float):
    if isinstance(node, ast.Compare):
        left = _ev(node.left, value)
        for op, comp in zip(node.ops, node.comparators):
            right = _ev(comp, value)
            if type(op) not in _CMP or not _CMP[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        vals = [_ev(v, value) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _ev(node.operand, value)
    if isinstance(node, ast.Name):
        if node.id == "value":
            return value
        raise ValueError(f"unknown name in condition: {node.id!r} (only 'value' allowed)")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, bool)):
        return node.value
    raise ValueError(f"disallowed expression element: {ast.dump(node)}")


# ---- duration parsing ----------------------------------------------------

_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_DUR_UNIT = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _parse_duration(s: str) -> timedelta:
    m = _DUR_RE.match(s)
    if not m:
        raise ValueError(f"bad duration {s!r}; use forms like 7d, 12h, 30m")
    return timedelta(**{_DUR_UNIT[m.group(2)]: int(m.group(1))})


def _fmt_dt(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt else "（无）"


def default_state_path(data_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    return Path(data_dir) / "alert_state.json"
