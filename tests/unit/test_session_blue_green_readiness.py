"""Unit tests for atomic SessionReadinessGate and Blue-Green tab transition."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistudio_api.infrastructure.browser.cdp_client import CDPClient, CDPPage
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.session_state import (
    SessionContext,
    SessionReadinessGate,
    SessionState,
)


@pytest.mark.asyncio
async def test_session_readiness_gate_state_machine():
    """Verify gate transitions cleanly: UNINITIALIZED -> PREPARING -> READY -> FAILED."""
    gate = SessionReadinessGate()
    assert gate.state == SessionState.UNINITIALIZED
    assert not gate.is_ready()

    # When PREPARING, wait_ready blocks until ready
    gate.set_preparing()
    assert gate.state == SessionState.PREPARING

    mock_page = MagicMock(spec=CDPPage)
    mock_page.is_closed.return_value = False
    ctx = SessionContext(
        page=mock_page,
        snap_key="snap_123",
        auth_user="0",
        auth_file="/data/auth.json",
        base_template={"body": "[]"},
    )

    async def open_gate():
        await asyncio.sleep(0.05)
        gate.set_ready(ctx)

    task = asyncio.create_task(open_gate())
    ready_ctx = await gate.wait_ready(timeout_s=2.0)
    await task
    assert ready_ctx == ctx
    assert gate.is_ready()

    # Fail-fast check: when FAILED, wait_ready immediately raises without waiting
    gate.set_failed(RuntimeError("Test error"))
    assert gate.state == SessionState.FAILED
    with pytest.raises(RuntimeError, match="Session transition failed: Test error"):
        await gate.wait_ready(timeout_s=1.0)


@pytest.mark.asyncio
async def test_session_context_event_driven_draining():
    """Verify decr_in_flight sets drain_event when count reaches 0."""
    mock_page = MagicMock(spec=CDPPage)
    mock_page.is_closed.return_value = False
    ctx = SessionContext(
        page=mock_page,
        snap_key="snap_123",
        auth_user="0",
        auth_file=None,
        base_template={},
        in_flight=2,
    )
    assert not ctx.drain_event.is_set()

    ctx.decr_in_flight()
    assert ctx.in_flight == 1
    assert not ctx.drain_event.is_set()

    ctx.decr_in_flight()
    assert ctx.in_flight == 0
    assert ctx.drain_event.is_set()


@pytest.mark.asyncio
async def test_session_blue_green_transition_preserves_inflight():
    """Verify blue-green transition creates a new page when in_flight > 0 without killing running streams."""
    session = BrowserSession(port=9222)
    mock_proc = MagicMock()
    mock_proc.is_alive.return_value = True
    session._proc = mock_proc

    old_page = MagicMock(spec=CDPPage)
    old_page.is_closed.return_value = False
    old_page.target_id = "page_tab_1"
    old_page.url = "https://aistudio.google.com/u/0/prompts/new_chat"

    new_page = MagicMock(spec=CDPPage)
    new_page.is_closed.return_value = False
    new_page.target_id = "page_tab_2"
    new_page.url = "https://aistudio.google.com/u/1/prompts/new_chat"

    new_page.add_init_script = AsyncMock()
    mock_cdp = MagicMock(spec=CDPClient)
    mock_cdp.create_new_page = AsyncMock(return_value=new_page)
    mock_cdp.close_target = AsyncMock()
    session._cdp_client = mock_cdp

    # Setup old active context with in_flight = 1 (a request is actively reading)
    old_ctx = SessionContext(
        page=old_page,
        snap_key="snap_old",
        auth_user="0",
        auth_file="/data/acc1.json",
        base_template={"body": '["models/gemini-3.8-flash"]'},
        in_flight=1,
    )
    session._gate.set_ready(old_ctx)
    session._in_flight = 1

    # Mock bootstrap functions
    with (
        patch("aistudio_api.infrastructure.gateway.session.load_cookies_to_page", AsyncMock(return_value=True)),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch(
            "aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template",
            AsyncMock(return_value=("snap_new", {"body": '["models/gemini-3.8-flash"]'})),
        ),
    ):
        new_ctx = await session.transition_to_account("/data/acc2.json", force_refresh=True)

    # 1. New page is created and becomes active context
    assert new_ctx.page == new_page
    assert session.active_context == new_ctx
    assert session._gate.is_ready()

    # 2. Old page was NOT aborted or closed immediately because in_flight was 1!
    old_page.close.assert_not_called()
    assert old_ctx in session._retiring_contexts

    # 3. Old stream finishes and decrements in_flight
    old_ctx.decr_in_flight()
    await asyncio.sleep(0.05)

    # Now old page is cleanly closed and closed on CDP
    old_page.close.assert_called_once()
    mock_cdp.close_target.assert_called_once_with("page_tab_1")
    assert old_ctx not in session._retiring_contexts


def _ready_context(label: str) -> SessionContext:
    page = MagicMock(spec=CDPPage)
    page.target_id = label
    page.is_closed.return_value = False
    page.add_init_script = AsyncMock()
    return SessionContext(
        page=page, snap_key=label, auth_user="0", auth_file=f"{label}/auth.json",
        base_template={"url": label, "headers": {}, "body": '["models/old"]'},
    )


@pytest.mark.asyncio
async def test_request_lease_survives_switch_and_nested_scope():
    session = BrowserSession(9222)
    old = _ready_context("old")
    new = _ready_context("new")
    session.set_ready_context(old)
    async with session.request_scope() as leased:
        assert leased is old
        async with session.request_scope():
            assert old.in_flight == 1
        old.drain_event.set()
        old.incr_in_flight()
        assert not old.drain_event.is_set()
        old.decr_in_flight()
        session.set_ready_context(new)
        assert await session.ensure_ready() is old
        assert (await session.capture_template("flash"))["url"] == "old"
        assert session.request_context is old
    assert old.in_flight == 0
    assert session.last_request_context is old
    assert session.last_request_auth_file == "old/auth.json"
    assert session.request_context is new


@pytest.mark.asyncio
async def test_inherited_request_scope_takes_its_own_lease():
    session = BrowserSession(9222)
    old = _ready_context("old")
    session.set_ready_context(old)
    started = asyncio.Event()
    release = asyncio.Event()

    async def child():
        async with session.request_scope():
            started.set()
            await release.wait()

    async with session.request_scope():
        task = asyncio.create_task(child())
        await started.wait()
        assert old.in_flight == 2
        release.set()
        await task
        assert old.in_flight == 1
    assert old.in_flight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("bootstrap failed"), asyncio.CancelledError()])
async def test_failed_blue_green_transition_restores_old_ready_context(failure):
    session = BrowserSession(9222, auth_file="old/auth.json")
    old = _ready_context("old")
    new = _ready_context("new")
    session.set_ready_context(old)
    session._proc = MagicMock()
    session._proc.is_alive.return_value = True
    client = MagicMock(spec=CDPClient)
    client.create_new_page = AsyncMock(return_value=new.page)
    session._cdp_client = client
    with (
        patch("aistudio_api.infrastructure.gateway.session.load_cookies_to_page", AsyncMock(return_value=True)),
        patch("aistudio_api.infrastructure.gateway.session.navigate_to_aistudio", AsyncMock()),
        patch("aistudio_api.infrastructure.gateway.session.bootstrap_botguard_and_template", AsyncMock(side_effect=failure)),
        pytest.raises(type(failure)),
    ):
        await session.switch_auth("new/auth.json")
    assert session.active_context is old
    assert session.is_ready
    assert session.auth_file == "old/auth.json"
    new.page.close.assert_awaited_once()
    old.page.close.assert_not_awaited()
    session._proc.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_retiring_context_has_no_generation_deadline():
    session = BrowserSession(9222)
    old = _ready_context("old")
    old.incr_in_flight()
    session._retiring_contexts.append(old)
    drain = asyncio.create_task(session._drain_retiring_context(old))
    with patch("aistudio_api.infrastructure.gateway.session.asyncio.wait_for", side_effect=AssertionError("generation deadline")):
        await asyncio.sleep(0)
        assert not drain.done()
        old.page.close.assert_not_awaited()
        old.decr_in_flight()
        await drain
    old.page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cold_start_failure_does_not_reuse_previous_request_account():
    session = BrowserSession(9222, auth_file="new/auth.json")
    session._last_request_context.set(_ready_context("old"))
    session.ensure_ready = AsyncMock(side_effect=RuntimeError("CDP unavailable"))
    with pytest.raises(RuntimeError, match="CDP unavailable"):
        async with session.request_scope():
            raise AssertionError("request must not start")
    assert session.last_request_context is None
    assert session.last_request_auth_file == "new/auth.json"
    assert session._in_flight == 0


@pytest.mark.asyncio
async def test_clear_auth_releases_deleted_account_without_bootstrap():
    session = BrowserSession(9222, auth_file="deleted/auth.json")
    session._auth_user = "3"
    session._tier = "Pro"
    session._bootstrap_mode = "binding"
    session._close_internal = AsyncMock()
    session.transition_to_account = AsyncMock()
    await session.clear_auth()
    session._close_internal.assert_awaited_once()
    session.transition_to_account.assert_not_awaited()
    assert session.auth_file is None
    assert session.profile_dir is None
    assert session._auth_user is None
    assert session._tier is None
    assert session.bootstrap_mode is None
