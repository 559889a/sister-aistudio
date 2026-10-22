"""Streaming replay workflow for chat completions."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import aclosing
from pathlib import Path

from aistudio_api.application.bypass import (
    BYPASS_MISS_MESSAGE,
    BypassStreamExtractor,
    assess_completion,
    iter_wire_parts,
)
from aistudio_api.application.prefill import StreamDeduper
from aistudio_api.config import settings
from aistudio_api.domain.errors import (
    AistudioError,
    BrowserTransportError,
    RequestError,
    classify_error,
)
from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.stream_parser import (
    IncrementalJSONStreamParser,
)
from aistudio_api.infrastructure.gateway.wire_parser import (
    WIRE_RESPONSE_ID_INDEX,
    ResponsePart,
    parse_chunk_usage,
    parse_response_chunk,
    parse_response_part,
)
from aistudio_api.infrastructure.gateway.wire_spec import (
    WIRE_FINISH_REASON_CONTINUING,
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
        extractors: dict[int, BypassStreamExtractor] = {}
        dedupers: dict[int, StreamDeduper] = {}

        parser = IncrementalJSONStreamParser()
        latest_usage: dict[str, object] | None = None
        finish_reasons: dict[int, int] = {}
        latest_response_id: str | None = None
        raw_parts: list[str] = []
        status_code = 0
        # 预填充等上游确认 200（或第一帧数据）再发：上游失败时要能干净地报错，
        # 不能先给客户端吐一段正文再出错。
        pending_prefill = captured.prefill_text

        def _event(index: int, kind: str, payload: object) -> tuple[str, object]:
            if index == 0:
                return (kind, payload)
            return ("candidate_event", {"index": index, "type": kind, "payload": payload})

        def _consume_candidate(
            parsed_chunk: list[object], index: int,
        ) -> Generator[tuple[str, object], None, None]:
            if index not in dedupers:
                dedupers[index] = StreamDeduper(captured.prefill_text)
                if plan is not None:
                    extractors[index] = BypassStreamExtractor(
                        plan.tool_name, merge_plain_text=plan.merge_outside,
                    )
                if index and captured.prefill_text and status_code == 200:
                    yield _event(index, "body", captured.prefill_text)
            extractor = extractors.get(index)
            candidate = parse_response_chunk(parsed_chunk, index)
            if (
                candidate.finish_reason is not None
                and candidate.finish_reason != WIRE_FINISH_REASON_CONTINUING
            ):
                finish_reasons[index] = candidate.finish_reason
            body_delta = candidate.text
            if extractor is not None:
                parts = [
                    parse_response_part(raw)
                    for raw in iter_wire_parts(parsed_chunk, index)
                ]
                delta, other_parts = extractor.feed(parts)
                body_delta = "".join(
                    part.text for part in other_parts if isinstance(part, ResponsePart) and not part.thought
                ) + delta
            if candidate.thinking:
                yield _event(index, "thinking", candidate.thinking)
            if candidate.reasoning_images:
                yield _event(index, "reasoning_images", candidate.reasoning_images)
            other_calls = [
                call for call in candidate.function_calls
                if not tool_name or str(call.get("name") or "") != tool_name
            ]
            if other_calls:
                yield _event(index, "tool_calls", other_calls)
            if candidate.function_responses:
                yield _event(index, "function_responses", candidate.function_responses)
            if candidate.code_output:
                yield _event(index, "code_output", candidate.code_output)
            if candidate.images:
                yield _event(index, "images", candidate.images)
            if body_delta:
                emit = dedupers[index].feed(body_delta)
                if emit:
                    yield _event(index, "body", emit)
            if candidate.thought_signature:
                yield _event(index, "thought_signature", candidate.thought_signature)

        def _consume(
            parsed_chunk: list[object],
        ) -> Generator[tuple[str, object | None], None, None]:
            """单个解析后的 wire chunk → 对客户端的事件。

            feed() 的常规分块与 finish() 抢救出来的截断尾块**必须走同一份逻辑**：
            复制第二份的话，两条路对 bypass/预填充/deduper 的处理迟早会漂移。
            """
            nonlocal latest_usage, latest_response_id
            usage = parse_chunk_usage(parsed_chunk)
            if usage:
                latest_usage = usage
            if (
                len(parsed_chunk) > WIRE_RESPONSE_ID_INDEX
                and isinstance(parsed_chunk[WIRE_RESPONSE_ID_INDEX], str)
                and parsed_chunk[WIRE_RESPONSE_ID_INDEX]
            ):
                latest_response_id = str(parsed_chunk[WIRE_RESPONSE_ID_INDEX])
                yield ("response_id", latest_response_id)
            container = parsed_chunk[0] if parsed_chunk else None
            if isinstance(container, list):
                for index, raw_candidate in enumerate(container):
                    if isinstance(raw_candidate, list):
                        yield from _consume_candidate(parsed_chunk, index)

        import time as _time

        wire_t0 = _time.time()
        try:
            async with aclosing(self._session.send_streaming_request(
                body=modified_body,
                url=captured.url,
                headers=captured.headers,
            )) as source:
                async for event_type, payload in source:
                    if event_type == "status" and payload and not status_code:
                        status_code = int(str(payload))
                        if status_code == 200 and pending_prefill:
                            yield ("body", pending_prefill)
                            pending_prefill = ""
                        yield ("status", status_code)
                    elif event_type == "chunk" and payload:
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
        except Exception as exc:  # type: ignore[misc]
            # 502 探针（用户 2026-10-20 指令）：死前现场。长静默被上游掐的指纹
            # 是 elapsed 大 + 已收字节少；真断流则是 bytes 已有不少。
            from aistudio_api.infrastructure.upstreams.common import (
                attach_stream_probe,
                finalize_stream_probe,
                new_stream_probe,
            )

            probe = new_stream_probe("pg", model)
            probe["chunks_seen"] = len(raw_parts)
            probe["wire_bytes"] = sum(len(p) for p in raw_parts)
            probe["elapsed_s"] = round(_time.time() - wire_t0, 1)
            attach_stream_probe(exc, finalize_stream_probe(probe, exc))
            raise  # type: ignore[misc]  # 原异常重抛（已带 stream_probe），保持原类型与异常链

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
                exc: AistudioError = classify_error(status_code, raw_response)
            elif detail:
                exc = RequestError(status_code, detail)
            else:
                exc = RequestError(status_code, "")
            # 上游原始错误体留档（截断），给偶发 403/500 的现场记录用
            exc.raw_upstream_body = raw_response[:8192]
            raise exc
        if not raw_parts:
            # 200 但零数据帧：连接被重置时 Chrome 可能把「只有响应头」当正常完成
            # （2026-10-20 RST 实验实测的非流同款问题）——静默空流比报错更糟
            raise BrowserTransportError(
                "stream ended before any data arrived (connection truncated)"
            )

        if plan is not None and plan.mode == "required" and not extractors:
            raise RequestError(502, BYPASS_MISS_MESSAGE)
        for index, deduper in dedupers.items():
            tail = deduper.flush()
            if tail:
                yield _event(index, "body", tail)
            extractor = extractors.get(index)
            if extractor is not None:
                if plan is not None and plan.mode == "required" and not extractor.saw_call:
                    logger.warning("bypass 要求走合成工具，但模型没有调用 (%s)", tool_name)
                    raise RequestError(502, BYPASS_MISS_MESSAGE)
                if extractor.merged:
                    verdict = assess_completion(extractor.merged)
                    if not verdict.complete:
                        logger.warning(
                            "bypass 输出可能不完整（%s），已按现有内容交付", verdict.reason
                        )
            if index in finish_reasons:
                yield _event(index, "finish_reason", finish_reasons[index])
        yield ("usage", latest_usage)
        yield ("done", None)
