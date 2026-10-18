"""Daily digest card — one Feishu message, five focused charts.

The card is deliberately *not* a dump of all 23 series. It follows the user's
watch list: Hormuz transits, SPR stocks, Brent spot, CTFI composite, and the
aluminium pair. Each of the first four gets its own single-line chart with a
real value axis; aluminium and alumina differ by ~10x in level (both CNY/t),
so that pair is plotted as relative change from the window start and the
absolute prices stay in the rows below.

Feishu cards support a JSON 2.0 ``chart`` component (VChart), so these are
native charts — no PNG upload, no ``image_key``, no Feishu app credentials.
One card holds at most five charts, which matches the five focus items.

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

FOCUS_HORIZON = 180   # days shown in most focus charts
CHART_POINTS = 40     # sampled points per series; still small enough for one card

# The watch list. ``mode="raw"`` draws one series on its own value axis;
# ``mode="pct"`` draws a group as relative change (for series whose levels are
# not directly comparable). A per-item ``horizon`` overrides FOCUS_HORIZON.
FOCUS = [
    {"title": "霍尔木兹 · 日通行船数", "ids": ["portwatch.hormuz.transits"], "mode": "raw"},
    {"title": "SPR 原油库存", "ids": ["eia.crude.spr_stocks"], "mode": "raw", "horizon": 365},
    {"title": "布伦特原油现货", "ids": ["eia.brent.spot"], "mode": "raw"},
    {"title": "CTFI 综合指数", "ids": ["ctfi.composite"], "mode": "raw"},
    {
        "title": "铝价 / 氧化铝价（相对 %）",
        "ids": ["metals.aluminum.price", "metals.alumina.price"],
        "mode": "pct",
    },
]

# Card-sized labels: full names stay on the site, but they are too long for a
# one-line Feishu row on a phone. Fallback is display_name for new series.
SHORT_NAMES = {
    "portwatch.hormuz.transits": "霍尔木兹船数",
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


def _fmt_value(value: float | None, precision: int) -> str:
    if value is None:
        return "—"
    return f"{value:,.{precision}f}"


def _sample(points, now: datetime, horizon_days: int, max_points: int = CHART_POINTS):
    """Points inside the horizon, evenly downsampled to at most ``max_points``.

    First and last are always kept so the chart's endpoints match the row's
    window change. If the series has nothing inside the horizon (collection
    stopped), fall back to its last two points so a line still renders.
    """
    cutoff = now - timedelta(days=horizon_days)
    window = [p for p in points if p.value is not None and p.observed_at >= cutoff]
    if len(window) < 2:
        window = [p for p in points if p.value is not None][-2:]
    if len(window) > max_points:
        step = (len(window) - 1) / (max_points - 1)
        indices = sorted({round(i * step) for i in range(max_points)})
        window = [window[i] for i in indices]
    return window


def _pct_change(points) -> str:
    """Change across a sampled window, e.g. ▲2.7%; handles zero crossings."""
    if len(points) < 2:
        return ""
    first, last = points[0].value, points[-1].value
    if first == 0:
        return ""
    if first * last < 0:  # crossed zero: a percentage would be meaningless
        return "转正" if last > 0 else "转负"
    pct = (last - first) / abs(first) * 100
    if abs(pct) < 0.05:
        return "→0.0%"
    arrow = "▲" if pct > 0 else "▼"
    return f"{arrow}{abs(pct):.1f}%"


def _series_line(s: SeriesSnapshot, *, stale: bool, change: str) -> str:
    meta = s.meta
    latest = s.latest
    warn = "⚠️ " if stale else ""
    short = SHORT_NAMES.get(meta.series_id, meta.display_name)
    unit = UNIT_ALIASES.get(meta.unit, meta.unit)
    value = _fmt_value(latest.value if latest else None, meta.precision)
    line = f"{warn}**{short}** {value} {unit}".rstrip()
    if change:
        line += f" {change}"
    if latest is not None and latest.status != DataStatus.CONFIRMED:
        line += f"（{latest.status.value}）"
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


def _raw_chart(points, title: str) -> dict:
    """Single series on its own value axis (absolute levels)."""
    rows = [
        {"date": p.observed_at.strftime("%Y-%m-%d"), "value": p.value}
        for p in points
    ]
    return {
        "tag": "chart",
        "height": "240px",
        "chart_spec": {
            "type": "line",
            "title": {"text": title},
            "label": {"visible": False},
            "data": {"values": rows},
            "xField": "date",
            "yField": "value",
            "axes": [
                {"orient": "bottom", "label": {"autoHide": True, "autoRotate": True}},
                {"orient": "left"},
            ],
            "point": {"visible": False},
        },
    }


def _pct_chart(items, sampled, title: str) -> dict | None:
    """Group drawn as relative change from each series' window start."""
    rows = []
    for s in items:
        window = sampled.get(s.meta.series_id) or []
        if len(window) < 2:
            continue
        first = window[0].value
        if abs(first) < 1e-9:
            continue
        name = SHORT_NAMES.get(s.meta.series_id, s.meta.display_name)
        for p in window:
            rows.append(
                {
                    "date": p.observed_at.strftime("%Y-%m-%d"),
                    "series": name,
                    "change": round((p.value - first) / abs(first) * 100, 1),
                }
            )
    if not rows:
        return None
    return {
        "tag": "chart",
        "height": "240px",
        "color_theme": "rainbow",
        "chart_spec": {
            "type": "line",
            "title": {"text": title},
            "label": {"visible": False},
            "data": {"values": rows},
            "xField": "date",
            "yField": "change",
            "seriesField": "series",
            "axes": [
                {"orient": "bottom", "label": {"autoHide": True, "autoRotate": True}},
                {"orient": "left"},
            ],
            "legends": {"visible": True, "orient": "bottom"},
            "point": {"visible": False},
        },
    }


