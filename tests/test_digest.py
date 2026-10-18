"""Daily digest card: five focus charts, value rows, footer."""

from datetime import datetime, timedelta

from dashboard.contract import DataStatus, SeriesMeta
from dashboard.notify.digest import (
    CST,
    FOCUS,
    _pct_change,
    _sample,
    build_digest_card,
)
from dashboard.pipeline.snapshot import (
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


def _series(sid, values, *, unit="点", precision=0, stale=False, end=None):
    pts = _points(values, end)
    latest = pts[-1] if pts else None
    return SeriesSnapshot(
        meta=SeriesMeta(
            series_id=sid,
            display_name=sid,
            unit=unit,
            source="test",
            expected_interval=timedelta(days=1),
            precision=precision,
        ),
        category="core",
        points=pts,
        latest=latest,
        previous=pts[-2] if len(pts) >= 2 else None,
        health=SeriesHealth(
            ok=not stale,
            stale=stale,
            last_observed_at=latest.observed_at if latest else None,
        ),
    )


def _focus_snapshot(*, extra=None, collectors=None):
    series = [
        _series("portwatch.hormuz.transits", [4, 3, 2], unit="艘/日"),
        _series("eia.crude.spr_stocks", [100, 90, 80], unit="千桶"),
        _series("eia.brent.spot", [100, 110, 120], unit="美元/桶"),
        _series("ctfi.composite", [1000, 1010, 1020]),
        _series("metals.aluminum.price", [20000, 19000, 18000], unit="元/吨"),
        _series("metals.alumina.price", [3000, 2900, 2800], unit="元/吨"),
    ]
    series.extend(extra or [])
    return Snapshot(
        generated_at=NOW,
        window=180,
        categories=[],
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


def test_card_has_five_focus_charts():
    card = build_digest_card(_focus_snapshot(), now=NOW)

    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "每日看板 · 08-10"
    assert card["header"]["template"] == "blue"

    charts = _charts(card)
    assert len(charts) == len(FOCUS) == 5
    assert [c["chart_spec"]["title"]["text"] for c in charts] == [f["title"] for f in FOCUS]

    # first four charts are raw single-series lines
    raw_values = [r["value"] for r in charts[0]["chart_spec"]["data"]["values"]]
    assert raw_values == [4.0, 3.0, 2.0]
    assert "series" not in charts[0]["chart_spec"]["data"]["values"][0]

    # aluminium pair is relative change, two series
    pct_rows = charts[4]["chart_spec"]["data"]["values"]
    assert {r["series"] for r in pct_rows} == {"沪铝", "氧化铝"}

    text = _markdown(card)
    assert "霍尔木兹船数" in text and "2 艘/日 ▼50.0%" in text
    assert "SPR 库存" in text and "80 千桶 ▼20.0%" in text
    assert "布伦特原油" in text and "120 $/桶 ▲20.0%" in text
    assert "CTFI 综合" in text and "1,020 点 ▲2.0%" in text
    assert "沪铝" in text and "18,000 元/吨 ▼10.0%" in text
    assert "氧化铝" in text and "2,800 元/吨 ▼6.7%" in text


def test_footer_counts_hidden_series():
    card = build_digest_card(
        _focus_snapshot(extra=[_series("fx.dxy", [1, 2], unit="点")]), now=NOW
    )
    assert "其余 1 条见看板" in _markdown(card)


def test_missing_focus_series_is_skipped():
    snap = Snapshot(
        generated_at=NOW,
        window=180,
        categories=[],
        series=[_series("portwatch.hormuz.transits", [4, 3, 2], unit="艘/日")],
        collectors=[CollectorHealth(id="test", ok=True)],
    )
    card = build_digest_card(snap, now=NOW)
    assert len(_charts(card)) == 1
    assert "霍尔木兹船数" in _markdown(card)


def test_stale_focus_series_flags_card():
    stale_spr = _series(
        "eia.crude.spr_stocks", [100, 90, 80], unit="千桶",
        stale=True, end=NOW - timedelta(days=30),
    )
    snap = _focus_snapshot(extra=[])
    snap.series = [s for s in snap.series if s.meta.series_id != "eia.crude.spr_stocks"]
    snap.series.append(stale_spr)

    card = build_digest_card(snap, now=NOW)
    text = _markdown(card)
    assert "⚠️" in text
    assert "1 条关注序列中断" in text
    assert card["header"]["template"] == "orange"


def test_dashboard_link_is_in_footer():
    snap = _focus_snapshot()
    assert "打开看板" not in _markdown(build_digest_card(snap, now=NOW))
    card = build_digest_card(snap, now=NOW, site_url="https://example.pages.dev/")
    assert "[打开看板](https://example.pages.dev/)" in _markdown(card)


def test_sample_keeps_endpoints_and_respects_horizon():
    points = _points(list(range(40)))
    sampled = _sample(points, NOW, horizon_days=120, max_points=16)
    assert len(sampled) == 16
    assert sampled[0].observed_at == points[0].observed_at
    assert sampled[-1].observed_at == points[-1].observed_at

    old = _points([1, 2, 3], end=NOW - timedelta(days=200))
    assert len(_sample(old, NOW, horizon_days=30)) == 2


def test_pct_change_handles_sign_and_single_point():
    assert _pct_change(_points([1, 2, 3])) == "▲200.0%"
    assert _pct_change(_points([4, 3, 2])) == "▼50.0%"
    assert _pct_change(_points([1, -1])) == "转负"
    assert _pct_change(_points([7])) == ""


def test_empty_snapshot_still_builds_a_card():
    snap = Snapshot(
        generated_at=NOW, window=180, categories=[], series=[],
        collectors=[],
    )
    card = build_digest_card(snap, now=NOW)
    assert "暂无数据" in _markdown(card)
    assert "采集器 0/0 正常" in _markdown(card)
    assert _charts(card) == []
