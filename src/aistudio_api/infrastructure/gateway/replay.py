"""Captured request replay workflow."""

from __future__ import annotations

from aistudio_api.infrastructure.gateway.capture import CapturedRequest
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("replay")


class RequestReplayService:
    def __init__(self, session: BrowserSession):
        self._session = session

    async def replay(
        self, captured: CapturedRequest | None, body: str
    ) -> tuple[int, bytes]:
        """重放捕获到的请求。**不设生成等待时长上限**（整段生成可能跑几分钟）。"""
        if not captured:
            return 0, b""

        headers = {
            k: v
            for k, v in captured.headers.items()
            if k.lower() not in ("host", "content-length")
        }

        return await self._session.send_hooked_request(
            body=body,
            url=captured.url,
            headers=headers,
        )
