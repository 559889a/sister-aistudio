"""Runtime configuration helpers."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()

DEFAULT_TEXT_MODEL = os.getenv("AISTUDIO_DEFAULT_TEXT_MODEL", "gemini-3.8-flash")
DEFAULT_IMAGE_MODEL = os.getenv(
    "AISTUDIO_DEFAULT_IMAGE_MODEL", "gemini-3.1-flash-image-preview"
)
DEFAULT_BROWSER_PORT = 9222


def _load_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def _load_bool_env(*names: str, default: bool) -> bool:
    value = _load_env(*names)
    if value is None:
        return default
    return value not in ("0", "false", "False")


def _load_int_env(*names: str, default: int) -> int:
    value = _load_env(*names)
    if value is None:
        return default
    return int(value)


def _load_web_password() -> str | None:
    return os.getenv("AISTUDIO_WEB_PASSWORD") or os.getenv("AISTUDIO_ADMIN_PASSWORD")


def _load_dump_requests_env() -> bool:
    """检查 DEBUG / AISTUDIO_DEBUG 环境变量是否启用请求转储。

    注意：此环境变量专用于触发请求详细信息转储 (Dump)，不影响日志输出级别。
    """
    val = _load_env("DEBUG", "AISTUDIO_DEBUG", "AISTUDIO_DUMP_REQUESTS")
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on", "dump", "y", "t")


def _load_dump_to_file_env() -> bool:
    """检查 AISTUDIO_DUMP_FILE / AISTUDIO_DUMP_TO_FILE 环境变量是否启用请求转储落盘。"""
    val = _load_env("AISTUDIO_DUMP_FILE", "AISTUDIO_DUMP_TO_FILE")
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on", "dump", "y", "t")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_TEMPLATE_CONFIG_PATHS = (
    _PROJECT_ROOT / "defaults" / "config.yaml",
    _DEFAULT_CONFIG_PATH,
)


def resolve_config_file(config_path: str | os.PathLike[str] | None = None) -> Path:
    """解析配置文件路径（优先使用 AISTUDIO_CONFIG_FILE 环境变量）。

    若目标路径配置文件不存在，则自动从内置配置模板初始化复制，绝不覆盖已有配置文件。
    """
    if config_path is not None:
        target = Path(config_path)
    else:
        override = os.getenv("AISTUDIO_CONFIG_FILE")
        target = Path(override) if override else _DEFAULT_CONFIG_PATH

    if not target.exists():
        for template in _TEMPLATE_CONFIG_PATHS:
            if template.is_file():
                try:
                    if template.resolve() == target.resolve():
                        break
                except OSError:
                    pass
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, target)
                    break
                except OSError:
                    pass
    return target


def resolve_data_dir() -> Path:
    """解析数据存储根目录（优先使用 AISTUDIO_DATA_DIR 环境变量）。"""
    override = os.getenv("AISTUDIO_DATA_DIR")
    if override:
        path = Path(override).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    _DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_DATA_DIR.resolve()


def resolve_stats_file() -> Path:
    """解析调用量与 Token 统计持久化文件路径（优先使用 AISTUDIO_STATS_FILE）。"""
    override = os.getenv("AISTUDIO_STATS_FILE")
    if override:
        path = Path(override).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return resolve_data_dir() / "stats.json"


def resolve_rotator_state_file() -> Path:
    """解析账号调度与 429 冷却状态持久化文件路径（优先使用 AISTUDIO_ROTATOR_STATE_FILE）。"""
    override = os.getenv("AISTUDIO_ROTATOR_STATE_FILE")
    if override:
        path = Path(override).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return resolve_data_dir() / "rotator_state.json"


def discover_auth_file() -> str | None:
    override = os.getenv("AISTUDIO_AUTH_FILE")
    if override:
        return override
    accounts_override = os.getenv("AISTUDIO_ACCOUNTS_DIR")
    data_override = os.getenv("AISTUDIO_DATA_DIR")
    data_dir = Path(data_override) if data_override else _DEFAULT_DATA_DIR
    accounts_dir = Path(accounts_override) if accounts_override else data_dir / "accounts"
    registry_path = accounts_dir / "registry.json"
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            active_id = registry.get("active_account_id")
            if active_id:
                auth_path = accounts_dir / active_id / "auth.json"
                if auth_path.is_file():
                    return str(auth_path)
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    legacy_auth = data_dir / "auth.json"
    return str(legacy_auth) if legacy_auth.is_file() else None


def discover_proxy_url() -> str | None:
    env_proxy = (
        os.getenv("AISTUDIO_PROXY")
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
    )
    if env_proxy:
        return env_proxy
    # 尝试自动探测本地代理（如本机跑着的 Clash / v2ray）
    import socket

    for port in (7890, 7897, 10808):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.05)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return None


def build_browser_proxy(proxy_url: str | None) -> dict[str, str] | None:
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname:
        return None

    proxy: dict[str, str] = {
        "server": f"{parsed.scheme}://{parsed.hostname}",
    }
    if parsed.port:
        proxy["server"] += f":{parsed.port}"
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def resolve_timezone() -> str:
    """解析系统或配置时区（优先使用 AISTUDIO_TIMEZONE 环境变量）。"""
    override = os.getenv("AISTUDIO_TIMEZONE")
    if override and override.strip():
        return override.strip()
    try:
        from datetime import datetime

        tz = datetime.now().astimezone().tzinfo
        if tz:
            tz_str = str(tz)
            if "/" in tz_str:
                return tz_str
    except Exception:
        pass
    return "America/Los_Angeles"


def resolve_locale() -> str:
    """解析系统或配置语言环境（优先使用 AISTUDIO_LOCALE 环境变量）。"""
    override = os.getenv("AISTUDIO_LOCALE")
    if override and override.strip():
        return override.strip()
    return "en-US"


def _resolve_default_idle_timeout() -> int:
    env_val = os.getenv("AISTUDIO_BROWSER_IDLE_TIMEOUT")
    if env_val is not None and env_val.strip() != "":
        return int(env_val)
    return 1800


@dataclass(slots=True)
class Settings:
    port: int = int(os.getenv("AISTUDIO_PORT", "8080"))
    # 监听地址：默认 0.0.0.0（对局域网/其它设备开放）。只想本机访问就设 AISTUDIO_HOST=127.0.0.1。
    host: str = os.getenv("AISTUDIO_HOST", "0.0.0.0")
    browser_port: int = _load_int_env(
        "AISTUDIO_BROWSER_PORT", default=DEFAULT_BROWSER_PORT
    )
    browser_headless: bool = _load_bool_env("AISTUDIO_BROWSER_HEADLESS", default=True)
    browser_executable_path: str | None = os.getenv("AISTUDIO_BROWSER_EXECUTABLE")
    browser_idle_timeout: int = _resolve_default_idle_timeout()
    auth_file: str | None = discover_auth_file()
    tmp_dir: str = os.getenv("AISTUDIO_TMP_DIR", tempfile.gettempdir())
    proxy_url: str | None = discover_proxy_url()
    web_password: str | None = _load_web_password()
    api_keys: frozenset[str] = frozenset()
    timezone: str = resolve_timezone()
    locale: str = resolve_locale()
    dump_raw_response: bool = os.getenv("AISTUDIO_DUMP_RAW_RESPONSE", "0") in (
        "1",
        "true",
        "True",
    )
    dump_raw_response_dir: str = os.getenv(
        "AISTUDIO_DUMP_RAW_RESPONSE_DIR", tempfile.gettempdir()
    )
    data_dir: str = str(resolve_data_dir())
    accounts_dir: str = os.getenv("AISTUDIO_ACCOUNTS_DIR", "")
    stats_file: str = str(resolve_stats_file())
    rotator_state_file: str = str(resolve_rotator_state_file())
    config_file: str = str(resolve_config_file())
    persist_stats: bool = _load_bool_env("AISTUDIO_PERSIST_STATS", default=True)
    persist_rotator: bool = _load_bool_env("AISTUDIO_PERSIST_ROTATOR", default=True)
    # ── 上游通道（2026-10-20 引入 vertex2openai 的两条上游；默认仍是浏览器通道）──
    upstream_mode: str = os.getenv("AISTUDIO_UPSTREAM", "aistudio").strip().lower() or "aistudio"
    vertex_express_api_key: str = os.getenv("VERTEX_EXPRESS_API_KEY", "")
    google_project_id: str = os.getenv("GOOGLE_PROJECT_ID", "")
    # 默认 global：无位置的 express 入口由 Google 路由到项目默认区（实测 asia-southeast1），
    # 新模型（4-argon 等）只在 global 发布，在那儿回与「模型名写错」分不开的 404
    # （build2api 2026-10-14 同 key A/B：不钉 404 / 钉 global 429）。钉位置需配合项目号。
    express_location: str = os.getenv("AISTUDIO_EXPRESS_LOCATION", "global")
    console_cookie: str = os.getenv("AISTUDIO_CONSOLE_COOKIE", "")
    console_project_id: str = os.getenv("AISTUDIO_CONSOLE_PROJECT_ID", "")
    dump_requests: bool = _load_dump_requests_env()
    dump_to_file: bool = _load_dump_to_file_env()
    dump_dir: str = os.getenv("AISTUDIO_DUMP_DIR", str(_PROJECT_ROOT / "dumps"))
    log_level: str = "INFO"

    @property
    def auth_enabled(self) -> bool:
        """网页管理端鉴权是否开启。"""
        return bool(self.web_password)


settings = Settings()
