"""预填充（prefill）兼容的单元测试（对齐 build2api src/prefill.js）。"""

from __future__ import annotations

from aistudio_api.application import prefill as pf
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart


def _user(text: str) -> AistudioContent:
    return AistudioContent(role="user", parts=[AistudioPart(text=text)])


def _model(text: str) -> AistudioContent:
    return AistudioContent(role="model", parts=[AistudioPart(text=text)])


# ── 未闭合标签检测 ──


def test_detect_unclosed_tag_finds_open_tag_at_end():
    assert pf.detect_unclosed_tag("<thinking>我先想想") == "thinking"
    assert pf.detect_unclosed_tag("a <b><c>deep") == "c"


def test_detect_unclosed_tag_returns_none_when_closed():
    assert pf.detect_unclosed_tag("<thinking>abc</thinking>") is None
    assert pf.detect_unclosed_tag("<a><b>x</b></a>") is None
    assert pf.detect_unclosed_tag("no tags at all") is None


def test_detect_unclosed_tag_ignores_void_and_self_closing_tags():
    assert pf.detect_unclosed_tag("line<br>next") is None
    assert pf.detect_unclosed_tag("<img src='x'>") is None
    assert pf.detect_unclosed_tag("<tag/>") is None


def test_detect_unclosed_tag_ignores_false_positives_in_prose():
    # 裸尖括号凑不出开标签：前面紧挨字母数字的一律不认
    assert pf.detect_unclosed_tag("if a<b and b>c") is None
    assert pf.detect_unclosed_tag("用 List<String> 存") is None


def test_detect_unclosed_tag_implicitly_closes_nested_tags():
    # 闭标签隐式关掉它上面所有还开着的标签（同浏览器 HTML 解析）
    assert pf.detect_unclosed_tag("<a>step <b> mentioned </a>") is None


def test_detect_unclosed_tag_handles_attributes():
    assert pf.detect_unclosed_tag('<div class="x">hi') == "div"


# ── 续写指令 ──


def test_build_nudge_default_and_custom():
    assert pf.build_nudge("") == pf.DEFAULT_NUDGE
    assert pf.build_nudge("", nudge="  custom  ") == "custom"


def test_build_nudge_adds_cot_guard_for_unclosed_tag():
    nudge = pf.build_nudge("<thinking>开头", cot_guard=True)
    assert nudge.startswith(pf.DEFAULT_NUDGE)
    assert "<thinking>" in nudge
    assert "</thinking>" in nudge
    assert "写" in nudge  # 「先补完并闭合它」


def test_build_nudge_respects_cot_guard_switch():
    nudge = pf.build_nudge("<thinking>开头", cot_guard=False)
    assert nudge == pf.DEFAULT_NUDGE


# ── 请求改写 ──


def test_apply_prefill_appends_a_new_user_turn():
    contents = [_user("hi"), _model("<thinking>我先")]
    prefill = pf.apply_prefill(contents)

    assert prefill == "<thinking>我先"
    assert [content.role for content in contents] == ["user", "model", "user"]
    # 预填充那条 model 轮一个 part 都不该被动过
    assert len(contents[1].parts) == 1
    assert contents[1].parts[0].text == "<thinking>我先"
    assert "续" in contents[2].parts[0].text or "继续" in contents[2].parts[0].text
    assert "</thinking>" in contents[2].parts[0].text


def test_apply_prefill_trims_trailing_empty_contents():
    contents = [
        _user("hi"),
        _model("prefill"),
        AistudioContent(role="model", parts=[AistudioPart()]),
    ]
    prefill = pf.apply_prefill(contents)
    assert prefill == "prefill"
    assert [content.role for content in contents] == ["user", "model", "user"]


def test_apply_prefill_ignores_thought_parts_when_joining():
    contents = [
        _user("hi"),
        AistudioContent(
            role="model",
            parts=[
                AistudioPart(text="<thinking>想",
                thought=True),
                AistudioPart(text="正文开头"),
            ],
        ),
    ]
    assert pf.apply_prefill(contents) == "正文开头"


