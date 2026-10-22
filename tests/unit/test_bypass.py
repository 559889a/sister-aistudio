"""工具调用抗截断 bypass 的单元测试（对齐 build2api src/bypass.js 的单轮语义）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aistudio_api.application import bypass as bp, runtime_settings as rs_module
from aistudio_api.application.bypass import (
    BypassPlan,
    BypassStreamExtractor,
    assess_completion,
    build_declaration,
    build_wire_tool_config,
    build_wire_tools,
    collapse_cumulative,
    extract_content,
    extract_from_parts,
    inject_instruction,
    is_cumulative,
    iter_response_parts,
    random_tool_name,
    render_instruction,
    restore_non_stream,
)
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ModelOutput
from aistudio_api.infrastructure.gateway.capture import RequestCaptureService
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_part
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

# ── wire 夹具 ──


def wire_text(text: str) -> list[Any]:
    return [None, text]


def wire_fc(name: str, args: object) -> list[Any]:
    return [None, None, None, [name, args]]


def wire_thought_text(text: str) -> list[Any]:
    # thought flag 在下标 10
    return [None, text, None, None, None, None, None, None, None, None, True]


def make_chunk(parts: list[Any], finish: int | None = None) -> list[Any]:
    content = [parts, "model"]
    candidate: list[Any] = [content]
    if finish is not None:
        candidate.append(finish)
    container = [candidate]
    return [container]


def make_response(*chunks: list[Any]) -> str:
    return json.dumps([list(chunks)])


def parts_of(*chunks: list[Any]) -> list[Any]:
    return iter_response_parts(make_response(*chunks))


def parse_parts(*raw_parts: list[Any]) -> list[Any]:
    return [parse_response_part(raw) for raw in raw_parts]


@pytest.fixture
def patch_settings(monkeypatch):
    def apply(**overrides: object) -> dict[str, Any]:
        values = dict(rs_module.DEFAULTS)
        values.update(overrides)
        monkeypatch.setattr(rs_module.runtime_settings, "_values", values)
        return values

    return apply


# ── 请求侧：工具名 / 声明 / 指令 ──


def test_random_tool_name_sanitizes_prefix_and_is_unique():
    assert random_tool_name("my-prefix!").startswith("myprefix")
    assert random_tool_name("").startswith(bp.DEFAULT_PREFIX)
    assert random_tool_name("emit_") != random_tool_name("emit_")


def test_declaration_only_has_content_required_and_fiction_wording():
    declaration = build_declaration("emit_x")
    assert declaration["name"] == "emit_x"
    parameters = declaration["parameters"]
    assert list(parameters["properties"]) == ["content"]
    assert parameters["properties"]["content"]["type"] == "STRING"
    assert parameters["required"] == ["content"]

    description = str(declaration["description"]).lower()
    assert "shows the story text" in description
    assert "interactive-fiction" in description
    for forbidden in ("bypass", "circumvent", "safety mechanism"):
        assert forbidden not in description

    instruction = render_instruction("", "emit_x")
    assert "`emit_x`" in instruction
    # 内置指令里不该出现「绕过安全机制」这类措辞（语境不一致会引起模型收敛）
    lowered = instruction.lower()
    for forbidden in ("bypass", "circumvent", "safety mechanism"):
        assert forbidden not in lowered


def test_render_instruction_substitutes_placeholder_in_custom_template():
    assert render_instruction("call {tool} now", "emit_abc") == "call emit_abc now"
    assert render_instruction("  ", "emit_abc") == render_instruction("", "emit_abc")


def test_build_wire_tools_replaces_client_tools_with_single_synthetic_tool():
    tools = build_wire_tools("emit_x")
    assert len(tools) == 1
    tool = tools[0]
    assert tool[0] is None
    declarations = tool[1]
    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration[0] == "emit_x"
    assert "shows the story text" in declaration[1].lower()
    schema = declaration[2]
    assert schema[0] == 6  # OBJECT
    assert schema[6] == [["content", schema[6][0][1]]]
    assert schema[7] == ["content"]
    content_schema = schema[6][0][1]
    assert content_schema[0] == 1  # STRING


def test_build_wire_tool_config_modes():
    assert build_wire_tool_config("required", "emit_x") == [None, [2, ["emit_x"]]]
    assert build_wire_tool_config("prefer", "emit_x") == [None, [1]]
    # 未知值按 prefer 处理
    assert build_wire_tool_config("whatever", "emit_x") == [None, [1]]


# ── 请求侧：指令注入位置 ──


def test_inject_instruction_tail_appends_to_existing_user_turn():
    contents = [
        AistudioContent(role="user", parts=[AistudioPart(text="hi")]),
        AistudioContent(role="model", parts=[AistudioPart(text="hello")]),
        AistudioContent(role="user", parts=[AistudioPart(text="again")]),
    ]
    returned = inject_instruction(contents, "INSTR", "tail")
    assert returned is None
    assert len(contents) == 3
    assert contents[-1].parts[-1].text == "again\n\nINSTR"


def test_inject_instruction_tail_keeps_function_response_turn_pure():
    """末尾是纯 functionResponse 轮时不能并进去，必须另起一个 user 轮。"""
    contents = [
        AistudioContent(role="user", parts=[AistudioPart(text="hi")]),
        AistudioContent(
            role="model", parts=[AistudioPart(function_call=("real_tool", {}))]
        ),
        AistudioContent(
            role="user",
            parts=[AistudioPart(function_response=("real_tool", {"ok": True}))],
        ),
    ]
    inject_instruction(contents, "INSTR", "tail")
    assert len(contents) == 4
    assert contents[-2].parts[0].function_response is not None
    assert contents[-1].role == "user"
    assert contents[-1].parts[0].text == "INSTR"


def test_inject_instruction_head_inserts_before_first_user():
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="hi")])]
    inject_instruction(contents, "INSTR", "head")
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "INSTR\n\n"
    assert contents[0].parts[1].text == "hi"


def test_inject_instruction_head_inserts_new_turn_when_first_is_model():
    contents = [AistudioContent(role="model", parts=[AistudioPart(text="x")])]
    inject_instruction(contents, "INSTR", "head")
    assert len(contents) == 2
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "INSTR"


def test_inject_instruction_system_appends_to_existing_system():
    system = AistudioContent(role="user", parts=[AistudioPart(text="sys")])
    contents: list[AistudioContent] = []
    returned = inject_instruction(contents, "INSTR", "system", system)
    assert returned is system
    assert returned.parts[-1].text == "\n\nINSTR"
    # 没有 systemInstruction 时新建一份
    returned2 = inject_instruction([], "INSTR", "system", None)
    assert returned2 is not None
    assert returned2.parts[0].text == "INSTR"


def test_apply_bypass_returns_plan_and_injects_instruction():
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="hi")])]
    plan = bp.apply_bypass(contents, tool_prefix="emit_", mode="required")
    assert isinstance(plan, BypassPlan)
    assert plan.tool_name.startswith("emit_")
    assert plan.mode == "required"
    assert plan.merge_outside is True
    injected = contents[-1].parts[-1].text
    assert "A small note about how replies are displayed" in injected
    assert plan.tool_name in injected


# ── 提取侧：args → 正文 ──


def test_extract_content_variants():
    assert extract_content({"content": "hello"}) == "hello"
    assert extract_content({"text": "alt"}) == "alt"
    assert extract_content({"content": ["a", {"text": "b"}]}) == "ab"
    assert extract_content({"whatever": "only"}) == "only"
    assert extract_content({"a": "x", "b": "y"}) is None
    # 非 JSON 的裸字符串不是内容（与主项目 repairArgs 一致：解析不出来就当没有）
    assert extract_content("plain string") is None
    assert extract_content(None) is None
    # 整个 JSON 当成字符串塞进 args 的形态
    assert extract_content('{"content": "json"}') == "json"
    # 被截断的 JSON：结构修复兜底
    assert extract_content('{"content": "cut off') == "cut off"
    # 代码围栏
    assert extract_content('```json\n{"content": "fenced"}\n```') == "fenced"


# ── 提取侧：parts 合并（非流式） ──


def test_extract_from_parts_merges_outside_text_in_order():
    parts = parts_of(
        make_chunk([wire_text("Look: ")]),
        make_chunk([wire_fc("emit_x", {"content": "<a>story</a>"})]),
        make_chunk([wire_text(" done")]),
    )
    result = extract_from_parts(parts, "emit_x", merge_plain_text=True)
    assert result.content == "Look: <a>story</a> done"
    assert result.mixed is True
    assert result.call_count == 1


def test_extract_from_parts_collapses_cumulative_tool_args_once():
    parts = parts_of(
        make_chunk([wire_fc("emit_x", {"content": "hello"})]),
        make_chunk([wire_fc("emit_x", {"content": "hello world"})]),
        make_chunk([wire_fc("emit_x", {"content": "hello world"})]),
    )
    result = extract_from_parts(parts, "emit_x")
    assert result.content == "hello world"


def test_extract_from_parts_merge_off_keeps_only_tool_content():
    parts = parts_of(
        make_chunk([wire_text("outside")]),
        make_chunk([wire_fc("emit_x", {"content": "inside"})]),
    )
    result = extract_from_parts(parts, "emit_x", merge_plain_text=False)
    assert result.content == "inside"


def test_extract_from_parts_no_tool_returns_none_and_filters_synthetic_call():
    parts = parts_of(
        make_chunk([wire_text("direct answer")]),
        make_chunk([wire_fc("emit_x", {})]),
    )
    result = extract_from_parts(parts, "emit_x")
    assert result.content is None
    assert result.call_count == 0
    assert all(
        getattr(part, "function_call", None) is None for part in result.other_parts
    )


def test_extract_from_parts_keeps_thinking_untouched():
    parts = parts_of(
        make_chunk([wire_thought_text("thinking...")]),
        make_chunk([wire_fc("emit_x", {"content": "story"})]),
    )
    result = extract_from_parts(parts, "emit_x")
    assert result.content == "story"
    assert any(getattr(part, "thought", False) for part in result.other_parts)


# ── 完成判定 ──


def test_assess_completion_only_checks_last_tag():
    assert assess_completion("").complete is False
    assert assess_completion("   ").complete is False
    assert assess_completion("plain prose").complete is True
    assert assess_completion("<a>x</a><b>y</b>").complete is True
    verdict = assess_completion("<a>x</a><b>y")
    assert verdict.complete is False
    assert "<b>" in verdict.reason


def test_is_cumulative_probe():
    long_base = "a" * 200
    assert is_cumulative(long_base + "tail", long_base) is True
    assert is_cumulative(long_base + "tail", "a" * 200) is True
    assert is_cumulative("a" * 200, long_base) is False  # 等长不算累积
    assert is_cumulative("short", "longer base") is False
    assert is_cumulative("hello world", "hello") is True
    assert is_cumulative(long_base + "tail", "b" * 200) is False


def test_collapse_cumulative_variants():
    assert collapse_cumulative("hello", "") == ("hello", "hello")
    assert collapse_cumulative("hello", "hello") == ("", "hello")
    assert collapse_cumulative("hello world", "hello") == (" world", "hello world")
    assert collapse_cumulative("hello", "hello world") == ("", "hello world")
    # 不构成累积：退化成「去掉重叠」的保守策略，不整段重复
    assert collapse_cumulative("hello world", "abc") == ("hello world", "hello world")


# ── 流式提取 ──


def test_stream_extractor_emits_cumulative_args_once():
    extractor = BypassStreamExtractor("emit_x")
    chunks = [
        [wire_fc("emit_x", {"content": "hello"})],
        [wire_fc("emit_x", {"content": "hello world"})],
        [wire_fc("emit_x", {"content": "hello world"})],
    ]
    deltas = []
    for chunk in chunks:
        delta, _ = extractor.feed(parse_parts(*chunk))
        deltas.append(delta)
    assert "".join(deltas) == "hello world"
    assert extractor.merged == "hello world"
    assert extractor.saw_call is True


def test_stream_extractor_merges_outside_text_in_order():
    extractor = BypassStreamExtractor("emit_x", merge_plain_text=True)
    sequence = [
        [wire_text("A")],
        [wire_fc("emit_x", {"content": "B"})],
        [wire_text("C")],
    ]
    deltas = []
    for chunk in sequence:
        delta, _ = extractor.feed(parse_parts(*chunk))
        deltas.append(delta)
    assert "".join(deltas) == "ABC"
    assert extractor.mixed is True


def test_stream_extractor_merge_off_drops_text_after_call_but_keeps_before():
    extractor = BypassStreamExtractor("emit_x", merge_plain_text=False)
    delta_before, other_before = extractor.feed(parse_parts(wire_text("intro ")))
    assert delta_before == ""
    assert [part.text for part in other_before] == ["intro "]

    delta_call, _ = extractor.feed(parse_parts(wire_fc("emit_x", {"content": "B"})))
    assert delta_call == "B"
    delta_after, other_after = extractor.feed(parse_parts(wire_text(" ignored")))
    assert delta_after == ""
    assert other_after == []


def test_stream_extractor_skips_thinking_parts():
    extractor = BypassStreamExtractor("emit_x")
    delta, other = extractor.feed(parse_parts(wire_thought_text("hmm")))
    assert delta == ""
    assert any(part.thought for part in other)


# ── 非流式还原 ──


def test_restore_non_stream_prefer_miss_uses_direct_text_and_drops_synthetic_call():
    plan = BypassPlan(tool_name="emit_x", mode="prefer")
    raw = make_response(make_chunk([wire_text("direct answer")], finish=1))
    output = ModelOutput(
        candidates=[
            Candidate(text="direct answer", function_calls=[{"name": "emit_x"}])
        ]
    )
    restored = restore_non_stream(output, raw, plan)
    assert restored.text == "direct answer"
    assert restored.function_calls == []


def test_restore_non_stream_required_miss_raises_502():
    plan = BypassPlan(tool_name="emit_x", mode="required")
    raw = make_response(make_chunk([wire_text("direct answer")], finish=1))
    output = ModelOutput(candidates=[Candidate(text="direct answer")])
    with pytest.raises(RequestError) as excinfo:
        restore_non_stream(output, raw, plan)
    assert excinfo.value.status == 502


def test_restore_non_stream_extracts_content_and_keeps_thinking():
    plan = BypassPlan(tool_name="emit_x", mode="prefer")
    raw = make_response(
        make_chunk([wire_thought_text("<thinking>plan</thinking>")]),
        make_chunk([wire_fc("emit_x", {"content": "<a>story</a>"})], finish=1),
    )
    # 用真实解析器填好 thinking，再走还原
    from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output

    parsed = parse_text_output(raw)
    restored = restore_non_stream(parsed, raw, plan)
    assert restored.text == "<a>story</a>"
    assert "plan" in restored.thinking


def test_restore_non_stream_merges_outside_text_and_dedupes():
    plan = BypassPlan(tool_name="emit_x", mode="prefer", merge_outside=True)
    raw = make_response(
        make_chunk([wire_text("Look: ")]),
        make_chunk([wire_fc("emit_x", {"content": "story"})]),
        make_chunk([wire_text(" (end)")], finish=1),
    )
    from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output

    restored = restore_non_stream(parse_text_output(raw), raw, plan)
    assert restored.text == "Look: story (end)"


def test_restore_non_stream_logs_incomplete_output_but_delivers(caplog):
    plan = BypassPlan(tool_name="emit_x", mode="prefer")
    raw = make_response(
        make_chunk([wire_fc("emit_x", {"content": "<hello>cut"})], finish=1)
    )
    from aistudio_api.infrastructure.gateway.wire_parser import parse_text_output

    with caplog.at_level("WARNING"):
        restored = restore_non_stream(parse_text_output(raw), raw, plan)
    assert restored.text == "<hello>cut"  # 内容照常交付，不因「看起来没写完」报错
    assert any("可能不完整" in record.getMessage() for record in caplog.records)


# ── capture 接线（请求改写顺序：prefill → bypass → 哨兵 → 快照） ──


def _mock_capture_session() -> Any:
    from unittest.mock import AsyncMock, MagicMock

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
async def test_capture_applies_prefill_bypass_and_snapshot_after_rewrite(patch_settings):
    patch_settings(bypassEnabled=True, prefillEnabled=True, bypassMode="required")
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    contents = [
        AistudioContent(role="user", parts=[AistudioPart(text="hi")]),
        AistudioContent(role="model", parts=[AistudioPart(text="<thinking>开头")]),
    ]

    captured = await service.capture(
        prompt="hi", model="models/gemini-3.8-flash", contents=contents
    )
    assert captured is not None
    assert captured.prefill_text == "<thinking>开头"
    assert captured.bypass_plan is not None
    tool_name = captured.bypass_plan.tool_name

    body = json.loads(captured.body)
    roles = [content[1] for content in body[1]]
    assert roles == ["user", "model", "user"]
    last_user_text = body[1][-1][0][0][1]
    assert "A small note about how replies are displayed" in last_user_text
    assert tool_name in last_user_text
    # 预填充的 COT 守卫要求在思维链里补完并闭合
    assert "</thinking>" in last_user_text

    tools = body[6]
    assert tools[0][0] is None
    assert tools[0][1][0][0] == tool_name

    # 快照必须基于改写后的 contents（binding 与 body 是同一份）
    snapshot_arg = session.generate_snapshot.call_args.args[0]
    assert any(
        "A small note about how replies are displayed" in str(part.text)
        for content in snapshot_arg
        for part in content.parts
    )
    # 调用方持有的 contents 不能被就地改掉
    assert len(contents[-1].parts) == 1


@pytest.mark.asyncio
async def test_capture_skips_bypass_for_image_models(patch_settings):
    patch_settings(bypassEnabled=True, prefillEnabled=False)
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    contents = [AistudioContent(role="user", parts=[AistudioPart(text="draw")])]

    captured = await service.capture(
        prompt="draw", model="models/gemini-3.1-flash-image-preview", contents=contents
    )
    assert captured is not None
    assert captured.bypass_plan is None


@pytest.mark.asyncio
async def test_capture_system_position_injects_into_system_instruction(patch_settings):
    patch_settings(
        bypassEnabled=True,
        prefillEnabled=False,
        bypassInstructionPosition="system",
    )
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    system = AistudioContent(role="user", parts=[AistudioPart(text="sys prompt")])
    captured = await service.capture(
        prompt="hi",
        model="models/gemini-3.8-flash",
        contents=[AistudioContent(role="user", parts=[AistudioPart(text="hi")])],
        system_instruction_content=system,
    )
    assert captured is not None and captured.bypass_plan is not None
    body = json.loads(captured.body)
    system_wire = body[5]
    system_texts = [str(part[1]) for part in system_wire[0]]
    assert any("sys prompt" in text for text in system_texts)
    assert any("A small note about how replies are displayed" in text for text in system_texts)
    # 调用方持有的 systemInstruction 不能被就地改掉
    assert len(system.parts) == 1


@pytest.mark.asyncio
async def test_capture_respects_custom_instruction_and_tool_prefix(patch_settings):
    patch_settings(
        bypassEnabled=True,
        prefillEnabled=False,
        bypassInstruction="CUSTOM DELIVERY via {tool}",
        bypassToolPrefix="my_emit_",
    )
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    captured = await service.capture(
        prompt="hi",
        model="models/gemini-3.8-flash",
        contents=[AistudioContent(role="user", parts=[AistudioPart(text="hi")])],
    )
    assert captured is not None and captured.bypass_plan is not None
    tool_name = captured.bypass_plan.tool_name
    assert tool_name.startswith("my_emit_")
    body = json.loads(captured.body)
    last_text = body[1][-1][0][-1][1]
    # tail 位置：并进最后一条 user 轮（原文 + 空行 + 自定义指令）
    assert last_text.endswith(f"CUSTOM DELIVERY via {tool_name}")


@pytest.mark.asyncio
async def test_capture_prefill_off_leaves_request_untouched(patch_settings):
    patch_settings(
        bypassEnabled=False,
        prefillEnabled=False,
        sigSentinelThought=False,
        sigSentinelTool=False,
    )
    session = _mock_capture_session()
    service = RequestCaptureService(session)
    contents = [
        AistudioContent(role="user", parts=[AistudioPart(text="hi")]),
        AistudioContent(role="model", parts=[AistudioPart(text="prefill")]),
    ]
    captured = await service.capture(
        prompt="hi", model="models/gemini-3.8-flash", contents=contents
    )
    assert captured is not None
    assert captured.prefill_text == ""
    assert captured.bypass_plan is None
    body = json.loads(captured.body)
    # 只有原始两条 content：没有注入的 user 轮
    assert [content[1] for content in body[1]] == ["user", "model"]


# ── 流式 / 非流式回放接线（fake session，不发真请求） ──


class _FakeStreamSession:
    def __init__(self, chunks: list[Any], pieces: int = 3) -> None:
        self._chunks = chunks
        self._pieces = pieces

    async def send_streaming_request(self, **kwargs: object):
        yield ("status", "200")
        text = ")]}'\n" + json.dumps([self._chunks])
        size = max(1, len(text) // self._pieces)
        for start in range(0, len(text), size):
            yield ("chunk", text[start : start + size])


async def _run_stream_gateway(
    plan: BypassPlan | None,
    chunks: list[Any],
    *,
    prefill: str = "",
) -> list[tuple[str, Any]]:
    from aistudio_api.infrastructure.gateway.capture import CapturedRequest
    from aistudio_api.infrastructure.gateway.streaming import StreamingGateway

    gateway = StreamingGateway(session=_FakeStreamSession(chunks))
    captured = CapturedRequest(
        url="https://example.com/generate",
        headers={},
        body="[]",
        bypass_plan=plan,
        prefill_text=prefill,
    )
    events: list[tuple[str, Any]] = []
    async for event in gateway.stream_chat(captured=captured, model="m"):
        events.append(event)
    return events


@pytest.mark.asyncio
async def test_streaming_gateway_emits_extracted_body_once():
    plan = BypassPlan(tool_name="emit_x", mode="prefer", merge_outside=True)
    chunks = [
        make_chunk([wire_text("He")]),
        make_chunk([wire_fc("emit_x", {"content": "hello"})]),
        make_chunk([wire_fc("emit_x", {"content": "hello world"})]),
        make_chunk([wire_fc("emit_x", {"content": "hello world"})]),
        make_chunk([wire_text("!")]),
        make_chunk([], finish=1),
    ]
    events = await _run_stream_gateway(plan, chunks)

    body = "".join(str(payload) for kind, payload in events if kind == "body")
    assert body == "Hehello world!"
    # 合成工具的调用不能漏给客户端
    assert all(kind != "tool_calls" for kind, _ in events)
    assert ("finish_reason", 1) in events
    assert events[-1] == ("done", None)


@pytest.mark.asyncio
async def test_streaming_gateway_required_miss_raises_502():
    plan = BypassPlan(tool_name="emit_x", mode="required")
    chunks = [make_chunk([wire_text("direct answer")], finish=1)]
    with pytest.raises(RequestError) as excinfo:
        await _run_stream_gateway(plan, chunks)
    assert excinfo.value.status == 502


@pytest.mark.asyncio
async def test_streaming_gateway_prefill_is_emitted_first_and_deduped():
    plan = BypassPlan(tool_name="emit_x", mode="prefer")
    chunks = [make_chunk([wire_text("PREanswer")], finish=1)]
    events = await _run_stream_gateway(plan, chunks, prefill="PRE")

    body = "".join(str(payload) for kind, payload in events if kind == "body")
    assert body == "PREanswer"  # 预填充先发，模型抄回来的那截被切掉


class _FakeCaptureService:
    def __init__(self, captured: Any) -> None:
        self._captured = captured

    async def capture(self, **kwargs: object) -> Any:
        return self._captured


class _FakeReplayService:
    def __init__(self, raw: str) -> None:
        self._raw = raw

    async def replay(
        self, captured: object, body: str, timeout: int = 120
    ) -> tuple[int, bytes]:
        return 200, self._raw.encode("utf-8")


async def _run_client_generate(captured: Any, raw: str) -> ModelOutput:
    from unittest.mock import AsyncMock, MagicMock

    from aistudio_api.infrastructure.gateway.client import AIStudioClient
    from aistudio_api.infrastructure.gateway.session_state import SessionContext

    client = AIStudioClient(port=9222)
    context = SessionContext(page=MagicMock(), snap_key="snap", auth_user="0", auth_file=None, base_template={})
    client.session.ensure_ready = AsyncMock(return_value=context)
    client.session.clear_prompt = AsyncMock()
    client._capture_service = _FakeCaptureService(captured)  # type: ignore[assignment]
    client._replay_service = _FakeReplayService(raw)  # type: ignore[assignment]
    return await client.generate_content(model="m", capture_prompt="hi", contents=[])


@pytest.mark.asyncio
async def test_client_non_stream_extracts_bypass_content():
    from aistudio_api.infrastructure.gateway.capture import CapturedRequest

    plan = BypassPlan(tool_name="emit_x", mode="prefer", merge_outside=True)
    captured = CapturedRequest(
        url="https://example.com/generate",
        headers={},
        body="[]",
        bypass_plan=plan,
    )
    raw = make_response(
        make_chunk([wire_text("Look: ")]),
        make_chunk([wire_fc("emit_x", {"content": "story"})], finish=1),
    )
    output = await _run_client_generate(captured, raw)
    assert output.text == "Look: story"
    assert output.function_calls == []


@pytest.mark.asyncio
async def test_client_non_stream_required_miss_raises_502():
    from aistudio_api.infrastructure.gateway.capture import CapturedRequest

    captured = CapturedRequest(
        url="https://example.com/generate",
        headers={},
        body="[]",
        bypass_plan=BypassPlan(tool_name="emit_x", mode="required"),
    )
    raw = make_response(make_chunk([wire_text("direct")], finish=1))
    with pytest.raises(RequestError) as excinfo:
        await _run_client_generate(captured, raw)
    assert excinfo.value.status == 502


@pytest.mark.asyncio
async def test_client_non_stream_rejoins_prefill_without_duplication():
    from aistudio_api.infrastructure.gateway.capture import CapturedRequest

    captured = CapturedRequest(
        url="https://example.com/generate",
        headers={},
        body="[]",
        prefill_text="PRE",
    )
    raw = make_response(make_chunk([wire_text("PREanswer")], finish=1))
    output = await _run_client_generate(captured, raw)
    assert output.text == "PREanswer"
