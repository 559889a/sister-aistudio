"""google-genai SDK 桥接层（AistudioTransport）的 Phase 1 验证：fake gateway 全链路。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest

from aistudio_api.domain.errors import UsageLimitExceeded
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient


def _output(text: str = "你好，世界") -> ModelOutput:
    return ModelOutput(
        candidates=[Candidate(text=text, finish_reason=1)],
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        model="gemini-3.8-flash",
        response_id="resp_1",
    )


class FakeGateway:
    """AIStudioClient 调用面的鸭子类型替身（不碰 CDP/浏览器）。"""

    def __init__(
        self,
        output: ModelOutput | None = None,
        stream_events: list[tuple[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.output = output
        self.stream_events = stream_events
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> ModelOutput:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.output is not None
        return self.output

    async def stream_generate_content(self, **kwargs: Any) -> AsyncIterator[tuple[str, Any]]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        for event in self.stream_events or []:
            yield event


def _stream_events() -> list[tuple[str, Any]]:
    return [
        ("status", 200),
        ("body", "你好"),
        ("body", "，世界"),
        ("finish_reason", 1),
        ("usage", {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
        ("done", None),
    ]


@pytest.fixture(autouse=True)
def _no_account_scheduling(monkeypatch: pytest.MonkeyPatch):
    """桥接测试不依赖账号调度（generate_content_output 的前置步骤）。"""
    from aistudio_api.application import api_service_gemini

    async def _noop(model: str | None = None) -> None:
        return None

    monkeypatch.setattr(api_service_gemini, "ensure_active_account", _noop)


async def _make_sdk(fake: FakeGateway):
    from aistudio_api.application.sdk_bridge import build_sdk_client

    return build_sdk_client(cast(AIStudioClient, fake))


@pytest.mark.asyncio
async def test_non_stream_roundtrip():
    fake = FakeGateway(output=_output())
    sdk = await _make_sdk(fake)
    resp = await sdk.aio.models.generate_content(model="gemini-3.8-flash", contents="写一句问候")
    assert resp.text == "你好，世界"
    assert resp.usage_metadata is not None and resp.usage_metadata.total_token_count == 7
    assert resp.candidates and resp.candidates[0].finish_reason is not None
    assert str(resp.candidates[0].finish_reason).upper().endswith("STOP")
    # 请求侧：官方 JSON 已被翻译进网关调用面
    assert fake.calls and fake.calls[0]["model"]
    assert fake.calls[0]["contents"]


@pytest.mark.asyncio
async def test_stream_roundtrip():
    fake = FakeGateway(stream_events=_stream_events())
    sdk = await _make_sdk(fake)
    chunks = []
    async for chunk in await sdk.aio.models.generate_content_stream(
        model="gemini-3.8-flash", contents="写一句问候"
    ):
        chunks.append(chunk)
    assert "".join(c.text or "" for c in chunks) == "你好，世界"
    assert chunks[-1].usage_metadata is not None
    assert chunks[-1].usage_metadata.total_token_count == 7


@pytest.mark.asyncio
async def test_error_maps_to_api_error():
    fake = FakeGateway(error=UsageLimitExceeded("配额用完: daily quota"))
    sdk = await _make_sdk(fake)
    from google.genai import errors as genai_errors

    with pytest.raises(genai_errors.APIError) as exc_info:
        await sdk.aio.models.generate_content(model="gemini-3.8-flash", contents="hi")
    assert exc_info.value.code == 429


@pytest.mark.asyncio
async def test_unsupported_endpoint_rejected():
    from aistudio_api.application.sdk_bridge import AistudioTransport

    transport = AistudioTransport(cast(AIStudioClient, FakeGateway()))
    req = httpx.Request("POST", "http://aistudio-bridge.invalid/v1beta/models/m:countTokens")
    resp = await transport.handle_async_request(req)
    assert resp.status_code == 501
    assert b"UNIMPLEMENTED" in resp.content


@pytest.mark.asyncio
async def test_sdk_retry_disabled():
    """SDK 默认重试 5 次必须被关掉（用户零自动重试决策）：错误只打一次网关。"""
    fake = FakeGateway(error=UsageLimitExceeded("配额用完"))
    sdk = await _make_sdk(fake)
    from google.genai import errors as genai_errors

    with pytest.raises(genai_errors.APIError):
        await sdk.aio.models.generate_content(model="gemini-3.8-flash", contents="hi")
    assert len(fake.calls) == 1