def test_apply_prefill_does_not_treat_tool_call_ending_as_prefill():
    contents = [
        _user("hi"),
        AistudioContent(
            role="model", parts=[AistudioPart(function_call=("real_tool", {}))]
        ),
    ]
    assert pf.apply_prefill(contents) == ""
    assert len(contents) == 2


def test_apply_prefill_skips_image_ending():
    contents = [
        _user("hi"),
        AistudioContent(
            role="model", parts=[AistudioPart(inline_data=("image/png", "AAA"))]
        ),
    ]
    assert pf.apply_prefill(contents) == ""
    assert len(contents) == 2


def test_apply_prefill_returns_empty_for_user_last_request():
    contents = [_user("hi")]
    assert pf.apply_prefill(contents) == ""
    assert len(contents) == 1


def test_apply_prefill_ensures_user_last_for_blank_model_turn():
    """空白的 model 结尾当不了预填充，但仍要补一条续写指令，否则 3.x 直接 400。"""
    contents = [_user("hi"), _model("   ")]
    assert pf.apply_prefill(contents) == ""
    assert [content.role for content in contents] == ["user", "model", "user"]
    assert contents[-1].parts[0].text


def test_apply_prefill_disabled_is_noop():
    contents = [_user("hi"), _model("prefill")]
    assert pf.apply_prefill(contents, enabled=False) == ""
    assert len(contents) == 2


def test_apply_prefill_respects_custom_nudge():
    contents = [_user("hi"), _model("prefill")]
    pf.apply_prefill(contents, nudge="继续写 {tool}", cot_guard=False)
    assert contents[-1].parts[0].text == "继续写 {tool}"


# ── 响应拼接 ──


def test_strip_overlap_removes_repeated_prefill():
    assert pf.strip_overlap("ABC", "ABCdef") == "def"
    assert pf.strip_overlap("0123456789", "0123456789tail") == "tail"
    # 前缀重复无条件切；短于最小重叠长度只影响「后缀重叠」的判定
    assert pf.strip_overlap("AB", "ABcd") == "cd"
    assert pf.strip_overlap("0123456", "3456xyz") == "3456xyz"
    assert pf.strip_overlap("", "output") == "output"


def test_strip_overlap_keeps_non_overlapping_output():
    assert pf.strip_overlap("prefill text", "something else") == "something else"


def test_rejoin_prepends_prefill_once():
    assert pf.rejoin("ABC", "ABCdef") == "ABCdef"
    assert pf.rejoin("ABC", "def") == "ABCdef"
    assert pf.rejoin("", "def") == "def"


def test_longest_overlap_known_values():
    assert pf.longest_overlap("prefix overlapping", "overlapping tail") == 11
    assert pf.longest_overlap("abc", "ab") == 0  # window < min_overlap
    assert pf.longest_overlap("hello world", "world!!!") == 0  # 5 < min_overlap


def test_stream_deduper_strips_overlap_split_across_chunks():
    deduper = pf.StreamDeduper("ABCDEF")
    emitted = [
        deduper.feed("AB"),
        deduper.feed("CD"),
        deduper.feed("EFxyz"),
    ]
    assert "".join(emitted) == "xyz"
    assert "".join(emitted) + deduper.flush() == "xyz"


def test_stream_deduper_passes_through_when_no_overlap():
    deduper = pf.StreamDeduper("prefill")
    out = deduper.feed("完全不同")
    assert out == "完全不同"
    assert deduper.buffered == 0


def test_stream_deduper_flush_releases_buffer():
    deduper = pf.StreamDeduper("ABCDEF")
    assert deduper.feed("AB") == ""
    assert deduper.buffered == 2
    # 不构成重叠时 flush 要原样放出来
    assert deduper.flush() == "AB"
    assert deduper.flush() == ""


def test_stream_deduper_without_prefill_is_pass_through():
    deduper = pf.StreamDeduper("")
    assert deduper.feed("anything") == "anything"
    assert deduper.flush() == ""
