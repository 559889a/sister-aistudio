"""偶发故障现场落盘（incidents.jsonl）的探针测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import HTTPException

from aistudio_api.domain.errors import AuthError
from aistudio_api.infrastructure.utils.incidents import record_incident


def _read_incidents(data_dir: Path) -> list[dict]:
    path = data_dir / "incidents.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_record_incident_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    record_incident("request_failure", {"http_status": 403, "model": "m"})
    record_incident("request_failure", {"http_status": 502, "model": "m"})
    rows = _read_incidents(tmp_path)
    assert [r["http_status"] for r in rows] == [403, 502]
    assert all({"ts", "kind"} <= set(r) for r in rows)


def test_record_incident_rotates_at_size(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))

    big = "x" * 4096
    for i in range(600):  # 600 * ~4KB > 2MB 上限
        record_incident("bulk", {"i": i, "pad": big})
    assert (tmp_path / "incidents.jsonl.1").exists()
    # 滚动后主文件继续可写、行数重新累计
    record_incident("after_rotate", {"ok": True})
    rows = _read_incidents(tmp_path)
    assert rows and rows[-1]["kind"] == "after_rotate"


def test_record_incident_swallows_io_errors(tmp_path, monkeypatch):
    # 目录只读等 IO 故障不能外抛（取证绝不弄挂请求）
    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path / "not" / "writable"))
    (tmp_path / "not").mkdir()
    (tmp_path / "not" / "writable").write_text("occupied", encoding="utf-8")
    record_incident("boom", {"x": 1})  # 路径是文件，open(a) 会失败


@pytest.mark.asyncio
async def test_handle_attempt_exception_records_incident(tmp_path, monkeypatch):
    """403 分支必须先落现场再重启浏览器（探针要在页面活着时跑）。"""
    from unittest.mock import MagicMock

    from aistudio_api.application.api_service_gemini import handle_attempt_exception
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))
    client = MagicMock(spec=AIStudioClient)
    client._session = None  # 无页面 → page_state 记 closed-or-missing

    err = AuthError("禁止访问: WAA rejected")
    err.raw_upstream_body = '[,[7,"waa proof invalid"]]'
    with pytest.raises(HTTPException) as exc_info:
        await handle_attempt_exception(
            err,
            model_path="gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=client,
        )
    assert exc_info.value.status_code == 403
    rows = _read_incidents(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["http_status"] == 403
    assert row["error_class"] == "AuthError"
    assert row["page_state"] == {"page": "closed-or-missing"}
    assert row["raw_upstream_body"] == '[,[7,"waa proof invalid"]]'
    # 429 是已知配额语义，不算偶发故障，不落现场
    from aistudio_api.domain.errors import UsageLimitExceeded

    with pytest.raises(HTTPException):
        await handle_attempt_exception(
            UsageLimitExceeded("配额用完"),
            model_path="gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=MagicMock(spec=AIStudioClient),
        )
    assert len(_read_incidents(tmp_path)) == 1


def test_transport_error_carries_net_evidence():
    from aistudio_api.infrastructure.gateway.transport import _transport_error

    class FakePage:
        rpc_net_failures: ClassVar[list] = [
            {"url": "u1", "error_text": "net::ERR_CONNECTION_RESET", "failed_at": 1.0},
            {"url": "u2", "error_text": "net::ERR_TIMED_OUT", "failed_at": 2.0},
        ]

    exc = _transport_error(FakePage(), "replay failed: Failed to fetch")
    assert exc.net_evidence[-1]["error_text"] == "net::ERR_TIMED_OUT"
    assert str(exc).startswith("replay failed")


@pytest.mark.asyncio
async def test_streaming_error_carries_raw_body(tmp_path, monkeypatch):
    """stream_chat 非 200 时异常要带上游原始错误体。"""
    from aistudio_api.infrastructure.gateway.streaming import StreamingGateway

    raw_err = 'w,[,[7,"permission denied by waa"]]'
    chunks = [("status", 403), ("chunk", raw_err)]

    class FakeSession:
        async def send_streaming_request(self, **kwargs):
            for etype, payload in chunks:
                yield (etype, payload)

    class FakeCaptured:
        url = "https://x/$rpc/GenerateContent"
        body = "[]"
        headers: ClassVar[dict] = {}
        bypass_plan = None
        prefill_text = ""

    gateway = StreamingGateway(session=FakeSession())
    with pytest.raises(AuthError) as exc_info:
        async for _ in gateway.stream_chat(captured=FakeCaptured(), model="m"):
            pass
    assert exc_info.value.raw_upstream_body == raw_err


def test_hook_chain_failures_classified_as_session_hang():
    """snapshot X 态 / hook 装失败 → 会话挂死类（502+重启浏览器），不再变 500 干瞪眼。"""
    from aistudio_api.domain.errors import is_browser_session_hang_error

    assert is_browser_session_hang_error(
        RuntimeError("Snapshot generation failed for content hash abcd1234")
    )
    assert is_browser_session_hang_error(
        RuntimeError("Hook install failed: no_snapshot_fn (url=https://aistudio.google.com/)")
    )
    assert is_browser_session_hang_error(RuntimeError("template capture timeout"))
    # 无关错误不能误伤
    assert not is_browser_session_hang_error(ValueError("some unrelated error"))
    assert not is_browser_session_hang_error(RuntimeError("wire codec mismatch"))


@pytest.mark.asyncio
async def test_stream_200_zero_chunks_raises_transport_error():
    """200 + 零数据帧 = 连接被截断，必须按传输失败报错而不是静默空流。"""
    from aistudio_api.domain.errors import BrowserTransportError
    from aistudio_api.infrastructure.gateway.streaming import StreamingGateway

    class FakeSession:
        async def send_streaming_request(self, **kwargs):
            yield ("status", 200)
            yield ("done", None)

    class FakeCaptured:
        url = "https://x/$rpc/GenerateContent"
        body = "[]"
        headers: ClassVar[dict] = {}
        bypass_plan = None
        prefill_text = ""

    gateway = StreamingGateway(session=FakeSession())
    with pytest.raises(BrowserTransportError) as exc_info:
        async for _ in gateway.stream_chat(captured=FakeCaptured(), model="m"):
            pass
    assert "zero" in str(exc_info.value) or "truncated" in str(exc_info.value)


@pytest.mark.asyncio
async def test_nonstream_empty_200_body_raises_transport_error():
    """非流 200 + 空 body：parse 出的空输出必须当传输失败，不许静默成功。"""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.domain.errors import BrowserTransportError
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    client = AIStudioClient.__new__(AIStudioClient)  # 跳过 __init__（不碰浏览器）

    class FakeSession:
        @asynccontextmanager
        async def request_scope(self):
            yield

        async def clear_prompt(self):
            return None

    captured = MagicMock()
    captured.body = "[]"
    captured.bypass_plan = None
    captured.prefill_text = ""

    client._session = FakeSession()
    client.capture_request = AsyncMock(return_value=captured)
    fake_replay = MagicMock()
    fake_replay.replay = AsyncMock(return_value=(200, b""))
    client._replay_service = fake_replay

    with pytest.raises(BrowserTransportError) as exc_info:
        await client.generate_content(model="m", capture_prompt="p")
    assert "empty 200" in str(exc_info.value)


@pytest.mark.asyncio
async def test_stream_failure_lands_probe_in_incidents(tmp_path, monkeypatch):
    """上游流中途死：stream_probe（死前收到多少/多久）必须落进 incidents.jsonl。"""
    from unittest.mock import MagicMock

    from aistudio_api.application import api_service_gemini as svc
    from aistudio_api.infrastructure.upstreams import vertex_express as ve

    monkeypatch.setenv("AISTUDIO_DATA_DIR", str(tmp_path))

    class _Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise RuntimeError("upstream died after long silence")

    class _FakeModels:
        async def generate_content_stream(self, **_kw):
            return _Stream()

    class _FakeAio:
        models = _FakeModels()

    client = object.__new__(ve.VertexExpressClient)
    client._client = MagicMock()
    client._client.aio = _FakeAio()
    client._project_id = ""
    client._location = ""
    client._session = None

    async def _noop(model=None):
        return None

    monkeypatch.setattr(svc, "ensure_active_account", _noop)

    from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

    contents = [AistudioContent.from_user_prompt("hi")]
    from aistudio_api.domain.errors import RequestError

    with pytest.raises(RequestError) as exc_info:
        async for _ in client.stream_generate_content(model="m", capture_prompt="p", contents=contents):
            pass
    probe = getattr(exc_info.value, "stream_probe", None)
    assert probe and probe["channel"] == "vertex_express" and probe["error_class"]
    # 走一遍网关错误处理，确认 incidents 收到 stream_probe
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await svc.handle_attempt_exception(
            exc_info.value, model_path="m", normalized_model="m", client=MagicMock()
        )
    rows = [json.loads(x) for x in (tmp_path / "incidents.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(r.get("stream_probe", {}).get("channel") == "vertex_express" for r in rows)
