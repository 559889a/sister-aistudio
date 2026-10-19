"""OpenAI 兼容端点路由单测（非流式 / 流式 SSE / models / 错误体 / 覆写生效）。"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.dependencies import get_client
from aistudio_api.application import runtime_settings as settings_module
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _patch_settings(monkeypatch, **overrides) -> None:
    """把运行期设置换成一份内存值（不写 data/settings.json）。"""
    values = dict(settings_module.DEFAULTS)
    values.update(overrides)
    monkeypatch.setattr(
        settings_module.runtime_settings, "values", lambda: dict(values)
    )


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=AIStudioClient)
    app.dependency_overrides[get_client] = lambda: client
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_client, None)


def _body(**overrides) -> dict:
    payload = {
        "model": "gemini-3.8-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }
    payload.update(overrides)
    return payload


def _parse_sse(text: str) -> list[dict]:
    payloads: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :]
        if raw == "[DONE]":
            continue
        payloads.append(json.loads(raw))
    return payloads


# ────────────────────────────── 非流式 ──────────────────────────────


@pytest.mark.asyncio
async def test_chat_completions_non_stream(mock_client: MagicMock):
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="Hello!", thinking="hidden", finish_reason=1)],
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "completion_tokens_details": {
                    "reasoning_tokens": 5,
                    "visible_tokens": 15,
                },
            },
        )
    )
    async with _http_client() as client:
        response = await client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "gemini-3.8-flash"
    assert data["id"].startswith("chatcmpl-")
    choice = data["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "Hello!"
    assert choice["message"]["reasoning_content"] == "hidden"
    assert choice["finish_reason"] == "stop"
    assert data["usage"]["prompt_tokens"] == 10
    assert data["usage"]["completion_tokens"] == 20
    assert data["usage"]["completion_tokens_details"] == {"reasoning_tokens": 5}


@pytest.mark.asyncio
async def test_chat_completions_returns_tool_calls(mock_client: MagicMock):
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[
                Candidate(
                    finish_reason=1,
                    function_calls=[
                        {"name": "get_weather", "args": {"city": "SF"}, "call_id": "c1"}
                    ],
                )
            ]
        )
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json=_body(
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "get_weather", "parameters": {}},
                    }
                ]
            ),
        )

    assert response.status_code == 200
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["id"] == "c1"
    assert call["function"]["name"] == "get_weather"
    # 工具声明确实被送到了上游
    kwargs = mock_client.generate_content.call_args.kwargs
    assert kwargs["tools"] is not None


@pytest.mark.asyncio
async def test_include_thoughts_false_drops_reasoning_content(
    mock_client: MagicMock, monkeypatch
):
    _patch_settings(monkeypatch, includeThoughts=False)
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="answer", thinking="thought", finish_reason=1)]
        )
    )
    async with _http_client() as client:
        response = await client.post("/v1/chat/completions", json=_body())

    message = response.json()["choices"][0]["message"]
    assert message["content"] == "answer"
    assert "reasoning_content" not in message
    # 请求侧也降到了最低档（3.8-flash 实测最低是 LOW；MINIMAL 会被上游 400）
    kwargs = mock_client.generate_content.call_args.kwargs
    assert kwargs["generation_config_overrides"]["thinking_config"] == [
        1,
        None,
        None,
        1,
    ]


@pytest.mark.asyncio
async def test_drop_max_tokens_drops_client_limit(
    mock_client: MagicMock, monkeypatch
):
    _patch_settings(monkeypatch, dropMaxTokens=True)
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(candidates=[Candidate(text="ok", finish_reason=1)])
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body(max_tokens=64)
        )

    assert response.status_code == 200
    kwargs = mock_client.generate_content.call_args.kwargs
    assert kwargs["max_tokens"] is None
    assert "max_tokens" not in kwargs["generation_config_overrides"]


@pytest.mark.asyncio
async def test_max_output_tokens_override(mock_client: MagicMock, monkeypatch):
    _patch_settings(monkeypatch, maxOutputTokens=512)
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(candidates=[Candidate(text="ok", finish_reason=1)])
    )
    async with _http_client() as client:
        await client.post("/v1/chat/completions", json=_body(max_tokens=64))

    kwargs = mock_client.generate_content.call_args.kwargs
    assert kwargs["max_tokens"] == 512
    assert kwargs["generation_config_overrides"]["max_tokens"] == 512


@pytest.mark.asyncio
async def test_temperature_override_reaches_upstream(mock_client: MagicMock, monkeypatch):
    _patch_settings(monkeypatch, temperature=0.25, topP=0.5, topK=200)
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(candidates=[Candidate(text="ok", finish_reason=1)])
    )
    async with _http_client() as client:
        await client.post(
            "/v1/chat/completions",
            json=_body(
                model="gemini-3-pro-preview", temperature=0.9, top_k=8
            ),
        )

    kwargs = mock_client.generate_content.call_args.kwargs
    assert kwargs["temperature"] == 0.25
    assert kwargs["top_p"] == 0.5
    assert kwargs["top_k"] == 63


@pytest.mark.asyncio
async def test_upstream_error_returns_openai_error_shape(mock_client: MagicMock):
    mock_client.generate_content = AsyncMock(side_effect=RequestError(500, "boom"))
    async with _http_client() as client:
        response = await client.post("/v1/chat/completions", json=_body())

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == 500
    assert error["type"] == "server_error"
    assert "boom" in error["message"]


@pytest.mark.asyncio
async def test_invalid_request_body_returns_400_openai_shape(mock_client: MagicMock):
    async with _http_client() as client:
        response = await client.post("/v1/chat/completions", json={"model": "m"})

    assert response.status_code == 400
    assert "error" in response.json()
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_empty_messages_returns_400(mock_client: MagicMock):
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions", json={"model": "m", "messages": []}
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


# ────────────────────────────── 流式 ──────────────────────────────


def _make_stream(events: list[tuple[str, object]]):
    async def fake_stream(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        for event in events:
            yield event

    return fake_stream


@pytest.mark.asyncio
async def test_chat_completions_stream_frames_and_done(mock_client: MagicMock):
    mock_client.stream_generate_content = _make_stream(
        [
            ("prepared", None),
            ("body", "Hel"),
            ("thinking", "hmm"),
            ("body", "lo"),
            ("thought_signature", "sig"),
            ("finish_reason", 1),
            (
                "usage",
                {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            ),
            ("done", None),
        ]
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body(stream=True)
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    text = response.text
    assert text.rstrip().endswith("data: [DONE]")
    payloads = _parse_sse(text)

    contents = "".join(
        frame["choices"][0]["delta"].get("content", "")
        for frame in payloads
        if frame["choices"]
    )
    assert contents == "Hello"
    reasoning = "".join(
        frame["choices"][0]["delta"].get("reasoning_content", "")
        for frame in payloads
        if frame["choices"]
    )
    assert reasoning == "hmm"
    finish_frames = [
        frame
        for frame in payloads
        if frame["choices"] and frame["choices"][0]["finish_reason"] is not None
    ]
    assert finish_frames[-1]["choices"][0]["finish_reason"] == "stop"
    # usage 挂在收尾帧
    assert finish_frames[-1]["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_chat_completions_stream_without_reasoning(
    mock_client: MagicMock, monkeypatch
):
    _patch_settings(monkeypatch, includeThoughts=False)
    mock_client.stream_generate_content = _make_stream(
        [("thinking", "hmm"), ("body", "visible"), ("finish_reason", 1), ("done", None)]
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body(stream=True)
        )

    payloads = _parse_sse(response.text)
    assert all(
        "reasoning_content" not in frame["choices"][0]["delta"]
        for frame in payloads
        if frame["choices"]
    )
    contents = "".join(
        frame["choices"][0]["delta"].get("content", "")
        for frame in payloads
        if frame["choices"]
    )
    assert contents == "visible"


@pytest.mark.asyncio
async def test_chat_completions_stream_include_usage(mock_client: MagicMock):
    mock_client.stream_generate_content = _make_stream(
        [
            ("body", "hi"),
            ("finish_reason", 1),
            (
                "usage",
                {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            ),
            ("done", None),
        ]
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions",
            json=_body(stream=True, stream_options={"include_usage": True}),
        )

    payloads = _parse_sse(response.text)
    usage_chunks = [frame for frame in payloads if frame["choices"] == []]
    assert len(usage_chunks) == 1
    assert usage_chunks[0]["usage"]["total_tokens"] == 3


@pytest.mark.asyncio
async def test_chat_completions_stream_tool_calls(mock_client: MagicMock):
    mock_client.stream_generate_content = _make_stream(
        [
            ("tool_calls", [{"name": "f", "args": {"a": 1}}]),
            ("tool_calls", [{"name": "f", "args": {"a": 1, "b": 2}}]),
            ("thought_signature", "sig-x"),
            ("finish_reason", 1),
            ("done", None),
        ]
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body(stream=True)
        )

    payloads = _parse_sse(response.text)
    tool_frames = [
        frame
        for frame in payloads
        if frame["choices"] and "tool_calls" in frame["choices"][0]["delta"]
    ]
    assert len(tool_frames) == 1
    call = tool_frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["function"]["name"] == "f"
    assert json.loads(call["function"]["arguments"]) == {"a": 1, "b": 2}
    assert call["extra_content"] == {"google": {"thought_signature": "sig-x"}}
    finish = [
        frame
        for frame in payloads
        if frame["choices"] and frame["choices"][0]["finish_reason"] is not None
    ][-1]
    assert finish["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.asyncio
async def test_chat_completions_stream_error_frame(mock_client: MagicMock):
    async def failing_stream(
        *_args: object, **_kwargs: object
    ) -> AsyncGenerator[tuple[str, object], None]:
        yield ("body", "partial")
        raise RequestError(500, "upstream exploded")

    mock_client.stream_generate_content = failing_stream
    async with _http_client() as client:
        response = await client.post(
            "/v1/chat/completions", json=_body(stream=True)
        )

    text = response.text
    assert text.rstrip().endswith("data: [DONE]")
    payloads = _parse_sse(text)
    errors = [frame["error"] for frame in payloads if "error" in frame]
    assert errors
    assert errors[0]["code"] == 500
    assert "upstream exploded" in errors[0]["message"]


# ────────────────────────────── 两条端口行为一致 ──────────────────────────────


@pytest.mark.asyncio
async def test_gemini_port_uses_same_overrides(mock_client: MagicMock, monkeypatch):
    """includeThoughts=false 时 Gemini 原生端口也丢弃 thought parts、请求侧降到最低档。"""
    _patch_settings(monkeypatch, includeThoughts=False)
    mock_client.generate_content = AsyncMock(
        return_value=ModelOutput(
            candidates=[Candidate(text="answer", thinking="thought", finish_reason=1)]
        )
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1beta/models/gemini-3.8-flash:generateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert response.status_code == 200
    parts = response.json()["candidates"][0]["content"]["parts"]
    assert [part["text"] for part in parts] == ["answer"]
    assert all(not part.get("thought") for part in parts)
    kwargs = mock_client.generate_content.call_args.kwargs
    # 3.8-flash 实测最低档是 LOW（MINIMAL 会被上游 400），includeThoughts=false 降到它
    assert kwargs["generation_config_overrides"]["thinking_config"] == [
        1,
        None,
        None,
        1,
    ]


@pytest.mark.asyncio
async def test_gemini_port_stream_drops_thinking(mock_client: MagicMock, monkeypatch):
    _patch_settings(monkeypatch, includeThoughts=False)
    mock_client.stream_generate_content = _make_stream(
        [("thinking", "hidden"), ("body", "visible"), ("finish_reason", 1), ("done", None)]
    )
    async with _http_client() as client:
        response = await client.post(
            "/v1beta/models/gemini-3.8-flash:streamGenerateContent",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    payloads = _parse_sse(response.text)
    all_parts = [
        part
        for frame in payloads
        for candidate in frame.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    ]
    assert all_parts == [{"text": "visible"}]


# ────────────────────────────── models / 鉴权 ──────────────────────────────


@pytest.mark.asyncio
async def test_list_models_openai_shape(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset())
    async with _http_client() as client:
        response = await client.get("/v1/models")

    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    assert data["data"]
    for item in data["data"]:
        assert not item["id"].startswith("models/")
        assert item["object"] == "model"
        assert item["owned_by"] == "google"


@pytest.mark.asyncio
async def test_chat_completions_requires_api_key(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", frozenset({"secret"}))
    async with _http_client() as client:
        unauthorized = await client.post("/v1/chat/completions", json=_body())
    assert unauthorized.status_code == 401
    error = unauthorized.json()["error"]
    assert error["code"] == 401
    assert error["type"] == "authentication_error"
