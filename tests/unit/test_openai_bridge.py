"""OpenAI ↔ Gemini 转换单测（请求映射、工具调用、响应、流式分帧、usage）。"""

from __future__ import annotations

import json

import pytest

from aistudio_api.api.schemas import (
    OpenAIChatCompletionRequest,
)
from aistudio_api.application.openai_bridge import (
    SSE_DONE,
    OpenAIStreamConverter,
    convert_usage,
    format_sse,
    map_finish_reason,
    openai_error_payload,
    openai_to_gemini_request,
    to_openai_model_list,
    to_openai_response,
)
from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, ModelOutput


def _request(**overrides) -> OpenAIChatCompletionRequest:
    payload = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}
    payload.update(overrides)
    return OpenAIChatCompletionRequest.model_validate(payload)


# ────────────────────────────── 请求：messages ──────────────────────────────


def test_request_converts_system_and_user_messages():
    payload = _request(
        messages=[
            {"role": "system", "content": "You are helpful"},
            {"role": "developer", "content": "Be terse"},
            {"role": "user", "content": "hello"},
        ]
    )
    gemini = openai_to_gemini_request(payload)

    assert gemini.systemInstruction is not None
    system_text = gemini.systemInstruction.parts[0].text
    assert system_text == "You are helpful\n\nBe terse"
    assert len(gemini.contents) == 1
    assert gemini.contents[0].role == "user"
    assert gemini.contents[0].parts[0].text == "hello"


def test_request_mid_conversation_system_appends_to_last_message():
    payload = _request(
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "system", "content": "inline rule"},
            {"role": "user", "content": "next"},
        ]
    )
    gemini = openai_to_gemini_request(payload)
    # 中途 system 追加到上一条 model 消息末尾的 text part
    assert gemini.contents[1].role == "model"
    assert gemini.contents[1].parts[-1].text == "answer\ninline rule"
    assert gemini.contents[2].parts[0].text == "next"
    assert gemini.systemInstruction is None


def test_request_mid_system_after_tool_result_starts_new_user_turn():
    payload = _request(
        messages=[
            {"role": "user", "content": "run tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q":"a"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
            {"role": "system", "content": "post-tool rule"},
            {"role": "user", "content": "continue"},
        ]
    )
    gemini = openai_to_gemini_request(payload)
    # FR 轮后不能把 system 塞进同一个 user 轮（否则会和下一条 user 合并成签名校验器拒收的形状）
    tool_turn = gemini.contents[2]
    assert tool_turn.parts[0].functionResponse is not None
    assert len(tool_turn.parts) == 1
    # system 另起了一条 user 轮，随后与普通 user 轮按「同角色合并」合成一个轮的两个 part
    assert gemini.contents[3].role == "user"
    assert [part.text for part in gemini.contents[3].parts] == [
        "post-tool rule",
        "continue",
    ]


def test_request_converts_tool_calls_and_tool_results():
    payload = _request(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city":"SF"}',
                        },
                        "extra_content": {
                            "google": {"thought_signature": "sig-1"}
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"temp": 20}',
            },
        ]
    )
    gemini = openai_to_gemini_request(payload)

    assistant = gemini.contents[1]
    assert assistant.role == "model"
    assert assistant.parts[0].text == "let me check"
    function_call = assistant.parts[1].functionCall
    assert function_call is not None
    assert function_call.name == "get_weather"
    assert function_call.args == {"city": "SF"}
    assert function_call.id == "call_1"
    # 思考签名挂在本轮第一个 functionCall 上
    assert assistant.parts[1].thoughtSignature == "sig-1"

    tool_turn = gemini.contents[2]
    function_response = tool_turn.parts[0].functionResponse
    assert function_response is not None
    assert function_response.name == "get_weather"
    assert function_response.response == {"temp": 20}


def test_request_tool_result_name_falls_back_to_unknown_function():
    payload = _request(
        messages=[
            {"role": "user", "content": "x"},
            {"role": "tool", "tool_call_id": "missing", "content": "plain text"},
        ]
    )
    gemini = openai_to_gemini_request(payload)
    function_response = gemini.contents[1].parts[0].functionResponse
    assert function_response is not None
    assert function_response.name == "unknown_function"
    assert function_response.response == {"result": "plain text"}


def test_request_merges_consecutive_same_role_but_not_tool_result_turns():
    payload = _request(
        messages=[
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c1", "content": "r2"},
            {"role": "user", "content": "next"},
        ]
    )
    gemini = openai_to_gemini_request(payload)
    assert gemini.contents[0].role == "user"
    assert [part.text for part in gemini.contents[0].parts] == ["a", "b"]
    # 连续的两条 tool 结果合并成一个 FR 轮
    assert len(gemini.contents[2].parts) == 2
    # FR 轮与后面的 user 轮不合并
    assert gemini.contents[3].role == "user"
    assert gemini.contents[3].parts[0].text == "next"


