"""流式 SSE 心跳测试：包装器行为 + 两条端口的真实分帧。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application.stream_heartbeat import (
    HEARTBEAT_EVENT,
    HEARTBEAT_INTERVAL_S,
    stream_with_heartbeat,
)
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.client import AIStudioClient


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=AIStudioClient)
    app.dependency_overrides[get_client] = lambda: client
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_client, None)


def _sse_payloads(text: str) -> list[dict]:
    frames: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :]
        if raw == "[DONE]":
            continue
        frames.append(json.loads(raw))
    return frames


def _is_openai_heartbeat(frame: dict) -> bool:
    choices = frame.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return False
    choice = choices[0]
    return choice.get("delta") == {} and choice.get("finish_reason") is None


async def _slow_stream(*_args, **_kwargs) -> AsyncGenerator[tuple[str, object], None]:
    await asyncio.sleep(2.5)
    yield ("body", "hello")
    yield ("finish_reason", 1)
    yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    yield ("done", None)


# ────────────────────────────── 包装器本身 ──────────────────────────────


def test_heartbeat_interval_is_one_second():
    assert HEARTBEAT_INTERVAL_S == 1.0
    assert HEARTBEAT_EVENT == "__heartbeat__"


@pytest.mark.asyncio
async def test_wrapper_emits_heartbeats_then_real_events_in_order():
    async def source():
        yield ("body", "a")
        await asyncio.sleep(0.2)
        yield ("body", "b")

    events = [
        event
        async for event in stream_with_heartbeat(source(), interval_s=0.05)
    ]
    tags = [tag for tag, _ in events]
    # 真实事件顺序不变、一个不丢
    real = [event for event in events if event[0] != HEARTBEAT_EVENT]
    assert real == [("body", "a"), ("body", "b")]
    # 0.2s 静默 > 0.05s 心跳间隔：中间必须有心跳
    assert tags.count(HEARTBEAT_EVENT) >= 2
    assert tags.index("body") == 0  # 第一条就是真实事件，心跳不抢跑


@pytest.mark.asyncio
async def test_wrapper_fast_source_has_no_heartbeat():
    async def source():
        for index in range(3):
            yield ("body", str(index))

    events = [
        event
        async for event in stream_with_heartbeat(source(), interval_s=1.0)
    ]
    assert [event[0] for event in events] == ["body", "body", "body"]
    assert [event[1] for event in events] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_wrapper_propagates_source_errors():
    async def source():
        yield ("body", "partial")
        raise ValueError("boom")

    collected = []
    with pytest.raises(ValueError, match="boom"):
        async for event in stream_with_heartbeat(source(), interval_s=1.0):
            collected.append(event)
    assert collected == [("body", "partial")]


@pytest.mark.asyncio
async def test_wrapper_cancels_producer_when_consumer_stops():
    closed = asyncio.Event()

    async def source():
        try:
            yield ("body", "first")
            await asyncio.sleep(10)
            yield ("body", "never")
        finally:
            closed.set()

    agen = stream_with_heartbeat(source(), interval_s=0.05)
    first = await agen.__anext__()
    assert first == ("body", "first")
    await agen.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1.0)


# ────────────────────────────── OpenAI 路由 ──────────────────────────────


@pytest.mark.asyncio
async def test_openai_route_heartbeat_before_first_body(mock_client: MagicMock):
    mock_client.stream_generate_content = _slow_stream
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert response.text.rstrip().endswith("data: [DONE]")
    frames = _sse_payloads(response.text)

    heartbeat_indexes = [
        index for index, frame in enumerate(frames) if _is_openai_heartbeat(frame)
    ]
    body_indexes = [
        index
        for index, frame in enumerate(frames)
        if frame.get("choices")
        and frame["choices"][0].get("delta", {}).get("content")
    ]
    assert len(heartbeat_indexes) >= 2
    assert body_indexes
    # 心跳全部在首块正文之前
    assert max(heartbeat_indexes) < min(body_indexes)
    # 心跳帧必须是合法 OpenAI chunk，且不带 usage / role
    for index in heartbeat_indexes:
        frame = frames[index]
        assert frame["object"] == "chat.completion.chunk"
        assert frame["id"].startswith("chatcmpl-")
        assert frame["model"] == "gemini-3.8-flash"
        assert isinstance(frame["created"], int)
        assert "usage" not in frame
        assert "role" not in frame["choices"][0]["delta"]

    contents = "".join(
        frame["choices"][0]["delta"].get("content", "")
        for frame in frames
        if frame.get("choices")
    )
    assert contents == "hello"


# ────────────────────────────── Gemini 原生路由 ──────────────────────────────


@pytest.mark.asyncio
async def test_gemini_route_heartbeat_before_first_body(mock_client: MagicMock):
    mock_client.stream_generate_content = _slow_stream
    async with _http_client() as client:
        response = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert response.status_code == 200
    text = response.text
    ping_count = text.count(": ping\n\n")
    assert ping_count >= 2

    first_body_at = text.find('"text": "hello"')
    assert first_body_at > 0
    # 心跳（注释行）全部在首块正文之前；它们不是 data 帧
    assert text.rfind(": ping\n\n", 0, first_body_at) >= 0
    assert text.count(": ping\n\n", first_body_at) == 0

    payloads = _sse_payloads(text)
    contents = "".join(
        part.get("text", "")
        for frame in payloads
        for candidate in frame.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
        if not part.get("thought")
    )
    assert contents == "hello"


# ────────────────────────────── 错误传播 ──────────────────────────────


async def _failing_stream(*_args, **_kwargs) -> AsyncGenerator[tuple[str, object], None]:
    # 先吐一段再炸：流已经发出去之后的错误要用错误帧收尾，而不是抛成 500
    yield ("body", "partial")
    await asyncio.sleep(0.02)
    raise RequestError(500, "upstream exploded")


@pytest.mark.asyncio
async def test_openai_route_error_after_heartbeat_capable_stream(
    mock_client: MagicMock,
):
    mock_client.stream_generate_content = _failing_stream
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-3.8-flash",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )

    text = response.text
    assert text.rstrip().endswith("data: [DONE]")
    frames = _sse_payloads(text)
    errors = [frame["error"] for frame in frames if "error" in frame]
    assert errors
    assert errors[0]["code"] == 500
    assert "upstream exploded" in errors[0]["message"]


@pytest.mark.asyncio
async def test_gemini_route_error_frame(mock_client: MagicMock):
    mock_client.stream_generate_content = _failing_stream
    async with _http_client() as client:
        response = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    frames = _sse_payloads(response.text)
    errors = [frame["error"] for frame in frames if "error" in frame]
    assert errors
    assert errors[0]["code"] == 500
