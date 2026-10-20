"""OpenAI Chat Completions ↔ 内部 Gemini 请求的双向转换。

对端点是 `POST /v1/chat/completions`（非流式 + 流式 SSE）与 `GET /v1/models`。
上游调用不在这里重写：请求先用 :func:`openai_to_gemini_request` 变成与
Gemini 原生端口完全同形的 :class:`GeminiGenerateContentRequest`，再走
`application.api_service_gemini` 的同一套 normalize → capture → replay → parse
链路（含账号轮换/重试），响应才由本模块翻回 OpenAI 形状。

语义参考主项目 `C:\\build2api\\src\\convert.js`：
- 顶部 system 合并进 systemInstruction；中途 system 追加到上一条消息末尾
  （上一条是纯 functionResponse 的工具结果轮时另起一条 user 轮，避免被合并）；
- 相邻同角色消息合并，但「工具结果轮」与普通 user 轮不合并；
- tool_calls ↔ functionCall、tool 消息 ↔ functionResponse 双向映射；
- finish_reason / usage 从上游 finishReason / usageMetadata 解析并折算成 OpenAI 口径。
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Mapping

from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiFunctionCall,
    GeminiFunctionResponse,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
    GeminiSafetySetting,
    GeminiTool,
    OpenAIChatCompletionRequest,
    OpenAIChatMessage,
    OpenAIToolCall,
)
from aistudio_api.application.api_service_gemini import (
    classify_gemini_error_payload,
    to_gemini_finish_reason,
)
from aistudio_api.application.overrides import effort_to_level
from aistudio_api.domain.models import Candidate, ModelOutput

# Gemini finishReason（字符串名）→ OpenAI finish_reason
FINISH_REASON_MAP: dict[str, str | None] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "BLOCKLIST": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "LANGUAGE": "content_filter",
    "MALFORMED_FUNCTION_CALL": "stop",
    "UNEXPECTED_TOOL_CALL": "stop",
    "TOO_MANY_TOOL_CALLS": "stop",
    "OTHER": "stop",
    "IMAGE_OTHER": "stop",
    "NO_IMAGE": "stop",
    "IMAGE_RECITATION": "content_filter",
    "MISSING_THOUGHT_SIGNATURE": "stop",
    # 未指定不代表结束，不能当终止信号
    "FINISH_REASON_UNSPECIFIED": None,
}

_DATA_URI_RE = re.compile(r"^data:([^;,]+)(;base64)?,(.*)$", re.DOTALL)

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    409: "conflict_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "bad_gateway",
    503: "service_unavailable",
}


def map_finish_reason(reason: str | None) -> str | None:
    """Gemini finishReason 名 → OpenAI finish_reason（未知按 stop）。"""
    if not reason:
        return None
    mapped = FINISH_REASON_MAP.get(reason)
    return "stop" if mapped is None and reason not in FINISH_REASON_MAP else mapped


def openai_error_payload(exc: Exception) -> dict[str, object]:
    """把任意上游/本地异常折成 OpenAI 风格的 error 对象。"""
    code, message, _status = classify_gemini_error_payload(exc)
    if code >= 500:
        error_type = _ERROR_TYPES.get(code, "server_error")
    else:
        error_type = _ERROR_TYPES.get(code, "invalid_request_error")
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": code,
        }
    }


def format_sse(payload: object) -> str:
    """一帧 OpenAI SSE（`data: {...}\\n\\n`）。"""
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


SSE_DONE = "data: [DONE]\n\n"


# ────────────────────────────── 请求：OpenAI → Gemini ──────────────────────────────


def openai_to_gemini_request(
    payload: OpenAIChatCompletionRequest,
) -> GeminiGenerateContentRequest:
    """OpenAI chat.completions 请求 → 内部 Gemini 请求（含工具与思考意图）。"""
    if not payload.messages:
        raise ValueError("messages is required")
    system_texts, contents = _convert_messages(payload.messages)
    system_instruction = None
    if system_texts:
        system_instruction = GeminiContent(
            role="user",
            parts=[GeminiPart(text="\n\n".join(system_texts))],
        )
    generation_config = _build_generation_config(payload)
    tools = _convert_tools(payload.tools)
    tool_config = _convert_tool_choice(payload.tool_choice)
    safety_settings = _convert_safety_settings(payload.safety_settings)
    return GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=system_instruction,
        generationConfig=generation_config,
        tools=tools,
        toolConfig=tool_config,
        safetySettings=safety_settings,
    )


def _convert_messages(
    messages: list[OpenAIChatMessage],
) -> tuple[list[str], list[GeminiContent]]:
    system_texts: list[str] = []
    contents: list[GeminiContent] = []
    leading_system = True
    id_to_name: dict[str, str] = {}

    for message in messages:
        role = str(getattr(message, "role", "") or "").strip().lower()
        if role in ("system", "developer"):
            text = _extract_text(message.content)
            if not text:
                continue
            if leading_system:
                system_texts.append(text)
            else:
                _append_mid_system(contents, text)
            continue

        leading_system = False

        if role in ("tool", "function"):
            contents.append(
                GeminiContent(
                    role="user",
                    parts=[_tool_result_to_part(message, id_to_name)],
                )
            )
            continue

        if role == "assistant":
            parts = _content_to_parts(message.content)
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if tool_calls:
                parts.extend(_tool_calls_to_parts(tool_calls, id_to_name))
            _attach_turn_signature(parts, message, tool_calls)
            if parts:
                contents.append(GeminiContent(role="model", parts=parts))
            continue

        # user 及其它未知角色一律当 user
        parts = _content_to_parts(message.content)
        if parts:
            contents.append(GeminiContent(role="user", parts=parts))

    return system_texts, _merge_consecutive(contents)


def _append_mid_system(contents: list[GeminiContent], text: str) -> None:
    """中途 system：追加到上一条消息末尾的 text part；FR 轮另起一条 user。"""
    if not contents or _is_tool_result_turn(contents[-1]):
        contents.append(GeminiContent(role="user", parts=[GeminiPart(text=text)]))
        return
    last = contents[-1]
    for part in reversed(last.parts):
        if isinstance(part.text, str):
            part.text = f"{part.text}\n{text}"
            return
    last.parts.append(GeminiPart(text=text))


def _is_tool_result_turn(content: GeminiContent) -> bool:
    return bool(content.parts) and all(
        part.functionResponse is not None for part in content.parts
    )


def _merge_consecutive(contents: list[GeminiContent]) -> list[GeminiContent]:
    """合并相邻同角色消息（工具结果轮与普通轮不合并，保持 FR 轮独立）。"""
    merged: list[GeminiContent] = []
    for content in contents:
        last = merged[-1] if merged else None
        same_kind = (
            last is not None and _is_tool_result_turn(last) == _is_tool_result_turn(content)
        )
        if last is not None and last.role == content.role and same_kind:
            last.parts.extend(content.parts)
        else:
            merged.append(GeminiContent(role=content.role, parts=list(content.parts)))
    return [content for content in merged if content.parts]


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type in (None, "text") and isinstance(item.get("text"), str):
                texts.append(str(item["text"]))
        return "\n".join(texts)
    return ""


def _content_to_parts(content: object) -> list[GeminiPart]:
    if isinstance(content, str):
        return [GeminiPart(text=content)] if content else []
    if not isinstance(content, list):
        return []
    parts: list[GeminiPart] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type in (None, "text") and isinstance(item.get("text"), str):
            if item["text"]:
                parts.append(GeminiPart(text=str(item["text"])))
            continue
        if item_type == "image_url":
            raw_url = item.get("image_url")
            url = ""
            if isinstance(raw_url, Mapping):
                url = str(raw_url.get("url") or "")
            elif isinstance(raw_url, str):
                url = raw_url
            inline = _data_url_to_inline(url)
            if inline is not None:
                parts.append(GeminiPart(inlineData=inline))
            continue
        if item_type == "input_audio":
            audio = item.get("input_audio")
            if isinstance(audio, Mapping) and isinstance(audio.get("data"), str):
                fmt = str(audio.get("format") or "wav").strip().lower()
                mime = "audio/mp3" if fmt == "mp3" else f"audio/{fmt}"
                parts.append(
                    GeminiPart(
                        inlineData=GeminiInlineData(
                            mimeType=mime, data=str(audio["data"])
                        )
                    )
                )
            continue
        if item_type == "file":
            file_obj = item.get("file")
            if isinstance(file_obj, Mapping) and isinstance(
                file_obj.get("file_data"), str
            ):
                inline = _data_url_to_inline(str(file_obj["file_data"]))
                if inline is not None:
                    parts.append(GeminiPart(inlineData=inline))
    return parts


def _data_url_to_inline(url: str) -> GeminiInlineData | None:
    if not url.startswith("data:"):
        return None
    match = _DATA_URI_RE.match(url)
    if not match:
        return None
    mime_type, is_base64, payload = match.group(1), match.group(2), match.group(3)
    if not is_base64:
        try:
            from urllib.parse import unquote_to_bytes

            payload = base64.b64encode(
                unquote_to_bytes(payload)
            ).decode("ascii")
        except Exception:
            return None
    return GeminiInlineData(mimeType=mime_type, data=payload)


def _parse_tool_arguments(raw: object) -> object:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        # 模型偶尔吐出非法 JSON，原样塞进一个字段总比整条请求失败好
        return {"_raw": str(raw)}
    if isinstance(parsed, (dict, list)):
        return parsed
    return {"value": parsed}


def _tool_calls_to_parts(
    tool_calls: list[OpenAIToolCall], id_to_name: dict[str, str]
) -> list[GeminiPart]:
    parts: list[GeminiPart] = []
    for call in tool_calls:
        function = getattr(call, "function", None)
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        call_id = getattr(call, "id", None)
        if isinstance(call_id, str) and call_id:
            id_to_name[call_id] = name
        args = _parse_tool_arguments(getattr(function, "arguments", None))
        parts.append(
            GeminiPart(
                functionCall=GeminiFunctionCall(
                    name=name,
                    args=args,
                    id=call_id if isinstance(call_id, str) and call_id else None,
                ),
                thoughtSignature=_signature_from_tool_call(call),
            )
        )
    return parts


def _signature_from_extra(extra: object) -> str | None:
    if not isinstance(extra, Mapping):
        return None
    google = extra.get("google")
    if isinstance(google, Mapping):
        signature = google.get("thought_signature")
        if isinstance(signature, str) and signature:
            return signature
    signature = extra.get("thought_signature")
    return signature if isinstance(signature, str) and signature else None


def _signature_from_tool_call(call: OpenAIToolCall) -> str | None:
    signature = _signature_from_extra(getattr(call, "extra_content", None))
    if signature:
        return signature
    for attr in ("thought_signature", "thoughtSignature"):
        value = getattr(call, attr, None)
        if isinstance(value, str) and value:
            return value
    return None


def _attach_turn_signature(
    parts: list[GeminiPart],
    message: OpenAIChatMessage,
    tool_calls: list[OpenAIToolCall],
) -> None:
    """把上一轮的思考签名挂回本轮第一个 functionCall（模型没调工具则挂首个 part）。"""
    if not parts:
        return
    signature = None
    if tool_calls:
        signature = _signature_from_tool_call(tool_calls[0])
    if not signature:
        signature = _signature_from_extra(getattr(message, "extra_content", None))
    if not signature:
        return
    target = next(
        (index for index, part in enumerate(parts) if part.functionCall is not None),
        0,
    )
    parts[target].thoughtSignature = signature


def _tool_result_to_part(
    message: OpenAIChatMessage, id_to_name: dict[str, str]
) -> GeminiPart:
    name = str(getattr(message, "name", "") or "")
    if not name:
        tool_call_id = getattr(message, "tool_call_id", None)
        name = id_to_name.get(tool_call_id, "") if isinstance(tool_call_id, str) else ""
    if not name:
        name = "unknown_function"
    text = _extract_text(message.content)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        response: object = parsed
    elif parsed is not None:
        response = {"result": parsed}
    else:
        response = {"result": text}
    return GeminiPart(
        functionResponse=GeminiFunctionResponse(name=name, response=response)
    )


def _build_generation_config(
    payload: OpenAIChatCompletionRequest,
) -> GeminiGenerationConfig | None:
    values: dict[str, object] = {}
    if payload.temperature is not None:
        values["temperature"] = float(payload.temperature)
    if payload.top_p is not None:
        values["topP"] = float(payload.top_p)
    if payload.top_k is not None:
        values["topK"] = int(payload.top_k)
    if payload.presence_penalty is not None:
        values["presencePenalty"] = float(payload.presence_penalty)
    if payload.frequency_penalty is not None:
        values["frequencyPenalty"] = float(payload.frequency_penalty)
    if payload.seed is not None:
        values["seed"] = int(payload.seed)

    max_tokens = (
        payload.max_completion_tokens
        if payload.max_completion_tokens is not None
        else payload.max_tokens
    )
    if max_tokens is not None:
        try:
            tokens = int(max_tokens)
        except (TypeError, ValueError):
            tokens = 0
        if tokens >= 1:
            values["maxOutputTokens"] = tokens

    if payload.stop is not None:
        stops = [payload.stop] if isinstance(payload.stop, str) else list(payload.stop)
        stops = [str(item) for item in stops if str(item)]
        if stops:
            values["stopSequences"] = stops

    if payload.n is not None:
        try:
            candidate_count = int(payload.n)
        except (TypeError, ValueError):
            candidate_count = 0
        if candidate_count > 1:
            values["candidateCount"] = candidate_count

    response_format = payload.response_format
    if isinstance(response_format, Mapping):
        fmt_type = response_format.get("type")
        if fmt_type == "json_object":
            values["responseMimeType"] = "application/json"
        elif fmt_type == "json_schema":
            json_schema = response_format.get("json_schema")
            if isinstance(json_schema, Mapping):
                values["responseMimeType"] = "application/json"
                schema = json_schema.get("schema")
                if isinstance(schema, Mapping):
                    values["responseSchema"] = dict(schema)

    thinking_config = _thinking_config(payload)
    if thinking_config:
        values["thinkingConfig"] = thinking_config

    if not values:
        return None
    return GeminiGenerationConfig(**values)  # type: ignore[arg-type]


def _thinking_config(payload: OpenAIChatCompletionRequest) -> dict[str, object] | None:
    thinking: dict[str, object] = {}
    effort = payload.reasoning_effort
    level = None
    if isinstance(payload.thinking, Mapping):
        effort = effort or payload.thinking.get("effort")
        raw_level = payload.thinking.get("thinkingLevel") or payload.thinking.get("level")
        if raw_level:
            level = str(raw_level).strip().lower()
        include = payload.thinking.get("includeThoughts")
        if isinstance(include, bool):
            thinking["includeThoughts"] = include
    if level is None:
        level = effort_to_level(effort if isinstance(effort, str) else None)
    if level:
        thinking["thinkingLevel"] = level
    return thinking or None


def _normalize_parameters(parameters: object) -> dict[str, object] | None:
    if not isinstance(parameters, Mapping):
        return None
    params = dict(parameters)
    declared_type = str(params.get("type") or "").strip().lower()
    if declared_type == "object":
        return params
    if not declared_type and "properties" in params:
        params["type"] = "object"
        return params
    # Gemini 要求有 parameters 时必须是 OBJECT；客户端常只给 properties/required
    return {"type": "object", "properties": {}}


def _convert_tools(tools: object) -> list[GeminiTool] | None:
    if not isinstance(tools, list) or not tools:
        return None
    declarations: list[dict[str, object]] = []
    for tool in tools:
        tool_type = str(getattr(tool, "type", "") or "function")
        if tool_type and tool_type != "function":
            continue
        function = getattr(tool, "function", None)
        if function is None:
            continue
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        declaration: dict[str, object] = {"name": name}
        description = getattr(function, "description", None)
        if isinstance(description, str) and description:
            declaration["description"] = description
        parameters = _normalize_parameters(getattr(function, "parameters", None))
        if parameters is not None:
            declaration["parameters"] = parameters
        declarations.append(declaration)
    if not declarations:
        return None
    return [GeminiTool(functionDeclarations=declarations)]


def _convert_tool_choice(tool_choice: object) -> dict[str, object] | None:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        mode = {
            "none": "NONE",
            "auto": "AUTO",
            "required": "ANY",
            "any": "ANY",
        }.get(tool_choice.strip().lower())
        if mode is None:
            return None
        return {"functionCallingConfig": {"mode": mode}}
    if isinstance(tool_choice, Mapping):
        function = tool_choice.get("function")
        if isinstance(function, Mapping) and function.get("name"):
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [str(function["name"])],
                }
            }
    return None


def _convert_safety_settings(
    settings: object,
) -> list[GeminiSafetySetting] | None:
    if not isinstance(settings, list) or not settings:
        return None
    converted: list[GeminiSafetySetting] = []
    for item in settings:
        if not isinstance(item, Mapping):
            continue
        category = item.get("category")
        threshold = item.get("threshold")
        if not isinstance(category, str) or not isinstance(threshold, str):
            continue
        converted.append(GeminiSafetySetting(category=category, threshold=threshold))
    return converted or None


# ────────────────────────────── 响应：Gemini → OpenAI ──────────────────────────────


def convert_usage(usage: Mapping[str, object] | None) -> dict[str, object] | None:
    """内部 usage（OpenAI 口径）→ OpenAI 响应里的 usage。"""
    if not usage:
        return None
    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    total = _as_int(usage.get("total_tokens")) or (prompt + completion)
    result: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    completion_details = usage.get("completion_tokens_details")
    reasoning = 0
    visible = 0
    if isinstance(completion_details, Mapping):
        reasoning = _as_int(completion_details.get("reasoning_tokens"))
        visible = _as_int(completion_details.get("visible_tokens"))
    if reasoning > 0:
        result["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    if visible > 0 and completion == 0:
        result["completion_tokens"] = visible
    cached = _as_int(usage.get("cached_tokens"))
    prompt_details = usage.get("prompt_tokens_details")
    if cached <= 0 and isinstance(prompt_details, Mapping):
        cached = _as_int(prompt_details.get("cached_tokens"))
    if cached > 0:
        result["prompt_tokens_details"] = {"cached_tokens": cached}
    return result


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _function_call_args(function_call: Mapping[str, object]) -> object:
    args = function_call.get("args")
    if args is not None:
        return args
    arguments = function_call.get("arguments")
    if arguments is not None:
        if isinstance(arguments, str):
            try:
                return json.loads(arguments)
            except (TypeError, ValueError):
                return {"_raw": arguments}
        return arguments
    raw = function_call.get("raw")
    if isinstance(raw, list) and len(raw) > 1:
        return raw[1]
    return {}


def _function_call_name(function_call: Mapping[str, object]) -> str:
    return str(function_call.get("name") or "unknown_function")


def _function_call_signature(function_call: Mapping[str, object]) -> str | None:
    for key in ("thought_signature", "thoughtSignature"):
        value = function_call.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _signature_extra(signature: str) -> dict[str, object]:
    return {"google": {"thought_signature": signature}}


def _tool_calls_to_openai(
    function_calls: list[dict[str, object]],
    *,
    request_id: str,
    candidate_index: int = 0,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for position, function_call in enumerate(function_calls):
        if not isinstance(function_call, Mapping):
            continue
        call_id = function_call.get("call_id") or function_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_{request_id}_{candidate_index}_{position}"
        args = _function_call_args(function_call)
        entry: dict[str, object] = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": _function_call_name(function_call),
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        }
        signature = _function_call_signature(function_call)
        if signature and position == 0:
            entry["extra_content"] = _signature_extra(signature)
        calls.append(entry)
    return calls


def _images_markdown(candidate: Candidate | None) -> str:
    if candidate is None:
        return ""
    chunks: list[str] = []
    for image in candidate.images:
        encoded = base64.b64encode(image.data).decode("ascii")
        chunks.append(f"\n![image](data:{image.mime};base64,{encoded})\n")
    if candidate.code_output:
        chunks.append(f"\n```\n{candidate.code_output}\n```\n")
    return "".join(chunks)


def _candidate_finish_reason(
    candidate: Candidate | None, *, has_tool_calls: bool
) -> str:
    wire_code = candidate.finish_reason if candidate is not None else None
    reason = map_finish_reason(to_gemini_finish_reason(wire_code)) or "stop"
    if has_tool_calls and reason == "stop":
        return "tool_calls"
    return reason


def _candidate_to_choice(
    candidate: Candidate | None,
    *,
    index: int,
    request_id: str,
    include_thoughts: bool,
) -> dict[str, object]:
    text = (candidate.text if candidate is not None else "") or ""
    text += _images_markdown(candidate)
    message: dict[str, object] = {"role": "assistant", "content": text or None}
    if include_thoughts and candidate is not None and candidate.thinking:
        message["reasoning_content"] = candidate.thinking
    tool_calls: list[dict[str, object]] = []
    if candidate is not None:
        tool_calls = _tool_calls_to_openai(
            list(candidate.function_calls),
            request_id=request_id,
            candidate_index=index,
        )
    if tool_calls:
        message["tool_calls"] = tool_calls
    elif candidate is not None and candidate.thought_signature:
        message["extra_content"] = _signature_extra(candidate.thought_signature)
    return {
        "index": index,
        "message": message,
        "finish_reason": _candidate_finish_reason(
            candidate, has_tool_calls=bool(tool_calls)
        ),
    }


def to_openai_response(
    output: ModelOutput,
    *,
    model: str,
    request_id: str,
    include_thoughts: bool = True,
) -> dict[str, object]:
    """内部 ModelOutput → OpenAI chat.completion 响应对象（n>1 时逐 candidate 成 choice）。"""
    created = int(time.time())
    candidates: list[Candidate | None] = list(output.candidates) or [None]
    choices = [
        _candidate_to_choice(
            candidate,
            index=index,
            request_id=request_id,
            include_thoughts=include_thoughts,
        )
        for index, candidate in enumerate(candidates)
    ]
    response: dict[str, object] = {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": choices,
    }
    usage = convert_usage(output.usage)
    if usage:
        response["usage"] = usage
    return response


def to_openai_model_list(models: object) -> dict[str, object]:
    """现有 `/v1beta/models` 的模型数据 → OpenAI /v1/models 形状。"""
    created = int(time.time())
    data: list[dict[str, object]] = []
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, Mapping):
                continue
            methods = item.get("supportedGenerationMethods")
            if (
                isinstance(methods, list)
                and methods
                and not any("generateContent" in str(m) for m in methods)
            ):
                continue
            raw_id = str(item.get("id") or item.get("name") or "")
            model_id = raw_id.removeprefix("models/")
            if not model_id:
                continue
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": created,
                    "owned_by": "google",
                }
            )
    return {"object": "list", "data": data}


# ────────────────────────────── 流式：Gemini 事件 → OpenAI chunk ──────────────────────────────


class OpenAIStreamConverter:
    """把内部流式事件（body/thinking/tool_calls/usage/finish_reason…）翻成 OpenAI chunk。

    有状态：只发一次 role、末尾补 finish_reason 与 usage。工具调用在收尾时一次性发出
    （AI Studio 流里的 functionCall 参数是「累积快照」——每帧是到目前为止的完整值，
    逐帧转发会把同一段 JSON 重复给客户端；OpenAI 客户端本来也要等到 finish 才执行工具）。
    思考签名如果同一条上游 chunk 里先于/晚于 tool_calls 到达，都会在收尾时挂到第一个调用上。
    """

    def __init__(
        self,
        *,
        model: str,
        request_id: str,
        include_thoughts: bool = True,
        include_usage: bool = False,
    ) -> None:
        self.model = model
        self.request_id = request_id
        self.include_thoughts = include_thoughts
        self.include_usage = include_usage
        self.role_sent = False
        self.finish_reason: str | None = None
        self.usage: dict[str, object] | None = None
        self.response_id: str | None = None
        self.turn_signature: str | None = None
        self.tool_calls: list[dict[str, object]] = []
        # 一条调用的「身份」→ tool_calls 下标。不能直接用 payload 里的位置：并行调用
        # 分在不同帧里时每帧都从 0 开始，会把第二条调用并进第一条（参数被覆盖、丢调用）。
        self._call_id_slots: dict[str, int] = {}
        # 函数名 → 该名字名下的槽位（按创建顺序）；同一帧里的同名并行调用靠序号区分
        self._name_slots: dict[str, list[int]] = {}
        self.saw_tool_call = False

    # ---------- 帧构造 ----------
    def _envelope(
        self, delta: dict[str, object], finish_reason: str | None = None
    ) -> dict[str, object]:
        return {
            "id": f"chatcmpl-{self.request_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.model,
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason}
            ],
        }

    def _ensure_role(self, out: list[dict[str, object]]) -> None:
        if self.role_sent:
            return
        out.append(self._envelope({"role": "assistant", "content": ""}))
        self.role_sent = True

    # ---------- 事件 ----------
    def convert_event(self, event_type: str, payload: object) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        if event_type == "usage":
            if isinstance(payload, Mapping):
                self.usage = convert_usage(payload)
            return out
        if event_type == "finish_reason":
            name = (
                to_gemini_finish_reason(payload)
                if isinstance(payload, int) and not isinstance(payload, bool)
                else str(payload)
            )
            mapped = map_finish_reason(name)
            if mapped:
                self.finish_reason = mapped
            return out
        if event_type == "response_id":
            self.response_id = str(payload)
            return out
        if event_type == "thought_signature":
            if isinstance(payload, str) and payload:
                self.turn_signature = payload
            return out
        if event_type in ("done", "prepared"):
            return out

        if event_type == "thinking":
            if not self.include_thoughts:
                return out
            text = str(payload)
            if not text:
                return out
            self._ensure_role(out)
            out.append(self._envelope({"reasoning_content": text}))
            return out

        if event_type == "body":
            text = str(payload)
            if not text:
                return out
            self._ensure_role(out)
            out.append(self._envelope({"content": text}))
            return out

        if event_type in ("images", "reasoning_images"):
            if event_type == "reasoning_images" and not self.include_thoughts:
                return out
            markdown = _stream_images_markdown(payload)
            if not markdown:
                return out
            self._ensure_role(out)
            out.append(self._envelope({"content": markdown}))
            return out

        if event_type == "tool_calls":
            self._absorb_tool_calls(payload)
            return out
        return out

    def _absorb_tool_calls(self, payload: object) -> None:
        if not isinstance(payload, list):
            return
        occurrences: dict[str, int] = {}
        for function_call in payload:
            if not isinstance(function_call, Mapping):
                continue
            name = _function_call_name(function_call)
            occurrence = occurrences.get(name, 0)
            occurrences[name] = occurrence + 1
            args_json = json.dumps(
                _function_call_args(function_call), ensure_ascii=False
            )
            signature = _function_call_signature(function_call)
            slot = self._call_slot(function_call, name, occurrence)
            while len(self.tool_calls) <= slot:
                self.tool_calls.append(
                    {
                        "id": None,
                        "name": "",
                        "arguments": "",
                        "emitted": False,
                        "signature": None,
                    }
                )
            entry = self.tool_calls[slot]
            if not entry["name"]:
                entry["name"] = name
            # 上游流里参数是累积快照：变长且前缀相同才算增长，否则整体替换
            previous = str(entry["arguments"])
            if args_json.startswith(previous):
                entry["arguments"] = args_json
            elif previous.startswith(args_json):
                pass
            else:
                entry["arguments"] = args_json
            if signature and not entry["signature"]:
                entry["signature"] = signature
            self.saw_tool_call = True

    def _call_slot(
        self, function_call: Mapping[str, object], name: str, occurrence: int
    ) -> int:
        """给一条调用找它固定的 tool_calls 下标（累积快照要落到同一个坑里）。

        优先用上游给的 call_id；没有就按函数名找：
        - 同名只有一个槽位时直接复用（分帧到达的累积快照都落在这里）；
        - 同一帧里出现多个同名调用时用帧内序号区分（并行调用不会被并成一条）。
        不能直接用帧内位置：并行调用分在不同帧里时每帧都从 0 开始，位置会把
        第二条调用并进第一条（参数被覆盖、丢调用）。
        """
        call_id = function_call.get("call_id") or function_call.get("id")
        if isinstance(call_id, str) and call_id:
            known = self._call_id_slots.get(call_id)
            if known is not None:
                return known

        same_name = self._name_slots.setdefault(name, [])
        if occurrence < len(same_name):
            slot = same_name[occurrence]
        else:
            slot = len(self.tool_calls)
            same_name.append(slot)
        if isinstance(call_id, str) and call_id:
            self._call_id_slots[call_id] = slot
        return slot

    def _flush_tool_calls(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for index, entry in enumerate(self.tool_calls):
            arguments = str(entry["arguments"])
            if entry["emitted"]:
                if arguments and arguments != entry.get("sent_arguments"):
                    out.append(
                        self._envelope(
                            {
                                "tool_calls": [
                                    {
                                        "index": index,
                                        "id": entry["id"],
                                        "type": "function",
                                        "function": {"arguments": arguments},
                                    }
                                ]
                            }
                        )
                    )
                    entry["sent_arguments"] = arguments
                continue
            if not entry["id"]:
                entry["id"] = f"call_{self.request_id}_{index}"
            call: dict[str, object] = {
                "index": index,
                "id": entry["id"],
                "type": "function",
                "function": {
                    "name": entry["name"],
                    "arguments": arguments,
                },
            }
            signature = entry.get("signature") or (
                self.turn_signature if index == 0 else None
            )
            if signature:
                call["extra_content"] = _signature_extra(str(signature))
            out.append(self._envelope({"tool_calls": [call]}))
            entry["emitted"] = True
            entry["sent_arguments"] = arguments
        return out

    def finish(self) -> list[dict[str, object]]:
        """收尾：补 role、工具调用、finish_reason、usage（可选单独一帧）。"""
        out: list[dict[str, object]] = []
        self._ensure_role(out)
        out.extend(self._flush_tool_calls())
        reason = self.finish_reason
        if reason is None:
            reason = "tool_calls" if self.saw_tool_call else "stop"
        elif reason == "stop" and self.saw_tool_call:
            reason = "tool_calls"
        final_chunk = self._envelope({}, reason)
        if self.usage and not self.include_usage:
            final_chunk["usage"] = self.usage
        out.append(final_chunk)
        if self.include_usage and self.usage:
            out.append(
                {
                    "id": f"chatcmpl-{self.request_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model,
                    "choices": [],
                    "usage": self.usage,
                }
            )
        return out

    @property
    def total_usage(self) -> dict[str, object] | None:
        return self.usage


def _stream_images_markdown(payload: object) -> str:
    if not isinstance(payload, list):
        return ""
    chunks: list[str] = []
    for image in payload:
        mime = getattr(image, "mime", None)
        data = getattr(image, "data", None)
        if isinstance(mime, str) and isinstance(data, (bytes, bytearray)):
            encoded = base64.b64encode(bytes(data)).decode("ascii")
            chunks.append(f"\n![image](data:{mime};base64,{encoded})\n")
    return "".join(chunks)


__all__ = [
    "FINISH_REASON_MAP",
    "SSE_DONE",
    "OpenAIStreamConverter",
    "convert_usage",
    "format_sse",
    "map_finish_reason",
    "openai_error_payload",
    "openai_to_gemini_request",
    "to_openai_model_list",
    "to_openai_response",
]
