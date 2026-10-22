"""Hook-first request capture workflow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from aistudio_api.application.bypass import (
    BypassPlan,
    apply_bypass,
    build_wire_tool_config,
    build_wire_tools,
)
from aistudio_api.application.prefill import apply_prefill
from aistudio_api.application.runtime_settings import (
    bypass_enabled,
    prefill_cot_guard,
    prefill_enabled,
    runtime_settings,
    sig_sentinel_thought,
    sig_sentinel_tool,
)
from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.infrastructure.gateway.model_defaults import resolve_model_defaults
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.signatures import apply_signature_sentinels
from aistudio_api.infrastructure.gateway.wire_codec import modify_body
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("capture")


@dataclass
class CapturedRequest:
    url: str
    headers: dict[str, str]
    body: str
    model: str = ""
    snapshot: str = ""
    # 响应还原用：bypass 的合成工具名/模式，以及预填充正文（空串=没有预填充）
    bypass_plan: BypassPlan | None = None
    prefill_text: str = ""


def _clone_part(part: AistudioPart) -> AistudioPart:
    """浅拷贝 part：下面会就地把指令/哨兵写进去，别改到调用方持有的对象。"""
    return replace(part)


def _clone_content(content: AistudioContent) -> AistudioContent:
    parts = getattr(content, "parts", None)
    return AistudioContent(
        role=content.role,
        parts=[_clone_part(part) for part in parts] if isinstance(parts, list) else [],
    )


def _clone_contents(contents: list[AistudioContent]) -> list[AistudioContent]:
    return [_clone_content(content) for content in contents]


def _runtime_rewrites_enabled() -> bool:
    return bool(
        prefill_enabled()
        or bypass_enabled()
        or sig_sentinel_thought()
        or sig_sentinel_tool()
    )


class RequestCaptureService:
    """Single-page hook flow modeled after camoufox-api."""

    def __init__(self, session: BrowserSession):
        self._session = session
        self._templates: dict[str, CapturedRequest] = {}
        self._active_ctx_id: int | None = None
        self._lock = asyncio.Lock()

    def clear_templates(self) -> None:
        """清空捕获的请求模板（账号切换或强制刷新时调用）。"""
        self._templates.clear()
        logger.info("已清空捕获模板缓存")

    async def capture(
        self,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        images: list[str | tuple[str, str]] | None = None,
        contents: list[AistudioContent] | None = None,
        system_instruction: str | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        tool_config: list[object] | None = None,
        safety_settings: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        force_refresh: bool = False,
    ) -> CapturedRequest | None:
        if force_refresh:
            self._templates.pop(model, None)
        template = await self._ensure_template(model)

        # ── 运行期改写（prefill → bypass → 签名哨兵）：必须在生成快照之前做完 ──
        # 快照（WAA proof）与页面输入框里那份文本、以及 body 里的 contents 是同一份，
        # 改完 contents 再生成快照，binding 才对得上。
        prefill_text = ""
        bypass_plan: BypassPlan | None = None
        effective_tools = tools
        effective_tool_config = tool_config
        effective_system = system_instruction_content
        rewrite_enabled = _runtime_rewrites_enabled()

        if rewrite_enabled:
            if contents:
                working_contents: list[AistudioContent] | None = _clone_contents(
                    contents
                )
            else:
                working_contents = [
                    AistudioContent.from_user_prompt(prompt=prompt, images=images)
                ]
            if effective_system is not None:
                effective_system = _clone_content(effective_system)
        else:
            working_contents = contents

        if working_contents is not None and rewrite_enabled:
            if prefill_enabled():
                prefill_text = apply_prefill(
                    working_contents,
                    cot_guard=prefill_cot_guard(),
                    nudge=str(runtime_settings.get("prefillNudge") or ""),
                )
                if prefill_text:
                    logger.info("预填充兼容: %d 字", len(prefill_text))

            if bypass_enabled() and not resolve_model_defaults(model).is_image_model:
                instruction_position = str(
                    runtime_settings.get("bypassInstructionPosition") or "tail"
                )
                if effective_system is None and system_instruction:
                    effective_system = AistudioContent(
                        role="user", parts=[AistudioPart(text=system_instruction)]
                    )
                bypass_plan = apply_bypass(
                    working_contents,
                    system_instruction=effective_system,
                    tool_prefix=str(runtime_settings.get("bypassToolPrefix") or ""),
                    instruction_template=str(
                        runtime_settings.get("bypassInstruction") or ""
                    ),
                    position=instruction_position,
                    mode=str(runtime_settings.get("bypassMode") or "prefer"),
                    merge_outside=bool(runtime_settings.get("bypassMergeOutside")),
                )
                effective_system = bypass_plan.system_instruction
                effective_tools = build_wire_tools(bypass_plan.tool_name)
                effective_tool_config = build_wire_tool_config(
                    bypass_plan.mode, bypass_plan.tool_name
                )
                logger.info(
                    "工具调用 bypass: tool=%s, mode=%s, position=%s",
                    bypass_plan.tool_name,
                    bypass_plan.mode,
                    instruction_position,
                )

            replaced = apply_signature_sentinels(
                working_contents,
                system_instruction=effective_system,
                thought=sig_sentinel_thought(),
                tool=sig_sentinel_tool(),
            )
            if replaced:
                logger.debug("签名哨兵: 替换 %d 处 thoughtSignature", replaced)

        snapshot_contents = working_contents or [
            AistudioContent.from_user_prompt(prompt=prompt, images=images)
        ]

        # 签名是一次性的票据（根据请求内容穿进 snapshot 计算），每次请求实时生成，不可复用缓存
        snapshot = await self._session.generate_snapshot(snapshot_contents)

        body = modify_body(
            template.body,
            model=model,
            prompt=prompt,
            contents=working_contents,
            system_instruction=system_instruction,
            system_instruction_content=effective_system,
            tools=effective_tools,
            tool_config=effective_tool_config,
            safety_settings=safety_settings,
            images=images,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=sanitize_plain_text,
            snapshot=snapshot,
        )
        captured = CapturedRequest(
            url=template.url,
            headers=dict(template.headers),
            body=body,
            model=model,
            snapshot=snapshot,
            bypass_plan=bypass_plan,
            prefill_text=prefill_text,
        )
        logger.info(
            "Hook 拦截成功: model=%s, snapshot=%s chars, body=%s chars",
            captured.model,
            len(captured.snapshot),
            len(captured.body),
        )
        return captured

    async def _ensure_template(self, model: str) -> CapturedRequest:
        ctx = self._session.request_context
        ctx_id = ctx.context_id if ctx else None
        async with self._lock:
            # Check ownership after acquiring the lock: a queued request may lease another tab.
            if self._active_ctx_id != ctx_id:
                self._templates.clear()
                self._active_ctx_id = ctx_id
            if model in self._templates:
                return self._templates[model]

            captured = await self._session.capture_template(model)
            headers_dict = captured.get("headers")
            headers = {
                str(k): str(v)
                for k, v in (
                    headers_dict.items() if isinstance(headers_dict, dict) else []
                )
            }
            template = CapturedRequest(
                url=str(captured.get("url") or ""),
                headers=headers,
                body=str(captured.get("body") or ""),
                model=model,
            )
            self._templates[model] = template
            logger.info("Hook 模板已就绪并缓存: model=%s", model)
            return template
