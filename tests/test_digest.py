"""Daily digest card: JSON 2.0 native charts, value rows, footer."""

from datetime import datetime, timedelta

from dashboard.contract import DataStatus, SeriesMeta
from dashboard.notify.digest import (
    CST,
    _pct_change,
    _sample,
    build_digest_card,
)
from dashboard.pipeline.snapshot import (
    CategoryMeta,
    CollectorHealth,
    Point,
    SeriesHealth,
    SeriesSnapshot,
    Snapshot,
)

NOW = datetime(2026, 8, 10, 9, 30, tzinfo=CST)


def _points(values, end=None):
    end = end or NOW
    start = end - timedelta(days=len(values) - 1)
    return [
        Point(observed_at=start + timedelta(days=i), as_of=NOW, value=v, status=DataStatus.CONFIRMED)
        for i, v in enumerate(values)
    ]


def _series(sid, name, unit, values, *, category="core", precision=0, stale=False, end=None):
    pts = _points(values, end)
    latest = pts[-1] if pts else None
    return SeriesSnapshot(
        meta=SeriesMeta(
            series_id=sid,
            display_name=name,
            unit=unit,
            source="test",
            expected_interval=timedelta(days=1),
            precision=precision,
        ),
        category=category,
        points=pts,
        latest=latest,
        previous=pts[-2] if len(pts) >= 2 else None,
        health=SeriesHealth(
            ok=not stale,
            stale=stale,
            last_observed_at=latest.observed_at if latest else None,
        ),
    )


def _snapshot(series, *, collectors=None):
    return Snapshot(
        generated_at=NOW,
        window=180,
        categories=[CategoryMeta(id="core", title="核心指标")],
        series=series,
        collectors=collectors if collectors is not None else [CollectorHealth(id="test", ok=True)],
    )


def _elements(card):
    return card["body"]["elements"]


def _markdown(card):
    return "\n".join(
        e["content"] for e in _elements(card) if e.get("tag") == "markdown"
    )


def _charts(card):
    return [e for e in _elements(card) if e.get("tag") == "chart"]


def test_card_builds_native_chart_and_value_rows():
    snap = _snapshot([
        _series("portwatch.hormuz.transits", "霍尔木兹海峡 · 日通行船数", "艘/日", [4, 3, 2]),
        _series("fx.dxy", "美元指数 (DXY)", "点", [99.5, 99.7], category="core", precision=2),
    ])
    card = build_digest_card(snap, now=NOW)

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "每日看板 · 08-10"
    assert card["header"]["template"] == "blue"

    text = _markdown(card)
    assert "**核心指标**" in text
    assert "霍尔木兹船数" in text          # short label; full name stays on the site
    assert "2 艘/日" in text
    assert "▼50.0%" in text                # 4 -> 2 over the window
    assert "美元指数" in text
    assert "99.70 点" in text
    assert "▲0.2%" in text                 # 99.5 -> 99.7
    assert "▁" not in text                 # no more glyph sparklines

    charts = _charts(card)
    assert len(charts) == 1
    assert charts[0]["chart_spec"]["title"]["text"] == "流量与宏观"
    rows = charts[0]["chart_spec"]["data"]["values"]
    by_series = {}
    for row in rows:
        by_series.setdefault(row["series"], []).append(row["change"])
    assert by_series["霍尔木兹船数"] == [0.0, -25.0, -50.0]
    assert by_series["美元指数"] == [0.0, 0.2]


def test_core_splits_into_two_charts():
    specs = [
        ("eia.crude.commercial_stocks", "商业原油库存", "千桶", 10),
        ("eia.crude.spr_stocks", "SPR 库存", "千桶", 20),
        ("eia.brent.spot", "布伦特原油", "美元/桶", 30),
        ("portwatch.hormuz.transits", "霍尔木兹海峡 · 日通行船数", "艘/日", 40),
        ("portwatch.hormuz.trade_volume", "霍尔木兹海峡 · 日通行贸易量", "估计吨/日", 50),
        ("treasury.yield.10y", "10 年期美债收益率", "%", 60),
        ("fx.dxy", "美元指数 (DXY)", "点", 70),
    ]
    snap = _snapshot([
        _series(sid, name, unit, [v, v + 1, v + 2])
        for sid, name, unit, v in specs
    ])
    charts = _charts(build_digest_card(snap, now=NOW))
    assert [c["chart_spec"]["title"]["text"] for c in charts] == [
        "能源库存与油价",
        "流量与宏观",
    ]
    assert [len({r["series"] for r in c["chart_spec"]["data"]["values"]}) for c in charts] == [3, 4]


def test_fiscal_is_text_only():
    snap = Snapshot(
        generated_at=NOW,
        window=180,
        categories=[CategoryMeta(id="fiscal", title="财政类")],
        series=[
            _series(
                "treasury.tga.closing_balance", "TGA 余额", "十亿美元", [1, 2],
                category="fiscal",
            )
        ],
        collectors=[CollectorHealth(id="test", ok=True)],
    )
    card = build_digest_card(snap, now=NOW)
    assert _charts(card) == []
    assert "TGA 余额" in _markdown(card)


def test_sample_keeps_endpoints_and_respects_horizon():
    points = _points(list(range(40)))
    sampled = _sample(points, NOW, horizon_days=120, max_points=16)
    assert len(sampled) == 16
    assert sampled[0].observed_at == points[0].observed_at
    assert sampled[-1].observed_at == points[-1].observed_at

    # everything older than the horizon -> fall back to the last two points
    old = _points([1, 2, 3], end=NOW - timedelta(days=200))
    assert len(_sample(old, NOW, horizon_days=30)) == 2


def test_pct_change_handles_sign_and_single_point():
    assert _pct_change(_points([1, 2, 3])) == "▲200.0%"
    assert _pct_change(_points([4, 3, 2])) == "▼50.0%"
    assert _pct_change(_points([1, -1])) == "转负"
    assert _pct_change(_points([7])) == ""


def test_stale_series_and_failed_collector_are_flagged():
    snap = _snapshot(
        [_series("s.old", "旧序列", "点", [1, 2], stale=True, end=NOW - timedelta(days=30))],
        collectors=[CollectorHealth(id="eia", ok=False, error="boom")],
    )
    card = build_digest_card(snap, now=NOW)

    text = _markdown(card)
    assert "⚠️" in text
    assert "旧序列" in text               # falls back to display_name (not in SHORT_NAMES)
    assert "最后" not in text
    assert "采集器失败" in text and "eia（boom）" in text
    assert "1 条序列采集中断" in text
    assert card["header"]["template"] == "orange"


def test_dashboard_link_is_in_footer():
    snap = _snapshot([_series("s", "序列", "点", [1, 2])])

    assert "打开看板" not in _markdown(build_digest_card(snap, now=NOW))

    card = build_digest_card(snap, now=NOW, site_url="https://example.pages.dev/")
    assert "[打开看板](https://example.pages.dev/)" in _markdown(card)


def test_empty_snapshot_still_builds_a_card():
    card = build_digest_card(_snapshot([], collectors=[]), now=NOW)
    assert "暂无数据" in _markdown(card)
    assert "采集器 0/0 正常" in _markdown(card)
    assert _charts(card) == []
