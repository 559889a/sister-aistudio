"""思考签名哨兵（signatures）的单元测试（对齐 build2api src/signatures.js）。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.application import runtime_settings as rs_module
from aistudio_api.infrastructure.gateway.capture import RequestCaptureService
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.signatures import (
    SKIP_SENTINEL,
    apply_signature_sentinels,
    apply_to_parts,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

REAL_SIG = "real-signature-from-upstream"


def _contents() -> list[AistudioContent]:
    return [
        AistudioContent(
            role="user",
            parts=[
                AistudioPart(text="question", thought_signature=REAL_SIG),
                AistudioPart(
                    function_call=("real_tool", {"x": 1}),
                    thought_signature=REAL_SIG,
                ),
            ],
        ),
        AistudioContent(
            role="model",
            parts=[
                AistudioPart(text="thinking", thought=True, thought_signature=REAL_SIG),
                AistudioPart(
                    inline_data=("image/png", "AAAA"), thought_signature=REAL_SIG
                ),
            ],
        ),
    ]


def test_skip_sentinel_is_official_escape_hatch():
    assert SKIP_SENTINEL == "skip_thought_signature_validator"


def test_both_switches_on_replace_every_signature():
    contents = _contents()
    replaced = apply_signature_sentinels(contents)
    assert replaced == 4
    signatures = [
        part.thought_signature for content in contents for part in content.parts
    ]
    assert signatures == [SKIP_SENTINEL, SKIP_SENTINEL, SKIP_SENTINEL, SKIP_SENTINEL]


def test_thought_switch_off_keeps_non_tool_signatures_but_replaces_tool_ones():
    contents = _contents()
    apply_signature_sentinels(contents, thought=False, tool=True)

    assert contents[0].parts[0].thought_signature == REAL_SIG  # 普通文本 part
    assert contents[0].parts[1].thought_signature == SKIP_SENTINEL  # functionCall
    assert contents[1].parts[0].thought_signature == REAL_SIG  # 思考文本 part
    assert contents[1].parts[1].thought_signature == REAL_SIG  # inlineData


def test_tool_switch_off_keeps_function_call_signature_but_replaces_others():
    contents = _contents()
    apply_signature_sentinels(contents, thought=True, tool=False)

    assert contents[0].parts[1].thought_signature == REAL_SIG  # functionCall 保留
    assert contents[0].parts[0].thought_signature == SKIP_SENTINEL
    assert contents[1].parts[0].thought_signature == SKIP_SENTINEL


def test_both_switches_off_is_noop():
    contents = _contents()
    assert apply_signature_sentinels(contents, thought=False, tool=False) == 0
    signatures = [
        part.thought_signature for content in contents for part in content.parts
    ]
    assert signatures == [REAL_SIG, REAL_SIG, REAL_SIG, REAL_SIG]


def test_repeated_calls_are_idempotent():
    contents = _contents()
    assert apply_signature_sentinels(contents) == 4
    assert apply_signature_sentinels(contents) == 0
    assert contents[0].parts[0].thought_signature == SKIP_SENTINEL


def test_system_instruction_is_covered():
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="hi")])]
    system = AistudioContent(
        role="user",
        parts=[AistudioPart(text="sys", thought_signature=REAL_SIG)],
    )
    assert apply_signature_sentinels(contents, system_instruction=system) == 1
    assert system.parts[0].thought_signature == SKIP_SENTINEL


def test_missing_and_empty_signatures_are_ignored():
    parts = [
        AistudioPart(text="no signature"),
        AistudioPart(text="empty", thought_signature=""),
    ]
    assert apply_to_parts(parts) == 0
    assert parts[0].thought_signature is None
    assert parts[1].thought_signature == ""


def test_none_contents_are_tolerated():
    assert apply_signature_sentinels(None) == 0


# ── 接线：capture 写进上游 body 的确实是哨兵 ──


@pytest.fixture
def patch_settings(monkeypatch):
    def apply(**overrides: object) -> dict[str, Any]:
        values = dict(rs_module.DEFAULTS)
        values.update(overrides)
        monkeypatch.setattr(rs_module.runtime_settings, "_values", values)
        return values

    return apply


def _mock_capture_session() -> Any:
    session = MagicMock(spec=BrowserSession)
    session.capture_template = AsyncMock(
        return_value={
            "url": "https://example.com/generate",
            "headers": {"content-type": "application/json"},
            "body": '["models/gemini-3.8-flash",[[[[null,"hi"]],"user"]],null,'
            '[null,null,null,128],"!orig"]',
        }
    )
    session.generate_snapshot = AsyncMock(return_value="!fresh_snap")
    return session


@pytest.mark.asyncio
async def test_capture_writes_sentinels_into_upstream_body(patch_settings):
    patch_settings(prefillEnabled=False)
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    contents = [
        AistudioContent(
            role="user",
            parts=[
                AistudioPart(text="hi", thought_signature=REAL_SIG),
                AistudioPart(
                    function_call=("real_tool", {"x": 1}),
                    thought_signature=REAL_SIG,
                ),
            ],
        )
    ]
    captured = await service.capture(
        prompt="hi", model="models/gemini-3.8-flash", contents=contents
    )
    assert captured is not None
    body = json.loads(captured.body)
    text_part = body[1][0][0][0]
    call_part = body[1][0][0][1]
    assert text_part[14] == SKIP_SENTINEL
    # AistudioPart.to_wire 把 functionCall 写在 ALT 槽（下标 10）
    assert call_part[10][0] == "real_tool"
    assert call_part[14] == SKIP_SENTINEL


@pytest.mark.asyncio
async def test_capture_keeps_real_signature_when_switches_off(patch_settings):
    patch_settings(
        prefillEnabled=False,
        sigSentinelThought=False,
        sigSentinelTool=False,
    )
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    contents = [
        AistudioContent(
            role="user", parts=[AistudioPart(text="hi", thought_signature=REAL_SIG)]
        )
    ]
    captured = await service.capture(
        prompt="hi", model="models/gemini-3.8-flash", contents=contents
    )
    assert captured is not None
    body = json.loads(captured.body)
    assert body[1][0][0][0][14] == REAL_SIG
