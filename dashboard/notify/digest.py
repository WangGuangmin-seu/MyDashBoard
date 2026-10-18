"""Daily digest card — every morning, one Feishu card with every series.

Why text and not PNG charts: a custom-bot webhook can send text / rich text /
interactive cards, but a card image needs an ``image_key``, and an image_key can
only be obtained by uploading through the app-tenant token of a Feishu *app*.
The dashboard deliberately uses a webhook-only bot (no app credentials, no
admin approval; see CLAUDE.md §飞书), so trend lines are rendered as Unicode
block glyphs inside the card instead of attached images.

The digest is a *view* over the committed snapshot: it reads the same
``web/data/snapshot.json`` the frontend reads, so what arrives in Feishu is
exactly what the site shows. No extra upstream calls, no extra state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..contract import DataStatus
from ..pipeline.snapshot import SeriesSnapshot, Snapshot, heartbeat_stale

# China has no DST, so a fixed offset is correct and needs no tzdata package
# (Windows Python does not ship the IANA database).
CST = timezone(timedelta(hours=8), name="Asia/Shanghai")

_SPARK = "▁▂▃▄▅▆▇█"
SPARK_WIDTH = 12

# Card-sized labels: full names stay on the site, but they are too long for a
# one-line Feishu row on a phone. Fallback is display_name for new series.
SHORT_NAMES = {    "portwatch.hormuz.transits": "霍尔木兹船数",
    "portwatch.hormuz.trade_volume": "霍尔木兹货量",
    "portwatch.bab_el_mandeb.transits": "曼德海峡船数",
    "portwatch.bab_el_mandeb.trade_volume": "曼德海峡货量",
    "portwatch.cape_of_good_hope.transits": "好望角船数",
    "portwatch.cape_of_good_hope.trade_volume": "好望角货量",
    "eia.crude.commercial_stocks": "商业原油库存",
    "eia.crude.spr_stocks": "SPR 库存",
    "eia.brent.spot": "布伦特原油",
    "treasury.yield.10y": "10Y 美债",
    "fx.dxy": "美元指数",
    "treasury.tga.closing_balance": "TGA 余额",
    "treasury.mts.receipts": "财政收入",
    "treasury.mts.outlays": "财政支出",
    "treasury.mts.deficit": "财政赤字",
    "ctfi.composite": "CTFI 综合",
    "ctfi.ct1": "CT1 WS",
    "ctfi.ct1.tce": "CT1 TCE",
    "ctfi.ct2": "CT2 WS",
    "metals.aluminum.price": "沪铝",
    "metals.alumina.price": "氧化铝",
    "metals.aluminum.inventory": "铝锭库存",
    "metals.aluminum.sim_margin": "冶炼毛利",
}

# Compact units for the card; the site keeps the full unit string.
UNIT_ALIASES = {
    "估计吨/日": "吨/日",
    "美元/桶": "$/桶",
    "WS点数": "WS",
    "美元/天": "$/天",
    "十亿美元": "十亿$",
}


def _window(points, width: int = SPARK_WIDTH):
    """The plotted subset: last ``width`` points that carry a value."""
    return [p for p in points if p.value is not None][-width:]


def sparkline(points, width: int = SPARK_WIDTH) -> str:
    """Compact trend line: last ``width`` non-null values mapped to 8 levels.

    Returns "" when fewer than two values exist (a single point has no trend).
    """
    values = [p.value for p in _window(points, width)]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    if hi == lo:
        return _SPARK[3] * len(values)
    span = hi - lo
    last = len(_SPARK) - 1
    return "".join(_SPARK[round((v - lo) / span * last)] for v in values)


def _window_change(points, width: int = SPARK_WIDTH) -> str:
    """Percent change across the plotted window (not just the last step)."""
    window = _window(points, width)
    if len(window) < 2:
        return ""
    first, last = window[0].value, window[-1].value
    if first == 0:
        return ""
    if first * last < 0:  # crossed zero: a percentage would be meaningless
        return "转正" if last > 0 else "转负"
    pct = (last - first) / abs(first) * 100
    if abs(pct) < 0.05:
        return "→0.0%"
    arrow = "▲" if pct > 0 else "▼"
    return f"{arrow}{abs(pct):.1f}%"


def _fmt_value(value: float | None, precision: int) -> str:
    if value is None:
        return "—"
    return f"{value:,.{precision}f}"


def _series_line(s: SeriesSnapshot, *, stale: bool) -> str:
    meta = s.meta
    latest = s.latest
    warn = "⚠️ " if stale else ""
    short = SHORT_NAMES.get(meta.series_id, meta.display_name)
    unit = UNIT_ALIASES.get(meta.unit, meta.unit)
    value = _fmt_value(latest.value if latest else None, meta.precision)
    line = f"{warn}**{short}** {value} {unit}".rstrip()
    change = _window_change(s.points)
    if change:
        line += f" {change}"
    if latest is not None and latest.status != DataStatus.CONFIRMED:
        line += f"（{latest.status.value}）"
    spark = sparkline(s.points)
    if spark:
        line += f"  {spark}"
    return line


def _is_stale(s: SeriesSnapshot, now: datetime) -> bool:
    """Recompute staleness at send time instead of trusting ``health.stale``.

    The snapshot's health is a judgement from the moment it was generated; if
    collection died afterwards, every card would keep saying "healthy" while
    the data freezes. The digest is a daily watch, so it must judge by now.
    """
    latest = s.latest
    return heartbeat_stale(
        now, latest.observed_at if latest else None, s.meta.expected_interval
    )


def build_digest_card(
    snapshot: Snapshot,
    *,
    now: datetime,
    site_url: str | None = None,
) -> dict:
    """Build the interactive card. Pure function: no I/O, easy to test.

    ``now`` is the send time (Asia/Shanghai) and only drives the title; the
    footer reports the snapshot's own ``generated_at`` so a stale snapshot is
    never disguised as fresh.
    """
    local_now = now.astimezone(CST)
    failed = [c for c in snapshot.collectors if not c.ok]
    # Judged at send time, not copied from the snapshot (see _is_stale).
    stale = [s for s in snapshot.series if _is_stale(s, now)]
    stale_ids = {s.meta.series_id for s in stale}

    sections: list[tuple[str, list[SeriesSnapshot]]] = []
    known = {c.id for c in snapshot.categories}
    for cat in snapshot.categories:
        items = [s for s in snapshot.series if s.category == cat.id]
        if items:
            sections.append((cat.title, items))
    leftover = [s for s in snapshot.series if s.category not in known]
    if leftover:
        sections.append(("其他", leftover))

    elements: list[dict] = []
    for index, (title, items) in enumerate(sections):
        body = "\n".join(
            _series_line(s, stale=s.meta.series_id in stale_ids) for s in items
        )
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{body}"}}
        )
        if index != len(sections) - 1:
            elements.append({"tag": "hr"})

    if not snapshot.series:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "暂无数据。"}}
        )

    if failed:
        names = "、".join(f"{c.id}（{c.error or '未知错误'}）" for c in failed)
        elements.append({"tag": "hr"})
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"⚠️ **采集器失败**：{names}"}}
        )

    generated = snapshot.generated_at.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    healthy = len(snapshot.collectors) - len(failed)
    footer = f"快照生成于 {generated} · 采集器 {healthy}/{len(snapshot.collectors)} 正常"
    if stale:
        footer += f" · {len(stale)} 条序列采集中断"
    footer += f" · 走势=最近{SPARK_WIDTH}点（日/周/月频跨度不同），%为区间首尾变化"

    elements.append(
        {"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]}
    )

    if site_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "打开看板"},
                        "type": "primary",
                        "url": site_url,
                    }
                ],
            }
        )

    # Routine digest is blue; orange when something needs a look. Red stays
    # reserved for threshold alerts, so a healthy digest never looks alarming.
    template = "orange" if (stale or failed) else "blue"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": f"每日看板 · {local_now.strftime('%m-%d')}",
            },
        },
        "elements": elements,
    }
