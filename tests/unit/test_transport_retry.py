"""浏览器页面传输失败（Failed to fetch 这类网络错误）的归类与重试口径。

背景（2026-10-19 实机）：一个 50 条消息、232 KB 的流式请求在浏览器页面里跑了 65 秒后
`streaming request failed: Failed to fetch`。这是页面 fetch 的连接层失败，既不是账号问题
也不是上游返回的 HTTP 错误；旧实现把它落进「未知严重异常 → 500」，客户端只看到 500。

口径（与主项目 `executor.js` 的「流中断 retryable」一致）：
1. 传输层失败统一抛 `BrowserTransportError`；
2. 流式**一个字节正文都没吐**时清模板缓存、原地重试（页面死了就重建会话）；
3. 已经吐过数据、或重试耗尽 → 502（`UNAVAILABLE`），不是 500；
4. 绝不因为传输失败去切账号（账号本身没坏）。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.application import api_service_gemini as svc
from aistudio_api.domain.errors import BrowserTransportError
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.gateway.transport import XHRStreamTransport


def _make_page() -> tuple[MagicMock, dict[str, Any]]:
    """假 CDPPage：记录 evaluate 的 rid，按需从 binding 推事件。"""
    page = MagicMock(spec=CDPPage)
    page.is_closed = MagicMock(return_value=False)
    state: dict[str, Any] = {"rid": None, "listener": None}

    def on_binding(name: str, callback):
        if name == "__aistudio_stream_push__":
            state["listener"] = callback

    page.on_binding = MagicMock(side_effect=on_binding)
    page.add_binding = AsyncMock()

    async def fake_evaluate(_expr, args=None, *a, **kw):
        if isinstance(args, dict) and "rid" in args:
            state["rid"] = args["rid"]
        return

    page.evaluate = AsyncMock(side_effect=fake_evaluate)
    return page, state


@pytest.mark.asyncio
async def test_stream_error_event_raises_browser_transport_error():
    """页面 fetch 失败（Failed to fetch）必须是 BrowserTransportError，不是普通 RuntimeError。"""
    transport = XHRStreamTransport()
    page, state = _make_page()

    async def push_failure():
        while state["rid"] is None or state["listener"] is None:
            import asyncio

            await asyncio.sleep(0.01)
        state["listener"](
            json.dumps({"rid": state["rid"], "type": "status", "status": 200})
        )
        state["listener"](
            json.dumps(
                {"rid": state["rid"], "type": "error", "message": "Failed to fetch"}
            )
        )

    import asyncio

    task = asyncio.create_task(push_failure())
    with pytest.raises(BrowserTransportError, match="Failed to fetch"):
        async for _ in transport.send_streaming_request(
            page, url="https://example.com/generate", headers={}, body="{}"
        ):
            pass
    await task


@pytest.mark.asyncio
async def test_closed_page_raises_browser_transport_error():
    transport = XHRStreamTransport()
    page, _state = _make_page()
    page.is_closed = MagicMock(return_value=True)

    with pytest.raises(BrowserTransportError, match="page closed"):
        async for _ in transport.send_streaming_request(
            page, url="https://example.com/generate", headers={}, body="{}"
        ):
            pass


@pytest.mark.asyncio
async def test_hooked_request_network_failure_is_transport_error():
    """非流式整段生成：页面返回 status=0 / Failed to fetch 也归到传输层失败。"""
    transport = XHRStreamTransport()
    page, _state = _make_page()
    page.evaluate = AsyncMock(return_value={"status": 0, "body": "Failed to fetch"})

    with pytest.raises(BrowserTransportError, match="replay failed"):
        await transport.send_hooked_request(
            page, url="https://example.com/generate", headers={}, body="{}"
        )


@pytest.mark.asyncio
async def test_transport_error_is_retried_before_first_byte():
    client = MagicMock()
    client._session = None
    client.clear_templates = MagicMock()

    retried = await svc.handle_attempt_exception(
        BrowserTransportError("streaming request failed: Failed to fetch"),
        attempt=0,
        model_path="models/gemini-3.8-flash",
        normalized_model="models/gemini-3.8-flash",
        client=client,
        has_yielded_data=False,
    )

    assert retried is True
    client.clear_templates.assert_called_once()


@pytest.mark.asyncio
async def test_transport_error_after_long_wait_is_502_without_retry():
    """等了很久才失败的页面 fetch 不重试（实测三次重试会把请求拖到 800+ 秒）。"""
    client = MagicMock()
    client._session = None
    client.clear_templates = MagicMock()

    with pytest.raises(HTTPException) as excinfo:
        await svc.handle_attempt_exception(
            BrowserTransportError("streaming request failed: Failed to fetch"),
            attempt=0,
            model_path="models/gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=client,
            has_yielded_data=False,
            attempt_elapsed_s=436.0,
        )

    assert excinfo.value.status_code == 502
    client.clear_templates.assert_not_called()


@pytest.mark.asyncio
async def test_transport_error_after_data_is_502():
    with pytest.raises(HTTPException) as excinfo:
        await svc.handle_attempt_exception(
            BrowserTransportError("streaming request failed: Failed to fetch"),
            attempt=1,
            model_path="models/gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=MagicMock(),
            has_yielded_data=True,
        )
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_transport_error_exhausted_is_502_not_500():
    with pytest.raises(HTTPException) as excinfo:
        await svc.handle_attempt_exception(
            BrowserTransportError("streaming request failed: Failed to fetch"),
            attempt=svc.MAX_RETRIES - 1,
            model_path="models/gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=MagicMock(),
            has_yielded_data=False,
        )
    assert excinfo.value.status_code == 502


def test_classify_maps_502_to_unavailable():
    code, _message, status = svc.classify_gemini_error_payload(
        HTTPException(502, detail={"message": "browser fetch failed"})
    )
    assert code == 502
    assert status == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_stream_raw_events_retries_transport_failure_once(monkeypatch):
    """第一次 Failed to fetch，第二次成功：客户端应拿到正文，而不是 500。"""
    calls = 0

    class _FakeClient:
        _session = None

        def clear_templates(self) -> None:
            pass

        async def stream_generate_content(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BrowserTransportError(
                    "streaming request failed: Failed to fetch"
                )
            yield ("body", "重试成功")
            yield ("finish_reason", 1)

    monkeypatch.setattr(svc, "MAX_RETRIES", 5)
    client = _FakeClient()
    events = [
        event
        async for event in svc.stream_raw_events(
            "models/gemini-3.8-flash",
            svc.GeminiGenerateContentRequest(
                contents=[{"role": "user", "parts": [{"text": "hi"}]}]
            ),
            client,  # type: ignore[arg-type]
        )
    ]
    bodies = [text for kind, text in events if kind == "body"]
    assert bodies == ["重试成功"]
    assert calls == 2
    assert not any(kind == "error" for kind, _ in events)
