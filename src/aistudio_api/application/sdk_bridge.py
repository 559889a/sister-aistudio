"""google-genai 官方 SDK 桥接层（设计见 docs/research/googlesdk-bridge-design.md）。

思路：不魔改 SDK——`HttpOptions.httpx_async_client` 官方支持注入自定义 httpx 客户端，
这里的 `AistudioTransport` 接管 SDK 的全部 HTTP：把 SDK 发出的官方 REST JSON 翻译成
我们既有网关调用（复用 /v1beta 端口的同一套 `generate_content_output` /
`stream_raw_events`，行为与现有端口**按构造对齐**），再把结果装回 `httpx.Response`——
非流式塞官方 JSON，流式塞产出 ``data: {...}\\n\\n`` 帧的 async 迭代器。

Phase 1（当前）：transport + 转换器 + `build_sdk_client`，**未接线到任何路由**，
仅由测试用 fake gateway 驱动。Phase 2 在 OpenAI 端口 feature flag 试点。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.application.api_service_gemini import (
    PreparedRequest,
    classify_gemini_error_payload,
    format_sse_error,
    format_sse_event,
    format_sse_usage,
    generate_content_output,
    stream_raw_events,
    to_gemini_finish_reason,
)
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.domain.models import ModelOutput
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

logger = get_logger("sdk_bridge")

# SDK 的请求路径形如 /v1beta/models/{model}:generateContent（流式多 ?alt=sse）
_GENERATE_PATH_RE = re.compile(r"^/[^/]+/models/(?P<model>[^:]+):(?P<method>stream)?[Gg]enerate[Cc]ontent$")
_JSON_HEADERS = {"content-type": "application/json"}
_SSE_HEADERS = {"content-type": "text/event-stream", "cache-control": "no-cache"}


def _parse_generate_path(path: str) -> tuple[str, bool] | None:
    m = _GENERATE_PATH_RE.match(path)
    if m is None:
        return None
    return m.group("model"), m.group("method") == "stream"


def _error_body(code: int, message: str, status: str) -> bytes:
    # SDK 的 raise_for_async_response 只认 {"error":{code,message,status}}（JSON 解析
    # 失败时 status 退化成 reason_phrase），非 JSON 体必须包成这个形状。
    return json.dumps(
        {"error": {"code": code, "message": message, "status": status}},
        ensure_ascii=False,
    ).encode("utf-8")


def _error_response(request: httpx.Request, code: int, message: str, status: str) -> httpx.Response:
    return httpx.Response(
        code, headers=_JSON_HEADERS, content=_error_body(code, message, status), request=request
    )


def _http_exception_response(request: httpx.Request, exc: Exception) -> httpx.Response:
    code, message, status_str = classify_gemini_error_payload(exc)
    return _error_response(request, code, message, status_str)


def _single_response_payload(prepared: PreparedRequest, output: ModelOutput) -> bytes:
    include_thoughts = prepared.include_thoughts
    resp = GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(
                    parts=to_gemini_parts(
                        output.text,
                        function_calls=output.function_calls,
                        function_responses=output.function_responses,
                        thinking=output.thinking if include_thoughts else "",
                        images=output.images,
                        reasoning_images=(output.reasoning_images if include_thoughts else []),
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
    return json.dumps(
        resp.model_dump(mode="json", exclude_none=True), ensure_ascii=False
    ).encode("utf-8")


class AistudioTransport(httpx.AsyncBaseTransport):
    """把 google-genai SDK 的官方 REST 请求接进我们的浏览器网关。

    只认 GenerateContent / StreamGenerateContent；其它端点（countTokens 等）按
    501 显式拒绝，防止静默错路由。gateway 只要求具备 AIStudioClient 的调用面
    （generate_content / stream_generate_content），测试可用替身。
    """

    def __init__(self, gateway: AIStudioClient) -> None:
        self._gateway = gateway

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from urllib.parse import unquote

        parsed = _parse_generate_path(request.url.path)
        if parsed is None:
            return _error_response(
                request, 501, f"sdk bridge 仅支持 GenerateContent: {request.url.path}", "UNIMPLEMENTED"
            )
        model, streaming = parsed
        model = unquote(model)
        try:
            payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error_response(request, 400, f"invalid JSON body: {exc}", "INVALID_ARGUMENT")
        try:
            req = GeminiGenerateContentRequest.model_validate(payload)
        except Exception as exc:
            return _error_response(request, 400, str(exc), "INVALID_ARGUMENT")

        if not streaming:
            try:
                prepared, output = await generate_content_output(model, req, self._gateway)
            except Exception as exc:
                return _http_exception_response(request, exc)
            return httpx.Response(
                200,
                headers=_JSON_HEADERS,
                content=_single_response_payload(prepared, output),
                request=request,
            )

        frames = self._sse_frames(model, req)
        # 首帧之前上游就失败的话，还能改成错误状态码（SDK 才会抛 APIError）；
        # 200 一旦发出就只能把错误当流内帧兜底。
        try:
            first = await frames.__anext__()
        except StopAsyncIteration:
            return httpx.Response(
                200, headers=_SSE_HEADERS, content=b"", request=request
            )
        except Exception as exc:
            return _http_exception_response(request, exc)

        async def _content() -> AsyncIterator[bytes]:
            yield first
            async for frame in frames:
                yield frame

        return httpx.Response(
            200, headers=_SSE_HEADERS, content=_content(), request=request
        )

    async def _sse_frames(
        self, model: str, req: GeminiGenerateContentRequest
    ) -> AsyncIterator[bytes]:
        """复用 /v1beta 流式渲染的事件语义（format_sse_event/usage），逐帧转 SSE。"""
        prepared: PreparedRequest | None = None
        final_usage: dict[str, object] | None = None
        final_finish_reason: str = "STOP"
        latest_response_id: str | None = None
        buffered_sig: str | None = None
        try:
            async for event_type, text in stream_raw_events(model, req, self._gateway):
                if event_type == "prepared":
                    prepared = text if isinstance(text, PreparedRequest) else None
                    buffered_sig = None
                    continue
                if event_type in ("status", "attempt", "done"):
                    continue
                if event_type == "usage":
                    final_usage = text if isinstance(text, dict) else None
                elif event_type == "finish_reason":
                    final_finish_reason = (
                        to_gemini_finish_reason(text) if isinstance(text, int) else str(text)
                    )
                elif event_type == "response_id":
                    latest_response_id = str(text)
                elif event_type == "thought_signature":
                    buffered_sig = str(text)
                else:
                    include_thoughts = (
                        prepared.include_thoughts if prepared is not None else True
                    )
                    if not include_thoughts and event_type in ("thinking", "reasoning_images"):
                        continue
                    frame = format_sse_event(
                        event_type,
                        text,
                        model_version=(
                            prepared.normalized.model if prepared is not None else model
                        ),
                        response_id=latest_response_id,
                        thought_signature=buffered_sig,
                    )
                    buffered_sig = None
                    if frame:
                        yield frame.encode("utf-8")
            usage_frame = format_sse_usage(
                final_usage,
                finish_reason=final_finish_reason,
                model_version=(prepared.normalized.model if prepared is not None else model),
                response_id=latest_response_id,
            )
            yield usage_frame.encode("utf-8")
        except Exception as exc:
            # 与 /v1beta 流式渲染同口径：首帧已发出后失败，状态码改不了，
            # 把错误装成流内 error 帧收尾（handle_attempt_exception 的浏览器
            # 重启等副作用在异常抛出时已经发生）。
            logger.warning("SDK 桥流式中途失败: %s", exc)
            yield format_sse_error(exc).encode("utf-8")


def build_sdk_client(gateway: AIStudioClient):
    """构造接进我们网关的 google-genai 客户端（全部官方公开 API，零 monkeypatch）。

    - ``base_url`` 任意假地址：只用于 SDK 侧路径拼装，请求根本不会发到网络；
      同时让 SDK 跳过鉴权充分性检查（api_key 仅进 header，被 transport 忽略）。
    - ``retry_options(attempts=1)``：关掉 SDK 默认 5 次重试（用户零自动重试决策）。
    - ``httpx_async_client`` 注入同时把 _use_aiohttp() 锁死为 False。
    """
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key="aistudio-bridge",
        http_options=types.HttpOptions(
            base_url="http://aistudio-bridge.invalid",
            api_version="v1beta",
            httpx_async_client=httpx.AsyncClient(transport=AistudioTransport(gateway)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
