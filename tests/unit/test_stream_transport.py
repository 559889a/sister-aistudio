"""Unit tests for XHRStreamTransport and CDP native stream binding."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
    build_hooked_request_args,
    build_streaming_init_args,
)
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


def test_request_args_carry_no_generation_timeout():
    """接口契约：传输参数里不许出现任何生成等待时长（看门狗红线，别加回来）。"""
    streaming_args = build_streaming_init_args(
        url="http://x", headers={}, body="{}", rid="r1"
    )
    hooked_args = build_hooked_request_args(url="http://x", headers={}, body="{}")
    for args in (streaming_args, hooked_args):
        assert not [key for key in args if "timeout" in key.lower()]


def test_hooked_request_gzips_large_body_after_signing():
    """>96KB 上传体走 gzip：base64 解压后与原文逐字节一致，并带上 Content-Encoding。

    binding/WAA 已按原文签完，这里只压最终传输体；流式参数复用同一份构造。
    """
    body = json.dumps(["models/gemini-3.8-flash", "A" * 200_000])
    args = build_hooked_request_args(url="http://x", headers={"X-A": "1"}, body=body)
    assert args["gzip"] is True
    assert args["headers"] == {"X-A": "1", "Content-Encoding": "gzip"}
    assert gzip.decompress(base64.b64decode(str(args["body"]))).decode("utf-8") == body
    assert len(str(args["body"])) < len(body) / 10

    streaming_args = build_streaming_init_args(
        url="http://x", headers={"X-A": "1"}, body=body, rid="r1"
    )
    assert streaming_args["gzip"] is True
    assert streaming_args["rid"] == "r1"


def test_hooked_request_keeps_small_body_and_preset_encoding():
    """小体不压；调用方已声明非 identity 的 Content-Encoding 时不重复压。"""
    small = build_hooked_request_args(url="http://x", headers={}, body="{}")
    assert small == {"url": "http://x", "headers": {}, "body": "{}"}

    preset = build_hooked_request_args(
        url="http://x", headers={"content-encoding": "br"}, body="B" * 200_000
    )
    assert "gzip" not in preset
    assert preset["headers"] == {"content-encoding": "br"}
    assert preset["body"] == "B" * 200_000


@pytest.mark.asyncio
async def test_transport_native_cdp_binding_stream_push():
    """Verify realtime stream event dispatch via CDP Runtime.bindingCalled."""
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    binding_listener = None

    def mock_on_binding(name: str, callback):
        nonlocal binding_listener
        if name == "__aistudio_stream_push__":
            binding_listener = callback

    page.on_binding = MagicMock(side_effect=mock_on_binding)
    page.add_binding = AsyncMock()

    captured_rid = None

    async def fake_evaluate(expr, args=None, *a, **kw):
        nonlocal captured_rid
        if isinstance(args, dict) and "rid" in args:
            captured_rid = args["rid"]
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    async def push_events():
        while captured_rid is None or binding_listener is None:
            await asyncio.sleep(0.01)

        binding_listener(
            json.dumps({"rid": captured_rid, "type": "status", "status": 200})
        )
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "chunk", "text": "Hello "})
        )
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "chunk", "text": "CDP Binding!"})
        )
        binding_listener(json.dumps({"rid": captured_rid, "type": "done"}))

    push_task = asyncio.create_task(push_events())

    collected = []
    async for tag, payload in transport.send_streaming_request(
        page,
        url="http://test.com",
        headers={},
        body="{}",
    ):
        collected.append((tag, payload))

    await push_task

    assert ("status", 200) in collected
    assert ("chunk", b"Hello ") in collected
    assert ("chunk", b"CDP Binding!") in collected


@pytest.mark.asyncio
async def test_transport_stream_never_fails_just_because_time_passed():
    """没有生成等待时长看门狗：上游静默很久也不会自己失败，只等 liveness。"""
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    binding_listener = None

    def mock_on_binding(name: str, callback):
        nonlocal binding_listener
        if name == "__aistudio_stream_push__":
            binding_listener = callback

    page.on_binding = MagicMock(side_effect=mock_on_binding)
    page.add_binding = AsyncMock()
    page.is_closed = MagicMock(return_value=False)

    captured_rid = None

    async def fake_evaluate(expr, args=None, *a, **kw):
        nonlocal captured_rid
        if isinstance(args, dict) and "rid" in args:
            captured_rid = args["rid"]
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    async def push_after_long_silence():
        while captured_rid is None or binding_listener is None:
            await asyncio.sleep(0.01)
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "status", "status": 200})
        )
        # 超过旧实现里最短的 1.2s 看门狗很多倍：现在必须继续等而不是报超时
        await asyncio.sleep(1.2)
        binding_listener(
            json.dumps({"rid": captured_rid, "type": "chunk", "text": "late"})
        )
        binding_listener(json.dumps({"rid": captured_rid, "type": "done"}))

    push_task = asyncio.create_task(push_after_long_silence())

    collected = []
    async for tag, payload in transport.send_streaming_request(
        page,
        url="http://test.com",
        headers={},
        body="{}",
    ):
        collected.append((tag, payload))

    await push_task

    assert ("status", 200) in collected
    assert ("chunk", b"late") in collected


@pytest.mark.asyncio
async def test_transport_stream_fails_when_page_closed():
    """页面/浏览器真的没了仍然要失败（基础设施失败，不是时间到期）。"""
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    page.on_binding = MagicMock()
    page.add_binding = AsyncMock()
    page.is_closed = MagicMock(return_value=False)

    async def fake_evaluate(expr, args=None, *a, **kw):
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    async def close_page_soon():
        await asyncio.sleep(0.05)
        page.is_closed = MagicMock(return_value=True)

    close_task = asyncio.create_task(close_page_soon())
    with pytest.raises(RuntimeError, match="Browser page closed during streaming"):
        async for _ in transport.send_streaming_request(
            page,
            url="http://test.com",
            headers={},
            body="{}",
        ):
            pass
    await close_task


@pytest.mark.asyncio
async def test_replay_failure_logs_cdp_net_layer_evidence(caplog):
    """页面 fetch 失败时要把 Network.loadingFailed 的 errorText 打进日志（断连归因）。"""
    from aistudio_api.domain.errors import BrowserTransportError

    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    # spec 会给未赋值属性生成 Mock（恒真），必须显式清掉哨兵
    page.has_net_trace = False
    page.rpc_net_requests = {}
    page.rpc_net_failures = []
    req_listener = None
    fail_listener = None

    def fake_on_request(cb):
        nonlocal req_listener
        req_listener = cb
        return lambda: None

    def fake_on_loading_failed(cb):
        nonlocal fail_listener
        fail_listener = cb
        return lambda: None

    page.on_request = MagicMock(side_effect=fake_on_request)
    page.on_response = MagicMock(side_effect=lambda cb: (lambda: None))
    page.on_loading_failed = MagicMock(side_effect=fake_on_loading_failed)

    async def fake_evaluate(expr, args=None, *a, **kw):
        # 模拟时序：CDP 先报网络层失败（连接被重置），页面 fetch 才 reject
        if req_listener is not None and fail_listener is not None:
            req_listener(
                {
                    "request_id": "r1",
                    "url": "https://alkalimakersuite-pa.clients6.google.com/$rpc/GenerateContent",
                    "method": "POST",
                    "headers": {},
                    "post_data": "",
                }
            )
            fail_listener(
                {
                    "requestId": "r1",
                    "errorText": "net::ERR_CONNECTION_RESET",
                    "canceled": False,
                    "type": "Fetch",
                }
            )
        return {"status": 0, "body": "TypeError: Failed to fetch"}

    page.evaluate = AsyncMock(side_effect=fake_evaluate)

    with (
        caplog.at_level("WARNING", logger="aistudio.transport"),
        pytest.raises(BrowserTransportError),
    ):
        await transport.send_hooked_request(
            page,
            url="https://alkalimakersuite-pa.clients6.google.com/$rpc/GenerateContent",
            headers={},
            body="[]",
        )

    assert "net::ERR_CONNECTION_RESET" in caplog.text
    # 证据里带响应头状态（none = 连响应头都没收到）
    assert "resp=none" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("init failed"), asyncio.CancelledError()])
async def test_stream_init_failure_removes_queue_and_preserves_original_error(monkeypatch, failure):
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.transport._ensure_authorization_header",
        AsyncMock(return_value={}),
    )
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    page.on_binding = MagicMock()
    page.add_binding = AsyncMock()
    initialized = []
    cleanup = []

    async def evaluate(expression, args=None, **_kwargs):
        if expression == STREAMING_INIT_JS:
            initialized.append(args["rid"])
            raise failure
        if expression == STREAM_CLEANUP_JS:
            cleanup.append(args)
            raise RuntimeError("cleanup failed too")

    page.evaluate = AsyncMock(side_effect=evaluate)
    source = transport.send_streaming_request(page, url="http://x", headers={}, body="[]")
    with pytest.raises(type(failure)) as caught:
        await anext(source)
    assert caught.value is failure
    assert transport.in_flight_count == 0
    assert cleanup == initialized


@pytest.mark.asyncio
async def test_cancellation_during_stream_init_cleans_queue_and_browser_request(monkeypatch):
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.transport._ensure_authorization_header",
        AsyncMock(return_value={}),
    )
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    page.on_binding = MagicMock()
    page.add_binding = AsyncMock()
    started = asyncio.Event()
    cleanup = []

    async def evaluate(expression, args=None, **_kwargs):
        if expression == STREAMING_INIT_JS:
            started.set()
            await asyncio.Event().wait()
        else:
            cleanup.append(args)

    page.evaluate = AsyncMock(side_effect=evaluate)
    source = transport.send_streaming_request(page, url="http://x", headers={}, body="[]")
    task = asyncio.create_task(anext(source))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.in_flight_count == 0
    assert len(cleanup) == 1


@pytest.mark.asyncio
async def test_closing_stream_after_status_cleans_browser_and_queue(monkeypatch):
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.transport._ensure_authorization_header",
        AsyncMock(return_value={}),
    )
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    page.has_stream_binding = False
    page.is_closed.return_value = False
    page.add_binding = AsyncMock()
    listener = None
    cleanup = []

    def on_binding(_name, callback):
        nonlocal listener
        listener = callback

    async def evaluate(expression, args=None, **_kwargs):
        if expression == STREAMING_INIT_JS:
            listener(json.dumps({"rid": args["rid"], "type": "status", "status": 200}))
        else:
            cleanup.append(args)

    page.on_binding.side_effect = on_binding
    page.evaluate = AsyncMock(side_effect=evaluate)
    source = transport.send_streaming_request(page, url="http://x", headers={}, body="[]")
    assert await anext(source) == ("status", 200)
    assert transport.in_flight_count == 1
    await source.aclose()
    assert transport.in_flight_count == 0
    assert len(cleanup) == 1


@pytest.mark.asyncio
async def test_stream_init_transport_error_keeps_cause_despite_cleanup_cancellation(monkeypatch):
    from aistudio_api.domain.errors import BrowserTransportError

    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.transport._ensure_authorization_header",
        AsyncMock(return_value={}),
    )
    transport = XHRStreamTransport()
    page = MagicMock(spec=CDPPage)
    page.on_binding = MagicMock()
    page.add_binding = AsyncMock()
    failure = RuntimeError("original CDP error")

    async def evaluate(expression, args=None, **_kwargs):
        if expression == STREAMING_INIT_JS:
            raise failure
        raise asyncio.CancelledError

    page.evaluate = AsyncMock(side_effect=evaluate)
    with pytest.raises(BrowserTransportError, match="original CDP error") as caught:
        await anext(transport.send_streaming_request(page, url="http://x", headers={}, body="[]"))
    assert caught.value.__cause__ is failure
    assert transport.in_flight_count == 0
