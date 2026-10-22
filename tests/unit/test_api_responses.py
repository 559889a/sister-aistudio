import pytest

from aistudio_api.api.responses import (
    to_gemini_parts,
    to_gemini_usage_metadata,
)
from aistudio_api.application.api_service_gemini import classify_gemini_error_payload
from aistudio_api.domain.errors import (
    AuthError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
)


def test_to_gemini_usage_metadata_uses_visible_and_reasoning_tokens():
    assert to_gemini_usage_metadata(
        {
            "prompt_tokens": 9,
            "completion_tokens": 316,
            "total_tokens": 325,
            "completion_tokens_details": {
                "reasoning_tokens": 290,
                "visible_tokens": 26,
            },
        }
    ).model_dump(mode="json", exclude_none=True) == {
        "promptTokenCount": 9,
        "candidatesTokenCount": 26,
        "thoughtsTokenCount": 290,
        "totalTokenCount": 325,
    }


def test_to_gemini_parts_keeps_function_call_and_response_parts():
    parts = to_gemini_parts(
        "",
        function_calls=[{"name": "getWeather", "args": {"city": "Shanghai"}}],
        function_responses=[{"name": "getWeather", "args": {"temperature": "24C"}}],
    )

    assert [part.model_dump(mode="json", exclude_none=True) for part in parts] == [
        {"functionCall": {"name": "getWeather", "args": {"city": "Shanghai"}}},
        {
            "functionResponse": {
                "name": "getWeather",
                "response": {"temperature": "24C"},
            }
        },
    ]


def test_to_gemini_parts_can_emit_thought_part():
    assert [
        part.model_dump(mode="json", exclude_none=True)
        for part in to_gemini_parts("答案", thinking="思考")
    ] == [
        {"text": "思考", "thought": True},
        {"text": "答案"},
    ]


def test_to_gemini_parts_can_emit_inline_image_data():
    from aistudio_api.domain.models import GeneratedImage

    parts = to_gemini_parts(
        "说明",
        images=[GeneratedImage(mime="image/png", data=b"png-bytes", size=9)],
    )

    assert [part.model_dump(mode="json", exclude_none=True) for part in parts] == [
        {"text": "说明"},
        {"inlineData": {"mimeType": "image/png", "data": "cG5nLWJ5dGVz"}},
    ]


def test_classify_gemini_error_payload():
    """Verify exception mapping to Gemini HTTP status and reason code."""
    assert classify_gemini_error_payload(SessionExpiredError("expired")) == (
        401,
        "All accounts have expired sessions. Please import fresh cookies.",
        "UNAUTHENTICATED",
    )
    assert classify_gemini_error_payload(AuthError("auth forbidden")) == (
        403,
        "auth forbidden",
        "PERMISSION_DENIED",
    )
    assert classify_gemini_error_payload(UsageLimitExceeded("quota exceeded")) == (
        429,
        "quota exceeded",
        "RESOURCE_EXHAUSTED",
    )
    assert classify_gemini_error_payload(ValueError("invalid input")) == (
        400,
        "invalid input",
        "INVALID_ARGUMENT",
    )
    assert classify_gemini_error_payload(RequestError(429, "rate limit")) == (
        429,
        "HTTP 429: rate limit",
        "RESOURCE_EXHAUSTED",
    )
    # JSPB bracket array error from upstream Google MakerSuite
    jspb_400 = 'HTTP 400: [,[3,"Invalid value (), Unexpected list for single non-message field.",[["type.googleapis.com/google.rpc.BadRequest"]]]]'
    assert classify_gemini_error_payload(RequestError(400, jspb_400)) == (
        400,
        "Invalid value (), Unexpected list for single non-message field.",
        "INVALID_ARGUMENT",
    )


