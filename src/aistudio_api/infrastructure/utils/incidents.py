"""偶发故障现场落盘（append-only JSONL）。

背景（2026-10-20 攻坚 500/403/502）：这类故障偶发、难以复现，而常规日志只进
stderr，服务重启即丢。每次终端态失败在这里追加一条结构化现场记录，把
「上游原始错误体 + CDP 网络层证据 + 失败瞬间的页面 hook/WAA 状态」留档，
下次案发直接取证，不用猜。

文件：``<data>/incidents.jsonl``，超过 2MB 滚动为 ``.1``（只留一代）。
所有写入失败静默吞掉——取证绝不能反过来弄挂请求。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aistudio_api.config import resolve_data_dir
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("incidents")

_MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()


def _incident_path() -> Path:
    return resolve_data_dir() / "incidents.jsonl"


def record_incident(kind: str, fields: dict[str, Any]) -> None:
    """追加一条故障现场记录；任何 IO 异常都只降级为一条 WARN。"""
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "kind": kind}
    record.update(fields)
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            path = _incident_path()
            if path.exists() and path.stat().st_size > _MAX_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:  # pragma: no cover - 取证失败不影响主流程
        log.warning("故障现场落盘失败: %s", exc)
