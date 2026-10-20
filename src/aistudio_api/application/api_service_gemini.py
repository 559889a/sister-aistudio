"""Gemini-compatible application service handlers."""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import NoReturn

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse

from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.chat_service import (
    NormalizedGeminiRequest,
    normalize_gemini_request,
)
from aistudio_api.application.overrides import OverrideResult, apply_overrides
from aistudio_api.application.stream_heartbeat import (
    HEARTBEAT_EVENT,
    stream_with_heartbeat,
)
from aistudio_api.domain.errors import (
    AistudioError,
    AuthError,
    BrowserTransportError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
    is_browser_session_hang_error,
    parse_upstream_grpc_error,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.browser.scripts import PROMPT_STATE_JS
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.incidents import record_incident
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_context import get_request_id

logger = get_logger("api.gemini")

# 用户拍板（2026-10-20）：网关层**不做任何自动重试**——不切备用 worker、不清模板
# 原地重打、不切账号重试。本次请求按真实错误返回，由客户端自己决定要不要重发。
# 唯一的自愈动作：特定故障（403/401、5xx、页面传输失败、会话挂死、登录页重定向）
# 时后台重启出问题的那台无头浏览器，让**后续**请求拿到干净页面。别把任何重试加回来。

# 周期性「上游 Ns 无任何数据」WARN 已按用户要求移除（2026-10-20）：bypass 请求
# 动辄几百秒整段返回，这条 WARN 只会刷屏。静默期只有 1s 心跳；完成后日志里的
# 「首包 X s / 总 Y s」才是判据，失败时另有 CDP 网络层证据。


def _fmt_elapsed(attempt_elapsed_s: float | None) -> str:
    return f", {attempt_elapsed_s:.1f}s" if attempt_elapsed_s is not None else ""


def _rid() -> str:
    rid = get_request_id()
    return f"({rid}) " if rid else ""


# finishReason 枚举对齐当前页面构建的一手证据（2026-10-20 bundle 复核，
# 见 逆向相关内容/independent-re-2026-10-20.md：mcf 错误文案 Map + FO:434 / B2:1862）。
# 注意 5=OTHER、6=LANGUAGE（曾长期互换）；15=Image error / 16=No image / 17=Image recitation。
WIRE_TO_GEMINI_FINISH_REASON: dict[int, str] = {
    0: "FINISH_REASON_UNSPECIFIED",
    1: "STOP",
    2: "MAX_TOKENS",
    3: "SAFETY",
    4: "RECITATION",
    5: "OTHER",
    6: "LANGUAGE",
    7: "BLOCKLIST",
    8: "PROHIBITED_CONTENT",
    9: "SPII",
    10: "MALFORMED_FUNCTION_CALL",
    11: "IMAGE_SAFETY",
    12: "UNEXPECTED_TOOL_CALL",
    13: "TOO_MANY_TOOL_CALLS",
    14: "IMAGE_PROHIBITED_CONTENT",
    15: "IMAGE_OTHER",
    16: "NO_IMAGE",
    17: "IMAGE_RECITATION",
    18: "MISSING_THOUGHT_SIGNATURE",
}


def to_gemini_finish_reason(wire_code: int | None) -> str:
    if wire_code is None:
        return "STOP"
    return WIRE_TO_GEMINI_FINISH_REASON.get(wire_code, "FINISH_REASON_UNSPECIFIED")


# HTTP 状态码 → Gemini 错误 status 字符串（HTTPException / RequestError 两处共用）
_HTTP_STATUS_TO_GEMINI: dict[int, str] = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    502: "UNAVAILABLE",
    503: "UNAVAILABLE",
    504: "DEADLINE_EXCEEDED",
}


