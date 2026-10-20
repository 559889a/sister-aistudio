"""Console batchGraphql 上游通道（Cloud 控制台 Cookie 直连，无浏览器）。

来源：bad-woman/vertex2openai 的 cookie_proxy.py，按我们的口径裁剪：零自动重试、
错误原样透传。端点/协议要点：
- POST cloudconsole-pa.clients6.google.com/.../AIPLATFORM_GRAPHQL:batchGraphgl
- 三段式 SAPISIDHASH（SAPISID / __Secure-1PAPISID / __Secure-3PAPISID，origin 是
  console.cloud.google.com——与 AI Studio 通道的 aistudio origin 不同）
- GraphQL 信封 {requestContext, querySignature, operationName, variables}，
  variables 里就是官方 GenerateContent 形状（model 为 projects/... 完整路径）
- 流式响应是连续 JSON 对象（括号配平切分），每个形如
  {"results": [{"data": {官方 GenerateContentResponse}}]} 或 {"error": ...}
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.upstreams.common import (
    contents_to_official,
    official_chunk_events,
    official_to_model_output,
    wire_safety_to_official,
    wire_tools_to_official,
)
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

logger = get_logger("upstreams.console_graphql")

BATCH_GRAPHQL_URL = (
    "https://cloudconsole-pa.clients6.google.com"
    "/v3/entityServices/AiplatformEntityService"
    "/schemas/AIPLATFORM_GRAPHQL:batchGraphql"
    "?key=AIzaSyCI-zsRP85UVOi0DjtiCwWBwQ1djDy741g&prettyPrint=false"
)
# 2026-10-04 Google 作废旧签名后重抓的 StreamGenerateContent 签名（登录模式）
STREAM_GENERATE_QUERY_SIGNATURE = "2/Hc4FpJfYmM+gO5TB0hcjYY0Iwj0rqLIVDZOhurBbu/I="
STREAM_GENERATE_OPERATION_NAME = "StreamGenerateContent"
_SAPISIDHASH_ORIGIN = "https://console.cloud.google.com"

_COOKIE_RE = {name: re.compile(rf"(?:^|;\s*){re.escape(name)}=([^;]*)") for name in ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")}


def _cookie_value(cookie_str: str, name: str) -> str:
    match = _COOKIE_RE[name].search(cookie_str)
    return match.group(1).strip() if match else ""


def _sapisid_hash(value: str, ts: int) -> str:
    digest = hashlib.sha1(f"{ts} {value} {_SAPISIDHASH_ORIGIN}".encode()).hexdigest()
    return f"{ts}_{digest}"


def build_console_authorization(cookie_str: str) -> str:
    """三段式 Authorization：SAPISIDHASH <h> SAPISID1PHASH <h> SAPISID3PHASH <h>。"""
    sapisid = _cookie_value(cookie_str, "SAPISID")
    sapisid_1p = _cookie_value(cookie_str, "__Secure-1PAPISID")
    sapisid_3p = _cookie_value(cookie_str, "__Secure-3PAPISID")
    primary = sapisid or sapisid_3p or sapisid_1p
    if not primary:
        raise RequestError(401, "Console Cookie 缺少 SAPISID 族字段，无法计算认证头")
    ts = int(time.time())
    return " ".join(
        (
            "SAPISIDHASH", _sapisid_hash(sapisid or primary, ts),
            "SAPISID1PHASH", _sapisid_hash(sapisid_1p or primary, ts),
            "SAPISID3PHASH", _sapisid_hash(sapisid_3p or primary, ts),
        )
    )


def _request_context(project_id: str) -> dict[str, object]:
    now_ms = int(time.time() * 1000)
    return {
        "clientVersion": "boq_cloud-boq-clientweb-vertexaistudio_20260609.06_p0",
        "pagePath": "/agent-platform/studio/multimodal",
        "pageViewId": now_ms % (10**15),
        "trackingId": str(int(time.time() * 1000000) % (10**17)),
        "backendOverrides": {},
        "clientSessionId": str(uuid.uuid4()).upper(),
        "projectId": project_id,
        "selectedPurview": {"projectId": project_id},
        "jurisdiction": "global",
        "experimentFlagsBinary": "",
        "localizationData": {"locale": "zh_CN", "timezone": "Asia/Hong_Kong"},
    }


class ConsoleGraphqlClient:
    """AIStudioClient 调用面的 Console Cookie 直连实现。"""

    def __init__(
        self,
        cookie: str,
        project_id: str,
        *,
        proxy_url: str | None = None,
    ) -> None:
        self._cookie = cookie.strip()
        self._project_id = project_id.strip()
        if not self._project_id:
            raise ValueError("console_graphql 上游需要 Project ID（AISTUDIO_CONSOLE_PROJECT_ID）")
        self._proxy_url = proxy_url
        self._session = None

    async def warmup(self) -> None:
        return None

    def clear_templates(self) -> None:
        return None

    async def close(self) -> None:
        return None


    def _headers(self) -> dict[str, str]:
        return {
            "authorization": build_console_authorization(self._cookie),
            "cookie": self._cookie,
            "content-type": "application/json",
            "x-goog-authuser": "0",
            "x-same-domain": "1",
            "origin": _SAPISIDHASH_ORIGIN,
            "referer": f"{_SAPISIDHASH_ORIGIN}/",
        }

    def _model_path(self, model: str) -> str:
        model = model.removeprefix("models/")
        if model.startswith("projects/"):
            return model
        return f"projects/{self._project_id}/locations/global/publishers/google/models/{model}"

    def _build_body(
        self,
        model: str,
        *,
        contents: list[AistudioContent] | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        tool_config: list[object] | None = None,
        safety_settings: list[list[object]] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        **_extra: Any,
    ) -> dict[str, object]:
        variables: dict[str, object] = {
            "contents": contents_to_official(contents),
            "model": self._model_path(model),
            "generationConfig": {},
            "safetySettings": wire_safety_to_official(safety_settings) or [
                {"category": c, "threshold": "OFF"}
                for c in (
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_JAILBREAK",
                )
            ],
        }
        gen_config: dict[str, object] = variables["generationConfig"]  # type: ignore[assignment]
        if temperature is not None:
            gen_config["temperature"] = temperature
        if top_p is not None:
            gen_config["topP"] = top_p
        if top_k is not None:
            gen_config["topK"] = top_k
        if max_tokens is not None:
            gen_config["maxOutputTokens"] = max_tokens
        if system_instruction_content is not None:
            variables["systemInstruction"] = {
                "parts": contents_to_official([system_instruction_content])[0]["parts"]
            }
        official_tools = wire_tools_to_official(tools)
        if official_tools:
            variables["tools"] = official_tools
        # shortcut: tool_config（wire 形状）未转换，该通道 v1 不支持 tool_choice
        _ = tool_config
        return {
            "requestContext": _request_context(self._project_id),
            "querySignature": STREAM_GENERATE_QUERY_SIGNATURE,
            "operationName": STREAM_GENERATE_OPERATION_NAME,
            "variables": variables,
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), proxy=self._proxy_url)

    @staticmethod
    def _graphql_error(obj: dict) -> str:
        if isinstance(obj.get("error"), dict):
            err = obj["error"]
            return str(err.get("message") or err)
        if isinstance(obj.get("error"), str):
            return obj["error"]
        for result in obj.get("results") or []:
            for item in result.get("errors") or []:
                msg = item.get("message") if isinstance(item, dict) else item
                if msg:
                    return str(msg)
        return ""

    @staticmethod
    async def _iter_json_objects(response: httpx.Response) -> AsyncIterator[dict]:
        """括号配平切分连续 JSON 对象（对方实现同款算法）。"""
        buffer = ""
        async for chunk in response.aiter_text():
            if not chunk:
                continue
            buffer += chunk
            while buffer:
                start = buffer.find("{")
                if start == -1:
                    buffer = ""
                    break
                depth = 0
                in_str = False
                escaped = False
                end = -1
                for i in range(start, len(buffer)):
                    ch = buffer[i]
                    if in_str:
                        if escaped:
                            escaped = False
                        elif ch == "\\":
                            escaped = True
                        elif ch == '"':
                            in_str = False
                        continue
                    if ch == '"':
                        in_str = True
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                if end == -1:
                    buffer = buffer[start:]
                    break
                candidate = buffer[start : end + 1]
                buffer = buffer[end + 1 :]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    continue

    async def generate_content(self, *, model: str, **params: Any) -> ModelOutput:
        body = self._build_body(model, **params)
        aggregated: dict[str, Any] = {"candidates": []}
        texts: list[str] = []
        async for data in self._stream_data(body):
            for event, payload in official_chunk_events(data):
                if event == "body":
                    texts.append(str(payload))
                elif event == "usage":
                    aggregated["usageMetadata"] = payload
        if texts:
            aggregated["candidates"] = [
                {"content": {"parts": [{"text": "".join(texts)}]}, "finishReason": "STOP"}
            ]
        output = official_to_model_output(aggregated, model)
        if not output.text and not output.candidates[0].function_calls:
            raise RequestError(502, "console_graphql 上游未返回任何内容")
        return output

    async def stream_generate_content(
        self, *, model: str, **params: Any
    ) -> AsyncIterator[tuple[str, object]]:
        body = self._build_body(model, **params)
        yield ("status", 200)
        async for data in self._stream_data(body):
            for event in official_chunk_events(data):
                yield event
        yield ("done", None)

    async def _stream_data(self, body: dict[str, object]) -> AsyncIterator[dict]:
        async with (
            self._client() as client,
            client.stream("POST", BATCH_GRAPHQL_URL, headers=self._headers(), json=body) as response,
        ):
                if response.status_code != 200:
                    raw = (await response.aread()).decode("utf-8", errors="replace")
                    raise RequestError(
                        response.status_code,
                        f"batchGraphql HTTP {response.status_code}: {raw[:500]}",
                    )
                async for obj in self._iter_json_objects(response):
                    err = self._graphql_error(obj)
                    if err:
                        raise RequestError(502, f"batchGraphql: {err[:500]}")
                    for result in obj.get("results") or []:
                        data = result.get("data")
                        if isinstance(data, dict) and data:
                            yield data