def test_request_content_parts_support_image_data_url():
    payload = _request(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ]
    )
    gemini = openai_to_gemini_request(payload)
    parts = gemini.contents[0].parts
    assert parts[0].text == "what is this"
    assert parts[1].inlineData is not None
    assert parts[1].inlineData.mimeType == "image/png"
    assert parts[1].inlineData.data == "AAAA"


# ────────────────────────────── 请求：参数与工具 ──────────────────────────────


def test_request_converts_tools_to_function_declarations():
    payload = _request(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
        tool_choice="required",
    )
    gemini = openai_to_gemini_request(payload)

    assert gemini.tools is not None
    declarations = gemini.tools[0].functionDeclarations
    assert declarations is not None
    declaration = declarations[0]
    assert declaration["name"] == "get_weather"
    assert declaration["description"] == "Get weather"
    assert declaration["parameters"]["type"] == "object"
    assert declaration["parameters"]["required"] == ["city"]
    assert gemini.toolConfig == {"functionCallingConfig": {"mode": "ANY"}}


def test_request_tool_choice_none_and_named_function():
    named = _request(
        messages=[{"role": "user", "content": "hi"}],
        tool_choice={"type": "function", "function": {"name": "do_thing"}},
    )
    gemini = openai_to_gemini_request(named)
    assert gemini.toolConfig == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["do_thing"],
        }
    }

    disabled = _request(messages=[{"role": "user", "content": "hi"}], tool_choice="none")
    assert openai_to_gemini_request(disabled).toolConfig == {
        "functionCallingConfig": {"mode": "NONE"}
    }


def test_request_maps_generation_config():
    payload = _request(
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.3,
        top_p=0.8,
        top_k=20,
        max_tokens=50,
        max_completion_tokens=100,
        stop=["END", "STOP"],
        presence_penalty=0.1,
        frequency_penalty=0.2,
        reasoning_effort="low",
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "out",
                "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        },
    )
    generation_config = openai_to_gemini_request(payload).generationConfig
    assert generation_config is not None
    assert generation_config.temperature == 0.3
    assert generation_config.topP == 0.8
    assert generation_config.topK == 20
    # max_completion_tokens 优先于 max_tokens
    assert generation_config.maxOutputTokens == 100
    assert generation_config.stopSequences == ["END", "STOP"]
    assert generation_config.presencePenalty == 0.1
    assert generation_config.frequencyPenalty == 0.2
    assert generation_config.responseMimeType == "application/json"
    assert generation_config.responseSchema == {
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }
    assert generation_config.thinkingConfig == {"thinkingLevel": "low"}


def test_request_without_generation_config_returns_none():
    gemini = openai_to_gemini_request(_request())
    assert gemini.generationConfig is None


def test_request_rejects_empty_messages():
    payload = OpenAIChatCompletionRequest.model_construct(model="m", messages=[])
    with pytest.raises(ValueError):
        openai_to_gemini_request(payload)


