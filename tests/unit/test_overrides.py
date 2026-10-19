"""参数覆写层单测（thinkingMode / thinkingLevel / includeThoughts / max_tokens / 采样）。"""

from __future__ import annotations

import pytest

from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiPart,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.overrides import (
    ClientThinking,
    apply_overrides,
    clamp_level,
    effort_to_level,
    get_model_capabilities,
    resolve_thinking,
    wire_thinking_level,
    wire_thinking_mode,
)

DEFAULTS = {
    "thinkingMode": "request",
    "thinkingLevel": "",
    "includeThoughts": True,
    "dropMaxTokens": False,
    "maxOutputTokens": None,
    "temperature": None,
    "topP": None,
    "topK": None,
}


def _settings(**overrides):
    values = dict(DEFAULTS)
    values.update(overrides)
    return values


def _prepare(model: str, **generation_config):
    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hi")])],
        generationConfig=(
            GeminiGenerationConfig(**generation_config) if generation_config else None
        ),
    )
    normalized = normalize_gemini_request(req, model)
    return req, normalized


# ── 模型能力：档位收敛 ──


def test_gemini_3_pro_only_supports_low_and_high():
    caps = get_model_capabilities("gemini-3-pro-preview")
    assert caps.thinking_kind == "level"
    assert caps.levels == ("low", "high")
    # medium 落到最近的一档（等距时取靠前的那档，与主项目一致）
    assert clamp_level(caps, "medium") == "low"
    assert clamp_level(caps, "minimal") == "low"
    assert clamp_level(caps, "high") == "high"
    assert clamp_level(caps, "bogus") == "high"


def test_gemini_3_flash_supports_all_levels_and_deprecates_sampling():
    caps = get_model_capabilities("gemini-3.8-flash")
    # 3.8-flash 实测不接受 MINIMAL（上游 400），最低档是 low；minimal 会被收敛到 low
    assert caps.levels == ("low", "medium", "high")
    assert caps.sampling_deprecated is True
    assert clamp_level(caps, "medium") == "medium"
    assert clamp_level(caps, "minimal") == "low"


def test_gemini_legacy_and_gemma_have_no_thinking_levels():
    assert get_model_capabilities("gemini-2.5-pro").thinking_kind == "none"
    assert clamp_level(get_model_capabilities("gemini-2.5-pro"), "high") is None
    assert get_model_capabilities("gemma-4-31b-it").thinking_kind == "none"


def test_model_caps_uses_upstream_thinking_levels_when_available():
    caps = get_model_capabilities(
        "gemini-4-future", discovery={"thinkingLevels": [1, 3], "is_image_model": False}
    )
    assert caps.levels == ("low", "high")


def test_effort_to_level_maps_openai_reasoning_effort():
    assert effort_to_level("none") == "minimal"
    assert effort_to_level("minimal") == "minimal"
    assert effort_to_level("low") == "low"
    assert effort_to_level("medium") == "medium"
    assert effort_to_level("high") == "high"
    assert effort_to_level("unknown") is None


def test_wire_helpers_roundtrip_level():
    assert wire_thinking_level([1, None, None, 3]) == "high"
    assert wire_thinking_level([9, None, None, 4]) == "minimal"
    assert wire_thinking_level([1, None, None]) is None
    assert wire_thinking_mode([9, None, None, 3]) == 9
    assert wire_thinking_mode(None) == 1


# ── resolve_thinking：三种模式 ──


def test_request_mode_follows_client_level():
    caps = get_model_capabilities("gemini-3.8-flash")
    resolution = resolve_thinking(
        caps, ClientThinking(level="high"), _settings()
    )
    assert resolution.thinking_config == [1, None, None, 3]
    assert resolution.include_thoughts is True
    assert resolution.level == "high"


def test_request_mode_clamps_client_level_to_model_support():
    caps = get_model_capabilities("gemini-3-pro-preview")
    resolution = resolve_thinking(caps, ClientThinking(level="medium"), _settings())
    assert resolution.level == "low"
    assert resolution.thinking_config == [1, None, None, 1]


def test_request_mode_without_client_thinking_leaves_config_untouched():
    caps = get_model_capabilities("gemini-3.8-flash")
    resolution = resolve_thinking(caps, None, _settings())
    assert resolution.thinking_config is None
    assert resolution.include_thoughts is True


