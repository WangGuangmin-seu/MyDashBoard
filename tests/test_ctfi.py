"""CTFI parser: extracts date + 4 series; raises on structure change (spec §4.2)."""

from datetime import datetime, timezone

import pytest

from dashboard.collectors.ctfi import parse_ctfi

# Trimmed but structurally faithful copy of the SSE singleIndex?indexType=ctfi page.
SAMPLE = """
<form></form>
<div class="title2">
    <tr>中国进口原油运价指数 CHINA IMPORT CRUDE OIL TANKER FREIGHT INDEX</tr>
    <tr><br>2026-07-31</tr>
</div>
<table width="880" border="1" class="lb1"><tbody>
  <tr class="csx1"><td class="ts1">航线</td><td>载货量</td><td>船型</td><td>单位</td><td>权重</td><td>本期</td><td>与上期比涨跌</td></tr>
  <tr><td>综合指数</td><td></td><td></td><td>点</td><td>100%</td><td>5167.74</td><td>280.31</td></tr>
  <tr><td>中东湾拉斯坦努拉—中国宁波(CT1)</td><td>270000MT</td><td>VLCC</td><td>点</td><td>60%</td><td>6501.44</td><td>470.01</td></tr>
  <tr><td>WS</td><td>385.67</td><td>27.88</td></tr>
  <tr><td>美元/吨</td><td>77.94</td><td>5.63</td></tr>
  <tr><td>美元/天(标准航速)</td><td>377985</td><td>30199</td></tr>
  <tr><td>美元/天(经济航速)</td><td>367357</td><td>29235</td></tr>
  <tr><td>西非马隆格/杰诺—中国宁波(CT2)</td><td>260000MT</td><td>VLCC</td><td>点</td><td>40%</td><td>3167.20</td><td>-4.22</td></tr>
  <tr><td>WS</td><td>150.00</td><td>-0.20</td></tr>
</tbody></table>
"""


def test_parse_extracts_date_and_four_series():
    observed_at, vals = parse_ctfi(SAMPLE)
    assert observed_at == datetime(2026, 7, 31, tzinfo=timezone.utc)
    assert vals == {
        "ctfi.composite": 5167.74,
        "ctfi.ct1": 385.67,       # CT1 WS, not the 点 sub-index
        "ctfi.ct1.tce": 377985.0,  # standard-speed TCE, not economic-speed
        "ctfi.ct2": 150.0,
    }


def test_ws_and_tce_are_separate_series():
    _, vals = parse_ctfi(SAMPLE)
    assert vals["ctfi.ct1"] != vals["ctfi.ct1.tce"]  # WS rate vs owner TCE (§4.2 note)


def test_missing_series_raises_not_partial():
    # drop the CT2 WS row → structure change → must raise, never return partial
    broken = SAMPLE.replace("<tr><td>WS</td><td>150.00</td><td>-0.20</td></tr>", "")
    with pytest.raises(RuntimeError, match="ctfi.ct2"):
        parse_ctfi(broken)


def test_missing_date_raises():
    broken = SAMPLE.replace("2026-07-31", "N/A")
    with pytest.raises(RuntimeError, match="date"):
        parse_ctfi(broken)
