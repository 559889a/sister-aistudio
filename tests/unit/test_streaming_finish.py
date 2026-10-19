"""streaming.py 的 parser.finish() 接线：上游流被截断时最后一段正文不丢。

解析层的 ``IncrementalJSONStreamParser.finish()`` 负责把缓冲里最后那个不完整的
chunk 修复出来；这里验证 ``StreamingGateway.stream_chat`` 在 async-for 结束后确实
调用了它，并且产出的 chunk 走的是与 feed 相同的处理路径（bypass/预填充/deduper
全部照常生效），不会重复也不会丢。
"""

from __future__ import annotations

import json
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
