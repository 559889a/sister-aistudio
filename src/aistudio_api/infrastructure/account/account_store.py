"""账号存储层，管理多 Google 账号的注册表和 storage state。"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from os.path import basename, join, realpath
from pathlib import Path
from typing import ClassVar


def _atomic_write_json(path: Path, data: object) -> None:
    """原子写入 JSON 文件（写唯一临时文件后原子替换，防止写穿或损坏）。"""
    target_path = path.resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(f".tmp.{os.getpid()}_{time.time_ns()}")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp_path.replace(target_path)
    except Exception:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise


# 默认搜索路径（与 config.py 保持一致）
_SEARCH_ROOTS: list[Path] = [
    Path.cwd(),
    Path(__file__)
    .resolve()
    .parents[4],  # src/aistudio_api/infrastructure/account -> 项目根
]


def _safe_account_dir(accounts_dir: Path, account_id: str) -> Path:
    """验证并返回安全的账号子目录路径，防止路径遍历攻击。"""
    raw = str(account_id or "").strip()
    clean_id = "".join(c for c in raw if c.isalnum() or c in ("-", "_", ".", "@"))
    safe_name = basename(clean_id)
    if (
        not safe_name
        or safe_name != account_id
        or safe_name in (".", "..")
        or ".." in safe_name
    ):
        raise ValueError(f"Invalid or unsafe account_id: {account_id!r}")
    base_str = realpath(str(accounts_dir))
    target_str = realpath(join(base_str, safe_name))
    prefix = base_str if base_str.endswith(os.sep) else base_str + os.sep
    if not target_str.startswith(prefix):
        raise ValueError(f"Account path escapes base directory: {account_id!r}")
    return Path(target_str)


def _resolve_accounts_dir() -> Path:
    """发现 accounts 目录，默认为 data/accounts。"""
    env = os.getenv("AISTUDIO_ACCOUNTS_DIR")
    if env:
        return Path(env).resolve()
    for root in _SEARCH_ROOTS:
        candidate = root / "data" / "accounts"
        if candidate.is_dir():
            return candidate
    # 默认在第一个搜索根下创建
    return (_SEARCH_ROOTS[0] / "data" / "accounts").resolve()


def _resolve_legacy_auth_file() -> Path | None:
    """查找遗留的 data/auth.json 文件。"""
    for root in _SEARCH_ROOTS:
        candidate = root / "data" / "auth.json"
        if candidate.is_file():
            return candidate
    return None


def _generate_account_id() -> str:
    """生成 acc_ 前缀的随机 ID。"""
    import secrets

    return f"acc_{secrets.token_hex(4)}"


@dataclass
class AccountMeta:
    """账号元数据。"""

    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None = None
    auth_user: str = "0"
    cookie_id: str | None = None
    tier: str = "Free"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AccountMeta:
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            email=str(data["email"]) if data.get("email") is not None else None,
            created_at=str(data.get("created_at") or ""),
            last_used=str(data["last_used"])
            if data.get("last_used") is not None
            else None,
            auth_user=str(data.get("auth_user") or "0"),
            cookie_id=str(data["cookie_id"])
            if data.get("cookie_id") is not None
            else None,
            tier=str(data.get("tier") or "Free"),
        )


@dataclass
class Registry:
    """账号注册表。"""

    accounts: dict[str, AccountMeta]
    active_account_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "accounts": {k: v.to_dict() for k, v in self.accounts.items()},
            "active_account_id": self.active_account_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Registry:
        raw_accounts = data.get("accounts")
        accounts_dict: dict[str, object] = (
            raw_accounts if isinstance(raw_accounts, dict) else {}
        )
        accounts = {
            k: AccountMeta.from_dict(v)
            for k, v in accounts_dict.items()
            if isinstance(v, dict)
        }
        raw_active_id = data.get("active_account_id")
        return cls(
            accounts=accounts,
            active_account_id=str(raw_active_id) if raw_active_id is not None else None,
        )


class AccountStore:
    """账号存储管理器（线程安全单例，支持原子文件替换）。"""

    _instances: ClassVar[dict[Path, AccountStore]] = {}
    _singleton_lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls, accounts_dir: Path | None = None) -> AccountStore:
        resolved_dir = (accounts_dir or _resolve_accounts_dir()).resolve()
        with cls._singleton_lock:
            if resolved_dir not in cls._instances:
                instance = super().__new__(cls)
                cls._instances[resolved_dir] = instance
            return cls._instances[resolved_dir]

    def __init__(self, accounts_dir: Path | None = None) -> None:
        if getattr(self, "_initialized", False):
            return
        self._accounts_dir = (accounts_dir or _resolve_accounts_dir()).resolve()
        self._registry_path = self._accounts_dir / "registry.json"
        self._registry: Registry | None = None
        self._lock = threading.Lock()
        self._ensure_dirs()
        self._migrate_legacy_if_needed()
        self._initialized = True

    def _ensure_dirs(self) -> None:
        """确保目录存在。"""
        self._accounts_dir.mkdir(parents=True, exist_ok=True)

    def _migrate_legacy_if_needed(self) -> None:
        """如果 accounts 目录为空且存在 data/auth.json，自动迁移。"""
        if self._registry_path.exists():
            return  # 已有注册表，无需迁移
        legacy = _resolve_legacy_auth_file()
        if legacy is None:
            return
        # 创建一个迁移账号
        account_id = "acc_migrated"
        now = datetime.now(UTC).isoformat()
        meta = AccountMeta(
            id=account_id,
            name="迁移的账号",
            email=None,
            created_at=now,
            last_used=now,
        )
        account_dir = self._accounts_dir / account_id
        account_dir.mkdir(parents=True, exist_ok=True)
        # 复制 auth.json
        shutil.copy2(legacy, account_dir / "auth.json")
        # 写入 meta.json
        _atomic_write_json(account_dir / "meta.json", meta.to_dict())
        # 创建注册表
        registry = Registry(
            accounts={account_id: meta},
            active_account_id=account_id,
        )
        self._save_registry(registry)

    def _load_registry(self) -> Registry:
        """加载注册表（线程安全）。"""
        with self._lock:
            if self._registry is not None:
                return self._registry
            if not self._registry_path.exists():
                self._registry = Registry(accounts={})
                return self._registry
            try:
                data = json.loads(self._registry_path.read_text(encoding="utf-8"))
                self._registry = Registry.from_dict(data)
                reconciled = False
                for acc_id, acc in self._registry.accounts.items():
                    account_dir = self._accounts_dir / acc_id
                    meta_path = account_dir / "meta.json"
                    if meta_path.is_file():
                        try:
                            mdata = json.loads(meta_path.read_text(encoding="utf-8"))
                            if isinstance(mdata, dict):
                                if not acc.email and mdata.get("email"):
                                    acc.email = str(mdata["email"])
                                    reconciled = True
                                if (
                                    (not acc.tier or acc.tier == "Free")
                                    and mdata.get("tier")
                                    and mdata.get("tier") != "Free"
                                ):
                                    acc.tier = str(mdata["tier"])
                                    reconciled = True
                        except Exception:
                            pass
                if reconciled:
                    _atomic_write_json(self._registry_path, self._registry.to_dict())
            except Exception:
                self._registry = Registry(accounts={})
            return self._registry

    def _save_registry(self, registry: Registry) -> None:
        """保存注册表（线程安全+原子写入）。"""
        with self._lock:
            self._registry = registry
            _atomic_write_json(self._registry_path, registry.to_dict())

    def list_accounts(self) -> list[AccountMeta]:
        """列出所有账号。"""
        registry = self._load_registry()
        return list(registry.accounts.values())

    def _find_registered_account(
        self, registry: Registry, account_id: str
    ) -> AccountMeta | None:
        for stored_id, meta in registry.accounts.items():
            if stored_id == account_id:
                return meta
        return None

    def get_account(self, account_id: str) -> AccountMeta | None:
        """获取单个账号。"""
        registry = self._load_registry()
        return self._find_registered_account(registry, account_id)

    def get_active_account(self) -> AccountMeta | None:
        """获取当前活跃账号（若未设置或失效，自动兜底至首个账号）。"""
        registry = self._load_registry()
        if (
            registry.active_account_id
            and registry.active_account_id in registry.accounts
        ):
            return registry.accounts[registry.active_account_id]
        if registry.accounts:
            first_id = next(iter(registry.accounts))
            registry.active_account_id = first_id
            self._save_registry(registry)
            return registry.accounts[first_id]
        return None

    def get_active_auth_path(self) -> Path | None:
        """获取当前活跃账号的 auth.json 路径。"""
        account = self.get_active_account()
        if account is None:
            return None
        return self._accounts_dir / account.id / "auth.json"

    def set_active_account(self, account_id: str) -> AccountMeta | None:
        """设置活跃账号，返回账号元数据或 None（如果不存在）。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        changed = registry.active_account_id != acc.id
        registry.active_account_id = acc.id
        now = datetime.now(UTC).isoformat()
        acc.last_used = now
        if changed:
            self._save_registry(registry)
        return acc

    def save_account(
        self,
        name: str,
        email: str | None,
        storage_state: dict[str, object],
        account_id: str | None = None,
        auth_user: str = "0",
        cookie_id: str | None = None,
        tier: str = "Free",
    ) -> AccountMeta:
        """保存新账号。"""
        registry = self._load_registry()
        now = datetime.now(UTC).isoformat()
        created_at = now
        resolved_tier = tier or "Free"
        if account_id is None and email:
            for acc in registry.accounts.values():
                if acc.email == email:
                    account_id = acc.id
                    break

        if account_id is not None:
            existing = self._find_registered_account(registry, account_id)
            if existing is not None:
                account_id = existing.id
                created_at = existing.created_at
                email = email or existing.email
                if resolved_tier == "Free" and existing.tier != "Free":
                    resolved_tier = existing.tier
        else:
            account_id = _generate_account_id()

        account_dir = _safe_account_dir(self._accounts_dir, account_id)
        safe_id = account_dir.name
        meta = AccountMeta(
            id=safe_id,
            name=name,
            email=email,
            created_at=created_at,
            last_used=now,
            auth_user=auth_user,
            cookie_id=cookie_id,
            tier=resolved_tier,
        )
        account_dir.mkdir(parents=True, exist_ok=True)
        # 原子写入 auth.json 与 meta.json
        _atomic_write_json(account_dir / "auth.json", storage_state)
        _atomic_write_json(account_dir / "meta.json", meta.to_dict())
        # 更新注册表
        registry.accounts[safe_id] = meta
        if registry.active_account_id is None:
            registry.active_account_id = safe_id
        self._save_registry(registry)
        return meta

    def delete_account(self, account_id: str) -> bool:
        """删除账号，返回是否成功。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return False
        safe_id = acc.id
        # 删除目录
        account_dir = _safe_account_dir(self._accounts_dir, safe_id)
        if account_dir.is_dir():
            shutil.rmtree(account_dir, ignore_errors=True)
        # 从注册表移除
        del registry.accounts[safe_id]
        if registry.active_account_id == safe_id:
            registry.active_account_id = next(iter(registry.accounts), None)
        self._save_registry(registry)
        return True

    def update_account(self, account_id: str, name: str) -> AccountMeta | None:
        """更新账号名称。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        acc.name = name
        # 同步更新 meta.json
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return acc

    def update_account_email(self, account_id: str, email: str) -> bool:
        """更新账号的登录邮箱，并持久化 meta.json 与 registry.json。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None or acc.email == email:
            return False
        acc.email = email
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return True

    def update_account_tier(self, account_id: str, tier: str) -> bool:
        """更新账号的订阅权益等级 (Free / Pro / Ultra / Plus)，并持久化 meta.json 与 registry.json。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return False
        clean_tier = (tier or "Free").strip().capitalize()
        if clean_tier not in ("Free", "Pro", "Ultra", "Plus"):
            clean_tier = "Free"
        if acc.tier == clean_tier:
            return False
        acc.tier = clean_tier
        account_dir = _safe_account_dir(self._accounts_dir, acc.id)
        meta_path = account_dir / "meta.json"
        if meta_path.exists():
            _atomic_write_json(meta_path, acc.to_dict())
        self._save_registry(registry)
        return True

    def get_auth_path(self, account_id: str) -> Path | None:
        """获取指定账号的 auth.json 路径。"""
        return self.get_auth_path_optional(account_id, require_exists=True)

    def get_auth_path_optional(
        self, account_id: str, *, require_exists: bool = False
    ) -> Path | None:
        """获取指定账号的 auth.json 路径，可选是否要求文件已存在。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        path = _safe_account_dir(self._accounts_dir, acc.id) / "auth.json"
        if require_exists and not path.exists():
            return None
        return path

    def get_profile_path(self, account_id: str) -> Path | None:
        """获取指定账号的 profile 目录路径。"""
        registry = self._load_registry()
        acc = self._find_registered_account(registry, account_id)
        if acc is None:
            return None
        return _safe_account_dir(self._accounts_dir, acc.id) / "profile"
