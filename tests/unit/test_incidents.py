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
