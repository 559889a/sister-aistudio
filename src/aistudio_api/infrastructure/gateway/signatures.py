"""思考签名（thoughtSignature）哨兵。

为什么：真签名是「这段内容由我生成」的凭据。把它原样回传时，上游会**校验之后
替换内容**——实测表现是同一份对话只要撞过一次拒绝，回滚/重放都再也破不开，
而换新对话就能过。网关大部分场景根本不依赖签名（不回放思考、不回放工具调用），
所以直接换成官方逃生口 `skip_thought_signature_validator`：上游跳过校验，
也就不存在「按签名把内容替换掉」这一步。

分两个开关，因为两者出现的场合不同：
- `thought`：正文/思考 part 上的 thoughtSignature（多轮思考回放）；
- `tool`：functionCall part 上的签名（工具回放，含 bypass 回放的那一份）。

两个默认都开（见 `application/runtime_settings.py`）。要恢复真签名回传，把对应
开关关掉即可。对应主项目 build2api 的 `src/signatures.js` 里的
`applySignatureSentinels`；本模块只做请求侧改写，不碰响应。
"""

from __future__ import annotations

from collections.abc import Iterable

from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

SKIP_SENTINEL = "skip_thought_signature_validator"


def apply_to_parts(
    parts: Iterable[object],
    *,
    thought: bool = True,
    tool: bool = True,
) -> int:
    """把一个 part 数组里的签名换成哨兵，返回替换条数。"""
    if not thought and not tool:
        return 0
    replaced = 0
    for part in parts:
        if not isinstance(part, AistudioPart):
            continue
        signature = part.thought_signature
        if not isinstance(signature, str) or not signature:
            continue
        # 已经是哨兵就别再动（重复调用要幂等）
        if signature == SKIP_SENTINEL:
            continue
        enabled = tool if part.function_call is not None else thought
        if not enabled:
            continue
        part.thought_signature = SKIP_SENTINEL
        replaced += 1
    return replaced


def apply_signature_sentinels(
    contents: Iterable[AistudioContent] | None,
    *,
    system_instruction: AistudioContent | None = None,
    thought: bool = True,
    tool: bool = True,
) -> int:
    """把发往上游的 contents（含 systemInstruction）里的 thoughtSignature 换成哨兵。

    @returns 被替换的签名条数（便于日志/测试断言）。
    """
    if not thought and not tool:
        return 0
    total = 0
    for content in contents or []:
        total += apply_to_parts(
            getattr(content, "parts", None) or [],
            thought=thought,
            tool=tool,
        )
    if system_instruction is not None:
        total += apply_to_parts(
            getattr(system_instruction, "parts", None) or [],
            thought=thought,
            tool=tool,
        )
    return total


__all__ = [
    "SKIP_SENTINEL",
    "apply_signature_sentinels",
    "apply_to_parts",
]
