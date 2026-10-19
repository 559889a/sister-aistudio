"""Gemini-compatible application service handlers."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    MAX_RETRIES,
    ensure_active_account,
    record_rotator_event,
    try_switch_account,
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
    is_transient_rpc_error,
    parse_upstream_grpc_error,
)
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_context import get_request_id

logger = get_logger("api.gemini")

# 浏览器传输失败（Failed to fetch 这类）只在这段时间内视为「瞬时抖动」可以去重试。
# 超过它才失败的请求通常已经在上游排队/生成很久，重试只会让客户端再多等一个同样长
# 的窗口（实测三次重试把一次请求拖到 800+ 秒），所以直接 502 让客户端看到失败。
_BROWSER_TRANSPORT_RETRY_MAX_S = 20.0

# 周期性「上游 Ns 无任何数据」WARN 已按用户要求移除（2026-10-20）：bypass 请求
# 动辄几百秒整段返回，这条 WARN 只会刷屏。静默期只有 1s 心跳；完成后日志里的
# 「首包 X s / 总 Y s」才是判据，失败时另有 CDP 网络层证据。


def _fmt_elapsed(attempt_elapsed_s: float | None) -> str:
    return f", {attempt_elapsed_s:.1f}s" if attempt_elapsed_s is not None else ""


def _rid() -> str:
    rid = get_request_id()
    return f"({rid}) " if rid else ""


def _is_pool_client(client: object) -> bool:
    """识别 pool 模式：``ClientPool`` 显式带 ``is_pool=True`` 标记。

    不能用 ``hasattr(client, "on_transport_error")``：测试里的普通 ``MagicMock``
    对任意属性名都返回 Mock，会被误判成 pool。
    """
    return getattr(client, "is_pool", False) is True


def _pool_account_id() -> str | None:
    """pool 模式下本次请求实际落在哪个账号（worker 绑定，而非全局 active）。"""
    try:
        from aistudio_api.infrastructure.gateway.client_pool import (
            get_current_account_id,
        )

        return get_current_account_id() or None
    except Exception:  # pragma: no cover - 运行时缺模块只影响日志里的账号归因
        return None


WIRE_TO_GEMINI_FINISH_REASON: dict[int, str] = {
    0: "FINISH_REASON_UNSPECIFIED",
    1: "STOP",
    2: "MAX_TOKENS",
    3: "SAFETY",
    4: "RECITATION",
    5: "LANGUAGE",
    6: "OTHER",
    7: "BLOCKLIST",
    8: "PROHIBITED_CONTENT",
    9: "SPII",
    10: "MALFORMED_FUNCTION_CALL",
    11: "IMAGE_SAFETY",
    12: "UNEXPECTED_TOOL_CALL",
    13: "TOO_MANY_TOOL_CALLS",
    14: "IMAGE_PROHIBITED_CONTENT",
    15: "NO_IMAGE",
    16: "IMAGE_RECITATION",
    17: "IMAGE_OTHER",
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


async def handle_attempt_exception(
    exc: Exception,
    *,
    attempt: int,
    model_path: str,
    normalized_model: str | None,
    client: AIStudioClient,
    has_yielded_data: bool = False,
    attempt_elapsed_s: float | None = None,
) -> bool:
    """Handle per-attempt error classification and account switching.

    Returns True if the operation should be retried, or raises HTTPException / re-raises.

    TWO MODES（由 client 上有无 pool 钩子区分）:
    - 单浏览器：行为与历史完全一致（403 切账号 + 账号冷却、429 切号、传输失败快速重试）。
    - pool（``ClientPool``）：403/401、5xx、502、页面传输失败都按 **worker 故障**处理——
      后台重启那台 Chromium，本请求立刻切另一台重试；这些错误**不给账号加冷却**。
    """
    target_model = normalized_model or model_path
    pooled = _is_pool_client(client)
    if pooled:
        # pool 模式下账号由 worker 绑定，不能拿全局 active 账号顶替
        failed_id = _pool_account_id()
    else:
        account_svc = runtime_state.account_service
        active_acc = account_svc.get_active_account() if account_svc else None
        failed_id = active_acc.id if active_acc else None

    async def _pool_hook(name: str, *hook_args: object) -> bool:
        """调用 pool 钩子（重启 worker + 返回「另一台现在能接」）；钩子异常不影响主流程。"""
        hook = getattr(client, name, None)
        if not callable(hook):
            return False
        try:
            return bool(await hook(*hook_args))
        except Exception as hook_exc:  # pragma: no cover - 防御：绝不让钩子把请求打成 500
            logger.warning("%spool 钩子 %s 执行失败: %s", _rid(), name, hook_exc)
            return False

    if isinstance(exc, ValueError):
        raise HTTPException(
            400, detail={"message": str(exc), "type": "bad_request"}
        ) from exc

    if isinstance(exc, SessionExpiredError):
        logger.warning("%s账号会话失效，已被重定向到登录页: %s", _rid(), exc)
        client.clear_templates()
        if pooled:
            # 登录态失效是账号问题（隔离 24h），但 worker 也一起换掉：重启那台 + 切另一台
            record_rotator_event(
                "session_expired", model=target_model, account_id=failed_id
            )
            can = await _pool_hook("on_auth_error", target_model)
            if not has_yielded_data and can and attempt < MAX_RETRIES - 1:
                logger.info(
                    "%s登录态失效：切另一台 worker 重试 (%d/%d)",
                    _rid(),
                    attempt + 1,
                    MAX_RETRIES,
                )
                return True
            raise HTTPException(
                401,
                detail={
                    "message": "All accounts encountered login redirect or expired sessions. Please import fresh cookies.",
                    "type": "auth_error",
                },
            ) from exc
        if not has_yielded_data and await try_switch_account(
            model=target_model,
            failed_account_id=failed_id,
            is_auth_error=True,
            is_session_expired=True,
        ):
            logger.info(
                "已切换至其他可用健康账号重试 (%d/%d)", attempt + 1, MAX_RETRIES
            )
            return True
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
        client.clear_templates()
        if pooled:
            # 用户 2026-10-19 拍板：403/401 不是配额问题，**不给账号加冷却**；
            # 按 worker 故障处理（后台重启 + 本请求立刻切另一台）。
            record_rotator_event("error", model=target_model, account_id=failed_id)
            can = await _pool_hook("on_auth_error", target_model)
            if not has_yielded_data and can and attempt < MAX_RETRIES - 1:
                logger.info(
                    "%s403/401 worker 故障：切另一台重试 (%d/%d)",
                    _rid(),
                    attempt + 1,
                    MAX_RETRIES,
                )
                return True
            raise HTTPException(
                403, detail={"message": str(exc), "type": "auth_error"}
            ) from exc
        record_rotator_event("auth_error", model=target_model)
        if not has_yielded_data and await try_switch_account(
            model=target_model, failed_account_id=failed_id, is_auth_error=True
        ):
            logger.info("切换至可用账号重试 (%d/%d)", attempt + 1, MAX_RETRIES)
            return True
        raise HTTPException(
            403, detail={"message": str(exc), "type": "auth_error"}
        ) from exc

    if isinstance(exc, UsageLimitExceeded):
        runtime_state.record(target_model, "rate_limited")
        if pooled:
            # 429 是真正的配额问题：保留账号/模型冷却；worker 也重启一份干净页面。
            record_rotator_event(
                "rate_limited", model=target_model, account_id=failed_id
            )
            can = await _pool_hook("on_rate_limited", target_model)
            if not has_yielded_data and can and attempt < MAX_RETRIES - 1:
                logger.warning(
                    "%s429 触发限额：worker 冷却 + 切另一台重试 (%d/%d): model=%s",
                    _rid(),
                    attempt + 1,
                    MAX_RETRIES,
                    target_model,
                )
                return True
            raise HTTPException(
                429,
                detail={
                    "message": f"All accounts reached daily quota for model '{target_model}'. Quota resets at 00:00 PST.",
                    "type": "rate_limit_exceeded",
                },
            ) from exc
        record_rotator_event("rate_limited", model=target_model)
        if not has_yielded_data and await try_switch_account(
            model=target_model, failed_account_id=failed_id
        ):
            logger.info(
                "429 触发限额，切换账号 (%d/%d): model=%s",
                attempt + 1,
                MAX_RETRIES,
                target_model,
            )
            return True
        logger.warning("%s全部账号在模型 %s 上均处于冷却状态", _rid(), target_model)
        raise HTTPException(
            429,
            detail={
                "message": f"All accounts reached daily quota for model '{target_model}'. Quota resets at 00:00 PST.",
                "type": "rate_limit_exceeded",
            },
        ) from exc

    if (
        isinstance(exc, RequestError)
        and exc.status == 204
        and attempt == 0
        and not has_yielded_data
    ):
        logger.warning("%sGemini 收到 204，清理模板缓存后重试一次", _rid())
        client.clear_templates()
        return True

    # 针对 Google 上游偶发且完全随机的 RPC 路由 404 (Ambiguous request for service '' and method '/GenerativeService.*')：
    # 此报错为上游端点偶发抖动，在未产生输出时当场立即重试（严禁切号以防浪费配额，其他 404 严格按原样报错抛出）
    if is_transient_rpc_error(exc) and not has_yielded_data and attempt < MAX_RETRIES - 1:
        logger.warning(
            "%s检测到 Google 上游偶发 404 (Ambiguous RPC method)，当场立即重试 (%d/%d): %s",
            _rid(),
            attempt + 1,
            MAX_RETRIES,
            clean_upstream_error_message(str(exc)),
        )
        client.clear_templates()
        return True

    if isinstance(exc, BrowserTransportError):
        # 页面里的 fetch/XHR 网络失败（`Failed to fetch` 这类）：请求拿不到任何响应。
        if pooled:
            # pool 模式：这是「这台浏览器坏了」的信号——后台重启它，本请求立刻切另一台。
            # 不再受「20s 内才重试」限制：失败的那台已经在重启，重试不会拖长窗口。
            record_rotator_event("error", model=target_model, account_id=failed_id)
            can = await _pool_hook("on_transport_error", target_model)
            if not has_yielded_data and can and attempt < MAX_RETRIES - 1:
                logger.warning(
                    "%s页面传输失败%s：切另一台 worker 重试 (%d/%d): %s",
                    _rid(),
                    _fmt_elapsed(attempt_elapsed_s),
                    attempt + 1,
                    MAX_RETRIES,
                    clean_upstream_error_message(str(exc)),
                )
                return True
            logger.error(
                "%s浏览器页面请求失败%s，无可用备用 worker: %s",
                _rid(),
                _fmt_elapsed(attempt_elapsed_s),
                clean_upstream_error_message(str(exc)),
            )
            raise HTTPException(
                502,
                detail={
                    "message": clean_upstream_error_message(str(exc)),
                    "type": "upstream_error",
                },
            ) from exc
        # 单浏览器路径：只有「很快就失败」才当作瞬时抖动重试（历史逻辑，保持不变）。
        elapsed_text = _fmt_elapsed(attempt_elapsed_s)
        quick_failure = (
            attempt_elapsed_s is None
            or attempt_elapsed_s <= _BROWSER_TRANSPORT_RETRY_MAX_S
        )
        if not has_yielded_data and attempt < MAX_RETRIES - 1 and quick_failure:
            logger.warning(
                "%s浏览器页面请求失败%s，重试 %d/%d: %s",
                _rid(),
                elapsed_text,
                attempt + 1,
                MAX_RETRIES,
                clean_upstream_error_message(str(exc)),
            )
            client.clear_templates()
            session = getattr(client, "_session", None)
            if session is not None:
                alive = True
                try:
                    alive = await session.is_alive(timeout_s=1.0)
                except Exception:
                    alive = False
                if not alive:
                    logger.warning("%s浏览器页面已不可用，重建会话后重试", _rid())
                    try:
                        await session.close()
                    except Exception as close_exc:
                        logger.warning("%s重建会话失败: %s", _rid(), close_exc)
            return True
        logger.error(
            "%s浏览器页面请求失败%s，不再重试: %s",
            _rid(),
            elapsed_text,
            clean_upstream_error_message(str(exc)),
        )
        raise HTTPException(
            502,
            detail={
                "message": clean_upstream_error_message(str(exc)),
                "type": "upstream_error",
            },
        ) from exc

    if is_browser_session_hang_error(exc):
        logger.warning(
            "%s模板或 BotGuard 捕获超时%s (%d/%d): %s",
            _rid(),
            _fmt_elapsed(attempt_elapsed_s),
            attempt + 1,
            MAX_RETRIES,
            exc,
        )
        client.clear_templates()
        if pooled:
            # 页面挂死 = 这台 worker 坏了：后台重启 + 本请求切另一台
            record_rotator_event("error", model=target_model, account_id=failed_id)
            can = await _pool_hook("on_transport_error", target_model)
            if not has_yielded_data and attempt < MAX_RETRIES - 1:
                logger.warning(
                    "%s会话挂起：后台重启当前 worker%s，切另一台重试 %d/%d",
                    _rid(),
                    "" if can else "（暂无备用，等待重启完成）",
                    attempt + 1,
                    MAX_RETRIES,
                )
                return True
            raise HTTPException(
                502,
                detail={"message": str(exc), "type": "upstream_error"},
            ) from exc
        if not has_yielded_data and await try_switch_account(
            model=target_model, failed_account_id=failed_id, is_auth_error=False
        ):
            logger.info("已切换账号重试 (%d/%d)", attempt + 1, MAX_RETRIES)
            return True
        if attempt < 2 and not has_yielded_data:
            if client._session is not None:
                await client._session._close_internal()
            return True

    if isinstance(exc, RequestError) and pooled and exc.status >= 500:
        # pool 模式：5xx 也按 worker 故障处理（重启 + 切另一台），但**不给账号冷却**，
        # 并且最终仍按上游原始状态码抛（不能变成 500）。
        clean_msg = clean_upstream_error_message(str(exc))
        status_code = exc.status if exc.status > 0 else 500
        record_rotator_event("error", model=target_model, account_id=failed_id)
        can = await _pool_hook("on_server_error", target_model, status_code)
        if not has_yielded_data and can and attempt < MAX_RETRIES - 1:
            logger.warning(
                "%s上游 %d%s：按 worker 故障切另一台重试 (%d/%d): %s",
                _rid(),
                status_code,
                _fmt_elapsed(attempt_elapsed_s),
                attempt + 1,
                MAX_RETRIES,
                clean_msg,
            )
            return True
        logger.warning(
            "%s上游 %d%s，无可用备用 worker: %s",
            _rid(),
            status_code,
            _fmt_elapsed(attempt_elapsed_s),
            clean_msg,
        )
        raise HTTPException(
            status_code, detail={"message": clean_msg, "type": "upstream_error"}
        ) from exc

    if isinstance(exc, RequestError):
        clean_msg = clean_upstream_error_message(str(exc))
        status_code = exc.status if exc.status > 0 else 500
        error_type = (
            "bad_request"
            if status_code == 400
            else "not_found"
            if status_code == 404
            else "upstream_error"
        )
        logger.warning(
            "%sGemini 上游请求错误 (%d)%s: %s",
            _rid(),
            status_code,
            _fmt_elapsed(attempt_elapsed_s),
            clean_msg,
        )
        raise HTTPException(
            status_code, detail={"message": clean_msg, "type": error_type}
        ) from exc

    if isinstance(exc, AistudioError):
        runtime_state.record(target_model, "errors")
        if pooled:
            # 其余上游异常同样按 worker 故障处理：重启 + 切另一台（仍未吐数据时）
            record_rotator_event("error", model=target_model, account_id=failed_id)
            can = await _pool_hook("on_server_error", target_model, 500)
            if not has_yielded_data and can and attempt < MAX_RETRIES - 1:
                logger.warning(
                    "%s服务端异常%s：按 worker 故障切另一台重试 (%d/%d): %s",
                    _rid(),
                    _fmt_elapsed(attempt_elapsed_s),
                    attempt + 1,
                    MAX_RETRIES,
                    exc,
                )
                return True
            logger.warning(
                "%sGemini 服务端异常%s: %s",
                _rid(),
                _fmt_elapsed(attempt_elapsed_s),
                exc,
            )
            raise HTTPException(
                500, detail={"message": str(exc), "type": "server_error"}
            ) from exc
        record_rotator_event("error", model=target_model)
        logger.warning(
            "%sGemini 服务端异常%s: %s", _rid(), _fmt_elapsed(attempt_elapsed_s), exc
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
    """非流式上游调用（含账号轮换/重试），返回归一化请求与上游输出。

    失败时按既有规则抛 HTTPException——OpenAI 桥会把它翻成 OpenAI 错误体。
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        await ensure_active_account(attempt, model=model_path)
        prepared: PreparedRequest | None = None
        attempt_started = time.perf_counter()
        try:
            prepared = prepare_gemini_request(req, model_path)
            normalized = prepared.normalized
            logger.debug(
                "%s上游尝试 %d/%d (非流式): model=%s, 对话轮数=%d",
                _rid(),
                attempt + 1,
                MAX_RETRIES,
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

            record_rotator_event(
                "success",
                model=normalized.model,
                account_id=_pool_account_id(),
            )
            runtime_state.record(normalized.model, "success", output.usage)
            logger.debug(
                "%s上游返回: 输入Tokens=%s, 输出Tokens=%s",
                _rid(),
                output.usage.get("prompt_tokens") if output.usage else 0,
                output.usage.get("completion_tokens") if output.usage else 0,
            )
            return prepared, output
        except Exception as exc:
            last_error = exc
            if await handle_attempt_exception(
                exc,
                attempt=attempt,
                model_path=model_path,
                normalized_model=prepared.normalized.model if prepared else None,
                client=client,
                attempt_elapsed_s=time.perf_counter() - attempt_started,
            ):
                continue
            raise

    # 循环走满 = 最后一次失败被判「可重试」但 attempts 已用尽（单浏览器路径的切号
    # 分支没有 attempt 守卫）。按真实错误分类抛，别一律谎报成 429 配额耗尽。
    if last_error is None or isinstance(last_error, UsageLimitExceeded):
        raise HTTPException(
            429,
            detail={
                "message": f"All accounts reached daily quota for model '{model_path}'. Quota resets at 00:00 PST.",
                "type": "rate_limit_exceeded",
            },
        ) from last_error
    code, message, _status = classify_gemini_error_payload(last_error)
    raise HTTPException(
        code, detail={"message": message, "type": "upstream_error"}
    ) from last_error


async def handle_gemini_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
    *,
    stream: bool,
):
    if stream:
        return _build_gemini_streaming_response(
            client=client, req=req, model_path=model_path
        )

    prepared, output = await generate_content_output(model_path, req, client)
    include_thoughts = prepared.include_thoughts
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(
                    parts=to_gemini_parts(
                        output.text,
                        function_calls=output.function_calls,
                        function_responses=output.function_responses,
                        thinking=output.thinking if include_thoughts else "",
                        images=output.images,
                        reasoning_images=(
                            output.reasoning_images if include_thoughts else []
                        ),
                    ),
                ),
                finishReason=to_gemini_finish_reason(
                    output.candidates[0].finish_reason if output.candidates else None
                ),
                index=0,
            )
        ],
        usageMetadata=to_gemini_usage_metadata(output.usage),
        modelVersion=prepared.normalized.model,
        responseId=output.response_id or None,
    )


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


