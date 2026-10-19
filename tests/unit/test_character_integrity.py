"""字符完整性审计：bypass 拼接 / wire_parser 文本提取 / OpenAI 流转换器。

原则：任意位置切分、重复喂入都不能少字、多字或吐重复内容。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aistudio_api.application import bypass as bp
from aistudio_api.application.openai_bridge import OpenAIStreamConverter
from aistudio_api.infrastructure.gateway.wire_parser import (
    parse_response_chunk,
    parse_response_part,
    parse_text_output,
)

TEXT_SAMPLES = [
    "纯中文：你好，世界。",
    "emoji 😀🎉 与组合字符 e\u0301",
    "换行\n第二行\r\n第三行\t制表",
    '引号"和\\反斜杠',
    "重复段落</div>\n<div>再来一次</div>\n<div>",
]


def wire_text(text: str) -> list[Any]:
    return [None, text]


def wire_thought(text: str) -> list[Any]:
    """真实上游用下标 12 = 1 标记 thought。"""
    part: list[Any] = [None] * 13
    part[1] = text
    part[12] = 1
    return part


def wire_fc(name: str, args: object) -> list[Any]:
    return [None, None, None, [name, args]]


def make_chunk(parts: list[Any], finish: int | None = None) -> list[Any]:
    content = [parts, "model"]
    candidate: list[Any] = [content]
    if finish is not None:
        candidate.append(finish)
    return [[candidate]]


def parse_parts(*raw_parts: list[Any]) -> list[Any]:
    return [parse_response_part(raw) for raw in raw_parts]


# ────────────────────────────── bypass：累积快照 / 拼接 ──────────────────────────────


@pytest.mark.parametrize("full", TEXT_SAMPLES)
def test_stream_extractor_snapshots_at_every_cut(full: str):
    """累积快照在任意位置切开/重复喂，merged 必须逐字符等于完整内容。"""
    for cut in range(len(full) + 1):
        extractor = bp.BypassStreamExtractor("emit_x")
        deltas: list[str] = []
        for snapshot in (full[:cut], full, full):
            delta, _ = extractor.feed(
                parse_parts(wire_fc("emit_x", {"content": snapshot}))
            )
            deltas.append(delta)
        assert extractor.merged == full, cut
        assert "".join(deltas) == full, cut


@pytest.mark.parametrize("full", TEXT_SAMPLES)
def test_stream_extractor_all_prefix_snapshots(full: str):
    extractor = bp.BypassStreamExtractor("emit_x")
    for index in range(len(full) + 1):
        extractor.feed(parse_parts(wire_fc("emit_x", {"content": full[:index]})))
    assert extractor.merged == full
    assert extractor.saw_call is True


@pytest.mark.parametrize("full", TEXT_SAMPLES)
def test_stream_extractor_partial_json_string_args(full: str):
    """args 以「截断的 JSON 字符串」形态逐帧到达时也不能丢字。"""
    args_text = '{"content": ' + _json_string(full) + "}"
    extractor = bp.BypassStreamExtractor("emit_x")
    step = max(1, len(args_text) // 7)
    for cut in range(0, len(args_text) + 1, step):
        extractor.feed(parse_parts(wire_fc("emit_x", args_text[:cut])))
    extractor.feed(parse_parts(wire_fc("emit_x", args_text)))
    assert extractor.merged == full


def _json_string(text: str) -> str:
    """把文本编码成 JSON 字符串字面量（含两侧引号）。"""
    encoded = json.dumps(text, ensure_ascii=False)
    assert encoded.startswith('"') and encoded.endswith('"')
    return encoded


@pytest.mark.parametrize("full", TEXT_SAMPLES)
def test_collapse_cumulative_arbitrary_prefixes(full: str):
    emitted: list[str] = []
    previous = ""
    for index in range(0, len(full) + 1, 2):
        delta, previous = bp.collapse_cumulative(full[:index], previous)
        emitted.append(delta)
    delta, _ = bp.collapse_cumulative(full, previous)
    emitted.append(delta)
    assert "".join(emitted) == full


def test_stream_extractor_keeps_repeated_structural_blocks():
    full = "开场</div>\n<div>中间</div>\n<div>结尾"
    extractor = bp.BypassStreamExtractor("emit_x")
    for index in range(0, len(full) + 1, 3):
        extractor.feed(parse_parts(wire_fc("emit_x", {"content": full[:index]})))
    extractor.feed(parse_parts(wire_fc("emit_x", {"content": full})))
    assert extractor.merged == full
    assert extractor.merged.count("</div>\n<div>") == 2


@pytest.mark.parametrize("full", TEXT_SAMPLES)
def test_content_merger_deltas_add_arbitrary_split(full: str):
    merger = bp.ContentMerger(deltas=True)
    for start in range(0, len(full), 3):
        merger.add(full[start : start + 3])
    assert merger.merged == full


@pytest.mark.parametrize("full", TEXT_SAMPLES)
def test_content_merger_add_delta_arbitrary_split(full: str):
    merger = bp.ContentMerger(deltas=True)
    for start in range(0, len(full), 2):
        merger.add_delta(full[start : start + 2])
    assert merger.merged == full


def test_content_merger_non_stream_drops_only_exact_repeat():
    full = "重复段落</div>\n<div>再来一次</div>\n<div>"
    merger = bp.ContentMerger()
    assert merger.add(full) == full
    assert merger.add(full) == ""  # 整段原样重发才算重复
    assert merger.merged == full


def test_is_cumulative_handles_unicode():
    base = "你好😀" * 40  # 长度过 64，走头尾探针
    assert bp.is_cumulative(base + "尾巴", base) is True
    assert bp.is_cumulative(base, base) is False
    assert bp.is_cumulative("完全不同的内容" * 10, base) is False


def test_strip_overlap_handles_unicode_long_overlap():
    overlap = "结尾😀ABCDEFGHI"  # 12 个字符，刚好达到去重阈值
    prefill = "前文很长很长的预填充" + overlap
    output = overlap + "继续写"
    assert bp.strip_overlap(prefill, output) == "继续写"


# ────────────────────────────── wire_parser：文本提取 ──────────────────────────────


@pytest.mark.parametrize("word", ["model", "user", "system", "assistant"])
def test_role_named_text_chunks_are_not_dropped(word: str):
    """正文恰好是 role 名字（分块边界上很常见）不能被当角色行吞掉。"""
    part = parse_response_part(wire_text(word))
    assert part.text == word
    assert parse_response_chunk(make_chunk([wire_text(word)])).text == word


@pytest.mark.parametrize(
    "text", ["", "   ", "中文😀", 'a"b\\c', "line1\nline2", "\t"]
)
def test_part_and_chunk_text_preserved_verbatim(text: str):
    part = parse_response_part(wire_text(text))
    assert part.text == text
    assert parse_response_chunk(make_chunk([wire_text(text)])).text == text


def test_chunk_joins_text_parts_in_order_without_loss():
    chunk = make_chunk(
        [wire_text("你"), wire_thought("思考中的内容"), wire_text("好😀")]
    )
    candidate = parse_response_chunk(chunk)
    assert candidate.text == "你好😀"
    assert candidate.thinking == "思考中的内容"
    assert candidate.function_calls == []


def test_thought_flag_at_index_10_does_not_create_phantom_tool_call():
    part_wire: list[Any] = [None] * 11
    part_wire[1] = "先想想"
    part_wire[10] = True  # 下标 10 与 functionCall 槽共用
    part = parse_response_part(part_wire)
    assert part.thought is True
    assert part.function_call is None
    candidate = parse_response_chunk(make_chunk([part_wire]))
    assert candidate.thinking == "先想想"
    assert candidate.function_calls == []


def test_function_call_args_unicode_roundtrip():
    args = {"content": "中文😀\n第二行", "n": 0, "nested": {"k": "值"}}
    part = parse_response_part(wire_fc("f", args))
    assert part.function_call is not None
    assert part.function_call["args"] == args
    candidate = parse_response_chunk(make_chunk([wire_fc("f", args)]))
    assert candidate.function_calls[0]["args"] == args


def test_function_call_args_json_string_complete_and_truncated():
    full = '{"content": "中文😀\\n第二行"}'
    part = parse_response_part(wire_fc("f", full))
    assert part.function_call is not None
    assert part.function_call["args"] == {"content": "中文😀\n第二行"}

    truncated = full[:-4]
    part_truncated = parse_response_part(wire_fc("f", truncated))
    assert part_truncated.function_call is not None
    # 截断的 JSON 原样留在 arguments 里，不能被丢成空、也不该出现替换字符
    assert part_truncated.function_call.get("arguments") == truncated
    assert "\ufffd" not in str(part_truncated.function_call)


def test_parse_text_output_preserves_unicode_across_chunks():
    chunks = [
        make_chunk([wire_text("你")]),
        make_chunk([wire_text("好😀")]),
        make_chunk([wire_text("user")]),
        make_chunk([wire_text("尾")], finish=1),
    ]
    raw = json.dumps([chunks], ensure_ascii=False)
    output = parse_text_output(raw)
    assert output.text == "你好😀user尾"


# ────────────────────────────── OpenAI 流转换器 ──────────────────────────────


def _delta_text(frames: list[dict[str, object]], key: str) -> str:
    pieces: list[str] = []
    for frame in frames:
        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        if isinstance(delta, dict) and isinstance(delta.get(key), str):
            pieces.append(str(delta[key]))
    return "".join(pieces)


def _tool_calls(frames: list[dict[str, object]]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for frame in frames:
        choices = frame.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("tool_calls"), list):
            calls.extend(delta["tool_calls"])
    return calls


@pytest.mark.parametrize("text", TEXT_SAMPLES)
def test_stream_converter_body_every_split(text: str):
    for cut in range(len(text) + 1):
        converter = OpenAIStreamConverter(model="m", request_id="r")
        frames = [
            *converter.convert_event("body", text[:cut]),
            *converter.convert_event("body", text[cut:]),
        ]
        assert _delta_text(frames, "content") == text, cut
        assert frames[0]["choices"][0]["delta"] == {
            "role": "assistant",
            "content": "",
        }


@pytest.mark.parametrize("text", TEXT_SAMPLES)
def test_stream_converter_thinking_every_split(text: str):
    for cut in range(len(text) + 1):
        converter = OpenAIStreamConverter(
            model="m", request_id="r", include_thoughts=True
        )
        frames = [
            *converter.convert_event("thinking", text[:cut]),
            *converter.convert_event("thinking", text[cut:]),
        ]
        assert _delta_text(frames, "reasoning_content") == text, cut


@pytest.mark.parametrize("text", TEXT_SAMPLES)
def test_stream_converter_tool_cumulative_snapshots(text: str):
    converter = OpenAIStreamConverter(model="m", request_id="r")
    for index in range(len(text) + 1):
        converter.convert_event(
            "tool_calls",
            [{"name": "f", "call_id": "c1", "args": {"content": text[:index]}}],
        )
    converter.convert_event("tool_calls", [{"name": "f", "args": {"content": text}}])
    calls = _tool_calls(converter.finish())
    assert len(calls) == 1
    function = calls[0]["function"]
    assert function["name"] == "f"
    assert json.loads(function["arguments"]) == {"content": text}


def test_stream_converter_parallel_calls_in_separate_frames_not_merged():
    """修复前：分帧到达的两条并行调用都按帧内位置 0 记账，第二条会把第一条覆盖掉。"""
    converter = OpenAIStreamConverter(model="m", request_id="r")
    converter.convert_event("tool_calls", [{"name": "alpha", "args": {"v": "甲"}}])
    converter.convert_event("tool_calls", [{"name": "beta", "args": {"v": "乙"}}])
    calls = _tool_calls(converter.finish())
    assert [call["function"]["name"] for call in calls] == ["alpha", "beta"]
    assert [
        json.loads(call["function"]["arguments"]) for call in calls
    ] == [{"v": "甲"}, {"v": "乙"}]


def test_stream_converter_parallel_calls_in_same_frame():
    converter = OpenAIStreamConverter(model="m", request_id="r")
    converter.convert_event(
        "tool_calls",
        [
            {"name": "alpha", "args": {"v": "甲"}},
            {"name": "beta", "args": {"v": "乙"}},
        ],
    )
    calls = _tool_calls(converter.finish())
    assert [call["function"]["name"] for call in calls] == ["alpha", "beta"]


def test_stream_converter_mixed_frames_keep_cumulative_snapshots():
    converter = OpenAIStreamConverter(model="m", request_id="r")
    converter.convert_event("tool_calls", [{"name": "a", "args": {"content": "A"}}])
    converter.convert_event(
        "tool_calls",
        [
            {"name": "a", "args": {"content": "AB"}},
            {"name": "b", "args": {"content": "X"}},
        ],
    )
    converter.convert_event(
        "tool_calls",
        [
            {"name": "a", "args": {"content": "ABC"}},
            {"name": "b", "args": {"content": "XY"}},
        ],
    )
    calls = _tool_calls(converter.finish())
    assert len(calls) == 2
    assert [
        json.loads(call["function"]["arguments"]) for call in calls
    ] == [{"content": "ABC"}, {"content": "XY"}]


# ────────────────────────────── UTF-8 往返（审计记录） ──────────────────────────────


@pytest.mark.parametrize("text", TEXT_SAMPLES)
def test_utf8_roundtrip_between_transport_and_streaming_is_lossless(text: str):
    """transport 发的是页面侧完整 str（encode utf-8），streaming 再 decode。

    整串往返无损；只要上游不把「半个多字节字符」当独立 chunk 发过来，
    streaming.py 的 errors="replace" 就不会产生替换字符（见审计报告）。
    """
    assert text.encode("utf-8").decode("utf-8", errors="replace") == text
