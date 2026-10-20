"""Gemini-compatible API routes."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from aistudio_api.application.api_service_gemini import (
    classify_gemini_error_payload,
    handle_gemini_generate_content,
)
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_context import (
    new_request_id,
    set_request_id,
)

from .dependencies import get_client
from .schemas import GeminiGenerateContentRequest

logger = get_logger("routes.gemini")
router = APIRouter()


@router.post("/v1beta/{model_path:path}:generateContent")
# 不设 response_model：走 SDK 类型化响应，富字段（grounding/logprobs 等）原样透传
async def generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient = Depends(get_client),
):
    request_id = new_request_id()
    set_request_id(request_id)
    started = time.perf_counter()
    logger.info(
        "→ POST /v1beta/%s:generateContent [gemini.native] %s msgs=%d",
        model_path,
        request_id,
        len(req.contents),
    )
    try:
        result = await handle_gemini_generate_content(
            model_path, req, client, stream=False
        )
    except Exception as exc:
        code, message, _status = classify_gemini_error_payload(exc)
        logger.error(
            "请求失败 %s %d [gemini.native %s]: %s",
            request_id,
            code,
            model_path,
            message,
        )
        raise
    logger.info(
        "← 完成 %s (%.1fs)", request_id, time.perf_counter() - started
    )
    return result


@router.post("/v1beta/{model_path:path}:streamGenerateContent")
async def stream_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient = Depends(get_client),
):
    request_id = new_request_id()
    set_request_id(request_id)
    logger.info(
        "→ POST /v1beta/%s:streamGenerateContent [gemini.native] %s msgs=%d",
        model_path,
        request_id,
        len(req.contents),
    )
    return await handle_gemini_generate_content(
        model_path, req, client, stream=True
    )
