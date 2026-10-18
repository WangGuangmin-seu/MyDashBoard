"""Daily digest card: sparkline levels, per-series lines, footer, button."""

from datetime import datetime, timedelta

from dashboard.contract import DataStatus, SeriesMeta
from dashboard.notify.digest import (
    CST,
    _span_label,
    _window_change,
    build_digest_card,
    sparkline,
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


def _div_text(card) -> str:
    return "\n".join(
        e["text"]["content"] for e in card["elements"] if e.get("tag") == "div"
    )


def test_sparkline_maps_values_and_handles_edges():
    line = sparkline(_points([1, 2, 3, 4]))
    assert len(line) == 4
    assert all(ch in "▁▂▃▄▅▆▇█" for ch in line)
    assert line[0] == "▁" and line[-1] == "█"

    flat = sparkline(_points([5, 5, 5]))
    assert flat == "▄▄▄"

    # nulls are skipped; a single remaining value has no trend
    assert sparkline(_points([None, 7])) == ""
    assert sparkline([]) == ""


def test_card_lists_every_series_with_value_delta_and_trend():
    snap = _snapshot([
        _series("portwatch.hormuz.transits", "霍尔木兹海峡 · 日通行船数", "艘/日", [4, 3, 2]),
        _series("fx.dxy", "美元指数 (DXY)", "点", [99.5, 99.7], category="core", precision=2),
    ])
    card = build_digest_card(snap, now=NOW)

    assert card["header"]["title"]["content"] == "每日看板 · 08-10"
    assert card["header"]["template"] == "blue"
    body = _div_text(card)
    assert "**核心指标**" in body
    assert "霍尔木兹海峡 · 日通行船数" in body
    assert "2 艘/日" in body
    assert "▼ 1" in body          # 3 -> 2
    assert "99.70 点" in body
    assert "▲ 0.2" in body        # 99.5 -> 99.7
    assert "▁" in body            # sparkline rendered inline
    # trend line carries its real span and window change
    assert "近3日" in body
    assert "▼50.0%" in body       # 4 -> 2 over the window
    assert "▲0.2%" in body        # 99.5 -> 99.7 over the window


def test_span_label_adapts_to_point_spacing_and_window_change():
    daily = _points([1, 2, 3])
    assert _span_label(daily) == "近3日"
    assert _window_change(daily) == "▲200.0%"

    weekly = [
        Point(observed_at=NOW - timedelta(days=14) + timedelta(days=7 * i),
              as_of=NOW, value=v, status=DataStatus.CONFIRMED)
        for i, v in enumerate([10, 11, 12])
    ]
    assert _span_label(weekly) == "近3周"

    assert _window_change(_points([4, 3, 2])) == "▼50.0%"
    assert _window_change(_points([1, -1])) == "转负"
    assert _window_change(_points([7])) == ""


def test_stale_series_and_failed_collector_are_flagged():
    snap = _snapshot(
        [_series("s.old", "旧序列", "点", [1, 2], stale=True, end=NOW - timedelta(days=30))],
        collectors=[CollectorHealth(id="eia", ok=False, error="boom")],
    )
    card = build_digest_card(snap, now=NOW)

    body = _div_text(card)
    assert "⚠️" in body
    assert "最后 2026-07-11" in body
    assert "采集器失败" in body and "eia（boom）" in body

    footer = next(e for e in card["elements"] if e["tag"] == "note")
    assert "1 条序列采集中断" in footer["elements"][0]["content"]
    assert card["header"]["template"] == "orange"


def test_button_only_when_site_url_is_configured():
    snap = _snapshot([_series("s", "序列", "点", [1, 2])])

    assert not any(e.get("tag") == "action" for e in build_digest_card(snap, now=NOW)["elements"])

    card = build_digest_card(snap, now=NOW, site_url="https://example.pages.dev/")
    action = next(e for e in card["elements"] if e.get("tag") == "action")
    button = action["actions"][0]
    assert button["url"] == "https://example.pages.dev/"
    assert button["text"]["content"] == "打开看板"


def test_empty_snapshot_still_builds_a_card():
    card = build_digest_card(_snapshot([], collectors=[]), now=NOW)
    assert "暂无数据" in _div_text(card)
    note = next(e for e in card["elements"] if e["tag"] == "note")
    assert "采集器 0/0 正常" in note["elements"][0]["content"]
