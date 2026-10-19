"""固定 N 台 Chromium 的 worker 池（负载均衡 + 失败自愈）。

用户要求（2026-10-19）：
- 固定 2 台浏览器、每台独立 CDP 端口（9222/9223）和独立 profile 目录，
  **两台都接请求**（不是纯热备），按 least-inflight 分发；
- 403/401 / 5xx / 502 / 页面 fetch-XHR 传输失败时，**当前 worker 后台
  kill + 重启 Chromium**，这次请求在还没给客户端吐字节时**立刻切到另一台**
  重试（不等重启完成）；这些失败**不给账号加冷却**——它们不是上游的配额问题；
- 429 才保留账号/模型的配额冷却；
- 登录态失效（真的被重定向到登录页，SessionExpiredError）仍按账号隔离。

单浏览器路径（``AISTUDIO_BROWSER_POOL_SIZE=1``）完全走旧的 ``AIStudioClient``，
本模块只在 pool_size>1 时由 lifespan 构造。
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import Any

from aistudio_api.api.state import runtime_state
from aistudio_api.config import clamp_pool_size, settings
from aistudio_api.domain.errors import RequestError
from aistudio_api.infrastructure.utils.logger import get_logger
from aistudio_api.infrastructure.utils.request_context import rid_suffix

logger = get_logger("pool")

# 失败 worker 的冷却时间：这段时间内 pick() 不再把请求派给它（后台重启仍在跑）。
WORKER_COOLDOWN_S = 15.0
# 重启退避：1s / 3s / 10s；连续三次失败后冷却 60s 再等下一次 pick 拉起。
RESTART_BACKOFF_S: tuple[float, ...] = (1.0, 3.0, 10.0)
RESTART_COOLDOWN_S = 60.0
# 全部 worker 暂不可用（都在冷却/启动）时，pick() 最多等这么久再报 503。
PICK_WAIT_S = 30.0


class WorkerState:
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    COOLING = "cooling"
    RESTARTING = "restarting"


# 请求级上下文：本次上游调用落在哪台 worker/哪个账号。
# asyncio 每个请求任务各自一份 context，不会串号；保留到请求结束（成功/失败 hook 都要读）。
_current_worker: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "aistudio_pool_worker", default=None
)
_current_account_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aistudio_pool_account", default=""
)


def get_current_worker() -> Any | None:
    """当前请求绑定的 worker（没有则 None）。"""
    return _current_worker.get()


def get_current_account_id() -> str:
    """当前请求绑定的账号 id（单浏览器/未绑定则空串）。"""
    return _current_account_id.get()


class BrowserWorker:
    """一台 Chromium + 一个 AIStudioClient + 它当前负责的账号。"""

    def __init__(
        self,
        index: int,
        *,
        port: int | None = None,
        account_id: str | None = None,
        pool: ClientPool | None = None,
        store: Any | None = None,
    ) -> None:
        from aistudio_api.infrastructure.gateway.client import AIStudioClient

        self.index = index
        self.port = settings.browser_port + index if port is None else port
        self.account_id = account_id
        self.inflight = 0
        self.state = WorkerState.STOPPED
        self.cooling_until = 0.0
        self.failures = 0
        self.last_used = 0.0
        self._pool = pool
        self._store = store
        self._start_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        # 每台 worker 一个独立 profile 目录（同一 auth_file 也加 -w0/-w1 后缀）
        self.client = AIStudioClient(
            port=self.port, profile_suffix=f"-w{index}"
        )

    # ---------- 账号 ----------
    def _resolve_auth_path(self) -> str | None:
        if not self.account_id or self._store is None:
            return None
        try:
            path = self._store.get_auth_path(self.account_id)
        except Exception:
            return None
        return str(path) if path is not None else None

    def _apply_account(self) -> None:
        path = self._resolve_auth_path()
        if path is not None:
            self.client.session.set_account(path)

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """拉起（或确认）这台 Chromium，并预热 BotGuard 模板。"""
        async with self._start_lock:
            if self.state == WorkerState.READY:
                return
            self.state = WorkerState.STARTING
            try:
                self._apply_account()
                await self.client.warmup()
                self.state = WorkerState.READY
                self.cooling_until = 0.0
                self.failures = 0
                logger.info(
                    "pool: worker #%d 就绪 (port=%d, account=%s)",
                    self.index,
                    self.port,
                    self.account_id or "-",
                )
            except Exception as exc:
                self.state = WorkerState.STOPPED
                self.failures += 1
                logger.warning(
                    "pool: worker #%d 启动失败 (port=%d): %s",
                    self.index,
                    self.port,
                    exc,
                )
                raise

    async def stop(self) -> None:
        with suppress(Exception):
            await self.client.close()
        self.state = WorkerState.STOPPED
        logger.info("pool: worker #%d 已停止 (port=%d)", self.index, self.port)

    async def restart(self, reason: str = "") -> bool:
        """后台重启：kill → 指数退避 → 重新拉起；连续失败则冷却 60s。"""
        async with self._restart_lock:
            self.state = WorkerState.RESTARTING
            logger.warning(
                "pool: worker #%d 开始后台重启 (%s, port=%d)",
                self.index,
                reason or "unknown",
                self.port,
            )
            for delay in RESTART_BACKOFF_S:
                with suppress(Exception):
                    await self.client.close()
                self.state = WorkerState.STOPPED
                await asyncio.sleep(delay)
                # 原账号仍在冷却（如 429）时，换一个 rotator 认为可用且没被另一台占用的账号
                if self._pool is not None:
                    self._pool.reassign_account(self)
                try:
                    await self.start()
                except Exception as exc:
                    logger.warning(
                        "pool: worker #%d 重启失败 (%.0fs 退避后): %s",
                        self.index,
                        delay,
                        exc,
                    )
                    continue
                logger.info(
                    "pool: worker #%d 重启完成 (account=%s)",
                    self.index,
                    self.account_id or "-",
                )
                return True
            self.state = WorkerState.COOLING
            self.cooling_until = time.time() + RESTART_COOLDOWN_S
            logger.error(
                "pool: worker #%d 连续重启失败，冷却 %d 秒",
                self.index,
                int(RESTART_COOLDOWN_S),
            )
            return False


class ClientPool:
    """N 台 BrowserWorker 的池：least-inflight 分发 + 失败自愈切换。"""

    is_pool = True

    def __init__(
        self,
        size: int | None = None,
        *,
        port_base: int | None = None,
        workers: list[Any] | None = None,
        account_store: Any | None = None,
    ) -> None:
        self.size = clamp_pool_size(size if size is not None else settings.browser_pool_size)
        self._port_base = settings.browser_port if port_base is None else port_base
        self._account_store = account_store
        self._accounts_assigned = False
        self._shared_account_warned = False
        self._start_lock = asyncio.Lock()
        self._rr = 0
        if workers is not None:
            self._workers: list[Any] = list(workers)
            self.size = len(self._workers)
        else:
            self._workers = [
                BrowserWorker(
                    index,
                    port=self._port_base + index,
                    pool=self,
                    store=self.store,
                )
                for index in range(self.size)
            ]
        self._restart_tasks: dict[int, asyncio.Task[Any]] = {}

    # ---------- 依赖 ----------
    @property
    def store(self) -> Any:
        if self._account_store is None:
            from aistudio_api.infrastructure.account.account_store import AccountStore

            self._account_store = AccountStore()
        return self._account_store

    @property
    def workers(self) -> list[Any]:
        return list(self._workers)

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """分配账号并**串行**拉起所有 worker（避免同时开两台 Chromium 的内存峰值）。"""
        async with self._start_lock:
            self._assign_accounts()
            for worker in self._workers:
                if worker.state == WorkerState.READY:
                    continue
                try:
                    await worker.start()
                except Exception:  # 单台失败不影响另一台；pick 会再尝试拉起
                    continue

    def _assign_accounts(self) -> None:
        if self._accounts_assigned:
            return
        try:
            accounts = self.store.list_accounts()
        except Exception:
            accounts = []
        if accounts:
            for index, worker in enumerate(self._workers):
                worker.account_id = accounts[index % len(accounts)].id
            if (
                len(accounts) == 1
                and len(self._workers) > 1
                and not self._shared_account_warned
            ):
                self._shared_account_warned = True
                logger.warning(
                    "pool: 当前只有 1 份账号，%d 台 worker 将共用同一份 cookie（预期行为，导入更多账号后自动分散）",
                    len(self._workers),
                )
        self._accounts_assigned = True

    async def warmup(self) -> None:
        """lifespan 调用的预热入口（幂等）。"""
        await self.start()

    async def close(self) -> None:
        for task in list(self._restart_tasks.values()):
            task.cancel()
        for task in list(self._restart_tasks.values()):
            with suppress(BaseException):
                await task
        self._restart_tasks.clear()
        for worker in self._workers:
            with suppress(Exception):
                await worker.stop()

    async def check_idle_timeouts(self) -> None:
        """逐 worker 检查空闲回收；被回收的那台下次 pick 时再拉起。"""
        for worker in self._workers:
            if worker.state != WorkerState.READY:
                continue
            session = getattr(getattr(worker, "client", None), "session", None)
            if session is None:
                continue
            try:
                closed = await session.check_idle_timeout()
            except Exception:
                continue
            if closed:
                worker.state = WorkerState.STOPPED
                logger.info(
                    "pool: worker #%d 空闲回收，下次请求再拉起", worker.index
                )

    # ---------- 选择 ----------
    def _account_available(self, account_id: str | None, model: str | None) -> bool:
        if not account_id:
            return True
        rotator = runtime_state.rotator
        if rotator is None:
            return True
        try:
            return bool(rotator.is_account_available(account_id, model))
        except Exception:
            return True

    async def _ensure_workers_started(self) -> None:
        for worker in self._workers:
            if worker.state == WorkerState.STOPPED:
                try:
                    await worker.start()
                except Exception:
                    continue

    async def pick(self, model: str | None = None) -> Any:
        """挑一台 worker：ready、非 cooling、账号对该模型可用，按 least-inflight。"""
        self._assign_accounts()
        await self._ensure_workers_started()
        deadline = time.time() + PICK_WAIT_S
        while True:
            now = time.time()
            ready = [w for w in self._workers if w.state == WorkerState.READY]
            if not ready:
                if now >= deadline:
                    raise RequestError(503, "没有可用的浏览器 worker（全部未就绪）")
                await asyncio.sleep(0.5)
                await self._ensure_workers_started()
                continue
            usable = [w for w in ready if w.cooling_until <= now]
            if not usable:
                if now >= deadline:
                    raise RequestError(503, "浏览器 worker 全部处于冷却/重启中")
                await asyncio.sleep(0.5)
                continue
            available = [
                w for w in usable if self._account_available(w.account_id, model)
            ]
            if not available:
                # 账号配额冷却（429）等待也没用，直接按限流返回
                raise RequestError(
                    429,
                    f"所有浏览器 worker 的账号在模型 '{model}' 上均处于冷却状态",
                )
            self._rr += 1
            available.sort(
                key=lambda w: (
                    w.inflight,
                    w.last_used,
                    (w.index - self._rr) % max(1, len(self._workers)),
                )
            )
            chosen = available[0]
            chosen.last_used = time.time()
            return chosen

    # ---------- 分发 ----------
    def _bind(self, worker: Any, model: str | None = None) -> None:
        _current_worker.set(worker)
        _current_account_id.set(str(worker.account_id or ""))
        logger.info(
            "pool: 请求%s -> worker #%d (account=%s, model=%s)",
            rid_suffix(),
            worker.index,
            worker.account_id or "-",
            model or "-",
        )

    async def generate_content(self, **kwargs: Any) -> Any:
        worker = await self.pick(kwargs.get("model"))
        self._bind(worker, kwargs.get("model"))
        worker.inflight += 1
        try:
            return await worker.client.generate_content(**kwargs)
        finally:
            worker.inflight = max(0, worker.inflight - 1)

    async def stream_generate_content(
        self, **kwargs: Any
    ) -> AsyncGenerator[tuple[str, object], None]:
        worker = await self.pick(kwargs.get("model"))
        self._bind(worker, kwargs.get("model"))
        worker.inflight += 1
        try:
            async for event in worker.client.stream_generate_content(**kwargs):
                yield event
        finally:
            worker.inflight = max(0, worker.inflight - 1)

    # ---------- 失败自愈钩子（由 handle_attempt_exception 调用） ----------
    async def on_transport_error(self, model: str | None = None) -> bool:
        """页面 fetch/XHR 传输失败：重启该 worker，返回「另一台现在能接」。"""
        return await self._worker_failure("transport", model)

    async def on_auth_error(self, model: str | None = None) -> bool:
        """401/403 权限/登录失败：重启该 worker，**不给账号加冷却**。"""
        return await self._worker_failure("auth", model)

    async def on_rate_limited(self, model: str | None = None) -> bool:
        """429 配额限额：账号冷却由调用方记账；worker 也后台重启一份干净页面。"""
        return await self._worker_failure("rate_limit", model)

    async def on_server_error(
        self, model: str | None = None, status: int = 500
    ) -> bool:
        """上游 5xx：按 worker 故障处理（重启 + 切另一台）。"""
        return await self._worker_failure(f"http_{status}", model)

    async def _worker_failure(self, reason: str, model: str | None) -> bool:
        # 让后台重启任务先拿到调度机会（这些钩子只是「标记 + 派活」，不阻塞请求）
        await asyncio.sleep(0)
        worker = get_current_worker() or self._primary()
        if worker is None:
            return False
        worker.failures += 1
        worker.cooling_until = time.time() + WORKER_COOLDOWN_S
        if worker.state == WorkerState.READY:
            worker.state = WorkerState.COOLING
        self._spawn_restart(worker, reason)
        standby = self._standby_worker(exclude=worker)
        standby_text = "否"
        if standby is not None:
            standby_text = f"#{standby.index}"
            if standby.state == WorkerState.STOPPED:
                standby_text += "（空闲休眠，将拉起）"
        logger.warning(
            "pool: worker #%d 故障 (%s, model=%s)，后台重启；切备用=%s",
            worker.index,
            reason,
            model or "-",
            standby_text,
        )
        return standby is not None

    def _has_standby(self, *, exclude: Any) -> bool:
        return self._standby_worker(exclude=exclude) is not None

    def _standby_worker(self, *, exclude: Any) -> Any | None:
        """找一台这次请求能接住的备用 worker。

        **空闲回收（STOPPED）也算备用**：它只是进程被回收了内存，`pick()` 会在下次
        请求时把它重新拉起。只认 READY 会把「另一台正好空闲休眠」误判成「无备用」——
        2026-10-20 现场：8 分钟的长请求失败时另一台在 30 分钟空闲后已被回收，
        日志写「切备用=否」，请求直接 502，其实它本可以拉起再战。
        """
        now = time.time()
        for worker in self._workers:
            if worker is exclude:
                continue
            if worker.cooling_until > now:
                continue
            if worker.state in (WorkerState.READY, WorkerState.STOPPED):
                return worker
        return None

    def _primary(self) -> Any | None:
        for worker in self._workers:
            if worker.state == WorkerState.READY:
                return worker
        return self._workers[0] if self._workers else None

    def _spawn_restart(self, worker: Any, reason: str) -> None:
        index = int(getattr(worker, "index", 0))
        existing = self._restart_tasks.get(index)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            worker.restart(reason), name=f"pool_restart_{index}"
        )
        self._restart_tasks[index] = task

        def _done(_task: asyncio.Task[Any]) -> None:
            self._restart_tasks.pop(index, None)
            with suppress(BaseException):
                _task.exception()

        task.add_done_callback(_done)

    async def wait_restarts(self, timeout_s: float = 10.0) -> None:
        """等待后台重启任务结束（测试/关闭用）。"""
        tasks = [task for task in self._restart_tasks.values() if not task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_s)

    def reassign_account(self, worker: Any) -> None:
        """重启前：原账号仍在冷却就从账号表里挑一个可用且没被另一台占用的。"""
        rotator = runtime_state.rotator
        if rotator is None:
            return
        try:
            accounts = self.store.list_accounts()
        except Exception:
            return
        if len(accounts) <= 1:
            return
        current = worker.account_id
        try:
            if current and rotator.is_account_available(current):
                return
        except Exception:
            return
        used = {
            w.account_id for w in self._workers if w is not worker and w.account_id
        }
        for account in accounts:
            if account.id in used:
                continue
            try:
                available = rotator.is_account_available(account.id)
            except Exception:
                continue
            if available:
                worker.account_id = account.id
                logger.info(
                    "pool: worker #%d 重启时切换账号 %s -> %s",
                    worker.index,
                    current or "-",
                    account.id,
                )
                return

    # ---------- 兼容单 client 的通用接口 ----------
    @property
    def current_worker(self) -> Any | None:
        return get_current_worker() or self._primary()

    @property
    def session(self) -> Any | None:
        worker = self.current_worker
        return getattr(getattr(worker, "client", None), "session", None)

    @property
    def _session(self) -> Any | None:
        """兼容 routes_accounts/models/system 里对 ``client._session`` 的直接用。"""
        return self.session

    def clear_templates(self) -> None:
        for worker in self._workers:
            client = getattr(worker, "client", None)
            if client is not None:
                with suppress(Exception):
                    client.clear_templates()


__all__ = [
    "PICK_WAIT_S",
    "RESTART_BACKOFF_S",
    "RESTART_COOLDOWN_S",
    "WORKER_COOLDOWN_S",
    "BrowserWorker",
    "ClientPool",
    "WorkerState",
    "get_current_account_id",
    "get_current_worker",
]

