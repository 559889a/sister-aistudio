"""「一次客户端请求 = 一次上游生成」的回归用例。

背景：老项目在 bypass 模式下出现过「一个输出要调用 n 次模型」的体感问题。
新项目的口径是：bypass 单轮、不续写；只有**失败**（403/429/204/偶发 404/浏览器挂起/
登录失效）才会重试，而且流式一旦吐出任何正文就不再重试。这里把这条口径钉死：

1. 非流式 bypass 成功路径：上游只被调用 1 次，正文从合成工具参数里还原；
2. bypassMode=required 但模型没调工具：也只调用 1 次，随后直接 502（不重试）；
3. 上游返回 RequestError 时，API 层立即转成 HTTPException，不进重试循环；
4. 流式已经吐出数据后遇到错误，也不再重试（切换账号只会浪费配额）；
5. 流式成功路径只消费一次上游流（没有「补一轮」的下游逻辑）。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.application import (
    api_service_gemini as svc,
    bypass as bypass_module,
    runtime_settings as rs_module,
)
from aistudio_api.domain.errors import AuthError, RequestError
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

FIXED_TOOL_NAME = "emit_once"


# ── wire 夹具（与 test_bypass.py 同形） ──


def _wire_text(text: str) -> list[Any]:
    return [None, text]


def _wire_fc(name: str, args: object) -> list[Any]:
    return [None, None, None, [name, args]]


def _make_response(*chunks: list[Any]) -> str:
    return json.dumps([list(chunks)])


def _make_chunk(parts: list[Any], finish: int | None = None) -> list[Any]:
    candidate: list[Any] = [[parts, "model"]]
    if finish is not None:
        candidate.append(finish)
    return [[candidate]]


@pytest.fixture
def patch_settings(monkeypatch):
    def apply(**overrides: object) -> dict[str, Any]:
        values = dict(rs_module.DEFAULTS)
        values.update(overrides)
        monkeypatch.setattr(rs_module.runtime_settings, "_values", values)
        return values

    return apply


def _fake_client(monkeypatch, raw_response: str) -> tuple[AIStudioClient, BrowserSession]:
    """真 client + 假 session：capture/replay 被截住，响应原文由用例给。"""
    session = BrowserSession(port=9222)
    session.capture_template = AsyncMock(
        return_value={
            "url": "https://example.com/generate",
            "headers": {"content-type": "application/json"},
            "body": '["models/gemini-3.8-flash",[[[[null,"hi"]],"user"]],null,[],null]',
        }
    )
    session.generate_snapshot = AsyncMock(return_value="!snap")
    session.send_hooked_request = AsyncMock(
        return_value=(200, raw_response.encode("utf-8"))
    )
    session.clear_prompt = AsyncMock()

    client = AIStudioClient(port=9222)
    client._session = session
    client._capture_service._session = session
    client._replay_service._session = session
    client._streaming_gateway._session = session
    # 合成工具名是随机的：固定下来，才能让响应里的 functionCall 对上号
    monkeypatch.setattr(
        bypass_module, "random_tool_name", lambda _prefix="": FIXED_TOOL_NAME
    )
    return client, session


def _contents() -> list[AistudioContent]:
    return [AistudioContent(role="user", parts=[AistudioPart(text="hi")])]


async def _generate(client: AIStudioClient):
    return await client.generate_content(
        model="models/gemini-3.8-flash",
        capture_prompt="hi",
        contents=_contents(),
        sanitize_plain_text=False,
    )


@pytest.mark.asyncio
async def test_bypass_success_calls_upstream_exactly_once(monkeypatch, patch_settings):
    patch_settings(bypassEnabled=True, bypassMode="prefer", prefillEnabled=False)
    raw = _make_response(
        _make_chunk([_wire_fc(FIXED_TOOL_NAME, {"content": "<p>秋景正文</p>"})], finish=1)
    )
    client, session = _fake_client(monkeypatch, raw)

    output = await _generate(client)

    # 一次客户端请求只打一次上游
    assert session.send_hooked_request.await_count == 1
    # 合成工具的参数被还原成普通正文，函数调用不外泄
    assert output.candidates[0].text == "<p>秋景正文</p>"
    assert not output.candidates[0].function_calls


@pytest.mark.asyncio
async def test_bypass_required_miss_calls_upstream_once_then_502(
    monkeypatch, patch_settings
):
    patch_settings(bypassEnabled=True, bypassMode="required", prefillEnabled=False)
    raw = _make_response(_make_chunk([_wire_text("模型直接写了正文")], finish=1))
    client, session = _fake_client(monkeypatch, raw)

    with pytest.raises(RequestError) as excinfo:
        await _generate(client)

    assert excinfo.value.status == 502
    # 关键：没调工具也不是「再问一次」，只发这一次
    assert session.send_hooked_request.await_count == 1


@pytest.mark.asyncio
async def test_request_error_is_not_retried_by_api_layer():
    """RequestError（含 bypass required 未命中）走的是立即报错分支，不进重试循环。"""
    with pytest.raises(HTTPException) as excinfo:
        await svc.handle_attempt_exception(
            RequestError(502, "bypass miss"),
            attempt=0,
            model_path="models/gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=MagicMock(),
        )
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_does_not_retry_after_data_was_yielded():
    """流式已经吐出正文后再遇到 403：直接报错，不换号、不重打上游。"""
    with pytest.raises(HTTPException) as excinfo:
        await svc.handle_attempt_exception(
            AuthError("403 The caller does not have permission"),
            attempt=0,
            model_path="models/gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=MagicMock(),
            has_yielded_data=True,
        )
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_stream_generates_once_per_client_request(monkeypatch, patch_settings):
    """流式成功路径：上游流只被消费一次（没有「补一轮」的下游逻辑）。"""
    patch_settings(bypassEnabled=False, prefillEnabled=False)
    client, _session = _fake_client(monkeypatch, "[]")

    calls = 0

    async def fake_stream(**_kwargs):
        nonlocal calls
        calls += 1
        yield ("body", "第一段")
        yield ("body", "第二段")
        yield ("finish_reason", 1)

    client.stream_generate_content = fake_stream  # type: ignore[method-assign]
    events = [
        event
        async for event in svc.stream_raw_events(
            "models/gemini-3.8-flash",
            svc.GeminiGenerateContentRequest(
                contents=[{"role": "user", "parts": [{"text": "hi"}]}]
            ),
            client,
        )
    ]
    bodies = [text for kind, text in events if kind == "body"]
    assert bodies == ["第一段", "第二段"]
    assert calls == 1
    assert len([1 for kind, _ in events if kind == "prepared"]) == 1
