"""Account failover and concurrency orchestration."""

from __future__ import annotations

import asyncio
import time

from aistudio_api.api.state import runtime_state
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("orchestrator")

_switch_lock: asyncio.Lock | None = None
_switch_lock_loop: asyncio.AbstractEventLoop | None = None
_last_switch_time: float = 0.0


def _get_switch_lock() -> asyncio.Lock:
    global _switch_lock, _switch_lock_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _switch_lock is None or _switch_lock_loop is not loop:
        _switch_lock = asyncio.Lock()
        _switch_lock_loop = loop
    return _switch_lock


async def try_switch_account(
    model: str | None = None,
    failed_account_id: str | None = None,
) -> bool:
    """尝试切换到下一个对目标 model 可用的账号。防止并发级联切号。

    调用方只有 ``ensure_active_account``（请求前置调度）：当前账号因 429 冷宫或
    登录态失效不可用时挑一个健康的。失败标记（429/SessionExpired）由
    ``handle_attempt_exception`` 直接记账，这里不再重复记录。
    """
    global _last_switch_time
    entry_time = time.time()

    # 乐观快速预检：当前活跃账号已被并发请求切换至健康状态时，直接免锁复用
    if failed_account_id:
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        if rotator is not None and account_service is not None:
            cur_active = account_service.get_active_account()
            if (
                cur_active
                and (
                    cur_active.id != failed_account_id
                    or _last_switch_time >= entry_time
                )
                and rotator.is_account_available(cur_active.id, model=model)
            ):
                return True

    async with _get_switch_lock():
        rotator = runtime_state.rotator
        account_service = runtime_state.account_service
        client = runtime_state.client

        if (
            rotator is None
            or account_service is None
            or client is None
            or client.session is None
        ):
            return False

        current_active = account_service.get_active_account()
        current_id = current_active.id if current_active else None

        # 双重检查 1：如果已有并发协程在 entry_time 之后完成切号，且当前账号对该模型可用，则直接复用
        if (
            _last_switch_time >= entry_time
            and current_id
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "已有并发请求完成切号，直接复用当前健康账号: %s (model=%s)",
                current_id,
                model,
            )
            return True

        # 双重检查 2：如果已经由并发协程切换到了新账号，且新账号对当前 model 可用，则直接复用
        if (
            failed_account_id
            and current_id
            and current_id != failed_account_id
            and rotator.is_account_available(current_id, model=model)
        ):
            logger.info(
                "已有并发请求完成切号，直接复用当前健康账号: %s (model=%s)",
                current_id,
                model,
            )
            return True

        next_account = await rotator.get_next_account(
            model=model,
            current_account_id=current_id,
            failed_account_id=failed_account_id,
        )
        if next_account is None:
            return False

        if current_id is None or next_account.id != current_id:
            result = await account_service.activate_account(
                next_account.id,
                client.session,
            )
            if result is not None:
                rotator.clear_cooldown(next_account.id, model=model)
                _last_switch_time = time.time()
            return result is not None

        # 单账号模式：当前账号就是唯一候选（429 冷宫到期/手动清零后走到这），
        # 刷新会话与 BotGuard 让下一个请求拿到干净页面
        if failed_account_id and failed_account_id == current_id:
            logger.info(
                "无其他可用备用账号，重新刷新当前账号会话与 BotGuard: %s",
                current_id,
            )
            client.clear_templates()
            result = await account_service.activate_account(
                current_id,
                client.session,
            )
            if result is not None:
                rotator.clear_cooldown(current_id, model=model)
                _last_switch_time = time.time()
            return result is not None
        return bool(
            current_id and rotator.is_account_available(current_id, model=model)
        )


async def ensure_active_account(model: str | None = None) -> None:
    """确保存在活跃账号且对目标模型可用（**请求前置调度**，不是失败重试）。

    只有 429 冷却（第 5 次打入冷宫）和登录态失效会让账号「不可用」——
    403 等浏览器故障不记账，不会触发这里的换号。
    """
    account_svc = runtime_state.account_service
    rotator = runtime_state.rotator
    current = account_svc.get_active_account() if account_svc else None
    if not current:
        await try_switch_account(model=model)
    elif (
        rotator and model and not rotator.is_account_available(current.id, model=model)
    ):
        # 当前账号对该模型已限流，提前切号
        await try_switch_account(model=model, failed_account_id=current.id)


def record_rotator_event(
    event: str,
    model: str | None = None,
    *,
    account_id: str | None = None,
) -> None:
    """记录调度器事件（成功、限流、错误、鉴权异常、登录态失效）。

    ``account_id`` 是给 pool 模式用的：一次请求可能落在任意 worker 自己的账号上，
    不能拿「全局活跃账号」顶替。单浏览器路径不传，行为不变。
    """
    rotator = runtime_state.rotator
    account_service = runtime_state.account_service
    account = None
    if account_id and account_service is not None:
        account = account_service.get_account(account_id)
    if account is None and account_service is not None:
        account = account_service.get_active_account()
    if not rotator or account is None:
        return
    if event == "success":
        rotator.record_success(account.id, model=model)
    elif event == "rate_limited":
        rotator.record_rate_limited(account.id, model=model)
    elif event == "session_expired":
        rotator.record_session_expired(account.id, model=model)
    elif event == "error":
        rotator.record_error(account.id, model=model)


__all__ = [
    "ensure_active_account",
    "record_rotator_event",
    "try_switch_account",
]
