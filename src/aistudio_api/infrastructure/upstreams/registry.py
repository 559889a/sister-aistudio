"""上游通道注册表：按模型名前缀把请求路由到对应通道（2026-10-20 用户设计）。

前缀语义（模型列表里的 alias）：
- ``pg/<model>``      → AI Studio 无头浏览器通道（默认，无前缀也走它）
- ``express/<model>`` → Vertex Express（官方 Key 直连）
- ``cookie/<model>``  → Console batchGraphql（控制台 Cookie 直连）

模型清单走本地 ``data/models.json``（可编辑；不存在时从 defaults/ 初始化），
不再依赖云端动态发现。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aistudio_api.infrastructure.utils.logger import get_logger

if TYPE_CHECKING:
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

logger = get_logger("upstreams.registry")

# 前缀 → 通道名；默认通道（无前缀）是浏览器
PREFIX_CHANNELS = {"pg/": "aistudio", "express/": "vertex_express", "cookie/": "console_graphql"}
DEFAULT_CHANNEL = "aistudio"

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_LIST = _PROJECT_ROOT / "defaults" / "models.json"


def resolve_models_file() -> Path:
    """data/models.json（可编辑）；不存在则从 defaults/ 初始化一份。"""
    from aistudio_api.config import resolve_data_dir

    target = resolve_data_dir() / "models.json"
    if not target.exists() and _DEFAULT_LIST.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_DEFAULT_LIST, target)
    return target


def load_local_models() -> list[dict[str, Any]]:
    """读本地模型清单；损坏/缺省时回退到内置两模型（gemini-3.8-flash / argon）。"""
    fallback = [
        {"id": "gemini-3.8-flash", "display_name": "Gemini 3.8 Flash"},
        {"id": "gemini-4-argon", "display_name": "Gemini 4 Argon"},
    ]
    path = resolve_models_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        models = raw.get("models") if isinstance(raw, dict) else raw
        if isinstance(models, list) and models:
            return [m for m in models if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        logger.warning("读取 models.json 失败（用内置清单）: %s", exc)
    return fallback


def local_model_list_payload(registry: UpstreamRegistry | None = None) -> dict[str, Any]:
    """本地清单 → OpenAI /v1/models 形状；只列三通道前缀别名（2026-10-20 用户指令：
    不要裸模型名）。裸名仍可调用（pick() 落默认通道），只是不进列表。"""
    now = int(time.time())
    data: list[dict[str, Any]] = []
    for model in load_local_models():
        base_id = str(model["id"]).removeprefix("models/")
        entries = [
            (prefix, base_id) for prefix in sorted(PREFIX_CHANNELS) if registry is None or registry.get(prefix) is not None
        ]
        for prefix, model_id in entries:
            data.append(
                {
                    "id": f"{prefix}{model_id}",
                    "object": "model",
                    "created": now,
                    "owned_by": "google",
                    "permission": [],
                    "root": base_id,
                    "parent": None,
                    **({"display_name": model["display_name"]} if model.get("display_name") else {}),
                }
            )
    return {"object": "list", "data": sorted(data, key=lambda item: item["id"])}


class UpstreamRegistry:
    """通道客户端注册 + 按模型名选路。"""

    def __init__(self) -> None:
        self._clients: dict[str, Any] = {}
        # 无前缀模型的默认通道（AISTUDIO_UPSTREAM 可覆盖；pg/ 前缀始终强制浏览器）
        self.default_channel = DEFAULT_CHANNEL

    def register(self, channel: str, client: Any) -> None:
        self._clients[channel] = client

    def get(self, channel_or_prefix: str):
        if channel_or_prefix in self._clients:
            return self._clients[channel_or_prefix]
        channel = PREFIX_CHANNELS.get(channel_or_prefix)
        return self._clients.get(channel) if channel else None

    def pick(self, model: str) -> tuple[AIStudioClient | Any, str, str]:
        """(client, 去前缀模型名, 通道名)。未知前缀原样当模型名走默认通道。

        兼容 Gemini 原生端口的 "models/express/..." 形态：前缀匹配前先剥
        "models/"（2026-10-22 实证：不剥则前缀永不命中，express/ 静默掉回浏览器通道）。
        """
        work = model.removeprefix("models/")
        for prefix, channel in PREFIX_CHANNELS.items():
            if work.startswith(prefix):
                client = self._clients.get(channel)
                if client is not None:
                    return client, work[len(prefix):], channel
                logger.warning("模型 %s 指向通道 %s，但该通道未配置凭据，回退默认通道", model, channel)
                break
        default = self._clients.get(self.default_channel) or self._clients.get(DEFAULT_CHANNEL)
        if default is None:
            default = next(iter(self._clients.values()), None)
        if default is None:
            raise RuntimeError("没有任何可用上游通道")
        return default, model, self.default_channel