def clean_upstream_error_message(raw_msg: str) -> str:
    """Extract a clean, readable error message from Google/JSPB upstream error strings."""
    if not raw_msg:
        return ""
    text = raw_msg.strip()
    if "[,[" not in text and "[null,[" not in text and not text.startswith("[,"):
        return text
    inner = text
    while True:
        m = re.match(r"^HTTP\s+\d+:\s*(.*)$", inner)
        if m:
            inner = m.group(1).strip()
        else:
            break
    _, msg = parse_upstream_grpc_error(inner)
    return msg or inner

def classify_gemini_error_payload(exc: Exception) -> tuple[int, str, str]:
    """Extract standard Gemini HTTP status code, message, and status string from an exception."""
    if isinstance(exc, SessionExpiredError):
        return (
            401,
            "All accounts have expired sessions. Please import fresh cookies.",
            "UNAUTHENTICATED",
        )
    if isinstance(exc, AuthError):
        return 403, str(exc), "PERMISSION_DENIED"
    if isinstance(exc, UsageLimitExceeded):
        return 429, str(exc), "RESOURCE_EXHAUSTED"
    if isinstance(exc, (ValueError, RequestValidationError)):
        return 400, str(exc), "INVALID_ARGUMENT"
    if isinstance(exc, HTTPException):
        status = exc.status_code
        detail = exc.detail
        raw_msg = detail.get("message") if isinstance(detail, dict) else detail
        msg = str(raw_msg) if raw_msg is not None else ""
        clean_msg = clean_upstream_error_message(msg)
        return (
            status,
            clean_msg,
            _HTTP_STATUS_TO_GEMINI.get(
                status, "INTERNAL" if status >= 500 else "UNKNOWN"
            ),
        )
    if isinstance(exc, RequestError):
        status = exc.status if exc.status > 0 else 500
        clean_msg = clean_upstream_error_message(str(exc))
        return (
            status,
            clean_msg,
            _HTTP_STATUS_TO_GEMINI.get(
                status, "INTERNAL" if status >= 500 else "UNKNOWN"
            ),
        )
    if isinstance(exc, AistudioError):
        return 500, str(exc), "INTERNAL"

    return 500, str(exc), "INTERNAL"


async def _capture_failure_evidence(
    exc: Exception,
    *,
    client: AIStudioClient,
    http_status: int,
    error_type: str,
    model: str,
    attempt_elapsed_s: float | None,
) -> None:
    """偶发故障现场落盘（500/403/502 攻坚，2026-10-20）：必须在重启浏览器**之前**调。

    探针拿失败瞬间的页面状态（hook/WAA/输入框），连同上游原始错误体、
    CDP 网络层证据一起写进 data/incidents.jsonl。全部 best-effort：
    探针失败记下失败原因，落盘失败只 WARN，绝不影响既有错误路径。
    """
    page_state: dict[str, object] | str = {}
    session = getattr(client, "_session", None)
    page = getattr(session, "page", None)
    if page is None or (callable(getattr(page, "is_closed", None)) and page.is_closed()):
        page_state = {"page": "closed-or-missing"}
    else:
        try:
            state = await page.evaluate(PROMPT_STATE_JS, timeout_s=2.0)
            page_state = state if isinstance(state, dict) else {"probe": str(state)}
        except Exception as probe_exc:
            page_state = {"probe_failed": str(probe_exc)}
    net = getattr(exc, "net_evidence", None)
    if not net and page is not None:
        raw_failures = getattr(page, "rpc_net_failures", None)
        if isinstance(raw_failures, list):
            net = raw_failures[-8:]
    record_incident(
        "request_failure",
        {
            "rid": get_request_id() or "",
            "http_status": http_status,
            "error_type": error_type,
            "error_class": type(exc).__name__,
            "message": str(exc)[:1000],
            "model": model,
            "elapsed_s": (
                round(attempt_elapsed_s, 1) if attempt_elapsed_s is not None else None
            ),
            "page_state": page_state,
            "net_evidence": net,
            "raw_upstream_body": getattr(exc, "raw_upstream_body", "") or "",
            "traceback": traceback.format_exc()[:4000],
        },
    )