def test_client_include_thoughts_wins_in_request_mode():
    caps = get_model_capabilities("gemini-3.8-flash")
    resolution = resolve_thinking(
        caps,
        ClientThinking(level="high", include_thoughts=False),
        _settings(includeThoughts=True),
    )
    assert resolution.include_thoughts is False
    # 这条 wire 没有 include_thoughts 字段；档位仍按客户端的来
    assert resolution.thinking_config == [1, None, None, 3]


def test_include_thoughts_off_requests_lowest_level_when_client_gave_none():
    caps = get_model_capabilities("gemini-3.8-flash")
    resolution = resolve_thinking(caps, None, _settings(includeThoughts=False))
    assert resolution.include_thoughts is False
    # 没有 include_thoughts 可传，请求侧降到最低档（3.8-flash 实测最低是 LOW，
    # MINIMAL 会被上游 400 拒绝）
    assert resolution.thinking_config == [1, None, None, 1]


def test_off_mode_forces_lowest_level_and_no_thoughts():
    caps = get_model_capabilities("gemini-3-pro-preview")
    resolution = resolve_thinking(
        caps, ClientThinking(level="high"), _settings(thinkingMode="off")
    )
    assert resolution.include_thoughts is False
    assert resolution.thinking_config == [1, None, None, 1]


def test_console_mode_uses_setting_level_clamped_to_model():
    caps = get_model_capabilities("gemini-3-pro-preview")
    resolution = resolve_thinking(
        caps,
        ClientThinking(level="high"),
        _settings(thinkingMode="console", thinkingLevel="medium"),
    )
    assert resolution.level == "low"
    assert resolution.thinking_config == [1, None, None, 1]


def test_console_mode_empty_level_uses_model_default():
    caps = get_model_capabilities("gemini-3.8-flash")
    resolution = resolve_thinking(
        caps, ClientThinking(level="high"), _settings(thinkingMode="console")
    )
    assert resolution.thinking_config is None
    assert resolution.level is None


def test_console_mode_keeps_include_thoughts_flag():
    caps = get_model_capabilities("gemini-3.8-flash")
    resolution = resolve_thinking(
        caps,
        ClientThinking(level="high", include_thoughts=True),
        _settings(thinkingMode="console", thinkingLevel="low", includeThoughts=False),
    )
    assert resolution.include_thoughts is False


def test_legacy_models_never_get_a_level():
    caps = get_model_capabilities("gemini-2.5-pro")
    resolution = resolve_thinking(
        caps,
        ClientThinking(level="high"),
        _settings(thinkingMode="console", thinkingLevel="high"),
    )
    assert resolution.thinking_config is None


def test_off_mode_on_legacy_model_still_suppresses_reasoning():
    caps = get_model_capabilities("gemini-2.5-pro")
    resolution = resolve_thinking(caps, None, _settings(thinkingMode="off"))
    assert resolution.thinking_config is None
    assert resolution.include_thoughts is False


# ── apply_overrides：采样参数 ──


def test_sampling_overrides_apply_and_clamp_top_k():
    req, normalized = _prepare(
        "gemini-3-pro-preview", temperature=0.9, topP=0.9, topK=12
    )
    result = apply_overrides(
        normalized,
        request=req,
        settings=_settings(temperature=0.2, topP=0.5, topK=100),
    )
    assert normalized.temperature == 0.2
    assert normalized.top_p == 0.5
    assert normalized.top_k == 63
    assert "temperature=0.2" in result.applied
    assert "topK->63" not in result.applied


def test_sampling_deprecated_model_strips_client_and_override_values():
    req, normalized = _prepare("gemini-3.8-flash", temperature=0.9, topK=12)
    result = apply_overrides(
        normalized, request=req, settings=_settings(temperature=0.2)
    )
    assert normalized.temperature is None
    assert normalized.top_k is None
    assert "sampling-stripped(model-unsupported)" in result.applied


def test_image_model_keeps_sampling_parameters():
    req, normalized = _prepare("gemini-3.1-flash-image-preview", temperature=0.9)
    apply_overrides(normalized, request=req, settings=_settings())
    # 生图模型在 AI Studio wire 上照常接受采样参数，不能被 flash 的废弃规则误伤
    assert normalized.temperature == 0.9


