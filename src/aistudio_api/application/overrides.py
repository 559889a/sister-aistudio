"""参数覆写层：把运行期设置施加到「已归一化」的 Gemini 请求上。

对齐主项目（C:\\build2api）的语义，见 `src/overrides.js` 与 `src/model-caps.js`：

1. 采样参数覆写（temperature / topP / topK，留空 = 用客户端值）；
2. 按模型能力裁剪（采样参数已废弃的模型剔除、topK 收敛到 63）；
3. maxOutputTokens 覆写 / dropMaxTokens（丢弃客户端的输出长度上限）；
4. thinkingConfig 裁决（request 跟随客户端 / off 强制最低 / console 用固定档位），
   并**按模型实际支持的档位收敛**（Gemini 3 Pro 只有 low / high，medium 落到最近一档）；
5. includeThoughts 裁决（响应侧据此丢弃 thought parts）。

两条端口（Gemini `/v1beta/...` 与 OpenAI `/v1/chat/completions`）都调用
:func:`apply_overrides`，保证「同一个上游请求」在两边被改写成一模一样。

**本文件的自我约束**：不改 `infrastructure/gateway/**`（另一位队友正在改）。
AI Studio 的 jspb thinking_config 只有 `[mode, null, null, <等级>]` 这一种形状，
没有 include_thoughts 字段（见 docs/WIRE_SPECIFICATION.md §4 与主项目逆向文档），
所以 `includeThoughts=false` 的「请求侧」只能尽量少要思考（降到该模型最低档），
「响应侧」由调用方按 :class:`OverrideResult.include_thoughts` 丢弃 thought parts。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aistudio_api.application.runtime_settings import runtime_settings
from aistudio_api.infrastructure.gateway.model_defaults import resolve_model_defaults
from aistudio_api.infrastructure.gateway.wire_types import ThinkingLevel

# thinking 档位的规范顺序（由浅到深）。clamp 的「最近一档」按这个序列量距离。
LEVEL_ORDER: tuple[str, ...] = ("minimal", "low", "medium", "high")

# AI Studio wire 的等级枚举：LOW=1 / MEDIUM=2 / HIGH=3 / MINIMAL=4。
THINKING_LEVEL_CODES: dict[str, int] = {
    "low": int(ThinkingLevel.LOW),
    "medium": int(ThinkingLevel.MEDIUM),
    "high": int(ThinkingLevel.HIGH),
    "minimal": int(ThinkingLevel.MINIMAL),
}
CODE_TO_LEVEL: dict[int, str] = {code: name for name, code in THINKING_LEVEL_CODES.items()}

THINKING_NONE = "none"
THINKING_LEVEL = "level"

TOP_K_MAX = 63

# 内置 thinking_config 数组里 index 0 是 mode（真机恒 1）——覆写时沿用上游模板里的值。
THINKING_MODE_INDEX = 0
THINKING_LEVEL_INDEX = 3

_NON_TEXT_PATTERN = re.compile(
    r"(embedding|embed|aqa|imagen|veo|image-generation|tts|-live-|audio-native)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ModelCapabilities:
    """模型能力档案（按模型名家族推导，未知模型保守地按最新 Gemini 3 处理）。"""

    family: str = "unknown"
    thinking_kind: str = THINKING_LEVEL
    levels: tuple[str, ...] = LEVEL_ORDER
    sampling_deprecated: bool = False
    supports_system_instruction: bool = True
    supports_tools: bool = True
    is_text_model: bool = True


_FAMILIES: tuple[tuple[str, re.Pattern[str], ModelCapabilities], ...] = (
    (
        "gemini-legacy",
        re.compile(r"^gemini-[12]\.", re.IGNORECASE),
        ModelCapabilities(
            family="gemini-legacy",
            thinking_kind=THINKING_NONE,
            levels=(),
        ),
    ),
    (
        "gemini-3-pro",
        re.compile(r"^gemini-3(?:\.\d+)?-pro", re.IGNORECASE),
        ModelCapabilities(
            family="gemini-3-pro",
            thinking_kind=THINKING_LEVEL,
            levels=("low", "high"),
        ),
    ),
    (
        "gemini-3-flash",
        re.compile(r"^gemini-3(?:\.\d+)?-flash", re.IGNORECASE),
        ModelCapabilities(
            family="gemini-3-flash",
            thinking_kind=THINKING_LEVEL,
            # 3.8-flash 实测不接受 MINIMAL（上游 400: Thinking level MINIMAL is not
            # supported for this model），从最低档起算就是 low。主项目 model-caps.js
            # 里写的是四档，那是更早的构建；这里以真机为准，minimal 会被收敛到 low。
            levels=("low", "medium", "high"),
            # Gemini 3 Flash 的采样参数已废弃，带上会报错（主项目 model-caps.js 同款）
            sampling_deprecated=True,
        ),
    ),
    (
        "gemini-3+",
        re.compile(r"^gemini-(?:[3-9]|\d{2,})", re.IGNORECASE),
        ModelCapabilities(
            family="gemini-3+",
            thinking_kind=THINKING_LEVEL,
            levels=LEVEL_ORDER,
        ),
    ),
    (
        "gemma",
        re.compile(r"^gemma", re.IGNORECASE),
        ModelCapabilities(
            family="gemma",
            thinking_kind=THINKING_NONE,
            levels=(),
            supports_system_instruction=False,
            supports_tools=False,
        ),
    ),
)

_FALLBACK_CAPABILITIES = ModelCapabilities(
    family="unknown",
    thinking_kind=THINKING_LEVEL,
    levels=LEVEL_ORDER,
)


def normalize_model_name(model: str | None) -> str:
    """去掉 `models/` 前缀与首尾空白（大小写保持原样）。"""
    return str(model or "").strip().removeprefix("models/").strip()


def get_model_capabilities(
    model: str | None,
    *,
    discovery: Mapping[str, object] | None = None,
) -> ModelCapabilities:
    """取模型能力档案。

    ``discovery`` 是 `/v1beta/models` 那条链路上游返回的模型元数据（可选）：
    有 ``thinkingLevels`` 时按上游实际上报的档位收敛，比按名字猜更准。
    不传时尝试读 model_discovery 的**同步缓存**（不触发网络请求）。
    """
    name = normalize_model_name(model)
    base = _FALLBACK_CAPABILITIES
    for _family, pattern, caps in _FAMILIES:
        if pattern.search(name):
            base = caps
            break
    base = ModelCapabilities(
        family=base.family,
        thinking_kind=base.thinking_kind,
        levels=base.levels,
        sampling_deprecated=base.sampling_deprecated,
        supports_system_instruction=base.supports_system_instruction,
        supports_tools=base.supports_tools,
        is_text_model=not bool(_NON_TEXT_PATTERN.search(name)),
    )
    if discovery is None:
        discovery = _cached_discovery_info(name)
    return _narrow_with_discovery(base, discovery)


def _cached_discovery_info(model: str) -> Mapping[str, object] | None:
    """同步读 model_discovery 的缓存（拿不到就返回 None，绝不发网络请求）。"""
    if not model:
        return None
    try:
        from aistudio_api.infrastructure.gateway.model_discovery import (
            model_discovery,
        )

        return model_discovery.get_cached_model_info(model)
    except Exception:  # 测试环境/异常状态都不该让参数覆写失败
        return None


def _narrow_with_discovery(
    caps: ModelCapabilities, discovery: Mapping[str, object] | None
) -> ModelCapabilities:
    """用上游模型元数据收窄档位；认不出来就原样返回。"""
    if not discovery:
        return caps
    updates: dict[str, Any] = {}
    raw_levels = discovery.get("thinkingLevels")
    if isinstance(raw_levels, (list, tuple)) and raw_levels:
        supported: list[str] = []
        for raw in raw_levels:
            if isinstance(raw, bool) or not isinstance(raw, int):
                continue
            level = CODE_TO_LEVEL.get(raw)
            if level and level not in supported:
                supported.append(level)
        if supported:
            ordered = tuple(level for level in LEVEL_ORDER if level in supported)
            updates["levels"] = ordered
            updates["thinking_kind"] = THINKING_LEVEL
    if discovery.get("is_image_model") is True:
        # 生图模型在 AI Studio 这条 wire 上照常接受采样参数（真机 fixture 里就有）
        updates["sampling_deprecated"] = False
        updates["is_text_model"] = False
    if not updates:
        return caps
    return ModelCapabilities(
        family=caps.family,
        thinking_kind=updates.get("thinking_kind", caps.thinking_kind),
        levels=updates.get("levels", caps.levels),
        sampling_deprecated=updates.get("sampling_deprecated", caps.sampling_deprecated),
        supports_system_instruction=caps.supports_system_instruction,
        supports_tools=caps.supports_tools,
        is_text_model=updates.get("is_text_model", caps.is_text_model),
    )


def clamp_level(caps: ModelCapabilities, level: str | None) -> str | None:
    """把任意档位名收敛到该模型支持的档位；不支持等级制则返回 None。"""
    if caps.thinking_kind != THINKING_LEVEL or not caps.levels:
        return None
    wanted = str(level or "").strip().lower()
    if not wanted:
        return None
    if wanted in caps.levels:
        return wanted
    wanted_index = LEVEL_ORDER.index(wanted) if wanted in LEVEL_ORDER else -1
    if wanted_index < 0:
        # 认不出的名字按「最深」处理（与主项目 clampLevel 的兜底一致）
        return caps.levels[-1]
    best: str | None = None
    best_distance = len(LEVEL_ORDER)
    for candidate in caps.levels:
        distance = abs(LEVEL_ORDER.index(candidate) - wanted_index)
        if distance < best_distance:
            best = candidate
            best_distance = distance
    return best


def effort_to_level(effort: str | None) -> str | None:
    """OpenAI 的 reasoning_effort 映射到档位名。"""
    mapping = {
        "none": "minimal",
        "minimal": "minimal",
        "low": "low",
        "medium": "medium",
        "high": "high",
    }
    return mapping.get(str(effort or "").strip().lower())


def build_thinking_wire(
    level: str | None, *, mode: int = 1
) -> list[object] | None:
    """把档位名编成 AI Studio 的 thinking_config 数组；等级制不适用时返回 None。"""
    if not level:
        return None
    code = THINKING_LEVEL_CODES.get(level)
    if code is None:
        return None
    return [int(mode), None, None, code]


def wire_thinking_level(wire: object) -> str | None:
    """从 thinking_config 数组里取档位名（认不出来返回 None）。"""
    if not isinstance(wire, (list, tuple)):
        return None
    if len(wire) <= THINKING_LEVEL_INDEX:
        return None
    code = wire[THINKING_LEVEL_INDEX]
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return CODE_TO_LEVEL.get(code)


def wire_thinking_mode(wire: object, *, default: int = 1) -> int:
    """从 thinking_config 数组里取 mode（index 0）；拿不到用上游模板的默认值。"""
    if not isinstance(wire, (list, tuple)) or not wire:
        return default
    mode = wire[THINKING_MODE_INDEX]
    if isinstance(mode, bool) or not isinstance(mode, int):
        return default
    return mode


@dataclass(frozen=True)
class ClientThinking:
    """客户端（或 OpenAI 桥）表达的思考意图。"""

    level: str | None = None
    effort: str | None = None
    include_thoughts: bool | None = None


@dataclass(frozen=True)
class ThoughtResolution:
    """thinkingConfig 的裁决结果。"""

    thinking_config: list[object] | None
    include_thoughts: bool
    level: str | None = None
    mode: str = "request"
    applied: tuple[str, ...] = ()


def resolve_thinking(
    caps: ModelCapabilities,
    client: ClientThinking | None,
    settings: Mapping[str, Any],
    *,
    mode_code: int = 1,
) -> ThoughtResolution:
    """裁决 thinkingConfig（主项目 overrides.resolveThinking 的 Python 版）。"""
    mode = str(settings.get("thinkingMode") or "request")
    global_include = _setting_bool(settings, "includeThoughts", True)
    client_wants = client.include_thoughts if client is not None else None
    # request 模式下客户端显式写的 includeThoughts 优先——全局设置只是默认值
    include = (
        client_wants if (mode == "request" and client_wants is not None) else global_include
    )

    if mode == "off":
        include = False

    if caps.thinking_kind == THINKING_NONE:
        # 2.x / Gemma 没有等级制：这条 wire 传不出 includeThoughts，也不该塞档位。
        return ThoughtResolution(
            thinking_config=None,
            include_thoughts=include,
            mode=mode,
        )

    def lowest_level() -> str | None:
        return caps.levels[0] if caps.levels else None

    if mode == "off":
        # 关不掉的模型（如 3 Pro 只有 low/high）就压到最低可用档
        level = lowest_level()
        return ThoughtResolution(
            thinking_config=build_thinking_wire(level, mode=mode_code),
            include_thoughts=False,
            level=level,
            mode=mode,
            applied=(f"thinking={level or 'default'}", "includeThoughts=false"),
        )

    if mode == "console":
        wanted = str(settings.get("thinkingLevel") or "").strip().lower()
        level = clamp_level(caps, wanted) if wanted else None
        applied = (f"thinking={level or 'default'}",)
        return ThoughtResolution(
            thinking_config=build_thinking_wire(level, mode=mode_code),
            include_thoughts=include,
            level=level,
            mode=mode,
            applied=applied,
        )

    # mode == "request"：尊重客户端
    if client is None:
        if not include and caps.levels:
            # 这条 wire 没有 include_thoughts 字段：要「不返回思维链」就只能少要思考，
            # 客户端又没指定档位，降到底档是唯一能表达的请求侧意图。
            level = lowest_level()
            return ThoughtResolution(
                thinking_config=build_thinking_wire(level, mode=mode_code),
                include_thoughts=False,
                level=level,
                mode=mode,
                applied=(f"thinking={level}", "includeThoughts=false"),
            )
        return ThoughtResolution(
            thinking_config=None,
            include_thoughts=include,
            mode=mode,
        )

    wanted = client.level
    if not wanted and client.effort:
        wanted = effort_to_level(client.effort)
    level = clamp_level(caps, wanted) if wanted else None
    if level is None and not include and caps.levels:
        # 同上：客户端要「不回传思维链」但没给档位时，请求侧降到底档
        level = lowest_level()
    applied: tuple[str, ...] = ()
    if level:
        applied = (f"thinking={level}",)
    return ThoughtResolution(
        thinking_config=build_thinking_wire(level, mode=mode_code),
        include_thoughts=include,
        level=level,
        mode=mode,
        applied=applied,
    )


@dataclass
class OverrideResult:
    """一次覆写的结果（调用方用 include_thoughts 决定响应侧是否丢弃 thought parts）。"""

    model: str
    include_thoughts: bool = True
    thinking: ThoughtResolution | None = None
    applied: list[str] = field(default_factory=list)


def apply_overrides(
    normalized: Any,
    *,
    request: Any | None = None,
    settings: Mapping[str, Any] | None = None,
    model: str | None = None,
    discovery: Mapping[str, object] | None = None,
) -> OverrideResult:
    """就地改写 :class:`NormalizedGeminiRequest`，返回本次生效的覆写项。

    ``request`` 是原始的 :class:`GeminiGenerateContentRequest`（Gemini 端口）或
    OpenAI 桥构造的同形请求，用来读客户端的 thinkingConfig 意图；不传时退化为
    读已归一化的 ``generation_config_overrides``。
    """
    values = _settings_values(settings)
    model_name = model or str(getattr(normalized, "model", "") or "")
    caps = get_model_capabilities(model_name, discovery=discovery)
    is_image_model = _is_image_model(model_name, discovery)
    overrides = getattr(normalized, "generation_config_overrides", None)
    if not isinstance(overrides, dict):
        overrides = {}
        normalized.generation_config_overrides = overrides
    applied: list[str] = []

    _apply_sampling(
        normalized,
        overrides,
        values,
        caps,
        is_image_model=is_image_model,
        applied=applied,
    )
    _apply_max_tokens(normalized, overrides, values, applied)
    thinking = _apply_thinking(request, overrides, values, caps, applied)

    return OverrideResult(
        model=model_name,
        include_thoughts=thinking.include_thoughts,
        thinking=thinking,
        applied=applied,
    )


def _apply_sampling(
    normalized: Any,
    overrides: dict[str, object],
    values: Mapping[str, Any],
    caps: ModelCapabilities,
    *,
    is_image_model: bool,
    applied: list[str],
) -> None:
    if caps.sampling_deprecated and not is_image_model:
        stripped = False
        for attr, key in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("top_k", "top_k"),
        ):
            if getattr(normalized, attr, None) is not None:
                stripped = True
            setattr(normalized, attr, None)
            if key in overrides:
                overrides.pop(key, None)
                stripped = True
        if stripped:
            applied.append("sampling-stripped(model-unsupported)")
        return

    temperature = _setting_float(values, "temperature")
    if temperature is not None:
        normalized.temperature = temperature
        overrides["temperature"] = temperature
        applied.append(f"temperature={temperature}")

    top_p = _setting_float(values, "topP")
    if top_p is not None:
        normalized.top_p = top_p
        overrides["top_p"] = top_p
        applied.append(f"topP={top_p}")

    top_k_override = _setting_int(values, "topK")
    if top_k_override is not None:
        value = min(top_k_override, TOP_K_MAX)
        normalized.top_k = value
        overrides["top_k"] = value
        applied.append(f"topK={value}")
    elif normalized.top_k is not None and normalized.top_k > TOP_K_MAX:
        normalized.top_k = TOP_K_MAX
        overrides["top_k"] = TOP_K_MAX
        applied.append(f"topK->{TOP_K_MAX}")


def _apply_max_tokens(
    normalized: Any,
    overrides: dict[str, object],
    values: Mapping[str, Any],
    applied: list[str],
) -> None:
    client_max = normalized.max_tokens
    if _setting_bool(values, "dropMaxTokens", False):
        if client_max is not None or "max_tokens" in overrides:
            applied.append("dropMaxTokens")
        normalized.max_tokens = None
        overrides.pop("max_tokens", None)
        return
    override = _setting_int(values, "maxOutputTokens")
    if override is not None:
        normalized.max_tokens = override
        overrides["max_tokens"] = override
        applied.append(f"maxOutputTokens={override}")


def _apply_thinking(
    request: Any | None,
    overrides: dict[str, object],
    values: Mapping[str, Any],
    caps: ModelCapabilities,
    applied: list[str],
) -> ThoughtResolution:
    existing_wire = overrides.get("thinking_config")
    client = _client_thinking(request, existing_wire)
    resolution = resolve_thinking(
        caps,
        client,
        values,
        mode_code=wire_thinking_mode(existing_wire),
    )
    if resolution.thinking_config is not None:
        overrides["thinking_config"] = resolution.thinking_config
    applied.extend(resolution.applied)
    return resolution


def _client_thinking(request: Any | None, existing_wire: object) -> ClientThinking | None:
    """从原始请求的 generationConfig.thinkingConfig 里读客户端意图。"""
    if request is None:
        level = wire_thinking_level(existing_wire)
        if level is None:
            return None
        return ClientThinking(level=level)
    generation_config = getattr(request, "generationConfig", None)
    raw = getattr(generation_config, "thinkingConfig", None)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        level = raw.get("thinkingLevel") or raw.get("level")
        effort = raw.get("_effort") or raw.get("effort")
        include = raw.get("includeThoughts")
        return ClientThinking(
            level=str(level).strip().lower() if level else None,
            effort=str(effort).strip().lower() if effort else None,
            include_thoughts=include if isinstance(include, bool) else None,
        )
    level = wire_thinking_level(raw)
    if level is None:
        return None
    return ClientThinking(level=level)


def _is_image_model(
    model: str | None, discovery: Mapping[str, object] | None
) -> bool:
    if discovery and discovery.get("is_image_model") is True:
        return True
    name = normalize_model_name(model)
    try:
        if resolve_model_defaults(name).is_image_model:
            return True
    except Exception:
        return "image" in name.lower()
    return "image" in name.lower()


def _settings_values(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    if settings is None:
        return runtime_settings.values()
    if isinstance(settings, Mapping):
        return dict(settings)
    values_method = getattr(settings, "values", None)
    if callable(values_method):
        raw = values_method()
        return dict(raw) if isinstance(raw, Mapping) else {}
    return {}


def _setting_bool(settings: Mapping[str, Any], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    return bool(default if value is None else value)


def _setting_float(settings: Mapping[str, Any], key: str) -> float | None:
    value = settings.get(key)
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _setting_int(settings: Mapping[str, Any], key: str) -> int | None:
    value = settings.get(key)
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CODE_TO_LEVEL",
    "LEVEL_ORDER",
    "THINKING_LEVEL_CODES",
    "TOP_K_MAX",
    "ClientThinking",
    "ModelCapabilities",
    "OverrideResult",
    "ThoughtResolution",
    "apply_overrides",
    "build_thinking_wire",
    "clamp_level",
    "effort_to_level",
    "get_model_capabilities",
    "normalize_model_name",
    "resolve_thinking",
    "wire_thinking_level",
    "wire_thinking_mode",
]