async def handle_attempt_exception(
    exc: Exception,
    *,
    model_path: str,
    normalized_model: str | None,
    client: AIStudioClient,
    attempt_elapsed_s: float | None = None,
) -> NoReturn:
    """错误归类 + 唯一自愈动作（重启浏览器）+ 抛出真实错误（绝不重试）。

    用户拍板（2026-10-20）：单浏览器 + 账号池；网关层不做任何自动重试——失败按
    真实错误返回，由客户端自己决定要不要重发。**只有 429 驱动换号**，且防误杀：
    第 5 次 429 才把账号/模型打入冷宫（前 4 次只计数，见 account_rotator）。

    - 403/401、页面传输失败、会话挂死、登录页重定向 → 杀掉浏览器进程（下次请求
      ``ensure_ready`` 重建）救**后续**请求；403 不加账号冷却、不换号。
    - 429 只记账（429 计数 +1，第 5 次冷却到美西午夜），不重启浏览器。
    - 登录态失效（SessionExpiredError）凭据已死，按账号隔离 24h 并重启浏览器。
    """
    target_model = normalized_model or model_path

    async def _restart_browser() -> None:
        """杀掉浏览器进程，下次请求 ensure_ready 重建。"""
        client.clear_templates()
        session = client._session
        if session is not None:
            try:
                await session._close_internal()
            except Exception as close_exc:  # pragma: no cover
                logger.warning("%s重启浏览器进程失败: %s", _rid(), close_exc)

    if isinstance(exc, ValueError):
        raise HTTPException(
            400, detail={"message": str(exc), "type": "bad_request"}
        ) from exc

    if isinstance(exc, SessionExpiredError):
        logger.warning("%s账号会话失效，已被重定向到登录页: %s", _rid(), exc)
        record_rotator_event("session_expired", model=target_model)
        await _capture_failure_evidence(
            exc, client=client, http_status=401, error_type="auth_error",
            model=target_model, attempt_elapsed_s=attempt_elapsed_s,
        )
        await _restart_browser()
        raise HTTPException(
            401,
            detail={
                "message": "All accounts encountered login redirect or expired sessions. Please import fresh cookies.",
                "type": "auth_error",
            },
        ) from exc

    if isinstance(exc, AuthError):
        logger.warning(
            "%s账号 403 权限拒绝%s: %s", _rid(), _fmt_elapsed(attempt_elapsed_s), exc
        )
        # 403/401 不是配额问题（2026-10-19 拍板）：不换号、不加账号冷却，按浏览器故障重启
        record_rotator_event("error", model=target_model)
        await _capture_failure_evidence(
            exc, client=client, http_status=403, error_type="auth_error",
            model=target_model, attempt_elapsed_s=attempt_elapsed_s,
        )
        await _restart_browser()
        raise HTTPException(
            403, detail={"message": str(exc), "type": "auth_error"}
        ) from exc

    if isinstance(exc, UsageLimitExceeded):
        runtime_state.record(target_model, "rate_limited")
        # 429 是唯一的换号驱动（2026-10-20 拍板）：第 5 次才冷却（见 account_rotator）
        record_rotator_event("rate_limited", model=target_model)
        logger.warning("%s429 触发限额: model=%s", _rid(), target_model)
        raise HTTPException(
            429,
            detail={
                "message": f"All accounts reached daily quota for model '{target_model}'. Quota resets at 00:00 PST.",
                "type": "rate_limit_exceeded",
            },
        ) from exc

    if isinstance(exc, BrowserTransportError):
        # 页面里的 fetch/XHR 网络失败（Failed to fetch 这类）：这台浏览器坏了。
        # 重启它救**后续**请求；本次请求 502，客户端要重试就重发一条新请求。
        record_rotator_event("error", model=target_model)
        logger.warning(
            "%s浏览器页面请求失败%s，后台重启浏览器，本次请求 502: %s",
            _rid(),
            _fmt_elapsed(attempt_elapsed_s),
            clean_upstream_error_message(str(exc)),
        )
        await _capture_failure_evidence(
            exc, client=client, http_status=502, error_type="upstream_error",
            model=target_model, attempt_elapsed_s=attempt_elapsed_s,
        )
        await _restart_browser()
        raise HTTPException(
            502,
            detail={
                "message": clean_upstream_error_message(str(exc)),
                "type": "upstream_error",
            },
        ) from exc

    if is_browser_session_hang_error(exc):
        # 模板/BotGuard 捕获挂死：这台浏览器坏了，重启它
        logger.warning(
            "%s模板或 BotGuard 捕获超时%s，后台重启浏览器: %s",
            _rid(),
            _fmt_elapsed(attempt_elapsed_s),
            exc,
        )
        client.clear_templates()
        record_rotator_event("error", model=target_model)
        await _capture_failure_evidence(
            exc, client=client, http_status=502, error_type="upstream_error",
            model=target_model, attempt_elapsed_s=attempt_elapsed_s,
        )
        await _restart_browser()
        raise HTTPException(
            502, detail={"message": str(exc), "type": "upstream_error"}
        ) from exc

    if isinstance(exc, RequestError):
        clean_msg = clean_upstream_error_message(str(exc))
        status_code = exc.status if exc.status > 0 else 500
        logger.warning(
            "%sGemini 上游请求错误 (%d)%s: %s",
            _rid(),
            status_code,
            _fmt_elapsed(attempt_elapsed_s),
            clean_msg,
        )
        error_type = (
            "bad_request"
            if status_code == 400
            else "not_found"
            if status_code == 404
            else "upstream_error"
        )
        await _capture_failure_evidence(
            exc, client=client, http_status=status_code, error_type=error_type,
            model=target_model, attempt_elapsed_s=attempt_elapsed_s,
        )
        raise HTTPException(
            status_code, detail={"message": clean_msg, "type": error_type}
        ) from exc

    if isinstance(exc, AistudioError):
        runtime_state.record(target_model, "errors")
        record_rotator_event("error", model=target_model)
        logger.warning(
            "%sGemini 服务端异常%s: %s", _rid(), _fmt_elapsed(attempt_elapsed_s), exc
        )
        await _capture_failure_evidence(
            exc, client=client, http_status=500, error_type="server_error",
            model=target_model, attempt_elapsed_s=attempt_elapsed_s,
        )
        raise HTTPException(
            500, detail={"message": str(exc), "type": "server_error"}
        ) from exc

    runtime_state.record(target_model, "errors")
    record_rotator_event("error", model=target_model)
    logger.error(
        "%sGemini 未知严重异常%s: %s", _rid(), _fmt_elapsed(attempt_elapsed_s), exc
    )
    logger.debug("Gemini 异常堆栈详情:", exc_info=True)
    await _capture_failure_evidence(
        exc, client=client, http_status=500, error_type="server_error",
        model=target_model, attempt_elapsed_s=attempt_elapsed_s,
    )
    raise HTTPException(
        500, detail={"message": str(exc), "type": "server_error"}
    ) from exc


