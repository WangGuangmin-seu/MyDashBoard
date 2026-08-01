"""Feishu signature (spec §7.2) and response-code checking (spec §7.4)."""

import base64
import hashlib
import hmac

import pytest
import respx
from httpx import Response

from dashboard.contract import DataStatus
from dashboard.notify.feishu import FeishuNotifier, gen_sign
from dashboard.pipeline.alerts import Alert


def test_gen_sign_matches_reference():
    secret, ts = "abc123", 1700000000
    got = gen_sign(secret, ts)
    # reference impl: key = "{ts}\n{secret}", empty message, HMAC-SHA256, base64
    expected = base64.b64encode(
        hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert got == expected


def _alert():
    return Alert(rule_id="r", kind="threshold", template="red", severity="critical",
                 series_id="s", message="boom", value=1.0, status=DataStatus.CONFIRMED)


@respx.mock
async def test_send_raises_on_nonzero_code_despite_http_200():
    # Feishu can reject a message yet return HTTP 200 (spec §7.4).
    respx.post("https://example.com/hook").mock(
        return_value=Response(200, json={"code": 19021, "msg": "sign match fail"})
    )
    notifier = FeishuNotifier("https://example.com/hook", secret="s")
    with pytest.raises(RuntimeError, match="19021"):
        await notifier.send(_alert())


@respx.mock
async def test_send_ok_on_code_zero():
    respx.post("https://example.com/hook").mock(return_value=Response(200, json={"code": 0}))
    notifier = FeishuNotifier("https://example.com/hook")
    await notifier.send(_alert())  # should not raise