# 会产生「真实输出」的流式事件：只有它们才算已经向客户端发过数据（用到重试判定里）。
_DATA_STREAM_EVENTS = frozenset(
    {"body", "thinking", "tool_calls", "images", "reasoning_images"}
)


async def stream_raw_events(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
):
    """流式上游调用（含账号轮换/重试）的公共入口。

    产出 ``("prepared", PreparedRequest)`` 一次（重试时会再产一次），随后原样转发
    ``client.stream_generate_content`` 的事件；重试耗尽仍失败时抛出最后的异常
    （由 Gemini SSE / OpenAI SSE 各自决定怎么把错误写给客户端）。
    """
    last_stream_error: Exception | None = None
    for stream_attempt in range(MAX_RETRIES):
        has_yielded_data = False
        attempt_started = time.perf_counter()
        await ensure_active_account(stream_attempt, model=model_path)
        prepared: PreparedRequest | None = None
        try:
            prepared = prepare_gemini_request(req, model_path)
            normalized = prepared.normalized
            yield ("attempt", stream_attempt + 1)
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
                force_refresh_capture=stream_attempt > 0,
            ):
                if event_type in _DATA_STREAM_EVENTS and text:
                    has_yielded_data = True
                yield (event_type, text)
            return
        except Exception as exc:
            last_stream_error = exc
            if await handle_attempt_exception(
                exc,
                attempt=stream_attempt,
                model_path=model_path,
                normalized_model=prepared.normalized.model if prepared else None,
                client=client,
                has_yielded_data=has_yielded_data,
                attempt_elapsed_s=time.perf_counter() - attempt_started,
            ):
                continue
            raise

    # 循环走满 = 最后一次失败被判「可重试」但 attempts 已用尽。必须把错误抛出去
    # （消费方渲染成流内错误帧），否则客户端只会看到一条静默的空流。
    if last_stream_error is not None:
        raise last_stream_error


