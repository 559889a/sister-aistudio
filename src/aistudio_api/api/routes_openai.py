"""OpenAI-compatible API routes (`/v1/chat/completions` + `/v1/models`).

上游能力全部复用 Gemini 原生端口那条链路：请求由
:func:`aistudio_api.application.openai_bridge.openai_to_gemini_request` 变成同形的
Gemini 请求，再交给 `application.api_service_gemini` 的
normalize → capture → replay → parse（含账号轮换/重试/参数覆写），最后把结果翻回
OpenAI 形状。这里只做「协议层」：路由、SSE 输出、错误体、请求级日志。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from aistudio_api.api.dependencies import get_client, get_runtime_state
from aistudio_api.api.schemas import (
    GeminiGenerateContentRequest,
    OpenAIChatCompletionRequest,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_gemini import (
    PreparedRequest,
    classify_gemini_error_payload,
    generate_content_output,
    stream_raw_events,
)
from aistudio_api.application.openai_bridge import (
    SSE_DONE,
    OpenAIStreamConverter,
    format_sse,
    openai_error_payload,
    openai_to_gemini_request,
    to_openai_response,
)
from aistudio_api.application.stream_heartbeat import (
    HEARTBEAT_EVENT,
    stream_with_heartbeat,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_context import (
    new_request_id,
    set_request_id,
)

if TYPE_CHECKING:
    from .state import RuntimeState

logger = get_logger("routes.openai")
router = APIRouter()

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
# 周期性「上游 Ns 无任何数据」WARN 已按用户要求移除（2026-10-20）：bypass 请求
# 动辄几百秒整段返回，这条 WARN 只会刷屏。静默期只有 1s 心跳；完成后日志里的
# 「首包 X s / 总 Y s」才是判据，失败时另有 CDP 网络层证据。


def _new_request_id() -> str:
    return new_request_id()


def _usage_text(usage: object) -> str:
    if not isinstance(usage, Mapping):
        return ""
    total = usage.get("total_tokens")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if total is None and prompt is None and completion is None:
        return ""
    return f", {total} tok (in {prompt} / out {completion})"


def _heartbeat_frame(model: str, request_id: str) -> dict[str, object]:
    """一条 OpenAI 心跳帧：合法 chunk、空 delta、不带 usage（主项目 _heartbeatFrame 同款）。"""
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    payload: OpenAIChatCompletionRequest,
    client: AIStudioClient = Depends(get_client),
):
    """OpenAI Chat Completions 兼容入口（非流式 JSON / 流式 SSE）。"""
    request_id = _new_request_id()
    set_request_id(request_id)
    mode = "stream" if payload.stream else "json"
    response_model = payload.model
    started = time.perf_counter()
    logger.info(
        "→ POST /v1/chat/completions [openai.chat] %s model=%s mode=%s msgs=%d tools=%d",
        request_id,
        payload.model,
        mode,
        len(payload.messages),
        len(payload.tools or []),
    )
    try:
        gemini_request = openai_to_gemini_request(payload)
    except Exception as exc:
        logger.error(
            "请求失败 %s 400 [openai.chat %s]: %s", request_id, payload.model, exc
        )
        return JSONResponse(status_code=400, content=openai_error_payload(exc))

    # 前缀选路（pg/ express/ cookie/）：通道定了再把前缀从模型名剥掉
    # （Gemini 请求体不带模型名，模型只跟着 payload.model 走下游）
    registry = runtime_state.upstreams
    if registry is not None:
        picked, clean_model, _channel = registry.pick(payload.model)
        if picked is not None:
            client = picked
        if clean_model != payload.model:
            payload = payload.model_copy(update={"model": clean_model})
    if payload.stream:
        return StreamingResponse(
            _stream_openai_chunks(
                payload, gemini_request, client, request_id, started, response_model
            ),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    try:
        prepared, output = await generate_content_output(
            payload.model, gemini_request, client
        )
        body = to_openai_response(
            output,
            model=response_model,
            request_id=request_id,
            include_thoughts=prepared.include_thoughts,
        )
        elapsed = time.perf_counter() - started
        logger.info(
            "← 完成 %s (%.1fs%s)",
            request_id,
            elapsed,
            _usage_text(body.get("usage")),
        )
        return JSONResponse(body)
    except Exception as exc:
        code, message, _status = classify_gemini_error_payload(exc)
        logger.error(
            "请求失败 %s %d [openai.chat %s]: %s",
            request_id,
            code,
            payload.model,
            message,
        )
        return JSONResponse(status_code=code, content=openai_error_payload(exc))


async def _stream_openai_chunks(
    payload: OpenAIChatCompletionRequest,
    gemini_request: GeminiGenerateContentRequest,
    client: AIStudioClient,
    request_id: str,
    started: float,
    response_model: str,
):
    """把内部流式事件翻成 OpenAI SSE 帧，末尾补 `data: [DONE]`。

    上游（尤其 bypass 首块之前）可能长时间静默：包一层心跳，1 秒没有事件就发一条
    合法空 delta chunk（converter 还没 ready 时也能发——它只依赖 model / request_id）。
    """
    include_usage = bool(
        payload.stream_options is not None and payload.stream_options.include_usage
    )
    converter: OpenAIStreamConverter | None = None
    attempt = 1
    bytes_out = 0
    headers_at: float | None = None
    first_event_at: float | None = None
    completed = False
    events = stream_with_heartbeat(stream_raw_events(payload.model, gemini_request, client))
    try:
        async for event_type, data in events:
            now = time.perf_counter()
            if event_type == HEARTBEAT_EVENT:
                frame = format_sse(_heartbeat_frame(response_model, request_id))
                bytes_out += len(frame)
                yield frame
                continue
            if event_type == "status":
                headers_at = now - started
                if first_event_at is None:
                    first_event_at = headers_at
                logger.debug(
                    "上游响应头 %s (%.1fs) (%s)",
                    data,
                    headers_at,
                    request_id,
                )
                continue
            if event_type == "attempt":
                attempt = int(data) if isinstance(data, int) else attempt + 1
                logger.debug("上游尝试 %d (%s)", attempt, request_id)
                continue
            if event_type == "prepared":
                if converter is None and isinstance(data, PreparedRequest):
                    converter = OpenAIStreamConverter(
                        model=response_model,
                        request_id=request_id,
                        include_thoughts=data.include_thoughts,
                        include_usage=include_usage,
                    )
                continue
            if converter is None:
                continue
            if first_event_at is None:
                first_event_at = now - started
            for chunk in converter.convert_event(event_type, data):
                frame = format_sse(chunk)
                bytes_out += len(frame)
                yield frame

        if converter is not None:
            for chunk in converter.finish():
                frame = format_sse(chunk)
                bytes_out += len(frame)
                yield frame
        yield SSE_DONE
        completed = True
    except (asyncio.CancelledError, GeneratorExit):
        logger.warning(
            "客户端断开 %s (%.1fs, 已发 %dB, 尝试 %d)",
            request_id,
            time.perf_counter() - started,
            bytes_out,
            attempt,
        )
        raise
    except Exception as exc:
        code, message, _status = classify_gemini_error_payload(exc)
        logger.error(
            "请求失败 %s %d [openai.chat %s]: %s",
            request_id,
            code,
            payload.model,
            message,
        )
        yield format_sse(openai_error_payload(exc))
        yield SSE_DONE
        return
    finally:
        await events.aclose()
        if completed:
            usage = converter.total_usage if converter is not None else None
            first = (
                f"{first_event_at:.1f}s" if first_event_at is not None else "-"
            )
            logger.info(
                "← 完成 %s (%.1fs, 首包 %s, %dB%s)",
                request_id,
                time.perf_counter() - started,
                first,
                bytes_out,
                _usage_text(usage),
            )


@router.get("/v1/models")
async def list_models(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, object]:
    """本地 models.json（2026-10-20 用户拍板：不走云端动态发现）。

    每个模型展开 base + 三通道 alias（pg/ 无头、express/ 官方 Key、cookie/ 控制台
    Cookie）；未配置凭据的通道不列。文件可直接编辑，改完即生效（无缓存）。
    """
    from aistudio_api.infrastructure.upstreams.registry import local_model_list_payload

    payload = local_model_list_payload(runtime_state.upstreams)
    logger.debug("本地模型列表完成 (共 %d 个)", len(payload.get("data") or []))
    return payload