@dataclass
class PreparedRequest:
    """一次请求的归一化结果 + 生效的参数覆写（两条端口共用）。"""

    normalized: NormalizedGeminiRequest
    overrides: OverrideResult

    @property
    def include_thoughts(self) -> bool:
        return self.overrides.include_thoughts


def prepare_gemini_request(
    req: GeminiGenerateContentRequest, model_path: str
) -> PreparedRequest:
    """归一化 + 参数覆写。Gemini 原生端口与 OpenAI 桥都走这里，保证改写一致。"""
    normalized = normalize_gemini_request(req, model_path)
    overrides = apply_overrides(normalized, request=req)
    if overrides.applied:
        logger.debug(
            "%s转换细节: 覆写: %s",
            _rid(),
            ", ".join(overrides.applied),
        )
    return PreparedRequest(normalized=normalized, overrides=overrides)


async def generate_content_output(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
) -> tuple[PreparedRequest, ModelOutput]:
    """非流式上游调用（单次，绝不重试），返回归一化请求与上游输出。

    失败时按既有规则抛 HTTPException——OpenAI 桥会把它翻成 OpenAI 错误体。
    """
    await ensure_active_account(model=model_path)
    prepared: PreparedRequest | None = None
    attempt_started = time.perf_counter()
    try:
        prepared = prepare_gemini_request(req, model_path)
        normalized = prepared.normalized
        logger.debug(
            "%s上游调用 (非流式): model=%s, 对话轮数=%d",
            _rid(),
            normalized.model,
            len(req.contents),
        )
        output = await client.generate_content(
            model=normalized.model,
            capture_prompt=normalized.capture_prompt,
            capture_images=normalized.capture_images,
            contents=normalized.contents,
            system_instruction_content=normalized.system_instruction,
            tools=normalized.tools,
            tool_config=normalized.tool_config,
            safety_settings=normalized.safety_settings,
            temperature=normalized.temperature,
            top_p=normalized.top_p,
            top_k=normalized.top_k,
            max_tokens=normalized.max_tokens,
            generation_config_overrides=normalized.generation_config_overrides,
            sanitize_plain_text=False,
        )

        record_rotator_event("success", model=normalized.model)
        runtime_state.record(normalized.model, "success", output.usage)
        logger.debug(
            "%s上游返回: 输入Tokens=%s, 输出Tokens=%s",
            _rid(),
            output.usage.get("prompt_tokens") if output.usage else 0,
            output.usage.get("completion_tokens") if output.usage else 0,
        )
        return prepared, output
    except Exception as exc:
        await handle_attempt_exception(
            exc,
            model_path=model_path,
            normalized_model=prepared.normalized.model if prepared else None,
            client=client,
            attempt_elapsed_s=time.perf_counter() - attempt_started,
        )


