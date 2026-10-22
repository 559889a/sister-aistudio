"""streaming.py 的 parser.finish() 接线：上游流被截断时最后一段正文不丢。

解析层的 ``IncrementalJSONStreamParser.finish()`` 负责把缓冲里最后那个不完整的
chunk 修复出来；这里验证 ``StreamingGateway.stream_chat`` 在 async-for 结束后确实
调用了它，并且产出的 chunk 走的是与 feed 相同的处理路径（bypass/预填充/deduper
全部照常生效），不会重复也不会丢。
"""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Any

import pytest

from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.streaming import StreamingGateway


def _wire_text(text: str) -> list[Any]:
    return [None, text]


def _make_chunk(parts: list[Any], finish: int | None = None) -> list[Any]:
    candidate: list[Any] = [[parts, "model"]]
    if finish is not None:
        candidate.append(finish)
    return [[candidate]]


def _stream_text(chunks: list[list[Any]]) -> str:
    return ")]}'\n" + json.dumps([chunks])


class _FakeStreamSession:
    """把一段文本按固定片数喂给网关（不发真请求）。"""

    def __init__(self, text: str, pieces: int = 3) -> None:
        self._text = text
        self._pieces = pieces

    async def send_streaming_request(self, **_kwargs: object):
        yield ("status", "200")
        size = max(1, len(self._text) // self._pieces)
        for start in range(0, len(self._text), size):
            yield ("chunk", self._text[start : start + size])


async def _run_gateway(text: str) -> list[tuple[str, Any]]:
    gateway = StreamingGateway(session=_FakeStreamSession(text))
    captured = CapturedRequest(url="https://example.com/generate", headers={}, body="[]")
    events: list[tuple[str, Any]] = []
    async for event in gateway.stream_chat(captured=captured, model="m"):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_truncated_stream_tail_is_delivered_via_parser_finish():
    chunks = [
        _make_chunk([_wire_text("Hello ")]),
        _make_chunk([_wire_text("wor")]),
        _make_chunk([_wire_text("ld!")]),
    ]
    full = _stream_text(chunks)
    # 在最后一个 chunk 的字符串刚写完、数组尚未闭合处截断
    marker = '"ld!"'
    cut = full.index(marker) + len(marker)
    truncated = full[:cut]

    events = await _run_gateway(truncated)

    body = "".join(str(payload) for kind, payload in events if kind == "body")
    assert body == "Hello world!"
    assert ("done", None) in events


@pytest.mark.asyncio
async def test_complete_stream_is_not_duplicated_by_finish():
    chunks = [
        _make_chunk([_wire_text("Hello ")]),
        _make_chunk([_wire_text("world")]),
        _make_chunk([], finish=1),
    ]
    events = await _run_gateway(_stream_text(chunks))

    body = "".join(str(payload) for kind, payload in events if kind == "body")
    assert body == "Hello world"
    assert ("finish_reason", 1) in events


@pytest.mark.asyncio
async def test_truncated_stream_without_any_body_still_terminates():
    """截断到什么都救不回来时：流正常收尾（usage/done），不能挂住或重复。"""
    events = await _run_gateway(")]}'\n[[")
    assert events
    assert ("done", None) in events
    body = "".join(str(payload) for kind, payload in events if kind == "body")
    assert body == ""


@pytest.mark.asyncio
async def test_finish_reason_22_continuing_does_not_break_stream():
    """finishReason 22=「继续中」是非终止码：流里出现后必须继续吃后续 chunk。"""
    text = (
        ")]}'\n"
        + json.dumps(
            [
                [
                    _make_chunk([_wire_text("第一段")], finish=22),
                    _make_chunk([_wire_text("第二段")]),
                    _make_chunk([_wire_text("第三段")], finish=1),
                ]
            ]
        )
    )
    events = await _run_gateway(text)
    body = "".join(t for etype, t in events if etype == "body")
    assert body == "第一段第二段第三段"
    finish = [t for etype, t in events if etype == "finish_reason"]
    assert finish == [1]


@pytest.mark.asyncio
async def test_terminal_candidate_does_not_drop_following_candidates_or_metadata():
    first = [_make_chunk([_wire_text("first")], 1)[0] + _make_chunk([_wire_text("other")])[0]]
    second = [_make_chunk([])[0] + _make_chunk([_wire_text(" tail")], 2)[0]]
    metadata = [None, None, [5, 3, 8], None, None, None, None, "late-rid"]
    events = await _run_gateway(_stream_text([first, second, metadata]))
    assert "".join(payload for kind, payload in events if kind == "body") == "first"
    indexed = [payload for kind, payload in events if kind == "candidate_event"]
    assert "".join(event["payload"] for event in indexed if event["type"] == "body") == "other tail"
    assert all(event["index"] == 1 for event in indexed)
    assert {"index": 1, "type": "finish_reason", "payload": 2} in indexed
    assert ("finish_reason", 1) in events
    assert ("response_id", "late-rid") in events
    assert next(payload for kind, payload in events if kind == "usage")["total_tokens"] == 8


@pytest.mark.asyncio
async def test_stream_delivers_truncated_usage_and_finish_only_tail():
    for tail in (_make_chunk([], 1), [None, None, [5, 2, 7]]):
        text = "[[" + json.dumps(_make_chunk([_wire_text("body")])) + "," + json.dumps(tail)[:-1]
        events = await _run_gateway(text)
        assert "".join(payload for kind, payload in events if kind == "body") == "body"
        if len(tail) > 2:
            assert next(payload for kind, payload in events if kind == "usage")["total_tokens"] == 7
        else:
            assert ("finish_reason", 1) in events


@pytest.mark.asyncio
@pytest.mark.parametrize("merge_outside", [False, True])
async def test_bypass_and_prefill_state_is_independent_for_each_candidate(merge_outside):
    from aistudio_api.application.bypass import BypassPlan

    def call(text):
        return [None, None, None, ["emit_x", {"content": text}]]

    chunks = [
        [_make_chunk([call("ABone")])[0] + _make_chunk([call("ABtwo")])[0]],
        [_make_chunk([call("ABone!")], 1)[0] + _make_chunk([call("ABtwo!")], 1)[0]],
    ]
    captured = CapturedRequest(
        url="http://x", headers={}, body="[]", prefill_text="AB",
        bypass_plan=BypassPlan(tool_name="emit_x", mode="required", merge_outside=merge_outside),
    )
    events = [event async for event in StreamingGateway(_FakeStreamSession(_stream_text(chunks))).stream_chat(captured=captured, model="m")]
    assert "".join(payload for kind, payload in events if kind == "body") == "ABone!"
    indexed = [payload for kind, payload in events if kind == "candidate_event"]
    assert "".join(event["payload"] for event in indexed if event["type"] == "body") == "ABtwo!"
    assert not any(kind == "tool_calls" for kind, _ in events)
    assert not any(event["type"] == "tool_calls" for event in indexed)


@pytest.mark.asyncio
async def test_stream_delivers_function_responses_and_code_output():
    part = [None] * 10
    part[4] = ["f", {"result": 1}, "call"]
    part[8] = [1, "print(1)"]
    part[9] = [1, "1"]
    events = await _run_gateway(_stream_text([_make_chunk([part], 1)]))
    responses = next(payload for kind, payload in events if kind == "function_responses")
    assert responses[0]["call_id"] == "call"
    assert ("code_output", "print(1)\n1") in events


@pytest.mark.asyncio
async def test_closing_gateway_closes_source_in_consuming_task():
    context = ContextVar("stream-test", default=None)
    owner = asyncio.current_task()
    closed = []

    class Session:
        async def send_streaming_request(self, **_kwargs):
            token = context.set("leased")
            try:
                yield ("status", 200)
                yield ("chunk", _stream_text([_make_chunk([_wire_text("body")])]))
            finally:
                closed.append(asyncio.current_task())
                context.reset(token)

    source = StreamingGateway(Session()).stream_chat(
        captured=CapturedRequest("http://x", {}, "[]"), model="m",
    )
    assert await anext(source) == ("status", 200)
    await source.aclose()
    assert closed == [owner]
    assert context.get() is None


@pytest.mark.asyncio
async def test_failure_after_finish_is_not_suppressed():
    class Session:
        async def send_streaming_request(self, **_kwargs):
            yield ("status", 200)
            yield ("chunk", _stream_text([_make_chunk([_wire_text("body")], 1)]))
            raise RuntimeError("late transport failure")

    with pytest.raises(RuntimeError, match="late transport failure"):
        _ = [event async for event in StreamingGateway(Session()).stream_chat(
            captured=CapturedRequest("http://x", {}, "[]"), model="m",
        )]


@pytest.mark.asyncio
async def test_required_bypass_does_not_succeed_on_metadata_only_stream():
    from aistudio_api.application.bypass import BypassPlan
    from aistudio_api.domain.errors import RequestError

    captured = CapturedRequest(
        "http://x", {}, "[]", bypass_plan=BypassPlan(tool_name="emit_x", mode="required"),
    )
    with pytest.raises(RequestError) as caught:
        _ = [event async for event in StreamingGateway(
            _FakeStreamSession(_stream_text([[None, None, [5, 0, 5]]])),
        ).stream_chat(captured=captured, model="m")]
    assert caught.value.status == 502
