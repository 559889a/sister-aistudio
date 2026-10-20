"""浏览器 worker 池单测：账号分配 / least-inflight / cooling / 故障重启 / 单 client 回归。

用假 worker + 假 client，不启动任何浏览器。口径（2026-10-20 收紧为**零自动重试**）：
- 403/401、500、502、页面传输失败 → 后台重启当前 worker，**本次请求不重试**、
  按真实错误抛出；这些失败**不给账号加冷却**（auth_cooldown 不置位）；
- 429 只保留账号/模型配额冷却，不重启 worker。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_gemini import handle_attempt_exception
from aistudio_api.config import clamp_pool_size, settings
from aistudio_api.domain.errors import (
    AuthError,
    BrowserTransportError,
    RequestError,
    SessionExpiredError,
    UsageLimitExceeded,
)
from aistudio_api.infrastructure.gateway import client_pool as pool_module
from aistudio_api.infrastructure.gateway.client_pool import (
    BrowserWorker,
    ClientPool,
    WorkerState,
    get_current_account_id,
    get_current_worker,
)

# ────────────────────────────── 假实现 ──────────────────────────────


@dataclass
class FakeAccount:
    id: str
    name: str = "acc"


class FakeStore:
    def __init__(self, accounts: list[FakeAccount]) -> None:
        self._accounts = accounts

    def list_accounts(self) -> list[FakeAccount]:
        return list(self._accounts)

    def get_account(self, account_id: str) -> FakeAccount | None:
        return next((a for a in self._accounts if a.id == account_id), None)

    def get_auth_path(self, account_id: str) -> Path | None:
        if next((a for a in self._accounts if a.id == account_id), None) is None:
            return None
        return Path(f"/tmp/{account_id}/auth.json")


class FakeSession:
    def __init__(self) -> None:
        self.auth_file: str | None = None
        self.switch_auth_calls: list[str | None] = []

    def set_account(self, auth_file: str | None) -> None:
        self.auth_file = auth_file

    async def switch_auth(self, auth_file: str | None) -> None:
        self.switch_auth_calls.append(auth_file)
        self.auth_file = auth_file

    @property
    def page(self) -> None:
        return None

    async def check_idle_timeout(self) -> bool:
        return False


class FakeAIClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.session = FakeSession()
        self.warmed = 0
        self.closed = 0
        self.templates_cleared = 0
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def warmup(self) -> None:
        self.warmed += 1

    async def close(self) -> None:
        self.closed += 1

    def clear_templates(self) -> None:
        self.templates_cleared += 1

    async def generate_content(self, **kwargs: object) -> object:
        self.calls.append(("generate_content", kwargs))
        if self.error is not None:
            raise self.error
        return "ok"

    async def stream_generate_content(self, **kwargs: object):
        self.calls.append(("stream_generate_content", kwargs))
        if self.error is not None:
            raise self.error
        yield ("body", "hi")


class FakeWorker:
    def __init__(self, index: int, *, account_id: str | None = None) -> None:
        self.index = index
        self.port = 9222 + index
        self.client = FakeAIClient()
        self.account_id = account_id
        self.inflight = 0
        self.state = WorkerState.READY
        self.cooling_until = 0.0
        self.failures = 0
        self.last_used = 0.0
        self.restart_calls: list[str] = []
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.state = WorkerState.READY
        self.cooling_until = 0.0

    async def stop(self) -> None:
        self.stop_calls += 1
        self.state = WorkerState.STOPPED

    async def restart(self, reason: str = "") -> bool:
        self.restart_calls.append(reason)
        self.state = WorkerState.READY
        self.cooling_until = 0.0
        return True


class FakeRotator:
    """只实现编排器/池子用到的部分，并把「是否给账号加冷却」显式记下来。"""

    def __init__(
        self,
        unavailable: set[str] | None = None,
        unavailable_models: dict[str, set[str]] | None = None,
    ) -> None:
        self.unavailable = unavailable or set()
        self.unavailable_models = unavailable_models or {}
        self.auth_cooldown: dict[str, float] = {}
        self.auth_error_calls: list[str] = []
        self.rate_limited_calls: list[tuple[str, str | None]] = []
        self.error_calls: list[tuple[str, str | None]] = []
        self.session_expired_calls: list[str] = []
        self.success_calls: list[str] = []

    def is_account_available(
        self, account_id: str, model: str | None = None
    ) -> bool:
        if account_id in self.unavailable:
            return False
        blocked = self.unavailable_models.get(model or "", set())
        return account_id not in blocked

    def record_auth_error(self, account_id: str, *_a: object, **_k: object) -> None:
        self.auth_error_calls.append(account_id)
        self.auth_cooldown[account_id] = 300.0

    def record_rate_limited(self, account_id: str, model: str | None = None) -> None:
        self.rate_limited_calls.append((account_id, model))

    def record_error(self, account_id: str, model: str | None = None) -> None:
        self.error_calls.append((account_id, model))

    def record_session_expired(self, account_id: str, model: str | None = None) -> None:
        self.session_expired_calls.append(account_id)

    def record_success(self, account_id: str, model: str | None = None) -> None:
        self.success_calls.append(account_id)


@pytest.fixture
def accounts() -> list[FakeAccount]:
    return [FakeAccount("acc_0"), FakeAccount("acc_1"), FakeAccount("acc_2")]


@pytest.fixture
def fake_store(accounts: list[FakeAccount]) -> FakeStore:
    return FakeStore(accounts)


@pytest.fixture
def fake_rotator(monkeypatch) -> FakeRotator:
    rotator = FakeRotator()
    account_service = SimpleNamespace(
        get_account=lambda account_id: FakeAccount(account_id),
        get_active_account=lambda: FakeAccount("acc_global"),
    )
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(runtime_state, "account_service", account_service)
    return rotator


def _pool(
    fake_store: FakeStore, workers: list[FakeWorker], monkeypatch
) -> ClientPool:
    monkeypatch.setattr(pool_module, "RESTART_BACKOFF_S", (0.0, 0.0, 0.0))
    pool = ClientPool(workers=workers, account_store=fake_store)
    return pool


async def _fail_on(
    pool: ClientPool,
    worker_index: int,
    exc: Exception,
    *,
    model: str = "gemini-3.8-flash",
) -> None:
    """让请求确定性地落到指定 worker 并失败（用 inflight 把其它 worker 挤下去）。"""
    for worker in pool.workers:
        worker.inflight = 0 if worker.index == worker_index else 99
    worker = pool.workers[worker_index]
    worker.client.error = exc
    try:
        with pytest.raises(type(exc)):
            await pool.generate_content(model=model, capture_prompt="x")
    finally:
        for w in pool.workers:
            w.inflight = 0


async def _attempt(
    pool: ClientPool,
    exc: Exception,
    *,
    model_path: str = "models/gemini-3.8-flash",
) -> HTTPException:
    with pytest.raises(HTTPException) as excinfo:
        await handle_attempt_exception(
            exc,
            model_path=model_path,
            normalized_model=model_path,
            client=pool,
        )
    return excinfo.value


# ────────────────────────────── 配置 ──────────────────────────────


def test_config_pool_size_clamped():
    assert clamp_pool_size(0) == 1
    assert clamp_pool_size(1) == 1
    assert clamp_pool_size(2) == 2
    assert clamp_pool_size(4) == 4
    assert clamp_pool_size(9) == 4
    assert clamp_pool_size(-3) == 1
    assert 1 <= settings.browser_pool_size <= 4


# ────────────────────────────── 账号分配 / 启动 ──────────────────────────────


@pytest.mark.asyncio
async def test_start_assigns_accounts_round_robin(fake_store, monkeypatch):
    workers = [FakeWorker(0), FakeWorker(1)]
    for worker in workers:
        worker.state = WorkerState.STOPPED
    pool = _pool(fake_store, workers, monkeypatch)

    await pool.start()

    assert [w.account_id for w in workers] == ["acc_0", "acc_1"]
    assert all(w.state == WorkerState.READY for w in workers)
    # 串行启动：每台只 start 一次
    assert [w.start_calls for w in workers] == [1, 1]


@pytest.mark.asyncio
async def test_single_account_shared_across_workers_warn_once(
    fake_store, monkeypatch, caplog
):
    single_store = FakeStore([FakeAccount("acc_only")])
    workers = [FakeWorker(0), FakeWorker(1)]
    pool = _pool(single_store, workers, monkeypatch)

    with caplog.at_level("INFO", logger="aistudio.pool"):
        await pool.start()
        await pool.start()

    assert [w.account_id for w in workers] == ["acc_only", "acc_only"]
    warnings = [
        record for record in caplog.records if "共用同一份 cookie" in record.message
    ]
    assert len(warnings) == 1


# ────────────────────────────── 选择策略 ──────────────────────────────


@pytest.mark.asyncio
async def test_pick_prefers_least_inflight(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    workers[0].inflight = 2
    workers[1].inflight = 0
    pool = _pool(fake_store, workers, monkeypatch)

    chosen = await pool.pick(model="gemini-3.8-flash")
    assert chosen.index == 1


@pytest.mark.asyncio
async def test_pick_round_robins_when_idle(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)

    first = await pool.pick(model=None)
    second = await pool.pick(model=None)
    assert {first.index, second.index} == {0, 1}


@pytest.mark.asyncio
async def test_pick_skips_cooling_worker(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    workers[0].cooling_until = pool_module.time.time() + 60
    pool = _pool(fake_store, workers, monkeypatch)

    chosen = await pool.pick(model=None)
    assert chosen.index == 1


@pytest.mark.asyncio
async def test_pick_raises_429_when_all_accounts_cooling(fake_store, monkeypatch):
    fake_rotator = FakeRotator(unavailable_models={"m": {"acc_0", "acc_1"}})
    monkeypatch.setattr(runtime_state, "rotator", fake_rotator)
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)

    with pytest.raises(RequestError) as exc_info:
        await pool.pick(model="m")
    assert exc_info.value.status == 429


@pytest.mark.asyncio
async def test_pick_waits_then_503_when_all_workers_cooling(
    fake_store, monkeypatch
):
    monkeypatch.setattr(pool_module, "PICK_WAIT_S", 0.1)
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    for worker in workers:
        worker.cooling_until = pool_module.time.time() + 60
    pool = _pool(fake_store, workers, monkeypatch)

    with pytest.raises(RequestError) as exc_info:
        await pool.pick(model=None)
    assert exc_info.value.status == 503


@pytest.mark.asyncio
async def test_pick_starts_stopped_worker(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    workers[1].state = WorkerState.STOPPED
    pool = _pool(fake_store, workers, monkeypatch)

    chosen = await pool.pick(model=None)
    assert workers[1].start_calls == 1
    assert chosen.state == WorkerState.READY


# ────────────────────────────── 分发 / contextvar ──────────────────────────────


@pytest.mark.asyncio
async def test_generate_content_binds_worker_and_account(
    fake_store, monkeypatch, fake_rotator
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)

    result = await pool.generate_content(model="m", capture_prompt="x")
    assert result == "ok"
    assert get_current_worker() is not None
    assert get_current_account_id() == get_current_worker().account_id
    assert get_current_worker().inflight == 0  # finally 里还原


@pytest.mark.asyncio
async def test_stream_generate_content_passes_through(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)

    events = [
        event
        async for event in pool.stream_generate_content(model="m", capture_prompt="x")
    ]
    assert events == [("body", "hi")]
    assert workers[0].inflight == 0


# ────────────────────────────── 故障重启钩子 ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook", "reason"),
    [
        ("on_transport_error", "transport"),
        ("on_auth_error", "auth"),
    ],
)
async def test_hooks_restart_failed_worker(
    fake_store, monkeypatch, fake_rotator, hook, reason
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    pool._bind(workers[0])

    await getattr(pool, hook)(model="m")

    await pool.wait_restarts()
    assert workers[0].restart_calls == [reason]
    assert workers[0].state == WorkerState.READY


@pytest.mark.asyncio
async def test_server_error_hook_restarts_worker(fake_store, monkeypatch, fake_rotator):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    workers[1].cooling_until = pool_module.time.time() + 60
    pool = _pool(fake_store, workers, monkeypatch)
    pool._bind(workers[0])

    await pool.on_server_error(model="m", status=502)

    await pool.wait_restarts()
    assert workers[0].restart_calls == ["http_502"]


# ────────────────────────────── handle_attempt_exception: pool 路径 ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_reason", "expected_status"),
    [
        (AuthError("禁止访问: forbidden"), "auth", 403),
        (RequestError(500, "boom"), "http_500", 500),
        (RequestError(502, "bad gateway"), "http_502", 502),
        (BrowserTransportError("streaming request failed: Failed to fetch"), "transport", 502),
    ],
)
async def test_pool_worker_failures_restart_without_retry_or_account_cooldown(
    fake_store, fake_rotator, monkeypatch, exc, expected_reason, expected_status
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    await _fail_on(pool, 0, exc)

    http_exc = await _attempt(pool, exc)

    # 本次请求不重试：按真实状态码抛出；failed worker 后台重启
    assert http_exc.status_code == expected_status
    await pool.wait_restarts()
    assert workers[0].restart_calls == [expected_reason]
    # 用户拍板：403/401/5xx/502/传输失败都不给账号加冷却
    assert fake_rotator.auth_error_calls == []
    assert fake_rotator.auth_cooldown == {}
    # 但失败计数照样记
    assert fake_rotator.error_calls


@pytest.mark.asyncio
async def test_pool_transport_error_uses_current_worker_account(
    fake_store, fake_rotator, monkeypatch
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    exc = BrowserTransportError("streaming request failed: Failed to fetch")
    await _fail_on(pool, 1, exc)

    await _attempt(pool, exc)

    # 账号归因用 worker 绑定的 acc_1，而不是全局 active / 主 worker
    assert [account for account, _model in fake_rotator.error_calls] == ["acc_1"]
    await pool.wait_restarts()


@pytest.mark.asyncio
async def test_pool_server_error_preserves_original_status_code(
    fake_store, fake_rotator, monkeypatch
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    workers[1].cooling_until = pool_module.time.time() + 60
    pool = _pool(fake_store, workers, monkeypatch)
    exc = RequestError(500, "boom")
    await _fail_on(pool, 0, exc)

    http_exc = await _attempt(pool, exc)

    # 原始状态码保留（不能变成 500 以外的新错误类型）
    assert http_exc.status_code == 500
    await pool.wait_restarts()
    assert workers[0].restart_calls == ["http_500"]


@pytest.mark.asyncio
async def test_pool_rate_limit_keeps_quota_cooldown_without_restart(
    fake_store, fake_rotator, monkeypatch
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    exc = UsageLimitExceeded("quota")
    await _fail_on(pool, 0, exc)

    http_exc = await _attempt(pool, exc)

    assert http_exc.status_code == 429
    # 429 保留账号/模型配额冷却，但不重启 worker（重启只服务于已废除的重试路径）
    assert fake_rotator.rate_limited_calls
    assert workers[0].restart_calls == []
    assert workers[0].state == WorkerState.READY


@pytest.mark.asyncio
async def test_pool_session_expired_isolates_account_and_restarts(
    fake_store, fake_rotator, monkeypatch
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    exc = SessionExpiredError("redirected to login")
    await _fail_on(pool, 0, exc)

    http_exc = await _attempt(pool, exc)

    assert http_exc.status_code == 401
    # 登录态失效仍按账号隔离（24h），403 不走到这里
    assert fake_rotator.session_expired_calls == ["acc_0"]
    await pool.wait_restarts()
    assert workers[0].restart_calls == ["auth"]


# ────────────────────────────── 单 client 路径回归 ──────────────────────────────


@pytest.mark.asyncio
async def test_single_client_auth_error_restarts_browser_without_account_cooldown(
    fake_rotator, monkeypatch
):
    """pool_size=1 的路径：403 同样按浏览器故障处理——重启浏览器、不加账号冷却、403 返回。"""
    client = MagicMock()
    client._session = MagicMock()
    client._session._close_internal = AsyncMock()
    client.clear_templates = MagicMock()
    # 明确模拟「单 client」：没有 is_pool 标记
    del client.is_pool

    with pytest.raises(HTTPException) as exc_info:
        await handle_attempt_exception(
            AuthError("forbidden"),
            model_path="models/gemini-3.8-flash",
            normalized_model="models/gemini-3.8-flash",
            client=client,
        )

    assert exc_info.value.status_code == 403
    # 不给账号加冷却（403 是浏览器/BotGuard 问题，不是账号问题）
    assert fake_rotator.auth_error_calls == []
    assert fake_rotator.auth_cooldown == {}
    assert fake_rotator.error_calls == [("acc_global", "models/gemini-3.8-flash")]
    # 单浏览器路径的重启 = 杀浏览器进程（下次请求 ensure_ready 重建）
    client._session._close_internal.assert_awaited_once()


# ────────────────────────────── worker 生命周期 / 空闲回收 / 换号 ──────────────────────────────


@pytest.mark.asyncio
async def test_browser_worker_lifecycle_uses_suffixed_account(
    fake_store, monkeypatch
):
    monkeypatch.setattr(pool_module, "RESTART_BACKOFF_S", (0.0, 0.0, 0.0))
    worker = BrowserWorker(0, account_id="acc_0", store=fake_store)
    fake_client = FakeAIClient()
    worker.client = fake_client

    await worker.start()
    assert worker.state == WorkerState.READY
    assert fake_client.warmed == 1
    assert fake_client.session.auth_file == str(fake_store.get_auth_path("acc_0"))

    await worker.restart("test")
    assert fake_client.closed >= 1
    assert fake_client.warmed >= 2
    assert worker.state == WorkerState.READY


@pytest.mark.asyncio
async def test_pool_check_idle_timeouts_stops_reclaimed_worker(
    fake_store, monkeypatch
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    workers[0].client.session.check_idle_timeout = AsyncMockReturning(True)

    await pool.check_idle_timeouts()

    assert workers[0].state == WorkerState.STOPPED
    assert workers[1].state == WorkerState.READY


@pytest.mark.asyncio
async def test_reassign_account_avoids_accounts_used_by_other_workers(
    fake_store, monkeypatch, fake_rotator
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    fake_rotator.unavailable = {"acc_0"}

    pool.reassign_account(workers[0])

    assert workers[0].account_id == "acc_2"


@pytest.mark.asyncio
async def test_pool_session_properties_and_clear_templates(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)

    assert pool.is_pool is True
    assert pool.session is workers[0].client.session
    assert pool._session is workers[0].client.session

    pool.clear_templates()
    assert all(w.client.templates_cleared == 1 for w in workers)


# ────────────────── 手动激活 / 删除后重绑（WebUI 切换要同步所有 worker） ──────────────────


@pytest.mark.asyncio
async def test_pool_activate_account_rebinds_all_workers(fake_store, monkeypatch):
    """WebUI 手动激活要同步所有 worker：READY 蓝绿热切，休眠的改绑定待拉起。"""
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    workers[1].state = WorkerState.STOPPED
    pool = _pool(fake_store, workers, monkeypatch)

    assert await pool.activate_account("acc_2") is True

    assert [w.account_id for w in workers] == ["acc_2", "acc_2"]
    hot, idle = workers[0].client.session, workers[1].client.session
    assert hot.switch_auth_calls == [str(fake_store.get_auth_path("acc_2"))]
    # 休眠的那台不能热切（switch_auth 会拉起浏览器），只预设账号
    assert idle.switch_auth_calls == []
    assert idle.auth_file == str(fake_store.get_auth_path("acc_2"))
    assert all(w.client.templates_cleared == 1 for w in workers)


@pytest.mark.asyncio
async def test_pool_activate_account_unknown_account_fails(fake_store, monkeypatch):
    workers = [FakeWorker(0, account_id="acc_0")]
    pool = _pool(fake_store, workers, monkeypatch)

    assert await pool.activate_account("missing") is False
    assert workers[0].account_id == "acc_0"


@pytest.mark.asyncio
async def test_pool_redistribute_rebinds_only_deleted_accounts(
    fake_store, monkeypatch
):
    workers = [FakeWorker(0, account_id="acc_0"), FakeWorker(1, account_id="acc_1")]
    pool = _pool(fake_store, workers, monkeypatch)
    fake_store._accounts = [FakeAccount("acc_0"), FakeAccount("acc_2")]  # acc_1 已删

    await pool.redistribute_accounts()

    assert workers[0].account_id == "acc_0"  # 仍存在：不动、不热切
    assert workers[0].client.session.switch_auth_calls == []
    assert workers[1].account_id == "acc_2"  # 已删：重绑到剩余账号
    assert workers[1].client.session.switch_auth_calls == [
        str(fake_store.get_auth_path("acc_2"))
    ]


class AsyncMockReturning:
    """极简异步替身：调用即返回固定值（避免引入 unittest.AsyncMock 语义差异）。"""

    def __init__(self, value: object) -> None:
        self._value = value

    async def __call__(self, *_args: object, **_kwargs: object) -> object:
        return self._value