def test_converted_request_encodes_into_wire_body():
    """端到端形状自检：OAI 请求 → 归一化/覆写 → jspb wire body（不经过浏览器）。"""
    from aistudio_api.application.chat_service import normalize_gemini_request
    from aistudio_api.application.overrides import apply_overrides
    from aistudio_api.infrastructure.gateway.wire_codec import modify_body

    payload = _request(
        messages=[
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hello"},
        ],
        temperature=0.7,
        max_tokens=64,
        reasoning_effort="low",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {"properties": {"q": {"type": "string"}}},
                },
            }
        ],
    )
    gemini = openai_to_gemini_request(payload)
    normalized = normalize_gemini_request(gemini, "gemini-3.8-flash")
    apply_overrides(normalized, request=gemini, settings={})

    template = '["models/original",null,null,null,"!snap"]'
    wire_body = json.loads(
        modify_body(
            template,
            model=normalized.model,
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
    )

    # contents: [[parts, role], ...]
    assert [content[1] for content in wire_body[1]] == ["user"]
    assert wire_body[1][0][0][0][1] == "hello"
    # systemInstruction: [[part, ...], role]
    assert wire_body[5][0][0][1] == "be nice"
    # generationConfig: 工具声明 + thinking 档位 + max_tokens
    generation_config = wire_body[3]
    assert generation_config[3] == 64
    assert generation_config[16] == [1, None, None, 1]
    # tools: [[null, [[name, description, parameters_schema]]]]
    declaration = wire_body[6][0][1][0]
    assert declaration[0] == "lookup"
    assert declaration[2][6] == [["q", [1]]]


# ────────────────────────────── 响应 ──────────────────────────────


@pytest.mark.parametrize(
    ("wire_code", "expected"),
    [
        (1, "stop"),
        (2, "length"),
        (3, "content_filter"),
        (4, "content_filter"),
        (6, "stop"),
        (None, "stop"),
    ],
)
def test_response_maps_finish_reason(wire_code, expected):
    output = ModelOutput(candidates=[Candidate(text="x", finish_reason=wire_code)])
    response = to_openai_response(output, model="m", request_id="r")
    assert response["choices"][0]["finish_reason"] == expected


def test_map_finish_reason_table():
    assert map_finish_reason("STOP") == "stop"
    assert map_finish_reason("MAX_TOKENS") == "length"
    assert map_finish_reason("SAFETY") == "content_filter"
    assert map_finish_reason("FINISH_REASON_UNSPECIFIED") is None
    assert map_finish_reason(None) is None
    assert map_finish_reason("SOMETHING_NEW") == "stop"


def test_response_reasoning_content_respects_include_thoughts():
    output = ModelOutput(
        candidates=[Candidate(text="visible", thinking="chain of thought")]
    )
    with_reasoning = to_openai_response(
        output, model="m", request_id="r", include_thoughts=True
    )
    message = with_reasoning["choices"][0]["message"]
    assert message["content"] == "visible"
    assert message["reasoning_content"] == "chain of thought"

    without_reasoning = to_openai_response(
        output, model="m", request_id="r", include_thoughts=False
    )
    message = without_reasoning["choices"][0]["message"]
    assert "reasoning_content" not in message


def test_response_tool_calls_and_finish_tool_calls():
    output = ModelOutput(
        candidates=[
            Candidate(
                text="",
                finish_reason=1,
                function_calls=[
                    {
                        "name": "get_weather",
                        "args": {"city": "SF"},
                        "call_id": "call_real",
                        "thought_signature": "sig-1",
                    },
                    {"name": "other", "arguments": '{"x": 1}'},
                ],
            )
        ]
    )
    response = to_openai_response(output, model="m", request_id="rid")
    choice = response["choices"][0]
    message = choice["message"]
    assert message["content"] is None
    assert choice["finish_reason"] == "tool_calls"
    calls = message["tool_calls"]
    assert calls[0]["id"] == "call_real"
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "SF"}
    assert calls[0]["extra_content"] == {
        "google": {"thought_signature": "sig-1"}
    }
    # 只有第一个调用带签名
    assert "extra_content" not in calls[1]
    assert json.loads(calls[1]["function"]["arguments"]) == {"x": 1}


def test_response_multiple_candidates_become_choices():
    output = ModelOutput(
        candidates=[
            Candidate(text="a", finish_reason=1),
            Candidate(text="b", finish_reason=2),
        ]
    )
    response = to_openai_response(output, model="m", request_id="r")
    choices = response["choices"]
    assert [choice["index"] for choice in choices] == [0, 1]
    assert [choice["message"]["content"] for choice in choices] == ["a", "b"]
    assert choices[1]["finish_reason"] == "length"


def test_response_with_tool_calls_keeps_content_filter():
    """SAFETY 不能被工具调用改写成 tool_calls（与主项目一致）。"""
    output = ModelOutput(
        candidates=[
            Candidate(
                finish_reason=3,
                function_calls=[{"name": "f", "args": {}}],
            )
        ]
    )
    choice = to_openai_response(output, model="m", request_id="r")["choices"][0]
    assert choice["finish_reason"] == "content_filter"


def test_response_usage_folds_reasoning_tokens():
    output = ModelOutput(
        candidates=[Candidate(text="ok")],
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 30,
            "total_tokens": 40,
            "cached_tokens": 4,
            "completion_tokens_details": {
                "reasoning_tokens": 8,
                "visible_tokens": 22,
            },
        },
    )
    usage = to_openai_response(output, model="m", request_id="r")["usage"]
    assert usage == {
        "prompt_tokens": 10,
        "completion_tokens": 30,
        "total_tokens": 40,
        "completion_tokens_details": {"reasoning_tokens": 8},
        "prompt_tokens_details": {"cached_tokens": 4},
    }


def test_convert_usage_handles_empty_and_nested_cached():
    assert convert_usage(None) is None
    assert convert_usage({}) is None
    assert convert_usage(
        {
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 2},
        }
    ) == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 2},
    }


def test_response_empty_candidates_is_stop_with_empty_content():
    response = to_openai_response(ModelOutput(), model="m", request_id="r")
    choice = response["choices"][0]
    assert choice["message"]["content"] is None
    assert choice["finish_reason"] == "stop"
    assert "usage" not in response


