"""Vertex AI Express 上游通道（官方 API Key 直连，无浏览器）。

来源：bad-woman/vertex2openai 的 ExpressSDKUpstream，按我们的口径裁剪：
**零自动重试**（原实现有 location 钉定失败回退裸模型名重试一次、429 退避重试，
全部不搬）、无多 Key 轮询（单 Key，配额语义交给客户端）。错误一律 RequestError
原样透传，不进 AI Studio 账号 rotator 账本。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from aistudio_api.domain.errors import RequestError
from aistudio_api.domain.models import ModelOutput
from aistudio_api.infrastructure.upstreams.common import (
    ChannelBypassExtractor,
    apply_channel_sentinels,
    attach_stream_probe,
    bypass_tool_config_official,
    bypass_tools_official,
    channel_streaming_flag_enabled,
    contents_to_official,
    finalize_stream_probe,
    maybe_channel_bypass,
    new_stream_probe,
    official_chunk_events,
    official_to_model_output,
    upstream_http_error,
    wire_safety_to_official,
    wire_tools_to_official,
)
from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.application.bypass import BypassPlan
    from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

logger = get_logger("upstreams.vertex_express")


class VertexExpressClient:
    """AIStudioClient 调用面的 Vertex Express 实现（两端口无差别接入）。"""

    def __init__(
        self,
        api_key: str,
        *,
        project_id: str = "",
        location: str = "",
        proxy_url: str | None = None,
    ) -> None:
        from google import genai
        from google.genai import types

        http_options: dict[str, Any] = {}
        if proxy_url:
            # SDK 官方支持 async_client_args 透传 httpx 构造参数
            import httpx

            http_options["async_client_args"] = {
                "transport": httpx.AsyncHTTPTransport(proxy=proxy_url)
            }
        self._genai_types = types
        self._client = genai.Client(
            vertexai=True,
            api_key=api_key,
            http_options=types.HttpOptions(**http_options) if http_options else None,
        )
        self._project_id = project_id.strip()
        self._location = location.strip()
        # 与 AIStudioClient 的故障处理兼容点：无会话可重启、无模板可清
        self._session = None

    # ---- AIStudioClient 兼容面 ----
    async def warmup(self) -> None:  # 无浏览器可预热
        return None

    def clear_templates(self) -> None:
        return None

    async def close(self) -> None:
        return None


    def _model_path(self, model: str) -> str:
        model = model.removeprefix("models/")
        if model.startswith(("projects/", "publishers/")):
            return model
        # 钉定 location：裸模型名会让 Google 后端自行路由，可能落到无此模型的区域 404
        if self._project_id and self._location:
            return (
                f"projects/{self._project_id}/locations/{self._location}"
                f"/publishers/google/models/{model}"
            )
        return model

    def _build_config(
        self,
        *,
        system_instruction_content: AistudioContent | None,
        tools: list[list] | None,
        tool_config: list[object] | None,
        safety_settings: list[list[object]] | None,
        temperature: float | None,
        top_p: float | None,
        top_k: int | None,
        max_tokens: int | None,
    ) -> dict[str, object]:
        config: dict[str, object] = {}
        if system_instruction_content is not None:
            config["system_instruction"] = {
                "parts": contents_to_official([system_instruction_content])[0]["parts"]
            }
        official_tools = wire_tools_to_official(tools)
        if official_tools:
            config["tools"] = official_tools
        safety = wire_safety_to_official(safety_settings)
        if safety:
            config["safety_settings"] = safety
        if temperature is not None:
            config["temperature"] = temperature
        if top_p is not None:
            config["top_p"] = top_p
        if top_k is not None:
            config["top_k"] = top_k
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        # shortcut: tool_config（wire 形状）与 generation_config_overrides（seed 等
        # wire 独有项）未转换，Express 通道 v1 不支持——需要时再补
        _ = tool_config
        return config

    def _call_kwargs(
        self, model: str, *, streaming: bool, **params: Any
    ) -> tuple[dict[str, Any], BypassPlan | None]:
        """(SDK 调用参数, bypass plan|None)。plan 本地传递，避免并发请求踩状态。

        streaming 标志只配流式调用：非流式（unary）请求带
        streamFunctionCallArguments 会被 Vertex 400 拒绝（2026-10-20 真机实测）。
        """
        contents: list[AistudioContent] | None = params.get("contents")
        system_instruction_content = params.get("system_instruction_content")
        plan = None
        if contents:
            contents, system_instruction_content, plan = maybe_channel_bypass(
                contents, system_instruction_content
            )
            contents, system_instruction_content = apply_channel_sentinels(
                contents, system_instruction_content
            )
        config = self._build_config(
            system_instruction_content=system_instruction_content,
            tools=params.get("tools") if plan is None else None,
            tool_config=params.get("tool_config"),
            safety_settings=params.get("safety_settings"),
            temperature=params.get("temperature"),
            top_p=params.get("top_p"),
            top_k=params.get("top_k"),
            max_tokens=params.get("max_tokens"),
        )
        if plan is not None:
            config["tools"] = bypass_tools_official(plan.tool_name)
            tool_cfg = bypass_tool_config_official(
                plan.tool_name,
                streaming=streaming and channel_streaming_flag_enabled(),
            )
            if tool_cfg:
                config["tool_config"] = tool_cfg
        return (
            {
                "model": self._model_path(model),
                "contents": contents_to_official(contents),
                "config": config or None,
            },
            plan,
        )

    def _dump(self, chunk) -> dict:
        data = chunk.model_dump(mode="json", by_alias=True, exclude_none=True)
        for k in ("sdkHttpResponse", "automaticFunctionCallingHistory"):
            data.pop(k, None)
        return data

    async def generate_content(self, *, model: str, **params: Any) -> ModelOutput:
        from google.genai import errors as genai_errors

        kwargs, plan = self._call_kwargs(model, streaming=False, **params)
        probe = new_stream_probe("vertex_express", model)
        try:
            resp = await self._client.aio.models.generate_content(**kwargs)
        except genai_errors.APIError as exc:
            raise attach_stream_probe(
                upstream_http_error(
                    int(getattr(exc, "code", 0) or 0),
                    str(getattr(exc, "message", "") or exc),
                ),
                finalize_stream_probe(probe, exc),
            ) from exc
        except Exception as exc:
            # 传输层失败（超时/断流）：502 语义 + 现场探针
            raise attach_stream_probe(
                RequestError(502, f"vertex_express transport: {exc}"),
                finalize_stream_probe(probe, exc),
            ) from exc
        data = self._dump(resp)
        output = official_to_model_output(data, model)
        if plan is not None:
            from aistudio_api.infrastructure.upstreams.common import (
                apply_extractor_nonstream,
            )

            apply_extractor_nonstream(output, ChannelBypassExtractor(plan))
        return output

    async def stream_generate_content(
        self, *, model: str, **params: Any
    ) -> AsyncIterator[tuple[str, object]]:
        from google.genai import errors as genai_errors

        kwargs, plan = self._call_kwargs(model, streaming=True, **params)
        extractor = ChannelBypassExtractor(plan) if plan is not None else None
        probe = new_stream_probe("vertex_express", model)
        from aistudio_api.infrastructure.upstreams.common import (
            touch_stream_probe,
        )

        try:
            stream = await self._client.aio.models.generate_content_stream(**kwargs)
            yield ("status", 200)
            async for chunk in stream:
                data = self._dump(chunk)
                body_chars = sum(
                    len(p.get("text") or "")
                    for c in data.get("candidates") or []
                    for p in (c.get("content") or {}).get("parts") or []
                    if isinstance(p, dict) and not p.get("thought")
                )
                think_chars = sum(
                    len(p.get("text") or "")
                    for c in data.get("candidates") or []
                    for p in (c.get("content") or {}).get("parts") or []
                    if isinstance(p, dict) and p.get("thought")
                )
                touch_stream_probe(probe, body_chars=body_chars, thinking_chars=think_chars)
                if extractor is not None:
                    for event in extractor.feed_data(data):
                        yield event
                else:
                    for event in official_chunk_events(data):
                        yield event
            if extractor is not None:
                for event in extractor.finish_events():
                    yield event
            yield ("done", None)
        except genai_errors.APIError as exc:
            raise attach_stream_probe(
                upstream_http_error(
                    int(getattr(exc, "code", 0) or 0),
                    str(getattr(exc, "message", "") or exc),
                ),
                finalize_stream_probe(probe, exc),
            ) from exc
        except Exception as exc:
            # 传输层失败（超时/断流）：502 语义 + 现场探针（用户 2026-10-20 指令）
            raise attach_stream_probe(
                RequestError(502, f"vertex_express transport: {exc}"),
                finalize_stream_probe(probe, exc),
            ) from exc
