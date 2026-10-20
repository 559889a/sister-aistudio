"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

from aistudio_api.application.bypass import (
    BYPASS_MISS_MESSAGE,
    BypassStreamExtractor,
    assess_completion,
    iter_wire_parts,
)
from aistudio_api.application.prefill import StreamDeduper
from aistudio_api.config import settings
from aistudio_api.domain.errors import RequestError, classify_error
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.wire_parser import (
    parse_chunk_usage,
    parse_response_chunk,
    parse_response_part,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("streaming")


def _dump_stream_exchange(
    *,
    model: str,
    url: str,
    modified_body: str,
    status_code: int,
    raw_response: str,
) -> None:
    if not settings.dump_raw_response:
        return

    out_dir = Path(settings.dump_raw_response_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = model.replace("/", "_")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    payload = {
        "kind": "stream_generate_content",
        "model": model,
        "url": url,
        "status_code": status_code,
        "modified_body": json.loads(modified_body),
        "raw_response": raw_response,
    }
    path = out_dir / f"aistudio_stream_generate_content_{safe_model}_{timestamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info("已落盘流式原始请求/响应: %s", path)


def _summarize_error_body(raw_response: str, limit: int = 500) -> str:
    text = raw_response.strip()
    if not text:
        return ""

    try:
        payload = json.loads(text)
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        compact = " ".join(text.split())

    if len(compact) > limit:
        return compact[:limit] + "..."
    return compact


class StreamingGateway:
    def __init__(self, session: BrowserSession | None = None):
        self._session = session

    async def stream_chat(
        self,
        *,
        captured: CapturedRequest,
        model: str,
    ) -> AsyncGenerator[tuple[str, object | None], None]:
        if not captured:
            raise ValueError("captured request is required")
        if self._session is None:
            raise RuntimeError("browser session is required for streaming xhr replay")
        modified_body = captured.body

        plan = captured.bypass_plan
        tool_name = plan.tool_name if plan is not None else ""
        extractor = (
            BypassStreamExtractor(plan.tool_name, merge_plain_text=plan.merge_outside)
            if plan is not None
            else None
        )
        # 预填充已经先发给了客户端，模型若把尾巴抄一遍由 deduper 切掉
        deduper = StreamDeduper(captured.prefill_text)

        parser = IncrementalJSONStreamParser()
        latest_usage: dict[str, object] | None = None
        latest_finish_reason: int | None = None
        latest_response_id: str | None = None
        raw_parts: list[str] = []
        status_code = 0
        # 预填充等上游确认 200（或第一帧数据）再发：上游失败时要能干净地报错，
        # 不能先给客户端吐一段正文再出错。
        pending_prefill = captured.prefill_text

        def _consume(
            parsed_chunk: list[object],
        ) -> Generator[tuple[str, object | None], None, None]:
            """单个解析后的 wire chunk → 对客户端的事件。

            feed() 的常规分块与 finish() 抢救出来的截断尾块**必须走同一份逻辑**：
            复制第二份的话，两条路对 bypass/预填充/deduper 的处理迟早会漂移。
            """
            nonlocal latest_usage, latest_finish_reason, latest_response_id
            usage = parse_chunk_usage(parsed_chunk)
            if usage:
                latest_usage = usage
            if (
                len(parsed_chunk) > 7
                and isinstance(parsed_chunk[7], str)
                and parsed_chunk[7]
            ):
                latest_response_id = parsed_chunk[7]
                yield ("response_id", latest_response_id)
            candidate = parse_response_chunk(parsed_chunk)
            if candidate.finish_reason is not None:
                latest_finish_reason = candidate.finish_reason
            body_delta = ""
            if extractor is not None:
                # 合成工具的 args 是累积增量：只吐多出来的尾巴
                parts = [
                    parse_response_part(raw) for raw in iter_wire_parts(parsed_chunk)
                ]
                delta, other_parts = extractor.feed(parts)
                # 不合并模式下，工具出场前的工具外正文还在 other_parts 里
                extra_pieces = [
                    str(part.text)
                    for part in other_parts
                    if getattr(part, "text", None) is not None
                    and not getattr(part, "thought", False)
                ]
                body_delta = "".join(extra_pieces) + delta
            if candidate.thinking:
                yield ("thinking", candidate.thinking)
            if candidate.reasoning_images:
                yield ("reasoning_images", candidate.reasoning_images)
            # 合成工具的调用不能漏给客户端；其它（真）工具调用照旧透传
            other_calls = [
                call
                for call in candidate.function_calls
                if str(call.get("name") or "") != tool_name
            ]
            if other_calls:
                yield ("tool_calls", other_calls)
            if candidate.images:
                yield ("images", candidate.images)
            if extractor is None and candidate.text:
                body_delta = candidate.text
            if body_delta:
                emit = deduper.feed(body_delta)
                if emit:
                    yield ("body", emit)
            if candidate.thought_signature:
                yield ("thought_signature", candidate.thought_signature)

        async for event_type, payload in self._session.send_streaming_request(
            body=modified_body,
            url=captured.url,
            headers=captured.headers,
        ):
            if event_type == "status" and payload and not status_code:
                status_code = int(str(payload))
                if status_code == 200 and pending_prefill:
                    yield ("body", pending_prefill)
                    pending_prefill = ""
                # 让上层能看到「响应头已到」——诊断长静默时用它区分
                # 「连头和状态都没拿到」还是「有响应头但迟迟没有正文」。
                yield ("status", status_code)
            elif event_type == "chunk" and payload:
                # 只有确认 200 才把预填充发给客户端；错误状态带 body 的流
                # 不能先吐一段正文再报错
                if pending_prefill and status_code == 200:
                    yield ("body", pending_prefill)
                    pending_prefill = ""
                if isinstance(payload, bytes):
                    text_payload = payload.decode("utf-8", errors="replace")
                else:
                    text_payload = str(payload)
                raw_parts.append(text_payload)
                for parsed_chunk in parser.feed(text_payload):
                    for event in _consume(parsed_chunk):
                        yield event
                    if latest_finish_reason is not None:
                        break
                if latest_finish_reason is not None:
                    break
        # 上游流可能被截断（网络断、页面 abort）而最后一个 chunk 只收到一半：
        # parser.finish() 把它抢救出来，仍然走 _consume，最后一段正文不会丢。
        for parsed_chunk in parser.finish():
            for event in _consume(parsed_chunk):
                yield event
        raw_response = "".join(raw_parts)
        _dump_stream_exchange(
            model=model,
            url=captured.url,
            modified_body=modified_body,
            status_code=status_code,
            raw_response=raw_response,
        )
        if status_code != 200:
            detail = _summarize_error_body(raw_response)
            if status_code in (401, 403, 429):
                raise classify_error(status_code, raw_response)
            if detail:
                raise RequestError(status_code, detail)
            raise RequestError(status_code, "")

        tail = deduper.flush()
        if tail:
            yield ("body", tail)
        if extractor is not None:
            if plan is not None and plan.mode == "required" and not extractor.saw_call:
                # 流式下上游响应头已经发出去了，这个 502 由 API 层渲染成流内错误帧；
                # 与主项目一致：required 只是给排查用，prefer 才是日常。
                logger.warning("bypass 要求走合成工具，但模型没有调用 (%s)", tool_name)
                raise RequestError(502, BYPASS_MISS_MESSAGE)
            if extractor.merged:
                verdict = assess_completion(extractor.merged)
                if not verdict.complete:
                    logger.warning(
                        "bypass 输出可能不完整（%s），已按现有内容交付", verdict.reason
                    )
        if latest_finish_reason is not None:
            yield ("finish_reason", latest_finish_reason)
        yield ("usage", latest_usage)
        yield ("done", None)