def test_response_id_and_object_shape():
    response = to_openai_response(
        ModelOutput(candidates=[Candidate(text="ok")]), model="gemini-3.8-flash", request_id="abc"
    )
    assert response["id"] == "chatcmpl-abc"
    assert response["object"] == "chat.completion"
    assert response["model"] == "gemini-3.8-flash"
    assert isinstance(response["created"], int)


# ────────────────────────────── 流式分帧 ──────────────────────────────


def _choices(chunk):
    return chunk["choices"]


def test_stream_converter_emits_role_then_text_then_finish():
    converter = OpenAIStreamConverter(model="m", request_id="rid")
    first = converter.convert_event("body", "Hel")
    more = converter.convert_event("body", "lo")
    converter.convert_event("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})
    converter.convert_event("finish_reason", 1)
    finish = converter.finish()

    assert first[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert first[1]["choices"][0]["delta"] == {"content": "Hel"}
    assert more[0]["choices"][0]["delta"] == {"content": "lo"}
    final = finish[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}
    # 未要求 include_usage 时 usage 挂在收尾帧上
    assert final["usage"] == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }


def test_stream_converter_reasoning_gated_by_include_thoughts():
    enabled = OpenAIStreamConverter(
        model="m", request_id="r", include_thoughts=True
    )
    frames = enabled.convert_event("thinking", "hmm")
    assert any(
        "reasoning_content" in frame["choices"][0]["delta"] for frame in frames
    )

    disabled = OpenAIStreamConverter(
        model="m", request_id="r", include_thoughts=False
    )
    assert disabled.convert_event("thinking", "hmm") == []
    body = disabled.convert_event("body", "visible")
    assert all("reasoning_content" not in frame["choices"][0]["delta"] for frame in body)


def test_stream_converter_tool_calls_are_emitted_once_with_cumulative_args():
    converter = OpenAIStreamConverter(model="m", request_id="rid")
    assert converter.convert_event("tool_calls", [{"name": "f", "args": {"a": 1}}]) == []
    assert (
        converter.convert_event(
            "tool_calls", [{"name": "f", "args": {"a": 1, "b": 2}}]
        )
        == []
    )
    converter.convert_event("thought_signature", "sig-9")
    converter.convert_event("finish_reason", 1)
    frames = converter.finish()

    tool_frames = [
        frame
        for frame in frames
        if frame["choices"] and "tool_calls" in frame["choices"][0]["delta"]
    ]
    assert len(tool_frames) == 1
    call = tool_frames[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "call_rid_0"
    assert call["function"]["name"] == "f"
    assert json.loads(call["function"]["arguments"]) == {"a": 1, "b": 2}
    assert call["extra_content"] == {"google": {"thought_signature": "sig-9"}}
    assert frames[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_converter_include_usage_emits_separate_usage_chunk():
    converter = OpenAIStreamConverter(
        model="m", request_id="r", include_usage=True
    )
    converter.convert_event("body", "hi")
    converter.convert_event(
        "usage", {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    )
    frames = converter.finish()
    final = frames[-2]
    usage_chunk = frames[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert "usage" not in final
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["total_tokens"] == 5


def test_stream_converter_respects_error_and_done_events():
    converter = OpenAIStreamConverter(model="m", request_id="r")
    assert converter.convert_event("done", None) == []
    assert converter.convert_event("prepared", object()) == []


def test_format_sse_and_done():
    assert format_sse({"a": 1}) == 'data: {"a": 1}\n\n'
    assert SSE_DONE == "data: [DONE]\n\n"


# ────────────────────────────── models / errors ──────────────────────────────


def test_to_openai_model_list_filters_and_strips_prefix():
    payload = to_openai_model_list(
        [
            {
                "id": "gemini-3.8-flash",
                "name": "models/gemini-3.8-flash",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "id": "embedding-001",
                "supportedGenerationMethods": ["embedContent"],
            },
            {"name": "models/gemini-4-argon", "supportedGenerationMethods": []},
        ]
    )
    assert payload["object"] == "list"
    ids = [item["id"] for item in payload["data"]]
    assert ids == ["gemini-3.8-flash", "gemini-4-argon"]
    assert all(item["owned_by"] == "google" for item in payload["data"])


def test_openai_error_payload_shape():
    error = openai_error_payload(RequestError(429, "quota exceeded"))
    assert error["error"]["code"] == 429
    assert error["error"]["type"] == "rate_limit_error"
    assert "quota exceeded" in error["error"]["message"]
    assert error["error"]["param"] is None

    server_error = openai_error_payload(RuntimeError("boom"))
    assert server_error["error"]["type"] == "server_error"