def _to_sdk_call(
    model_path: str, req: GeminiGenerateContentRequest
) -> tuple[str, list[dict[str, object]], dict[str, object]]:
    """GeminiGenerateContentRequest → SDK generate_content 调用参数（官方 JSON 形状）。

    模型字段名本身就是 camelCase，model_dump 直出；generationConfig 在官方 API
    里平铺在 config 顶层，直接并入。SDK 会经 transport 往返一次（校验/默认值/
    官方字段全集），最终仍走 generate_content_output / stream_raw_events 同一条
    网关调用面——账号调度、覆写、故障现场记录全部按构造继承。
    """
    contents = [c.model_dump(exclude_none=True) for c in req.contents]
    config: dict[str, object] = {}
    if req.systemInstruction is not None:
        config["system_instruction"] = req.systemInstruction.model_dump(exclude_none=True)
    if req.tools is not None:
        config["tools"] = [t.model_dump(exclude_none=True) for t in req.tools]
    if req.safetySettings is not None:
        config["safety_settings"] = [s.model_dump(exclude_none=True) for s in req.safetySettings]
    if req.toolConfig is not None:
        config["tool_config"] = dict(req.toolConfig)
    if req.generationConfig is not None:
        config.update(req.generationConfig.model_dump(exclude_none=True))
    return model_path.removeprefix("models/"), contents, config


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
):
    """Gemini /v1beta 端口入口（2026-10-20 用户拍板走 google-genai SDK 路径；
    OpenAI 端口保持 wire 手搓翻译，不经 SDK）。"""
    # 懒加载：sdk_bridge 反向依赖本模块的 generate_content_output / stream_raw_events
    from aistudio_api.application.sdk_bridge import get_sdk_client

    sdk = get_sdk_client(client)
    model, contents, config = _to_sdk_call(model_path, req)
    if stream:
        return _build_gemini_streaming_response(
            sdk=sdk, model=model, contents=contents, config=config, model_path=model_path
        )

    from google.genai import errors as genai_errors

    try:
        from typing import cast

        from google.genai import types as genai_types

        resp = await sdk.aio.models.generate_content(
            model=model,
            contents=contents,
            config=cast("genai_types.GenerateContentConfigDict | None", config or None),
        )
    except genai_errors.APIError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        raise HTTPException(
            code if code > 0 else 500,
            detail={
                "message": str(getattr(exc, "message", "") or exc),
                "type": "bad_request" if code == 400 else "upstream_error",
            },
        ) from exc
    data = resp.model_dump(mode="json", by_alias=True, exclude_none=True)
    # 剥掉 SDK 客户端侧内部注记（非 API 字段）；富字段（grounding/logprobs 等）
    # 原样透传——路由已去掉 response_model 收窄
    for k in ("sdkHttpResponse", "automaticFunctionCallingHistory"):
        data.pop(k, None)
    return data


