"""Daily digest card — one Feishu card with native charts, every morning.

Feishu cards support a JSON 2.0 ``chart`` component (VChart). That renders
real line charts inside the message, so no PNG upload, no ``image_key`` and no
Feishu *app* credentials are needed: the webhook-only bot is enough. The card
holds one chart per category (max five, per Feishu's recommendation) plus a
compact value row per series.

Charts plot **relative change from the window start (%)**, not raw values:
categories mix units (barrels, $/bbl, %, index points) and levels, so a shared
raw y-axis would be meaningless. The rows below carry the latest absolute
value and the same window change, so the chart and the number agree.

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

# Feishu recommends at most five chart components per card.
MAX_CHARTS = 5
CHART_POINTS = 16  # sampled points per series; keeps the card payload small

# Category -> time horizon shown in its chart. Fiscal mixes daily TGA with
# monthly MTS, so it needs a longer span than the purely daily ones.
HORIZONS = {
    "core": 120,
    "freight_index": 120,
    "aluminum": 120,
    "crude_freight": 180,
    "fiscal": 365,
}
DEFAULT_HORIZON = 120

# Chart grouping. Core has seven series (too many lines for one small chart),
# so it splits into two; fiscal is monthly and slow-moving, so it stays as
# value rows only. Everything else gets one chart per category, keeping the
# card within Feishu's recommended five charts.
CHART_GROUPS = {
    "core": [
        ("能源库存与油价", [
            "eia.crude.commercial_stocks",
            "eia.crude.spr_stocks",
            "eia.brent.spot",
        ]),
        ("流量与宏观", [
            "portwatch.hormuz.transits",
            "portwatch.hormuz.trade_volume",
            "treasury.yield.10y",
            "fx.dxy",
        ]),
    ],
    "fiscal": [],
}

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


def _sections(snapshot: Snapshot):
    """(category_id, title, series) in display order, uncategorised last."""
    sections = []
    known = {c.id for c in snapshot.categories}
    for cat in snapshot.categories:
        items = [s for s in snapshot.series if s.category == cat.id]
        if items:
            sections.append((cat.id, cat.title, items))
    leftover = [s for s in snapshot.series if s.category not in known]
    if leftover:
        sections.append(("other", "其他", leftover))
    return sections


def _chart_rows(items, sampled) -> list[dict]:
    """Flat VChart rows: change % from each series' window start."""
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
    return rows


def _line_chart(rows: list[dict], title: str | None = None) -> dict:
    """Feishu card 2.0 native chart (VChart line)."""
    spec = {
        "type": "line",
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
    }
    if title:
        spec["title"] = {"text": title}
    return {
        "tag": "chart",
        "height": "220px",
        "color_theme": "rainbow",
        "chart_spec": spec,
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
    stale = [s for s in snapshot.series if _is_stale(s, now)]
    stale_ids = {s.meta.series_id for s in stale}

    elements: list[dict] = []
    charts = 0
    for cat_id, title, items in _sections(snapshot):
        horizon = HORIZONS.get(cat_id, DEFAULT_HORIZON)
        sampled = {
            s.meta.series_id: _sample(s.points, now, horizon) for s in items
        }
        elements.append({"tag": "markdown", "content": f"**{title}**"})

        if cat_id in CHART_GROUPS:
            groups = CHART_GROUPS[cat_id]
        else:
            groups = [(None, [s.meta.series_id for s in items])]
        for subtitle, series_ids in groups:
            group_items = [s for s in items if s.meta.series_id in series_ids]
            rows = _chart_rows(group_items, sampled)
            if rows and charts < MAX_CHARTS:
                elements.append(_line_chart(rows, title=subtitle))
                charts += 1

        lines = [
            _series_line(
                s,
                stale=s.meta.series_id in stale_ids,
                change=_pct_change(sampled.get(s.meta.series_id) or []),
            )
            for s in items
        ]
        elements.append({"tag": "markdown", "content": "\n".join(lines)})

    if not snapshot.series:
        elements.append({"tag": "markdown", "content": "暂无数据。"})

    if failed:
        names = "、".join(f"{c.id}（{c.error or '未知错误'}）" for c in failed)
        elements.append(
            {"tag": "markdown", "content": f"⚠️ **采集器失败**：{names}"}
        )

    generated = snapshot.generated_at.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    healthy = len(snapshot.collectors) - len(failed)
    footer = f"快照生成于 {generated} · 采集器 {healthy}/{len(snapshot.collectors)} 正常"
    if stale:
        footer += f" · {len(stale)} 条序列采集中断"
    footer += " · 折线=相对窗口起点变化（%）"
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
