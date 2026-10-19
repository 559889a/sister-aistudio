"""Domain errors for AI Studio interactions."""

from __future__ import annotations

import json


class AistudioError(Exception):
    pass


class AuthError(AistudioError):
    pass


class SessionExpiredError(AuthError):
    """明确被 Google 重定向至登录页（Cookie 真正失效）。"""


class UsageLimitExceeded(AistudioError):
    pass


class RequestError(AistudioError):
    def __init__(self, status: int, message: str = ""):
        self.status = status
        super().__init__(f"HTTP {status}: {message}" if message else f"HTTP {status}")


class BrowserTransportError(RuntimeError):
    """页面里的 fetch/XHR 传输失败（网络层错误，不是上游返回的 HTTP 状态）。

    典型现场：`streaming request failed: Failed to fetch`——请求已经发出去了，但浏览器
    侧的连接被重置/中断，拿不到任何响应。这类失败和账号、鉴权无关，也不是上游的业务
    错误（那种会以 `RequestError(status)` 抛出来）。

    继承 RuntimeError 保持既有调用方与用例的捕获兼容；网关侧按「还没拿到任何响应」的
    瞬时传输失败处理：清模板缓存，未吐数据时原地重试，重试耗尽报 502 而不是 500。
    """


GRPC_CODE_TO_HTTP: dict[int, int] = {
    1: 499,  # CANCELLED
    2: 500,  # UNKNOWN
    3: 400,  # INVALID_ARGUMENT
    4: 504,  # DEADLINE_EXCEEDED
    5: 404,  # NOT_FOUND
    6: 409,  # ALREADY_EXISTS
    7: 403,  # PERMISSION_DENIED
    8: 429,  # RESOURCE_EXHAUSTED
    9: 400,  # FAILED_PRECONDITION
    10: 409,  # ABORTED
    11: 400,  # OUT_OF_RANGE
    12: 501,  # UNIMPLEMENTED
    13: 500,  # INTERNAL
    14: 503,  # UNAVAILABLE
    15: 500,  # DATA_LOSS
    16: 401,  # UNAUTHENTICATED
}


def parse_upstream_grpc_error(raw_response: str) -> tuple[int | None, str]:
    """Parse Google JSPB/JSON error structures into (grpc_code, message)."""
    if not raw_response:
        return None, ""
    text = raw_response.strip()

    normalized = text
    if normalized.startswith("[,"):
        normalized = "[null," + normalized[2:]

    try:
        data = json.loads(normalized)
        if isinstance(data, list):
            def _find_grpc(obj: object) -> tuple[int | None, str] | None:
                if isinstance(obj, list):
                    if (
                        len(obj) >= 2
                        and isinstance(obj[0], int)
                        and isinstance(obj[1], str)
                        and obj[1]
                    ):
                        return obj[0], obj[1]
                    for item in obj:
                        res = _find_grpc(item)
                        if res is not None:
                            return res
                elif isinstance(obj, dict):
                    err = obj.get("error")
                    if isinstance(err, dict) and "message" in err:
                        code = err.get("code")
                        return (int(code) if isinstance(code, (int, float)) else None), str(err["message"])
                return None

            found = _find_grpc(data)
            if found is not None:
                return found
    except Exception:
        pass

    return None, text


def classify_error(status: int, body: str) -> AistudioError:
    grpc_code, msg = parse_upstream_grpc_error(body)
    clean_msg = msg or body[:200]
    if grpc_code == 8 or status == 429:
        return UsageLimitExceeded(f"配额用完: {clean_msg}")
    if grpc_code == 16 or status == 401:
        return AuthError(f"认证失败: {clean_msg}")
    if grpc_code == 7 or status == 403:
        return AuthError(f"禁止访问: {clean_msg}")
    effective_status = GRPC_CODE_TO_HTTP.get(grpc_code, status) if grpc_code else status
    return RequestError(effective_status, clean_msg)
def is_transient_rpc_error(exc: Exception) -> bool:
    """识别 Google 上游偶发且完全随机的 RPC 路由 404 (Ambiguous request for service)。"""
    if isinstance(exc, RequestError) and exc.status == 404:
        err = str(exc).lower()
        return "ambiguous request for service" in err and "generatecontent" in err
    return False


def is_browser_session_hang_error(exc: Exception) -> bool:
    """识别由于 CDP 断开、模板捕获超时或 BotGuard 握手未就绪引起的会话挂起异常。"""
    if isinstance(exc, (RuntimeError, TimeoutError)):
        err = str(exc).lower()
        return any(
            k in err
            for k in (
                "template capture",
                "botguard",
                "timeout",
                "cdp",
                "closed",
                "aborted",
            )
        )
    return False