def _build_gemini_streaming_response(
    *,
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    async def stream_response():
        prepared: PreparedRequest | None = None
        started = time.perf_counter()
        bytes_out = 0
        attempt = 1
        headers_at: float | None = None
        first_event_at: float | None = None
        completed = False
        try:
            final_usage: dict[str, object] | None = None
            final_finish_reason: str = "STOP"
            latest_response_id: str | None = None
            buffered_thought_sig: str | None = None
            async for event_type, text in stream_with_heartbeat(
                stream_raw_events(model_path, req, client)
            ):
                now = time.perf_counter()
                if event_type == HEARTBEAT_EVENT:
                    # 原生 Gemini 路由没有安全的空 data 帧形状：发 SSE 注释行（规范要求忽略）
                    yield ": ping\n\n"
                    continue
                if event_type == "status":
                    headers_at = now - started
                    if first_event_at is None:
                        first_event_at = headers_at
                    logger.debug(
                        "%s上游响应头 %s (%.1fs)", _rid(), text, headers_at
                    )
                    continue
                if event_type == "attempt":
                    attempt = int(text) if isinstance(text, int) else attempt + 1
                    logger.debug("%s上游尝试 %d", _rid(), attempt)
                    continue
                if event_type == "prepared":
                    prepared = text if isinstance(text, PreparedRequest) else None
                    # 新的一轮尝试：上一轮失败留下的签名不能带进这一轮
                    buffered_thought_sig = None
                    continue
                if first_event_at is None:
                    first_event_at = now - started
                if event_type == "usage":
                    final_usage = text if isinstance(text, dict) else None
                elif event_type == "finish_reason":
                    final_finish_reason = (
                        to_gemini_finish_reason(text)
                        if isinstance(text, int)
                        else str(text)
                    )
                elif event_type == "response_id":
                    latest_response_id = str(text)
                elif event_type == "thought_signature":
                    buffered_thought_sig = str(text)
                elif event_type == "done":
                    continue
                else:
                    include_thoughts = (
                        prepared.include_thoughts if prepared is not None else True
                    )
                    if not include_thoughts and event_type in (
                        "thinking",
                        "reasoning_images",
                    ):
                        continue
                    chunk = format_sse_event(
                        event_type,
                        text,
                        model_version=(
                            prepared.normalized.model
                            if prepared is not None
                            else model_path
                        ),
                        response_id=latest_response_id,
                        thought_signature=buffered_thought_sig,
                    )
                    buffered_thought_sig = None
                    if chunk:
                        bytes_out += len(chunk)
                        yield chunk

            target_model = (
                prepared.normalized.model if prepared is not None else model_path
            )
            record_rotator_event(
                "success",
                model=target_model,
                account_id=_pool_account_id(),
            )
            if prepared is not None:
                runtime_state.record(prepared.normalized.model, "success", final_usage)
            usage_chunk = format_sse_usage(
                final_usage,
                finish_reason=final_finish_reason,
                model_version=target_model,
                response_id=latest_response_id,
            )
            bytes_out += len(usage_chunk)
            yield usage_chunk
            completed = True
        except (asyncio.CancelledError, GeneratorExit):
            logger.warning(
                "%s客户端断开 (%.1fs, 已发 %dB, 尝试 %d)",
                _rid(),
                time.perf_counter() - started,
                bytes_out,
                attempt,
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
                first = (
                    f"{first_event_at:.1f}s" if first_event_at is not None else "-"
                )
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