def format_sse_event(
    event_type: str,
    text: object,
    *,
    model_version: str | None = None,
    response_id: str | None = None,
    thought_signature: str | None = None,
) -> str | None:
    """Format a single Gemini streaming event into SSE chunk string."""
    if not text:
        return None

    parts: list[dict[str, object]] = []
    if event_type == "body":
        p: dict[str, object] = {"text": str(text)}
        if thought_signature:
            p["thoughtSignature"] = thought_signature
        parts.append(p)
    elif event_type == "thinking":
        p = {"text": str(text), "thought": True}
        if thought_signature:
            p["thoughtSignature"] = thought_signature
        parts.append(p)
    elif event_type == "tool_calls":
        fc_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", function_calls=fc_list)
        ]
        if thought_signature and parts:
            parts[0]["thoughtSignature"] = thought_signature
    elif event_type == "images":
        img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", images=img_list)
        ]
        if thought_signature and parts:
            parts[0]["thoughtSignature"] = thought_signature
    elif event_type == "reasoning_images":
        r_img_list = text if isinstance(text, list) else []
        parts = [
            part.model_dump(mode="json", exclude_none=True)
            for part in to_gemini_parts("", reasoning_images=r_img_list)
        ]
        if thought_signature and parts:
            parts[0]["thoughtSignature"] = thought_signature
    else:
        return None

    payload: dict[str, object] = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}]
    }
    if model_version:
        payload["modelVersion"] = model_version
    if response_id:
        payload["responseId"] = response_id
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def format_sse_usage(
    final_usage: dict[str, object] | None,
    *,
    finish_reason: str = "STOP",
    model_version: str | None = None,
    response_id: str | None = None,
) -> str:
    """Format final completion usage metadata into SSE chunk string."""
    effective_usage = final_usage or {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    usage_dict = to_gemini_usage_metadata(effective_usage).model_dump(
        mode="json", exclude_none=True
    )
    payload: dict[str, object] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": []},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": usage_dict,
    }
    if model_version:
        payload["modelVersion"] = model_version
    if response_id:
        payload["responseId"] = response_id
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def format_sse_error(exc: Exception) -> str:
    """Format exception into standard Gemini SSE error chunk."""
    code, msg, status_str = classify_gemini_error_payload(exc)
    return (
        "data: "
        + json.dumps(
            {
                "error": {
                    "code": code,
                    "message": msg,
                    "status": status_str,
                }
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


async def stream_raw_events(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
):
    """流式上游调用（单次，绝不重试）的公共入口。

    产出 ``("prepared", PreparedRequest)`` 一次，随后原样转发
    ``client.stream_generate_content`` 的事件；失败时 ``handle_attempt_exception``
    抛出最终错误（由 Gemini SSE / OpenAI SSE 各自渲染成错误帧）。
    """
    await ensure_active_account(model=model_path)
    prepared: PreparedRequest | None = None
    attempt_started = time.perf_counter()
    try:
        prepared = prepare_gemini_request(req, model_path)
        normalized = prepared.normalized
        yield ("attempt", 1)
        yield ("prepared", prepared)
        async for event_type, text in client.stream_generate_content(
            model=normalized.model,
            capture_prompt=normalized.capture_prompt,
            capture_images=normalized.capture_images,
            contents=normalized.contents,
            system_instruction_content=normalized.system_instruction,
            tools=normalized.tools,
            tool_config=normalized.tool_config,
            safety_settings=normalized.safety_settings,
            temperature=normalized.temperature,
            top_p=normalized.top_p,
            top_k=normalized.top_k,
            max_tokens=normalized.max_tokens,
            generation_config_overrides=normalized.generation_config_overrides,
            sanitize_plain_text=False,
        ):
            yield (event_type, text)
    except Exception as exc:
        await handle_attempt_exception(
            exc,
            model_path=model_path,
            normalized_model=prepared.normalized.model if prepared else None,
            client=client,
            attempt_elapsed_s=time.perf_counter() - attempt_started,
        )


def _build_gemini_streaming_response(
    *,
    sdk,
    model: str,
    contents: list[dict[str, object]],
    config: dict[str, object],
    model_path: str,
) -> StreamingResponse:
    async def _chunk_frames() -> AsyncIterator[tuple[str, str]]:
        """SDK 类型化 chunk → 官方 SSE 帧；顺带盯 finishReason 有没有到过。"""
        saw_finish = False
        from typing import cast

        from google.genai import types as genai_types

        stream = await sdk.aio.models.generate_content_stream(
            model=model,
            contents=contents,
            config=cast("genai_types.GenerateContentConfigDict | None", config or None),
        )
        async for chunk in stream:
            data = chunk.model_dump(mode="json", by_alias=True, exclude_none=True)
            for k in ("sdkHttpResponse", "automaticFunctionCallingHistory"):
                data.pop(k, None)
            for cand in data.get("candidates") or []:
                if isinstance(cand, dict) and cand.get("finishReason"):
                    saw_finish = True
            yield (
                "chunk",
                "data: " + json.dumps(data, ensure_ascii=False) + "\n\n",
            )
        if not saw_finish:
            # 官方流最后一块必带 finishReason；没有就是被截断（含 SDK 类型层
            # 吞掉流内 error 帧的情况）——补一帧错误，不许静默空流收场
            yield (
                "error_frame",
                format_sse_error(RequestError(502, "stream ended without finishReason")),
            )

    async def stream_response():
        started = time.perf_counter()
        bytes_out = 0
        first_event_at: float | None = None
        completed = False
        try:
            async for event_type, frame in stream_with_heartbeat(_chunk_frames()):
                if event_type == HEARTBEAT_EVENT:
                    # 原生 Gemini 路由没有安全的空 data 帧形状：发 SSE 注释行（规范要求忽略）
                    yield ": ping\n\n"
                    continue
                if first_event_at is None:
                    first_event_at = time.perf_counter() - started
                # stream_with_heartbeat 的元素签名是 object（心跳元组共用）；
                # 这里非心跳事件必为 SSE 帧字符串
                frame = str(frame)
                bytes_out += len(frame)
                yield frame
            completed = True
        except (asyncio.CancelledError, GeneratorExit):
            logger.warning(
                "%s客户端断开 (%.1fs, 已发 %dB)",
                _rid(),
                time.perf_counter() - started,
                bytes_out,
            )
            raise
        except Exception as exc:
            code, message, _status = classify_gemini_error_payload(exc)
            logger.error(
                "%s请求失败 %d [gemini.native %s]: %s",
                _rid(),
                code,
                model_path,
                message,
            )
            yield format_sse_error(exc)
        finally:
            if completed:
                first = f"{first_event_at:.1f}s" if first_event_at is not None else "-"
                logger.info(
                    "← 完成 %s (gemini.native %s, %.1fs, 首包 %s, %dB)",
                    get_request_id() or "-",
                    model_path,
                    time.perf_counter() - started,
                    first,
                    bytes_out,
                )

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
