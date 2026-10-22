"""流式 JSON 解析：任意切分 / 截断修复 / 不丢不重。

真实形状：`)]}'\\n` + `[[[candidate...]]]` 帧（外层数组 → 批次 → chunk）。
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
    _repair_truncated_json,
)
from aistudio_api.infrastructure.gateway.wire_parser import parse_response_chunk

TEXT_1 = "你"
TEXT_2 = '好😀\nnext "quote" \\ back'
THINKING = "思考：先想想 😀\n第二行"
TAIL = "tail"


def text_part(text: str) -> list[Any]:
    return [None, text]


def thought_part(text: str) -> list[Any]:
    """真实上游用下标 12 = 1 标记 thought。"""
    part: list[Any] = [None] * 13
    part[1] = text
    part[12] = 1
    return part


def make_chunk(parts: list[Any], finish: int | None = None) -> list[Any]:
    content = [parts, "model"]
    candidate: list[Any] = [content]
    if finish is not None:
        candidate.append(finish)
    return [[candidate]]


CHUNKS = [
    make_chunk([text_part(TEXT_1)]),
    make_chunk([text_part(TEXT_2)]),
    make_chunk([thought_part(THINKING)]),
    make_chunk([text_part(TAIL)], finish=1),
]
STREAM_TEXT = ")]}'\n" + json.dumps([CHUNKS], ensure_ascii=False)
EXPECTED_SIGNATURE = [
    (TEXT_1, "", None),
    (TEXT_2, "", None),
    ("", THINKING, None),
    (TAIL, "", 1),
]


def signature(chunks: list[list[Any]]) -> list[tuple[str, str, int | None]]:
    result: list[tuple[str, str, int | None]] = []
    for chunk in chunks:
        candidate = parse_response_chunk(chunk)
        result.append((candidate.text, candidate.thinking, candidate.finish_reason))
    return result


def drain(parser: IncrementalJSONStreamParser, data: str) -> list[list[Any]]:
    chunks = list(parser.feed(data))
    chunks.extend(parser.finish())
    return chunks


def drain_segments(segments: list[str]) -> list[list[Any]]:
    parser = IncrementalJSONStreamParser()
    chunks: list[list[Any]] = []
    for segment in segments:
        chunks.extend(parser.feed(segment))
    chunks.extend(parser.finish())
    return chunks


# ── 一次喂完 / 任意切分 ──


def test_single_feed_parses_expected_sequence():
    parser = IncrementalJSONStreamParser()
    chunks = drain(parser, STREAM_TEXT)
    assert signature(chunks) == EXPECTED_SIGNATURE


def test_every_two_way_split_matches_single_feed():
    baseline = signature(drain(IncrementalJSONStreamParser(), STREAM_TEXT))
    mismatches: list[tuple[int, list[tuple[str, str, int | None]]]] = []
    for cut in range(len(STREAM_TEXT) + 1):
        got = signature(
            drain_segments([STREAM_TEXT[:cut], STREAM_TEXT[cut:]])
        )
        if got != baseline:
            mismatches.append((cut, got))
    assert mismatches == []
    assert len(baseline) == len(EXPECTED_SIGNATURE)  # 不重复吐 chunk


def test_random_multi_segment_splits_match_single_feed():
    baseline = signature(drain(IncrementalJSONStreamParser(), STREAM_TEXT))
    rng = random.Random(20261019)
    for _ in range(100):
        segment_count = rng.randint(2, 8)
        cuts = sorted(rng.randint(0, len(STREAM_TEXT)) for _ in range(segment_count - 1))
        segments: list[str] = []
        start = 0
        for cut in [*cuts, len(STREAM_TEXT)]:
            segments.append(STREAM_TEXT[start:cut])
            start = cut
        got = signature(drain_segments(segments))
        assert got == baseline, segments
        assert len(got) == len(baseline)


def test_xssi_prefix_split_across_feeds():
    prefix = list(")]}'\n")
    segments = [*prefix, STREAM_TEXT[5:]]
    assert signature(drain_segments(segments)) == EXPECTED_SIGNATURE


def test_identical_adjacent_chunks_are_both_emitted():
    chunks = [make_chunk([text_part("same")]), make_chunk([text_part("same")])]
    stream = ")]}'\n" + json.dumps([chunks], ensure_ascii=False)
    got = signature(drain(IncrementalJSONStreamParser(), stream))
    assert got == [("same", "", None), ("same", "", None)]


def test_long_stream_keeps_text_and_trims_buffer():
    chunks = [
        make_chunk([text_part(f"第{i}段😀")], finish=1 if i == 299 else None)
        for i in range(300)
    ]
    expected = "".join(f"第{i}段😀" for i in range(300))
    stream = ")]}'\n" + json.dumps([chunks], ensure_ascii=False)
    parser = IncrementalJSONStreamParser()
    chunks_out: list[list[Any]] = []
    for start in range(0, len(stream), 37):
        chunks_out.extend(parser.feed(stream[start : start + 37]))
    chunks_out.extend(parser.finish())
    assert len(chunks_out) == 300
    assert "".join(parse_response_chunk(chunk).text for chunk in chunks_out) == expected
    assert len(parser.buffer) <= 8  # 每块解析完就修剪，不会越攒越大


def test_feed_repairs_invalid_complete_chunk_with_raw_control_char():
    """完整分块但 JSON 非法（字符串里有裸换行）：feed 里先修复，不能整块丢掉。"""
    invalid_stream = STREAM_TEXT.replace("\\nnext", "\nnext")
    assert json.loads(STREAM_TEXT[4:])  # 原始串本身是合法 JSON
    got = signature(drain(IncrementalJSONStreamParser(), invalid_stream))
    assert got == EXPECTED_SIGNATURE


# ── 截断 + finish() 抢救 ──


def _cut_truncation_cases() -> list[tuple[str, str, str]]:
    emoji_cut = STREAM_TEXT.index("好😀") + len("好😀")
    escape_cut = STREAM_TEXT.index("\\nnext") + 1  # 反斜杠之后、n 之前
    quote_cut = STREAM_TEXT.index('\\"quote') + 2  # 转义引号之后
    return [
        ("mid_text", STREAM_TEXT[:emoji_cut], "好😀"),
        ("after_backslash", STREAM_TEXT[:escape_cut], "好😀"),
        ("after_escaped_quote", STREAM_TEXT[:quote_cut], '好😀\nnext "'),
    ]


@pytest.mark.parametrize(
    ("name", "truncated", "expected_text"),
    _cut_truncation_cases(),
    ids=["mid_text", "after_backslash", "after_escaped_quote"],
)
def test_finish_salvages_truncated_text(name: str, truncated: str, expected_text: str):
    parser = IncrementalJSONStreamParser()
    # 第一块（"你"）完整；切点落在第二块内部
    assert signature(list(parser.feed(truncated))) == [(TEXT_1, "", None)]
    salvaged = list(parser.finish())
    assert len(salvaged) == 1, name
    assert parse_response_chunk(salvaged[0]).text == expected_text
    # 重复调用 finish 不能重复吐
    assert list(parser.finish()) == []


def test_finish_salvages_truncated_thought_part():
    cut = STREAM_TEXT.index("先想想") + 2
    parser = IncrementalJSONStreamParser()
    list(parser.feed(STREAM_TEXT[:cut]))
    salvaged = list(parser.finish())
    assert len(salvaged) == 1
    candidate = parse_response_chunk(salvaged[0])
    # thought 标记在 wire 里排在文本之后，截断会把它丢掉：内容能救，但可能落到
    # 正文通道（能救多少救多少；至少不丢字）
    assert candidate.thinking + candidate.text == "思考：先想"


def test_finish_saves_text_when_first_chunk_was_truncated():
    cut = STREAM_TEXT.index(TEXT_1) + len(TEXT_1)
    parser = IncrementalJSONStreamParser()
    assert list(parser.feed(STREAM_TEXT[:cut])) == []
    salvaged = list(parser.finish())
    assert len(salvaged) == 1
    assert parse_response_chunk(salvaged[0]).text == TEXT_1


def test_finish_is_noop_for_complete_stream():
    parser = IncrementalJSONStreamParser()
    assert len(drain(parser, STREAM_TEXT)) == len(EXPECTED_SIGNATURE)
    assert list(parser.finish()) == []
    assert list(parser.finish()) == []


def test_finish_is_noop_when_nothing_salvageable():
    # 结尾只剩批次层的逗号/空白：没有半个 chunk 可救
    parser = IncrementalJSONStreamParser()
    assert len(list(parser.feed(STREAM_TEXT))) == len(EXPECTED_SIGNATURE)
    assert list(parser.finish()) == []
    # 刚开一个数组但没有 candidate 内容，也救不出东西
    parser2 = IncrementalJSONStreamParser()
    list(parser2.feed(")]}'\n[[["))
    assert list(parser2.finish()) == []


# ── _repair_truncated_json 单元样例 ──


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ('[[[[[[null, "好😀', [[[[[[None, "好😀"]]]]]]),
        ("[1, 2,", [1, 2]),
        ('{"a": 1', {"a": 1}),
        ('{"a": [1, {"b": "c', {"a": [1, {"b": "c"}]}),
        ('[1, 2, {"k": }', [1, 2]),
        ('[null, "x', [None, "x"]),
    ],
)
def test_repair_truncated_json_salvages_prefix(fragment: str, expected: object):
    assert _repair_truncated_json(fragment) == expected


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        ('{"a": "x' + "\\", {"a": "x"}),
        ('{"a": "x\\u12', {"a": "x"}),
        ('{"a": "x\\u00e', {"a": "x"}),
    ],
)
def test_repair_truncated_json_handles_broken_escapes(
    fragment: str, expected: object
):
    assert _repair_truncated_json(fragment) == expected


@pytest.mark.parametrize(
    "fragment",
    ["", "   ", "garbage", "tru", '{"a": tru'],
)
def test_repair_truncated_json_returns_none_when_unrecoverable(fragment: str):
    assert _repair_truncated_json(fragment) is None


def test_repair_truncated_json_keeps_already_valid_json():
    assert _repair_truncated_json("[1, 2]") == [1, 2]
    assert _repair_truncated_json('"plain"') == "plain"


def test_repair_truncated_json_empty_array_is_harmless():
    assert _repair_truncated_json("[") == []


@pytest.mark.parametrize("chunk", [
    make_chunk([], finish=1),
    [None, None, [5, 2, 7]],
    [None, None, None, None, None, None, None, "rid"],
    make_chunk([thought_part("thought")]),
    [make_chunk([])[0] + make_chunk([text_part("second")], finish=2)[0]],
])
def test_finish_preserves_metadata_and_nonprimary_candidate_chunks(chunk):
    parser = IncrementalJSONStreamParser()
    assert list(parser.feed("[[" + json.dumps(chunk)[:-1])) == []
    assert list(parser.finish()) == [chunk]
    assert list(parser.finish()) == []


@pytest.mark.parametrize(("fragment", "expected"), [
    ('{"good": 1, "bad": tru', {"good": 1}),
    ("[[1, 2, tru", [[1, 2]]),
    ('[1, 2, {"k": }', [1, 2]),
])
def test_repair_preserves_complete_nested_siblings(fragment, expected):
    assert _repair_truncated_json(fragment) == expected


def test_finish_preserves_text_and_valid_args_before_nested_partial_token():
    chunk = make_chunk([
        text_part("kept"),
        [None, None, None, ["f", {"good": 1, "bad": True}]],
    ])
    full = "[[" + json.dumps(chunk)
    parser = IncrementalJSONStreamParser()
    list(parser.feed(full[:full.index("true") + 3]))
    salvaged = list(parser.finish())
    assert len(salvaged) == 1
    candidate = parse_response_chunk(salvaged[0])
    assert candidate.text == "kept"
    assert candidate.function_calls[0]["args"] == {"good": 1}
