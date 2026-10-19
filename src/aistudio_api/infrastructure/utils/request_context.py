"""请求级上下文（request id）。

路由层在入口生成一个短 request id（如 `mvf50ebkl6a0ysn` 那种），放进 ContextVar；
下游模块（上游调用、重试、浏览器传输）打日志时直接带上它，不用层层传参。
asyncio 的每个任务有独立的 context，不会串号。
"""

from __future__ import annotations

import contextvars

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aistudio_request_id", default=""
)


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id or "")


def get_request_id() -> str:
    return _request_id.get()


def new_request_id() -> str:
    import secrets

    return secrets.token_hex(6)


def rid_suffix(request_id: str | None = None) -> str:
    """日志里用 ` (rid)` 尾巴；没有请求上下文时返回空串。"""
    rid = request_id if request_id is not None else get_request_id()
    return f" ({rid})" if rid else ""


__all__ = ["get_request_id", "new_request_id", "rid_suffix", "set_request_id"]
