"""Account rotation and sticky dispatch for multi-account management."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aistudio_api.config import resolve_rotator_state_file, settings
from aistudio_api.infrastructure.utils.common import atomic_write_json

if TYPE_CHECKING:
    from aistudio_api.infrastructure.account.account_store import (
        AccountMeta,
        AccountStore,
    )
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("rotator")

# 用户拍板（2026-10-20）：避免误杀——单账号/模型要累计第 5 次 429 才打入冷宫
# （冷却到美西午夜配额重置）；前 4 次只计数不冷却，网关不因此换号。
RATE_LIMITED_COOLDOWN_THRESHOLD = 5


def get_pacific_date_key(ts: float | None = None) -> str:
    """获取美西太平洋时间（America/Los_Angeles）日期键值 YYYY-MM-DD。"""
    try:
        tz = ZoneInfo("America/Los_Angeles")
        dt = datetime.fromtimestamp(time.time() if ts is None else ts, tz=tz)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        current_ts = time.time() if ts is None else ts
        dt = datetime.fromtimestamp(current_ts - 28800, tz=UTC)
        return dt.strftime("%Y-%m-%d")


def get_seconds_until_pacific_midnight() -> float:
    """获取距离下一次美西太平洋时间午夜 00:00 的秒数。"""
    try:
        tz = ZoneInfo("America/Los_Angeles")
        now_dt = datetime.now(tz)
        tomorrow_dt = (now_dt + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(0.0, tomorrow_dt.timestamp() - now_dt.timestamp())
    except Exception:
        # 保底计算
        now = time.time()
        pacific_offset = -28800  # UTC-8
        now_pacific = now + pacific_offset
        seconds_in_day = 86400
        remaining = seconds_in_day - (int(now_pacific) % seconds_in_day)
        return float(remaining)


def normalize_model_key(model: str | None) -> str | None:
    """统一模型标识（去除 models/ 前缀并转小写），确保单模型统计准确隔离。"""
    if not model:
        return None
    return model.removeprefix("models/").lower()


@dataclass
class AccountStats:
    """单账号的运行与配额统计。"""

    account_id: str
    requests: int = 0
    success: int = 0
    rate_limited: int = 0
    global_rate_limited_streak: int = 0
    errors: int = 0
    auth_errors: int = 0
    last_used: float = 0.0
    last_rate_limited: float = 0.0
    last_auth_error: float = 0.0
    auth_cooldown: float = 0.0
    session_expired: bool = False
    last_session_expired: float = 0.0
    rate_limited_date_la: str | None = None
    model_cooldowns: dict[str, float] = field(default_factory=dict)
    model_rate_limited_dates: dict[str, str] = field(default_factory=dict)
    model_requests: dict[str, int] = field(default_factory=dict)
    model_requests_dates: dict[str, str] = field(default_factory=dict)
    model_rate_limited: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "requests": self.requests,
            "success": self.success,
            "rate_limited": self.rate_limited,
            "global_rate_limited_streak": self.global_rate_limited_streak,
            "errors": self.errors,
            "auth_errors": self.auth_errors,
            "last_used": self.last_used,
            "last_rate_limited": self.last_rate_limited,
            "last_auth_error": self.last_auth_error,
            "auth_cooldown": self.auth_cooldown,
            "session_expired": self.session_expired,
            "last_session_expired": self.last_session_expired,
            "rate_limited_date_la": self.rate_limited_date_la,
            "model_cooldowns": dict(self.model_cooldowns),
            "model_rate_limited_dates": dict(self.model_rate_limited_dates),
            "model_requests": dict(self.model_requests),
            "model_requests_dates": dict(self.model_requests_dates),
            "model_rate_limited": dict(self.model_rate_limited),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> AccountStats:
        raw_cd = data.get("model_cooldowns")
        model_cooldowns: dict[str, float] = {}
        if isinstance(raw_cd, dict):
            for k, v in raw_cd.items():
                with contextlib.suppress(ValueError, TypeError):
                    model_cooldowns[str(k)] = float(str(v))

        raw_dates = data.get("model_rate_limited_dates")
        model_rate_limited_dates: dict[str, str] = {}
        if isinstance(raw_dates, dict):
            for k, v in raw_dates.items():
                model_rate_limited_dates[str(k)] = str(v)

        raw_reqs = data.get("model_requests")
        model_requests: dict[str, int] = {}
        if isinstance(raw_reqs, dict):
            for k, v in raw_reqs.items():
                with contextlib.suppress(ValueError, TypeError):
                    model_requests[str(k)] = int(str(v))

        raw_req_dates = data.get("model_requests_dates")
        model_requests_dates: dict[str, str] = {}
        if isinstance(raw_req_dates, dict):
            for k, v in raw_req_dates.items():
                model_requests_dates[str(k)] = str(v)

        raw_limits = data.get("model_rate_limited")
        model_rate_limited: dict[str, int] = {}
        if isinstance(raw_limits, dict):
            for k, v in raw_limits.items():
                with contextlib.suppress(ValueError, TypeError):
                    model_rate_limited[str(k)] = int(str(v))

        def _as_int(v: object) -> int:
            try:
                return int(str(v)) if v is not None else 0
            except (ValueError, TypeError):
                return 0

        def _as_float(v: object) -> float:
            try:
                return float(str(v)) if v is not None else 0.0
            except (ValueError, TypeError):
                return 0.0

        return cls(
            account_id=str(data.get("account_id") or ""),
            requests=_as_int(data.get("requests")),
            success=_as_int(data.get("success")),
            rate_limited=_as_int(data.get("rate_limited")),
            errors=_as_int(data.get("errors")),
            global_rate_limited_streak=_as_int(data.get("global_rate_limited_streak")),
            auth_errors=_as_int(data.get("auth_errors")),
            last_used=_as_float(data.get("last_used")),
            last_rate_limited=_as_float(data.get("last_rate_limited")),
            last_auth_error=_as_float(data.get("last_auth_error")),
            auth_cooldown=_as_float(data.get("auth_cooldown")),
            session_expired=bool(data.get("session_expired")),
            last_session_expired=_as_float(data.get("last_session_expired")),
            rate_limited_date_la=str(data["rate_limited_date_la"])
            if data.get("rate_limited_date_la")
            else None,
            model_cooldowns=model_cooldowns,
            model_rate_limited_dates=model_rate_limited_dates,
            model_requests=model_requests,
            model_requests_dates=model_requests_dates,
            model_rate_limited=model_rate_limited,
        )

    def _check_date_reset(self, model: str | None, current_la: str) -> None:
        """检查美西午夜跨天，自动重置单日请求计数与限流锁定。"""
        model = normalize_model_key(model)
        if self.rate_limited_date_la and self.rate_limited_date_la < current_la:
            self.rate_limited_date_la = None
            self.global_rate_limited_streak = 0
        if (
            self.last_rate_limited
            and get_pacific_date_key(self.last_rate_limited) < current_la
        ):
            self.global_rate_limited_streak = 0

        models = (
            {model} if model
            else set(self.model_requests_dates) | set(self.model_rate_limited_dates)
        )
        for key in models:
            req_date = self.model_requests_dates.get(key)
            if req_date and req_date < current_la:
                self.model_requests[key] = 0
                self.model_requests_dates[key] = current_la

            limit_date = self.model_rate_limited_dates.get(key)
            if limit_date and limit_date < current_la:
                self.model_rate_limited_dates.pop(key, None)
                self.model_cooldowns.pop(key, None)
                self.model_rate_limited.pop(key, None)

    def is_available(
        self, model: str | None = None, *, ignore_auth_cooldown: bool = False
    ) -> bool:
        """检查账号在指定模型下是否可用（美西 0 点自动刷新）。"""
        now = time.time()
        if self.session_expired:
            if self.auth_cooldown > now:
                return False
            self.session_expired = False
        if not ignore_auth_cooldown and self.auth_cooldown > now:
            return False
        model = normalize_model_key(model)
        current_la = get_pacific_date_key(now)
        self._check_date_reset(model, current_la)

        # 检查全局 429 标记
        if self.rate_limited_date_la:
            return False

        if model:
            cd = self.model_cooldowns.get(model, 0.0)
            return now >= cd

        return True

    def get_cooldown_remaining(self, model: str | None = None) -> float:
        """获取距离配额刷新的剩余秒数。"""
        model = normalize_model_key(model)
        if self.is_available(model):
            return 0.0
        now = time.time()
        if self.auth_cooldown > now:
            return max(0.0, self.auth_cooldown - now)
        if model:
            cd = self.model_cooldowns.get(model, 0.0)
            if cd > now:
                return max(0.0, cd - now)
        return get_seconds_until_pacific_midnight()

    def record_success(self, model: str | None = None) -> None:
        now = time.time()
        self.requests += 1
        self.success += 1
        self.last_used = now
        self.auth_errors = 0
        self.auth_cooldown = 0.0
        model = normalize_model_key(model)
        self._check_date_reset(model, get_pacific_date_key(now))
        self.global_rate_limited_streak = 0
        self.rate_limited_date_la = None
        if model:
            la_date = get_pacific_date_key(now)
            self.model_requests[model] = self.model_requests.get(model, 0) + 1
            self.model_requests_dates[model] = la_date
            self.model_cooldowns.pop(model, None)
            self.model_rate_limited_dates.pop(model, None)
            # 成功一次就清零该模型的 429 计数（防误杀：抖动性 429 不累积进冷宫）
            self.model_rate_limited.pop(model, None)

    def record_rate_limited(self, model: str | None = None) -> None:
        """记一次 429：只计数；第 5 次（含）起才冷却到美西午夜（打入冷宫）。

        用户拍板（2026-10-20）：前 4 次不冷却——网关不因此换号（防误杀）；
        第 5 次把该账号/模型打入冷宫，直到美西午夜配额重置。
        """
        now = time.time()
        self.requests += 1
        self.rate_limited += 1
        self._check_date_reset(model, get_pacific_date_key(now))
        self.last_rate_limited = now
        self.last_used = now
        la_date = get_pacific_date_key(now)
        model = normalize_model_key(model)

        if model:
            limit_count = self.model_rate_limited.get(model, 0) + 1
            self.model_requests[model] = self.model_requests.get(model, 0) + 1
            self.model_requests_dates[model] = la_date
            self.model_rate_limited[model] = limit_count
            self.model_rate_limited_dates[model] = la_date
            if limit_count >= RATE_LIMITED_COOLDOWN_THRESHOLD:
                self.model_cooldowns[model] = now + get_seconds_until_pacific_midnight()
        else:
            self.global_rate_limited_streak += 1
            if self.global_rate_limited_streak >= RATE_LIMITED_COOLDOWN_THRESHOLD:
                self.rate_limited_date_la = la_date

    def record_error(self, model: str | None = None) -> None:
        self.requests += 1
        self.errors += 1
        now = time.time()
        self.last_used = now
        model = normalize_model_key(model)
        self._check_date_reset(model, get_pacific_date_key(now))
        if model:
            self.model_requests[model] = self.model_requests.get(model, 0) + 1
            self.model_requests_dates[model] = get_pacific_date_key(now)

    def record_session_expired(self, model: str | None = None) -> None:
        """记录账号登录态失效，隔离 24 小时或直至用户重置/重新导入。"""
        now = time.time()
        self.requests += 1
        self.errors += 1
        self.auth_errors += 1
        self.session_expired = True
        self.last_session_expired = now
        self.last_auth_error = now
        self.last_used = now
        # 隔离 24 小时或直至用户重新导入/手动重置
        self.auth_cooldown = now + 86400.0
        model = normalize_model_key(model)
        self._check_date_reset(model, get_pacific_date_key(now))
        if model:
            self.model_requests[model] = self.model_requests.get(model, 0) + 1
            self.model_requests_dates[model] = get_pacific_date_key(now)

    def clear_cooldown(self, model: str | None = None) -> None:
        """手动清除冷却与 429/403/失效锁定。"""
        self.auth_cooldown = 0.0
        self.auth_errors = 0
        self.session_expired = False
        self.last_session_expired = 0.0
        model = normalize_model_key(model)
        if model:
            self.model_cooldowns.pop(model, None)
            self.model_rate_limited_dates.pop(model, None)
            self.model_rate_limited.pop(model, None)
        else:
            self.rate_limited_date_la = None
            self.global_rate_limited_streak = 0
            self.model_cooldowns.clear()
            self.model_rate_limited_dates.clear()
            self.model_rate_limited.clear()


class AccountRotator:
    """黏性账号调度管理器。

    默认保持当前激活账号，直到特定模型遇到 429 配额耗尽时，
    自动切换到该模型今日仍有额度的下一个健康账号。
    """

    def __init__(self, account_store: AccountStore) -> None:
        self._store = account_store
        self._stats: dict[str, AccountStats] = {}
        self._lock = asyncio.Lock()
        self._last_saved_time: float = 0.0
        self._save_task: asyncio.Task | None = None
        self.load_state()
        self.cleanup_ghost_accounts()
        for account in self._store.list_accounts():
            if account.id not in self._stats:
                self._stats[account.id] = AccountStats(account_id=account.id)

    def load_state(self) -> None:
        """从持久化文件恢复账号统计与 429 锁定状态。"""
        if not settings.persist_rotator:
            return
        state_file = resolve_rotator_state_file()
        if not state_file.is_file():
            return
        try:
            raw = state_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                for acc_id, item in data.items():
                    if isinstance(item, dict):
                        self._stats[str(acc_id)] = AccountStats.from_dict(item)
                logger.info(
                    "已从 %s 恢复 %d 个账号的运行与限额状态",
                    state_file,
                    len(self._stats),
                )
        except Exception as e:
            logger.warning("从 %s 读取账号调度状态失败: %s", state_file, e)

    def save_state(self) -> None:
        """将账号调度统计与 429 冷却状态持久化到文件。"""
        if not settings.persist_rotator:
            return
        state_file = resolve_rotator_state_file()
        try:
            payload = {acc_id: stats.to_dict() for acc_id, stats in self._stats.items()}
            atomic_write_json(state_file, payload)
            self._last_saved_time = time.time()
        except Exception as e:
            logger.warning("持久化账号调度状态到 %s 失败: %s", state_file, e)

    def _schedule_debounced_save(self, delay: float = 2.0) -> None:
        if not settings.persist_rotator:
            return
        now = time.time()
        if now - self._last_saved_time >= 5.0:
            self.save_state()
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save_state()
            return

        if self._save_task is not None and not self._save_task.done():
            return

        async def _delayed():
            await asyncio.sleep(delay)
            self.save_state()

        self._save_task = loop.create_task(_delayed())

    def get_all_stats(self) -> dict[str, dict[str, object]]:
        """获取所有账号的运行与配额状态。"""
        result: dict[str, dict[str, object]] = {}
        current_la = get_pacific_date_key()
        for account in self._store.list_accounts():
            stats = self._stats.get(account.id, AccountStats(account_id=account.id))
            stats._check_date_reset(None, current_la)
            result[account.id] = {
                "name": account.name,
                "email": account.email,
                "tier": getattr(account, "tier", "Free") or "Free",
                "requests": stats.requests,
                "success": stats.success,
                "rate_limited": stats.rate_limited,
                "errors": stats.errors,
                "last_used": (
                    datetime.fromtimestamp(stats.last_used, tz=UTC).isoformat()
                    if stats.last_used
                    else None
                ),
                "last_rate_limited": (
                    datetime.fromtimestamp(stats.last_rate_limited, tz=UTC).isoformat()
                    if stats.last_rate_limited
                    else None
                ),
                "is_available": stats.is_available(),
                "session_expired": stats.session_expired,
                "auth_cooldown": max(0, int(stats.auth_cooldown - time.time()))
                if stats.auth_cooldown > time.time()
                else 0,
                "auth_errors": stats.auth_errors,
                "cooldown_remaining": int(stats.get_cooldown_remaining()),
                "model_cooldowns": {
                    m: int(stats.get_cooldown_remaining(m))
                    for m in (
                        set(stats.model_cooldowns.keys())
                        | {
                            k
                            for k, dt in stats.model_rate_limited_dates.items()
                            if dt == current_la
                        }
                    )
                    if not stats.is_available(m)
                    and int(stats.get_cooldown_remaining(m)) > 0
                },
                "model_rate_limited_dates": dict(stats.model_rate_limited_dates),
                "model_requests": dict(stats.model_requests),
                "model_rate_limited": dict(stats.model_rate_limited),
            }
        return result

    def get_account_stats(self, account_id: str) -> AccountStats | None:
        """获取指定账号的统计对象（若账号已存在于存储层但尚未登记，自动补齐默认统计）。"""
        stats = self._stats.get(account_id)
        if stats is None and any(
            a.id == account_id for a in self._store.list_accounts()
        ):
            stats = AccountStats(account_id=account_id)
            self._stats[account_id] = stats
        return stats

    def is_account_available(self, account_id: str, model: str | None = None) -> bool:
        """检查指定账号在特定模型下是否健康可用。"""
        stats = self.get_account_stats(account_id)
        if stats is None:
            # 刚导入/刚启动、还没跑过任何请求的账号没有统计对象。这里必须按
            # 「未限流」处理：返回 False 会让编排器把每个账号的**第一次**请求
            # 误判成「当前账号不可用」，触发一次强制重建会话（实测第一次请求
            # 就这样白丢 40 秒并失败）。
            return True
        return stats.is_available(model)

    def _get_available_accounts(
        self, model: str | None = None, *, ignore_auth_cooldown: bool = False
    ) -> list[tuple[AccountMeta, AccountStats]]:
        """获取在指定模型下尚未耗尽当日额度且未被鉴权锁定的账号列表。"""
        accounts = self._store.list_accounts()
        available: list[tuple[AccountMeta, AccountStats]] = []
        for account in accounts:
            stats = self._stats.setdefault(
                account.id, AccountStats(account_id=account.id)
            )
            if stats.is_available(model, ignore_auth_cooldown=ignore_auth_cooldown):
                available.append((account, stats))
        if model and available:
            try:
                from aistudio_api.infrastructure.gateway.model_discovery import (
                    model_discovery,
                )

                tier_matched = [
                    (acc, st)
                    for acc, st in available
                    if model_discovery.is_model_allowed_for_tier(
                        model, getattr(acc, "tier", "Free")
                    )
                ]
                if tier_matched:
                    return tier_matched
            except Exception:
                pass
        return available

    async def get_next_account(
        self,
        model: str | None = None,
        current_account_id: str | None = None,
        failed_account_id: str | None = None,
    ) -> AccountMeta | None:
        """获取下一个可用账号（排除失败账号，优先平滑故障转移至健康账号）。"""
        async with self._lock:
            available = self._get_available_accounts(model)
            if not available:
                # 保底：若所有账号都处于 auth_cooldown 中，允许 fallback 尝试恢复
                available = self._get_available_accounts(
                    model, ignore_auth_cooldown=True
                )
                if not available:
                    return None

            # 优先在排除失败账号的候选集中挑选
            candidates = [
                (a, s)
                for a, s in available
                if not (failed_account_id and a.id == failed_account_id)
            ]
            if candidates:
                # 黏性策略：如果当前账号未失败且在候选列表中，继续复用
                if current_account_id and not (
                    failed_account_id and current_account_id == failed_account_id
                ):
                    for a, _ in candidates:
                        if a.id == current_account_id:
                            return a
                # 故障转移：优先用该模型与总使用次数少的充沛额度健康账号
                norm_m = normalize_model_key(model)
                account, _ = min(
                    candidates,
                    key=lambda x: (
                        x[1].model_requests.get(norm_m, 0) if norm_m else 0,
                        x[1].requests,
                        x[1].errors,
                        x[1].last_used,
                    ),
                )
                if current_account_id and account.id != current_account_id:
                    logger.info("账号故障转移切换: %s (model=%s)", account.name, model)
                return account
            if failed_account_id:
                for a, _ in available:
                    if a.id == failed_account_id:
                        return a

            return available[0][0]

    def record_success(self, account_id: str, model: str | None = None) -> None:
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        self._stats[account_id].record_success(model)
        self._schedule_debounced_save()

    def record_rate_limited(
        self,
        account_id: str,
        model: str | None = None,
    ) -> None:
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        stats = self._stats[account_id]
        stats.record_rate_limited(model)
        norm_m = normalize_model_key(model)
        count = (
            stats.model_rate_limited.get(norm_m, 0)
            if norm_m else stats.global_rate_limited_streak
        )
        logger.warning(
            "账号 %s 在模型 %s 第 %d 次 429（累计 %d 次打入冷宫）",
            account_id,
            model or "all",
            count,
            RATE_LIMITED_COOLDOWN_THRESHOLD,
        )

        self.save_state()

    def record_error(self, account_id: str, model: str | None = None) -> None:
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        self._stats[account_id].record_error(model)
        self.save_state()

    def record_session_expired(
        self,
        account_id: str,
        model: str | None = None,
    ) -> None:
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        self._stats[account_id].record_session_expired(model)
        logger.error("账号 %s 登录态失效，已隔离 24 小时或直至重新导入凭据", account_id)
        self.save_state()

    def clear_cooldown(self, account_id: str, model: str | None = None) -> None:
        """清除指定账号的 429 锁定。"""
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)
        self._stats[account_id].clear_cooldown(model)
        logger.info("已手动清除账号 %s 锁定 (model=%s)", account_id, model)

        self.save_state()

    def clear_all_cooldowns(self) -> None:
        """清除全部账号的所有模型 429 锁定。"""
        for stats in self._stats.values():
            stats.clear_cooldown()
        logger.info("已手动清除全部账号的 429 锁定状态")

        self.save_state()

    def add_account(self, account_id: str) -> None:
        if account_id not in self._stats:
            self._stats[account_id] = AccountStats(account_id=account_id)

        self.save_state()

    def remove_account(self, account_id: str) -> None:
        self._stats.pop(account_id, None)

        self.save_state()

    def cleanup_ghost_accounts(self) -> list[str]:
        """清理已从 AccountStore 中删除但仍残留在 rotator_state 中的幽灵账号/Cookie。"""
        existing_ids = {a.id for a in self._store.list_accounts()}
        ghost_ids = [
            acc_id for acc_id in list(self._stats.keys()) if acc_id not in existing_ids
        ]
        for gid in ghost_ids:
            self._stats.pop(gid, None)
        if ghost_ids:
            logger.info(
                "已清理 %d 个幽灵账号/Cookie 残留状态: %s", len(ghost_ids), ghost_ids
            )
            self.save_state()
        return ghost_ids


_rotator: AccountRotator | None = None


def init_rotator(account_store: AccountStore) -> AccountRotator:
    global _rotator
    _rotator = AccountRotator(account_store)
    return _rotator
