"""新上游通道（vertex_express / console_graphql）的转换与调用面测试。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.gateway.wire_types import (
    AistudioContent,
    AistudioPart,
)
from aistudio_api.infrastructure.upstreams.common import (
    contents_to_official,
    finish_name_to_wire,
    official_to_model_output,
    wire_safety_to_official,
    wire_schema_to_official,
    wire_tools_to_official,
)


def _contents() -> list[AistudioContent]:
    return [
        AistudioContent(
            role="user",
            parts=[
                AistudioPart(text="你好"),
                AistudioPart(inline_data=("image/png", "aGk=")),
                AistudioPart(function_call=("get_weather", {"city": "北京"})),
            ],
        )
    ]


def test_contents_to_official_round_shape():
    official = contents_to_official(_contents())
    assert official[0]["role"] == "user"
    parts = official[0]["parts"]
    assert parts[0] == {"text": "你好"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": "aGk="}}
    assert parts[2] == {"functionCall": {"name": "get_weather", "args": {"city": "北京"}}}


def test_wire_schema_round_trip():
    # 与 chat_service.encode_schema_to_wire 的正向输出对拍（手写 wire 形状）
    wire = [
        6,  # OBJECT
        None,
        None,
        None,
        None,
        None,
        [["city", [1]], ["days", [3]]],  # properties
        ["city"],  # required
    ]
    official = wire_schema_to_official(wire)
    assert official["type"] == "OBJECT"
    assert official["properties"]["city"] == {"type": "STRING"}
    assert official["properties"]["days"] == {"type": "INTEGER"}
    assert official["required"] == ["city"]
    # 数组 + 枚举
    arr = [5, None, None, None, ["a", "b"], [1]]
    assert wire_schema_to_official(arr)["items"] == {"type": "STRING"}


def test_wire_tools_and_safety_conversion():
    tools = [[["get_weather", "查天气", [6, None, None, None, None, None, [["city", [1]]], ["city"]]]]]
    official = wire_tools_to_official(tools)
    decl = official[0]["functionDeclarations"][0]
    assert decl["name"] == "get_weather"
    assert decl["description"] == "查天气"
    assert decl["parameters"]["required"] == ["city"]
    safety = wire_safety_to_official([[None, None, 8, 4]])
    assert safety == [{"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"}]


def test_finish_name_to_wire():
    assert finish_name_to_wire("STOP") == 1
    assert finish_name_to_wire("MAX_TOKENS") == 2
    assert finish_name_to_wire("FINISH_REASON_UNSPECIFIED") is None
    assert finish_name_to_wire(None) is None


def test_official_to_model_output():
    data = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "思考中", "thought": True},
                        {"text": "北京晴"},
                        {"functionCall": {"name": "f", "args": {"x": 1}}},
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5, "totalTokenCount": 8},
        "responseId": "resp_1",
    }
    out = official_to_model_output(data, "gemini-3.8-flash")
    assert out.text == "北京晴"
    assert out.thinking == "思考中"
    assert out.function_calls == [{"name": "f", "args": {"x": 1}}]
    assert out.candidates[0].finish_reason == 1
    assert out.usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert out.response_id == "resp_1"


# ── Vertex Express 通道 ──


class _FakeSDKModels:
    def __init__(self, resp: Any = None, error: Exception | None = None, chunks: list | None = None):
        self._resp = resp
        self._error = error
        self._chunks = chunks
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return self._resp

    async def generate_content_stream(self, **kwargs):
        self.calls.append(kwargs)

        class _Stream:
            def __init__(self, chunks, error):
                self._chunks = chunks or []
                self._error = error

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._error:
                    raise self._error
                if not self._chunks:
                    raise StopAsyncIteration
                return self._chunks.pop(0)

        return _Stream(list(self._chunks), self._error)


class _FakeSDKClient:
    def __init__(self, models: _FakeSDKModels):
        class _Aio:
            pass

        aio = _Aio()
        aio.models = models
        self.aio = aio


class _FakeResponse:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self, **_kwargs):
        return dict(self._data)


def _make_express_client(fake: _FakeSDKClient):
    from aistudio_api.infrastructure.upstreams.vertex_express import VertexExpressClient

    client = object.__new__(VertexExpressClient)
    client._client = fake
    client._project_id = "proj-1"
    client._location = "us-central1"
    client._session = None
    return client


@pytest.mark.asyncio
async def test_express_generate_content_and_model_path():
    fake = _FakeSDKModels(
        resp=_FakeResponse(
            {
                "candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
            }
        )
    )
    client = _make_express_client(_FakeSDKClient(fake))
    out = await client.generate_content(
        model="gemini-3.8-flash",
        capture_prompt="p",
        contents=_contents(),
        temperature=0.7,
        top_p=0.9,
        top_k=None,
        max_tokens=100,
    )
    assert out.text == "hi"
    call = fake.calls[0]
    # 钉定 location：完整资源路径
    assert call["model"] == "projects/proj-1/locations/us-central1/publishers/google/models/gemini-3.8-flash"
    assert call["config"]["temperature"] == 0.7
    assert call["config"]["max_output_tokens"] == 100


@pytest.mark.asyncio
async def test_express_error_passthrough_no_retry():
    from google.genai import errors as genai_errors

    err = genai_errors.APIError(429, "quota exceeded", None)
    fake = _FakeSDKModels(error=err)
    client = _make_express_client(_FakeSDKClient(fake))
    with pytest.raises(RequestError) as exc_info:
        await client.generate_content(model="m", capture_prompt="p", contents=_contents())
    assert exc_info.value.status == 429
    assert len(fake.calls) == 1  # 零重试：单次调用


@pytest.mark.asyncio
async def test_express_stream_events():
    fake = _FakeSDKModels(
        chunks=[
            _FakeResponse({"candidates": [{"content": {"parts": [{"text": "你"}]}}]}),
            _FakeResponse(
                {
                    "candidates": [{"content": {"parts": [{"text": "好"}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
                }
            ),
        ]
    )
    client = _make_express_client(_FakeSDKClient(fake))
    events = [e async for e in client.stream_generate_content(model="m", capture_prompt="p", contents=_contents())]
    types = [e[0] for e in events]
    assert types[0] == "status"
    assert ("body", "你") in events and ("body", "好") in events
    assert ("finish_reason", 1) in events
    assert ("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}) in events
    assert types[-1] == "done"


# ── Console batchGraphql 通道 ──


def _make_graphql_client(handler) -> Any:
    from aistudio_api.infrastructure.upstreams.console_graphql import (
        ConsoleGraphqlClient,
    )

    client = object.__new__(ConsoleGraphqlClient)
    client._cookie = "SAPISID=abc; __Secure-1PAPISID=abc; __Secure-3PAPISID=abc; SID=x"
    client._project_id = "proj-9"
    client._proxy_url = None
    client._session = None
    client._client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_graphql_stream_and_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        assert auth.startswith("SAPISIDHASH ") and "SAPISID3PHASH" in auth
        assert request.url.host == "cloudconsole-pa.clients6.google.com"
        body = json.loads(request.content)
        assert body["operationName"] == "StreamGenerateContent"
        assert body["variables"]["model"].startswith("projects/proj-9/locations/global/")
        chunks = [
            {"results": [{"data": {"candidates": [{"content": {"parts": [{"text": "早"}]}}]}}]},
            {"results": [{"data": {"candidates": [{"content": {"parts": [{"text": "安"}]}, "finishReason": "STOP"}],
                                   "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2, "totalTokenCount": 4}}}]},
        ]
        return httpx.Response(200, text="".join(json.dumps(c) for c in chunks))

    client = _make_graphql_client(handler)
    events = [e async for e in client.stream_generate_content(model="m", capture_prompt="p", contents=_contents())]
    assert ("body", "早") in events and ("body", "安") in events
    assert ("finish_reason", 1) in events
    assert types_tail(events) == "done"


def types_tail(events):
    return events[-1][0]


@pytest.mark.asyncio
async def test_graphql_error_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"error": {"message": "QUERY_SIGNATURE_NOT_FOUND"}}))

    client = _make_graphql_client(handler)
    with pytest.raises(RequestError) as exc_info:
        async for _ in client.stream_generate_content(model="m", capture_prompt="p", contents=_contents()):
            pass
    assert "QUERY_SIGNATURE_NOT_FOUND" in str(exc_info.value)


@pytest.mark.asyncio
async def test_graphql_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = _make_graphql_client(handler)
    with pytest.raises(RequestError) as exc_info:
        await client.generate_content(model="m", capture_prompt="p", contents=_contents())
    assert exc_info.value.status == 403


@pytest.mark.asyncio
async def test_graphql_nonstream_aggregates():
    def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {"results": [{"data": {"candidates": [{"content": {"parts": [{"text": "第一"}]}}]}}]},
            {"results": [{"data": {"candidates": [{"content": {"parts": [{"text": "第二"}]}}]}}]},
        ]
        return httpx.Response(200, text="".join(json.dumps(c) for c in chunks))

    client = _make_graphql_client(handler)
    out = await client.generate_content(model="m", capture_prompt="p", contents=_contents())
    assert out.text == "第一第二"