def test_clean_upstream_error_message():
    from aistudio_api.application.api_service_gemini import clean_upstream_error_message

    assert clean_upstream_error_message("") == ""
    assert clean_upstream_error_message("Normal plain error") == "Normal plain error"
    assert (
        clean_upstream_error_message("HTTP 429: rate limit") == "HTTP 429: rate limit"
    )
    jspb_nested = 'HTTP 400: [,[3,"Please enable tool_config.include_server_side_tool_invocations to use Built-in tools with Function calling.",[["type.googleapis.com/details"]]]]'
    assert (
        clean_upstream_error_message(jspb_nested)
        == "Please enable tool_config.include_server_side_tool_invocations to use Built-in tools with Function calling."
    )
    jspb_404 = 'HTTP 404: [,[5,"Requested entity was not found."]]'
    assert clean_upstream_error_message(jspb_404) == "Requested entity was not found."


@pytest.mark.asyncio
async def test_handle_attempt_exception_404_raises_without_retry():
    """404（含偶发的 Ambiguous RPC 抖动）一律按原状态码返回，网关不重试。"""
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from aistudio_api.application.api_service_gemini import handle_attempt_exception
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.clear_templates = MagicMock()

    ambiguous_err = RequestError(
        404,
        "Ambiguous request for service '' and method '/GenerativeService.StreamGenerateContent'. Please use fully qualified (unique) service and method names to call this method.",
    )
    with pytest.raises(HTTPException) as exc_info:
        await handle_attempt_exception(
            ambiguous_err,
            model_path="gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=mock_client,
        )
    assert exc_info.value.status_code == 404

    other_404 = RequestError(404, "Model 'models/nonexistent' not found.")
    with pytest.raises(HTTPException) as exc_info:
        await handle_attempt_exception(
            other_404,
            model_path="nonexistent",
            normalized_model="models/nonexistent",
            client=mock_client,
        )
    assert exc_info.value.status_code == 404

@pytest.mark.asyncio
async def test_build_gemini_streaming_response_passes_requested_model():
    from unittest.mock import MagicMock

    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiPart,
    )
    from aistudio_api.application.api_service_gemini import (
        handle_gemini_generate_content,
    )
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    passed_model = None

    async def fake_stream_generate_content(*args, **kwargs):
        nonlocal passed_model
        passed_model = kwargs.get("model")
        yield ("chunk", b"data: test")
        yield ("finish_reason", "STOP")

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.stream_generate_content = MagicMock(side_effect=fake_stream_generate_content)

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])]
    )
    resp = await handle_gemini_generate_content(
        "gemini-2.5-pro", req, mock_client, stream=True
    )

    chunks = [chunk async for chunk in resp.body_iterator]
    assert len(chunks) > 0
    assert passed_model == "models/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_streaming_metadata_then_error_emits_error_frame_without_retry():
    """仅收到 thought_signature 等元数据后遇错：渲染错误帧，只打一次上游（零重试）。"""
    from unittest.mock import MagicMock

    from aistudio_api.api.schemas import (
        GeminiContent,
        GeminiGenerateContentRequest,
        GeminiPart,
    )
    from aistudio_api.application.api_service_gemini import (
        handle_gemini_generate_content,
    )
    from aistudio_api.domain.errors import RequestError
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    attempt_count = 0

    async def fake_stream_generate_content(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        # 仅产生内部元数据事件，尚未发出实际正文 chunk，随即遇到 204/抖动异常
        yield ("thought_signature", "sig_early")
        raise RequestError(204, "No content")

    mock_client = MagicMock(spec=AIStudioClient)
    mock_client.stream_generate_content = MagicMock(side_effect=fake_stream_generate_content)
    mock_client.clear_templates = MagicMock()

    req = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])]
    )
    resp = await handle_gemini_generate_content(
        "gemini-3.8-flash", req, mock_client, stream=True
    )

    chunks = [chunk async for chunk in resp.body_iterator]
    # 只打一次上游；失败渲染成流内错误帧，不补一轮
    assert attempt_count == 1
    assert any("error" in str(c) for c in chunks)
    assert not any("Hello world" in str(c) for c in chunks)