def build_digest_card(
    snapshot: Snapshot,
    *,
    now: datetime,
    site_url: str | None = None,
) -> dict:
    """Build the JSON 2.0 card. Pure function: no I/O, easy to test.

    ``now`` is the send time (Asia/Shanghai) and drives the title plus the
    staleness judgement; the footer reports the snapshot's own ``generated_at``
    so a stale snapshot is never disguised as fresh.
    """
    local_now = now.astimezone(CST)
    failed = [c for c in snapshot.collectors if not c.ok]
    by_id = {s.meta.series_id: s for s in snapshot.series}

    elements: list[dict] = []
    focus_series: list[SeriesSnapshot] = []
    for item in FOCUS:
        items = [by_id[sid] for sid in item["ids"] if sid in by_id]
        if not items:
            continue
        focus_series.extend(items)
        horizon = item.get("horizon", FOCUS_HORIZON)
        sampled = {
            s.meta.series_id: _sample(s.points, now, horizon) for s in items
        }

        if item["mode"] == "raw":
            s = items[0]
            window = sampled[s.meta.series_id]
            if len(window) >= 2:
                elements.append(_raw_chart(window, item["title"]))
            elements.append(
                {
                    "tag": "markdown",
                    "content": _series_line(
                        s,
                        stale=_is_stale(s, now),
                        change=_pct_change(window),
                    ),
                }
            )
            continue

        chart = _pct_chart(items, sampled, item["title"])
        if chart is not None:
            elements.append(chart)
        lines = [
            _series_line(
                s,
                stale=_is_stale(s, now),
                change=_pct_change(sampled[s.meta.series_id]),
            )
            for s in items
        ]
        elements.append({"tag": "markdown", "content": "\n".join(lines)})

    if not focus_series:
        elements.append({"tag": "markdown", "content": "暂无数据。"})

    if failed:
        names = "、".join(f"{c.id}（{c.error or '未知错误'}）" for c in failed)
        elements.append({"tag": "markdown", "content": f"⚠️ **采集器失败**：{names}"})

    stale = [s for s in focus_series if _is_stale(s, now)]
    focus_ids = {s.meta.series_id for s in focus_series}
    hidden = len(snapshot.series) - len(focus_ids)

    generated = snapshot.generated_at.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    healthy = len(snapshot.collectors) - len(failed)
    footer = f"快照生成于 {generated} · 采集器 {healthy}/{len(snapshot.collectors)} 正常"
    if stale:
        footer += f" · {len(stale)} 条关注序列中断"
    if hidden:
        footer += f" · 其余 {hidden} 条见看板"
    if site_url:
        footer += f" · [打开看板]({site_url})"
    elements.append({"tag": "markdown", "content": footer})

    # Routine digest is blue; orange when something needs a look. Red stays
    # reserved for threshold alerts, so a healthy digest never looks alarming.
    template = "orange" if (stale or failed) else "blue"
    return {
        "schema": "2.0",
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": f"每日看板 · {local_now.strftime('%m-%d')}",
            },
        },
        "body": {"elements": elements},
    }