# ── apply_overrides：max_tokens ──


def test_drop_max_tokens_removes_client_limit():
    req, normalized = _prepare("gemini-3-pro-preview", maxOutputTokens=128)
    assert normalized.generation_config_overrides["max_tokens"] == 128
    result = apply_overrides(normalized, request=req, settings=_settings(dropMaxTokens=True))
    assert normalized.max_tokens is None
    assert "max_tokens" not in normalized.generation_config_overrides
    assert "dropMaxTokens" in result.applied


def test_max_output_tokens_override_wins_over_client_value():
    req, normalized = _prepare("gemini-3-pro-preview", maxOutputTokens=128)
    result = apply_overrides(
        normalized, request=req, settings=_settings(maxOutputTokens=512)
    )
    assert normalized.max_tokens == 512
    assert normalized.generation_config_overrides["max_tokens"] == 512
    assert "maxOutputTokens=512" in result.applied


def test_leave_empty_keeps_client_values():
    req, normalized = _prepare(
        "gemini-3-pro-preview", temperature=0.9, maxOutputTokens=128
    )
    apply_overrides(normalized, request=req, settings=_settings())
    assert normalized.temperature == 0.9
    assert normalized.max_tokens == 128
    assert "max_tokens" in normalized.generation_config_overrides


# ── apply_overrides：thinking ──


def test_apply_overrides_sets_thinking_wire_from_client_level():
    req, normalized = _prepare(
        "gemini-3.8-flash", thinkingConfig={"thinkingLevel": "high"}
    )
    result = apply_overrides(normalized, request=req, settings=_settings())
    assert normalized.generation_config_overrides["thinking_config"] == [
        1,
        None,
        None,
        3,
    ]
    assert result.thinking is not None
    assert result.include_thoughts is True


def test_apply_overrides_off_mode_overrides_client_thinking():
    req, normalized = _prepare(
        "gemini-3-pro-preview", thinkingConfig={"thinkingLevel": "high"}
    )
    result = apply_overrides(
        normalized, request=req, settings=_settings(thinkingMode="off")
    )
    assert normalized.generation_config_overrides["thinking_config"] == [
        1,
        None,
        None,
        1,
    ]
    assert result.include_thoughts is False


def test_apply_overrides_keeps_model_default_thinking_for_image_model():
    req, normalized = _prepare("gemini-3.1-flash-image-preview")
    # conftest 的 image 模型默认档位是 MINIMAL
    assert normalized.generation_config_overrides["thinking_config"] == [
        1,
        None,
        None,
        4,
    ]
    apply_overrides(normalized, request=req, settings=_settings())
    assert normalized.generation_config_overrides["thinking_config"] == [
        1,
        None,
        None,
        4,
    ]


def test_apply_overrides_preserves_wire_mode_from_template():
    req, normalized = _prepare(
        "gemini-3.8-flash", thinkingConfig=[9, None, None, 3]
    )
    apply_overrides(normalized, request=req, settings=_settings())
    assert normalized.generation_config_overrides["thinking_config"] == [
        9,
        None,
        None,
        3,
    ]


def test_apply_overrides_accepts_runtime_settings_object():
    """settings 缺省时读 runtime_settings；这里用一个带 values() 的假对象验证接口。"""

    class FakeSettings:
        def values(self):
            return _settings(thinkingMode="off")

    req, normalized = _prepare("gemini-3-pro-preview")
    result = apply_overrides(normalized, request=req, settings=FakeSettings())
    assert result.include_thoughts is False
    assert normalized.generation_config_overrides["thinking_config"] == [
        1,
        None,
        None,
        1,
    ]


@pytest.mark.parametrize(
    ("model", "level", "expected"),
    [
        ("gemini-3-pro-preview", "medium", "low"),
        ("gemini-3-pro-preview", "low", "low"),
        ("gemini-3-pro-preview", "high", "high"),
        ("gemini-3.8-flash", "minimal", "low"),
        ("gemini-3.8-flash", "medium", "medium"),
        ("gemini-4-argon", "high", "high"),
    ],
)
def test_clamp_level_matrix(model, level, expected):
    assert clamp_level(get_model_capabilities(model), level) == expected
