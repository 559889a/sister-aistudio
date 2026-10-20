"""新上游通道（Vertex Express / Console batchGraphql）的共享转换层。

两条通道都吃**官方 Gemini JSON 形状**，而我们两条端口汇聚后的调用面传的是
wire 侧结构（AistudioContent / wire 工具 / wire 安全设置）——这里做方向性转换：
wire → 官方（请求侧）、官方 → wire 语义（响应侧，产出 ModelOutput / 事件流，
与 AI Studio 浏览器通道同一套词汇表，下游零改动）。

来源：bad-woman/vertex2openai（2026-10-20 快照）+ 我们自己的 chat_service 编码表。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING

from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import Candidate, GeneratedImage, ModelOutput
from aistudio_api.infrastructure.gateway.wire_spec import SchemaIndex
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.infrastructure.gateway.wire_types import (
        AistudioContent,
        AistudioPart,
    )

logger = get_logger("upstreams.common")

# 与 chat_service.SCHEMA_TYPE_CODES 互逆（wire 下标 0 的类型码 → 官方 type 名）
_WIRE_TYPE_NAMES = {1: "STRING", 2: "NUMBER", 3: "INTEGER", 4: "BOOLEAN", 5: "ARRAY", 6: "OBJECT"}

_SAFETY_CATEGORY_NAMES = {
    7: "HARM_CATEGORY_HARASSMENT",
    8: "HARM_CATEGORY_HATE_SPEECH",
    9: "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    10: "HARM_CATEGORY_DANGEROUS_CONTENT",
}
_SAFETY_THRESHOLD_NAMES = {
    1: "BLOCK_LOW_AND_ABOVE",
    2: "BLOCK_MEDIUM_AND_ABOVE",
    3: "BLOCK_ONLY_HIGH",
    4: "BLOCK_NONE",
    5: "OFF",
}

# 官方 finishReason 名 → wire 码（api_service_gemini 的映射表取逆）
_FINISH_NAME_TO_WIRE: dict[str, int] = {}


def _finish_map() -> dict[str, int]:
    if not _FINISH_NAME_TO_WIRE:
        from aistudio_api.application.api_service_gemini import (
            WIRE_TO_GEMINI_FINISH_REASON,
        )

        for code, name in WIRE_TO_GEMINI_FINISH_REASON.items():
            _FINISH_NAME_TO_WIRE.setdefault(name, code)
    return _FINISH_NAME_TO_WIRE


def finish_name_to_wire(name: str | None) -> int | None:
    if not name or name.endswith("UNSPECIFIED"):
        return None
    return _finish_map().get(str(name).upper())


def part_to_official(part: AistudioPart) -> dict[str, object]:
    """AistudioPart → 官方 Part dict（text/inlineData/functionCall/...）。"""
    out: dict[str, object] = {}
    if part.file_id:
        out["fileData"] = {"fileUri": part.file_id}
    elif part.inline_data:
        mime, b64 = part.inline_data
        out["inlineData"] = {"mimeType": mime, "data": b64}
    elif part.function_call:
        fc = {"name": part.function_call[0], "args": part.function_call[1] or {}}
        if len(part.function_call) > 2 and part.function_call[2]:
            fc["id"] = part.function_call[2]
        out["functionCall"] = fc
    elif part.function_response:
        name, payload = part.function_response[0], part.function_response[1]
        out["functionResponse"] = {"name": name, "response": payload or {}}
    elif part.executable_code:
        out["executableCode"] = {"code": part.executable_code[1]}
    elif part.code_execution_result:
        out["codeExecutionResult"] = {"outcome": "OK", "output": part.code_execution_result[1]}
    else:
        out["text"] = part.text or ""
    if part.thought:
        out["thought"] = True
    if part.thought_signature:
        out["thoughtSignature"] = part.thought_signature
    return out


def contents_to_official(
    contents: Iterable[AistudioContent] | None,
) -> list[dict[str, object]]:
    if not contents:
        return []
    return [
        {
            "role": content.role or "user",
            "parts": [part_to_official(p) for p in content.parts],
        }
        for content in contents
    ]


def wire_schema_to_official(wire: object) -> dict[str, object]:
    """encode_schema_to_wire 的逆：JSPB schema 数组 → 官方 JSON Schema dict。"""
    if not isinstance(wire, list) or not wire:
        return {"type": "OBJECT"}

    def _get(arr: list, idx: int) -> object:
        return arr[idx] if len(arr) > idx else None

    type_code = _get(wire, SchemaIndex.TYPE)
    schema: dict[str, object] = {
        "type": _WIRE_TYPE_NAMES.get(int(type_code), "OBJECT") if isinstance(type_code, int) else "OBJECT"
    }
    fmt = _get(wire, SchemaIndex.FORMAT)
    if isinstance(fmt, str) and fmt:
        schema["format"] = fmt
    desc = _get(wire, SchemaIndex.DESCRIPTION)
    if isinstance(desc, str) and desc:
        schema["description"] = desc
    nullable = _get(wire, SchemaIndex.NULLABLE)
    if nullable is True:
        schema["nullable"] = True
    enum_vals = _get(wire, SchemaIndex.ENUM)
    if isinstance(enum_vals, list) and enum_vals:
        schema["enum"] = [str(x) for x in enum_vals]
    items = _get(wire, SchemaIndex.ITEMS)
    if isinstance(items, list) and items:
        schema["items"] = wire_schema_to_official(items)
    props = _get(wire, SchemaIndex.PROPERTIES)
    if isinstance(props, list) and props:
        schema["properties"] = {
            str(p[0]): wire_schema_to_official(p[1])
            for p in props
            if isinstance(p, list) and len(p) >= 2 and isinstance(p[1], list)
        }
    required = _get(wire, SchemaIndex.REQUIRED)
    if isinstance(required, list) and required:
        schema["required"] = [str(x) for x in required]
    return schema


def wire_tools_to_official(tools: Iterable[Iterable] | None) -> list[dict[str, object]]:
    """wire 工具声明（[[name, desc?, params_wire], ...] 一组一 declaration）→ 官方 tools。"""
    if not tools:
        return []
    declarations: list[dict[str, object]] = []
    for tool in tools:
        if not isinstance(tool, list):
            continue
        for decl in tool:
            if not isinstance(decl, list) or not decl or not isinstance(decl[0], str):
                continue
            official: dict[str, object] = {"name": decl[0]}
            if len(decl) > 1 and isinstance(decl[1], str) and decl[1]:
                official["description"] = decl[1]
            if len(decl) > 2 and isinstance(decl[2], list) and decl[2]:
                official["parameters"] = wire_schema_to_official(decl[2])
            declarations.append(official)
    return [{"functionDeclarations": declarations}] if declarations else []


def wire_safety_to_official(
    safety_settings: Iterable[Iterable] | None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in safety_settings or []:
        if not isinstance(entry, list) or len(entry) < 4:
            continue
        category = entry[2] if isinstance(entry[2], int) else None
        threshold = entry[3] if isinstance(entry[3], int) else None
        if category not in _SAFETY_CATEGORY_NAMES or threshold not in _SAFETY_THRESHOLD_NAMES:
            continue
        category_name = _SAFETY_CATEGORY_NAMES[category]
        threshold_name = _SAFETY_THRESHOLD_NAMES[threshold]
        out.append({"category": category_name, "threshold": threshold_name})
    return out


def usage_to_ours(usage: dict | None) -> dict[str, object]:
    usage = usage or {}
    prompt = int(usage.get("promptTokenCount", 0) or 0)
    completion = int(usage.get("candidatesTokenCount", 0) or 0)
    total = int(usage.get("totalTokenCount", 0) or (prompt + completion))
    thought = usage.get("thoughtsTokenCount")
    out: dict[str, object] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    if thought is not None:
        out["thoughts_token_count"] = int(thought or 0)
    return out


def official_chunk_events(data: dict) -> list[tuple[str, object]]:
    """官方 GenerateContentResponse dict → 我们的事件词汇表（流式用）。

    词汇与 client.stream_generate_content（AI Studio 通道）一致：
    ("body", str) / ("thinking", str) / ("tool_calls", list) / ("images", list) /
    ("thought_signature", str) / ("finish_reason", int) / ("usage", dict)。
    """
    events: list[tuple[str, object]] = []
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                events.append(("thinking" if part.get("thought") else "body", text))
            fc = part.get("functionCall")
            if isinstance(fc, dict) and fc.get("name"):
                events.append(("tool_calls", [dict(fc)]))
            inline = part.get("inlineData")
            if isinstance(inline, dict) and inline.get("data"):
                b64 = str(inline["data"])
                events.append(
                    (
                        "images",
                        [
                            GeneratedImage(
                                mime=str(inline.get("mimeType") or "image/png"),
                                data=b64.encode("utf-8"),
                                size=len(b64),
                                thought_signature=str(part.get("thoughtSignature") or ""),
                            )
                        ],
                    )
                )
            if part.get("thoughtSignature"):
                events.append(("thought_signature", str(part["thoughtSignature"])))
        wire_finish = finish_name_to_wire(candidate.get("finishReason"))
        if wire_finish is not None:
            events.append(("finish_reason", wire_finish))
    if isinstance(data.get("usageMetadata"), dict) and data["usageMetadata"]:
        events.append(("usage", usage_to_ours(data["usageMetadata"])))
    return events


def official_to_model_output(data: dict, model: str) -> ModelOutput:
    """官方响应 dict → ModelOutput（非流式聚合；与 AI Studio 通道同构）。"""
    candidate = Candidate()
    texts: list[str] = []
    thoughts: list[str] = []
    for event, payload in official_chunk_events(data):
        if event == "body":
            texts.append(str(payload))
        elif event == "thinking":
            thoughts.append(str(payload))
        elif event == "tool_calls":
            candidate.function_calls.extend(payload)  # type: ignore[arg-type]
        elif event == "images":
            candidate.images.extend(payload)  # type: ignore[arg-type]
        elif event == "thought_signature":
            candidate.thought_signature = str(payload)
        elif event == "finish_reason" and isinstance(payload, int):
            candidate.finish_reason = payload
    candidate.text = "".join(texts)
    candidate.thinking = "".join(thoughts)
    usage = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
    return ModelOutput(
        candidates=[candidate],
        model=model,
        response_id=str(data.get("responseId") or ""),
        usage=usage_to_ours(usage) if usage else {},
    )


def upstream_http_error(status: int, message: str) -> RequestError:
    """新上游的错误一律 RequestError 原样透传（不进 AI Studio 账号 rotator 账本，
    不触发浏览器重启——那两样都是浏览器通道的语义）。"""
    return RequestError(status if status > 0 else 502, message or "upstream error")


async def stream_official_events(
    chunk_source: AsyncIterator[dict],
) -> AsyncIterator[tuple[str, object]]:
    """官方 chunk 流 → 我们的事件流（带首帧 status/done 收尾，两条通道共用）。"""
    yield ("status", 200)
    async for data in chunk_source:
        for event in official_chunk_events(data):
            yield event
    yield ("done", None)
