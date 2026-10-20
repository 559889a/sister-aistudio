"""运行期设置（与主项目 build2api 的 settings.js 对齐的一组旋钮）。

和 `config.yaml` 的分工：
- `config.yaml` 是**启动期配置**（监听、鉴权、浏览器、模型默认档案），改了要重载/重启；
- 这里的设置是**运行期设置**，落在 `data/settings.json`，WebUI 改完立即生效，
  直接决定「每次请求怎么改写上游请求 / 怎么翻译回来」。

键名与语义尽量和主项目保持一致，方便两边对照维护：
`thinkingMode` / `thinkingLevel` / `includeThoughts` / `dropMaxTokens` /
`prefillEnabled` / `prefillCotGuard` / `bypassEnabled` / `sigSentinelThought` /
`sigSentinelTool` …
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from aistudio_api.config import resolve_data_dir
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("settings")

GROUP_LABELS: dict[str, str] = {
    "override": "参数覆写",
    "prefill": "预填充兼容",
    "bypass": "工具调用 bypass",
}

# type 取值：bool / enum / int? / float? / text
SCHEMA: list[dict[str, Any]] = [
    # ── 参数覆写 ──
    {
        "key": "thinkingMode",
        "type": "enum",
        "values": ["request", "off", "console"],
        "value_labels": {
            "request": "跟随客户端",
            "off": "强制关到最低",
            "console": "强制用下面的档位",
        },
        "default": "request",
        "group": "override",
        "label": "思考控制",
        "desc": "跟随客户端时它传什么就用什么（reasoning_effort / thinkingLevel 都认）。",
    },
    {
        "key": "thinkingLevel",
        "type": "enum",
        "values": ["", "minimal", "low", "medium", "high"],
        "value_labels": {
            "": "用模型默认",
            "minimal": "minimal（最省）",
            "low": "low",
            "medium": "medium",
            "high": "high（最深）",
        },
        "default": "",
        "group": "override",
        "label": "思考等级",
        "desc": "会按模型实际支持的档位收敛：3 Pro 只有 low / high，填 medium 会落到最近的一档。",
        "depends_on": {"thinkingMode": ["console"]},
    },
    {
        "key": "includeThoughts",
        "type": "bool",
        "default": True,
        "group": "override",
        "label": "返回思考内容",
        "desc": "关掉后上游不再回传思维链，OpenAI 格式下 reasoning_content 为空。",
    },
    {
        "key": "dropMaxTokens",
        "type": "bool",
        "default": False,
        "group": "override",
        "label": "丢弃客户端的输出长度上限",
        "desc": "客户端预设写死了很小的 max_tokens、正文被截断时打开。",
    },
    {
        "key": "maxOutputTokens",
        "type": "int?",
        "min": 1,
        "max": 1048576,
        "default": None,
        "group": "override",
        "label": "输出长度上限覆写",
        "desc": "留空 = 用客户端的值。",
        "depends_on": {"dropMaxTokens": [False]},
        "advanced": True,
    },
    {
        "key": "temperature",
        "type": "float?",
        "min": 0.0,
        "max": 2.0,
        "default": None,
        "group": "override",
        "label": "温度覆写",
        "desc": "留空 = 用客户端的值。填了就无条件覆盖。",
        "advanced": True,
    },
    {
        "key": "topP",
        "type": "float?",
        "min": 0.0,
        "max": 1.0,
        "default": None,
        "group": "override",
        "label": "top_p 覆写",
        "desc": "留空 = 用客户端的值。",
        "advanced": True,
    },
    {
        "key": "topK",
        "type": "int?",
        "min": 1,
        "max": 63,
        "default": None,
        "group": "override",
        "label": "top_k 覆写",
        "desc": "留空 = 用客户端的值。Gemini 上限 63，超了会被收敛。",
        "advanced": True,
    },
    # ── 预填充兼容 ──
    {
        "key": "prefillEnabled",
        "type": "bool",
        "default": True,
        "group": "prefill",
        "label": "启用预填充兼容",
        "desc": (
            "Gemini 3.x 拒绝以 assistant 消息结尾的请求，但 SillyTavern 一类前端靠它锚定格式。"
            "开启后自动补一条续写指令，再把预填充内容拼回响应开头。"
        ),
    },
    {
        "key": "prefillCotGuard",
        "type": "bool",
        "default": True,
        "group": "prefill",
        "label": "思维链闭合守卫",
        "desc": (
            "预填充停在未闭合标签里时（如 <thinking>），追加一句要求先补完并闭合它，"
            "否则模型常直接跳去写正文、前端按标签抽不到内容。"
        ),
        "depends_on": {"prefillEnabled": [True]},
    },
    {
        "key": "prefillNudge",
        "type": "text",
        "max_length": 2000,
        "default": "",
        "group": "prefill",
        "label": "自定义续写指令",
        "desc": "留空 = 用内置指令。这条会作为 user 消息追加在预填充之后。",
        "depends_on": {"prefillEnabled": [True]},
        "advanced": True,
    },
    # ── 工具调用 bypass ──
    {
        "key": "bypassEnabled",
        "type": "bool",
        "default": False,
        "group": "bypass",
        "label": "启用工具调用 bypass",
        "desc": "让模型把正文写进一次合成函数调用的参数里，网关再还原成普通正文。用于绕开只扫描正文流的外部审查截断。",
    },
    {
        "key": "bypassMode",
        "type": "enum",
        "values": ["prefer", "required"],
        "value_labels": {
            "prefer": "就用它直接输出的正文",
            "required": "报错 502（用来排查预设）",
        },
        "default": "prefer",
        "group": "bypass",
        "label": "模型压根没调这个工具时",
        "desc": "报错模式便于确认预设有没有把控制指令压掉；日常用「就用它直接输出的正文」。",
        "depends_on": {"bypassEnabled": [True]},
        "advanced": True,
    },
    {
        "key": "bypassMergeOutside",
        "type": "bool",
        "default": True,
        "group": "bypass",
        "label": "合并写在工具外面的正文",
        "desc": "模型只把一部分放进工具、其余直接写出来时，把两边按顺序合并去重。关掉则只取工具里那份，可能丢内容。",
        "depends_on": {"bypassEnabled": [True]},
        "advanced": True,
    },
    {
        "key": "bypassInstructionPosition",
        "type": "enum",
        "values": ["tail", "system", "head"],
        "value_labels": {
            "tail": "对话末尾（最强）",
            "system": "合进系统指令",
            "head": "对话最前面",
        },
        "default": "tail",
        "group": "bypass",
        "label": "控制指令放哪",
        "desc": "控制指令就是告诉模型「通过这个工具输出」的那段话。",
        "depends_on": {"bypassEnabled": [True]},
        "advanced": True,
    },
    {
        "key": "bypassInstruction",
        "type": "text",
        "max_length": 4000,
        "default": "",
        "group": "bypass",
        "label": "自定义控制指令",
        "desc": "留空 = 用内置指令（互动式小说语境的投递指令）。`{tool}` 会替换成本次的工具名。",
        "depends_on": {"bypassEnabled": [True]},
        "advanced": True,
    },
    {
        "key": "sigSentinelThought",
        "type": "bool",
        "default": True,
        "group": "bypass",
        "label": "思考签名哨兵",
        "desc": (
            "把发往上游的思考签名一律换成官方逃生口 skip_thought_signature_validator。"
            "真签名是「这段内容由我生成」的凭据：原样回传时上游会校验后替换内容，"
            "表现就是撞过一次拒绝以后整段对话再也破不开、回滚对话又能过。默认开。"
        ),
    },
    {
        "key": "sigSentinelTool",
        "type": "bool",
        "default": True,
        "group": "bypass",
        "label": "工具签名哨兵",
        "desc": "把 functionCall part 上的签名（含 bypass 回放的那一份）同样换成哨兵值。默认开；要用真签名回放就关掉它。",
    },
    {
        "key": "bypassToolPrefix",
        "type": "text",
        "max_length": 32,
        "default": "emit_",
        "group": "bypass",
        "label": "合成工具名前缀",
        "desc": "每次请求都会在前缀后拼一段随机串，避免和客户端自己的工具重名。",
        "depends_on": {"bypassEnabled": [True]},
        "advanced": True,
    },
]

SCHEMA_BY_KEY: dict[str, dict[str, Any]] = {item["key"]: item for item in SCHEMA}
DEFAULTS: dict[str, Any] = {item["key"]: item["default"] for item in SCHEMA}


def _settings_path() -> Path:
    """设置文件位置：$AISTUDIO_SETTINGS_FILE 优先，否则 <data>/settings.json。"""
    env_path = os.getenv("AISTUDIO_SETTINGS_FILE")
    if env_path:
        return Path(env_path)
    return resolve_data_dir() / "settings.json"


def _coerce(item: dict[str, Any], raw: object) -> Any:
    """按 schema 把输入收窄成合法值；非法值抛 ValueError。"""
    key = item["key"]
    kind = item["type"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str) and raw.lower() in ("true", "1", "yes"):
            return True
        if isinstance(raw, str) and raw.lower() in ("false", "0", "no"):
            return False
        raise ValueError(f"{key}: 需要布尔值，得到 {raw!r}")
    if kind == "enum":
        if raw not in item["values"]:
            raise ValueError(f"{key}: 只接受 {item['values']}，得到 {raw!r}")
        return raw
    if kind in ("int?", "float?"):
        if raw is None or raw == "":
            return None
        try:
            value = int(raw) if kind == "int?" else float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key}: 需要数字，得到 {raw!r}") from exc
        low = item.get("min")
        high = item.get("max")
        if low is not None and value < low:
            value = low
        if high is not None and value > high:
            value = high
        return value
    if kind == "text":
        value = "" if raw is None else str(raw)
        limit = item.get("max_length")
        if limit is not None:
            value = value[: int(limit)]
        return value
    raise ValueError(f"{key}: 未知类型 {kind}")


class RuntimeSettings:
    """运行期设置的读写与持久化（原子写盘，进程内共享一份）。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _settings_path()
        self._lock = threading.RLock()
        self._values: dict[str, Any] = dict(DEFAULTS)
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        try:
            if not self._path.is_file():
                return
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("读取运行期设置失败（用默认值）: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            item = SCHEMA_BY_KEY.get(str(key))
            if item is None:
                continue
            try:
                self._values[str(key)] = _coerce(item, value)
            except ValueError as exc:
                log.warning("忽略非法设置项: %s", exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._values, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(payload + "\n", encoding="utf-8")
        tmp.replace(self._path)

    # ---------- 读写 ----------
    def values(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, DEFAULTS.get(key, default))

    def update(self, patch: dict[str, object]) -> dict[str, Any]:
        """按 schema 校验后合并、原子落盘。非法项整体拒绝（不写半套）。"""
        if not isinstance(patch, dict):
            raise ValueError("settings patch 必须是对象")
        validated: dict[str, Any] = {}
        for key, raw in patch.items():
            item = SCHEMA_BY_KEY.get(str(key))
            if item is None:
                raise ValueError(f"未知设置项: {key}")
            validated[str(key)] = _coerce(item, raw)
        with self._lock:
            self._values.update(validated)
            self._save()
        log.info("运行期设置已更新: %s", ", ".join(sorted(validated)))
        return self.values()

    def schema(self) -> dict[str, object]:
        """给 WebUI 的 schema + 当前值（带分组标题）。"""
        return {
            "groups": [
                {"key": group, "label": label} for group, label in GROUP_LABELS.items()
            ],
            "fields": [dict(item) for item in SCHEMA],
            "values": self.values(),
            "path": str(self._path),
        }


runtime_settings = RuntimeSettings()

# ---------- 语义 helper（消费方直接用，别再自己解释 schema） ----------


def prefill_enabled() -> bool:
    return bool(runtime_settings.get("prefillEnabled"))


def prefill_cot_guard() -> bool:
    return bool(runtime_settings.get("prefillCotGuard"))


def bypass_enabled() -> bool:
    return bool(runtime_settings.get("bypassEnabled"))


def sig_sentinel_thought() -> bool:
    return bool(runtime_settings.get("sigSentinelThought"))


def sig_sentinel_tool() -> bool:
    return bool(runtime_settings.get("sigSentinelTool"))


__all__ = [
    "DEFAULTS",
    "GROUP_LABELS",
    "SCHEMA",
    "RuntimeSettings",
    "bypass_enabled",
    "prefill_cot_guard",
    "prefill_enabled",
    "runtime_settings",
    "sig_sentinel_thought",
    "sig_sentinel_tool",
]
