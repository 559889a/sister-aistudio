"""Consumer regressions for account identity, switching and quota restoration."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from aistudio_api.api.routes_accounts import (
    BundleAccountItem,
    ImportBundleRequest,
    ImportCookiesRequest,
    delete_account,
    delete_cookie_group,
    detect_account_email,
    import_bundle,
    import_cookies,
    to_account_response,
)
from aistudio_api.api.state import RuntimeState, runtime_state
from aistudio_api.application import account_rotator
from aistudio_api.application.account_orchestrator import (
    record_rotator_event,
    try_switch_account,
)
from aistudio_api.application.account_rotator import AccountRotator, AccountStats
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.gateway.session import BrowserSession


@pytest.fixture
def service(tmp_path):
    return AccountService(AccountStore(accounts_dir=tmp_path / "accounts"))


def save(service, name="Account", email="account@example.com", **kwargs):
    return service.save_account_from_cookies(
        name=name, email=email, storage_state={"cookies": []}, **kwargs
    )


def session_state(service):
    session = MagicMock(spec=BrowserSession)
    session.switch_auth = AsyncMock()
    session.ensure_botguard_service = AsyncMock()
    session.close = AsyncMock()
    session.clear_auth = AsyncMock()
    client = MagicMock()
    client._session = session
    client.session = session
    return session, RuntimeState(client=client, account_service=service)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["switch_auth", "ensure_botguard_service"])
async def test_failed_activation_keeps_persisted_active(service, failure):
    old = save(service)
    new = save(service, email="new@example.com")
    session, _ = session_state(service)
    getattr(session, failure).side_effect = RuntimeError("browser failed")
    with pytest.raises(RuntimeError, match="browser failed"):
        await service.activate_account(new.id, session)
    assert service.get_active_account().id == old.id


def test_reimport_tracks_email_not_mutable_google_slot(service):
    old = save(service, auth_user="0", tier="Pro")
    new = save(service, auth_user="2", tier="Free")
    assert new.id == old.id
    assert new.created_at == old.created_at
    assert new.auth_user == "2"
    assert new.tier == "Pro"
    assert len(service.list_accounts()) == 1
    refreshed = save(service, email=None, account_id=old.id)
    assert refreshed.email == old.email
    assert refreshed.created_at == old.created_at


@pytest.mark.asyncio
async def test_legacy_groups_are_independent_and_deletable(service):
    first = save(service)
    second = save(service, email="second@example.com")
    assert to_account_response(first).cookie_id != to_account_response(second).cookie_id
    result = await delete_cookie_group(
        cookie_id=to_account_response(second).cookie_id,
        account_service=service,
        runtime_state=RuntimeState(account_service=service),
    )
    assert result == {"deleted": 1}
    assert service.get_account(first.id) is not None


@pytest.mark.asyncio
async def test_delete_last_account_clears_browser_credentials(service):
    account = save(service)
    session, state = session_state(service)
    assert await delete_account(account.id, service, state) == {"ok": True}
    session.clear_auth.assert_awaited_once()
    session.switch_auth.assert_not_awaited()
    assert service.get_active_account() is None


@pytest.mark.asyncio
async def test_cookie_import_activates_first_and_does_not_switch_for_inactive(service):
    session, state = session_state(service)
    response = await import_cookies(
        ImportCookiesRequest(cookies="SID=first", email="first@example.com", tier="Free"),
        service, state,
    )
    session.switch_auth.assert_awaited_once_with(
        str(service.get_account_auth_path(response.account_id))
    )
    session.switch_auth.reset_mock()
    await import_cookies(
        ImportCookiesRequest(cookies="SID=second", email="second@example.com", tier="Free"),
        service, state,
    )
    session.switch_auth.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["accounts", "content"])
async def test_bundle_preserves_id_and_accepts_cookie_array(service, source):
    old = save(service)
    item = {
        "id": old.id,
        "name": "Refreshed",
        "email": old.email,
        "cookies": "SID=fresh",
        "tier": "Pro",
    }
    if source == "accounts":
        req = ImportBundleRequest(accounts=[BundleAccountItem(**item)])
    else:
        item["cookies"] = [{"name": "SID", "value": "fresh"}]
        req = ImportBundleRequest(content=json.dumps([item]))
    response = await import_bundle(req, service, RuntimeState(account_service=service))
    assert response.imported_count == 1
    assert response.accounts[0].id == old.id
    assert len(service.list_accounts()) == 1
    assert service.get_account(old.id).created_at == old.created_at


@pytest.mark.asyncio
async def test_bundle_with_only_filtered_cookies_reports_failure(service):
    req = ImportBundleRequest(accounts=[BundleAccountItem(cookies="NID=discarded")])
    with pytest.raises(HTTPException) as exc:
        await import_bundle(req, service, RuntimeState(account_service=service))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("extract_fails", [False, True])
async def test_email_probe_restores_browser_account(service, monkeypatch, extract_fails):
    current = save(service)
    target = save(service, email="target@example.com")
    session, state = session_state(service)
    session.auth_file = str(service.get_account_auth_path(current.id))
    session._auth_file = session.auth_file
    session.page = MagicMock()
    session.extract_account_email = AsyncMock(
        side_effect=RuntimeError("extract failed") if extract_fails else None,
        return_value="target@example.com",
    )
    session.extract_account_tier = AsyncMock(return_value="Free")
    monkeypatch.setattr(
        "aistudio_api.api.routes_accounts.probe_account_email", AsyncMock(return_value=None)
    )
    if extract_fails:
        with pytest.raises(RuntimeError, match="extract failed"):
            await detect_account_email(target.id, service, state)
    else:
        await detect_account_email(target.id, service, state)
    assert session.switch_auth.await_args_list[0].args == (
        str(service.get_account_auth_path(target.id)),
    )
    assert session.switch_auth.await_args_list[-1].args == (session.auth_file,)
    assert service.get_active_account().id == current.id


def test_deleted_request_account_is_not_charged_to_new_active(service, monkeypatch):
    active = save(service)
    rotator = MagicMock(spec=AccountRotator)
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    record_rotator_event("rate_limited", model="gemini-flash", account_id="deleted")
    rotator.record_rate_limited.assert_not_called()
    record_rotator_event("success", model="gemini-flash", account_id=active.id)
    rotator.record_success.assert_called_once_with(active.id, model="gemini-flash")


def test_midnight_snapshot_resets_idle_models_and_errors(service, monkeypatch):
    now = [datetime(2026, 10, 21, 23, 59, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()]
    monkeypatch.setattr(account_rotator.time, "time", lambda: now[0])
    account = save(service)
    rotator = AccountRotator(service._store)
    rotator.record_error(account.id, "models/gemini-flash")
    rotator.record_rate_limited(account.id, "gemini-other")
    now[0] += 120
    row = rotator.get_all_stats()[account.id]
    assert row["requests"] == 2
    assert row["model_requests"] == {"gemini-flash": 0, "gemini-other": 0}
    assert row["model_rate_limited"] == {}
    rotator.record_error(account.id, "gemini-flash")
    assert rotator.get_all_stats()[account.id]["model_requests"]["gemini-flash"] == 1


def test_clearing_cooldown_keeps_daily_usage():
    stats = AccountStats(account_id="account")
    stats.record_success("gemini-flash")
    for _ in range(5):
        stats.record_rate_limited("gemini-flash")
    stats.clear_cooldown("gemini-flash")
    assert stats.model_requests["gemini-flash"] == 6
    assert stats.is_available("gemini-flash")
    stats.clear_cooldown()
    assert stats.model_requests["gemini-flash"] == 6


def test_global_429_streak_is_separate_from_lifetime_stats(monkeypatch):
    now = [datetime(2026, 10, 21, 23, 59, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()]
    monkeypatch.setattr(account_rotator.time, "time", lambda: now[0])
    stats = AccountStats(account_id="account")
    for _ in range(4):
        stats.record_rate_limited()
    stats = AccountStats.from_dict(stats.to_dict())
    assert stats.global_rate_limited_streak == 4
    stats.record_success()
    stats.record_rate_limited()
    assert stats.is_available()
    assert stats.rate_limited == 5
    now[0] += 120
    stats.record_rate_limited()
    assert stats.global_rate_limited_streak == 1
    assert stats.is_available()


@pytest.mark.parametrize("month,day,hour,expected", [(3, 8, 1, 22 * 3600), (11, 1, 0, 25 * 3600)])
def test_midnight_uses_elapsed_time_across_dst(monkeypatch, month, day, hour, expected):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, month, day, hour, tzinfo=tz)

    monkeypatch.setattr(account_rotator, "datetime", FrozenDatetime)
    assert account_rotator.get_seconds_until_pacific_midnight() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_delete_preserves_accounts_when_replacement_switch_fails(service, group):
    active = save(service, cookie_id="first")
    backup = save(service, email="backup@example.com", cookie_id="second")
    session, state = session_state(service)
    session.switch_auth.side_effect = RuntimeError("switch failed")
    with pytest.raises(RuntimeError, match="switch failed"):
        if group:
            await delete_cookie_group("first", service, state)
        else:
            await delete_account(active.id, service, state)
    assert service.get_account(active.id) is not None
    assert service.get_account(backup.id) is not None
    assert service.get_active_account().id == active.id


def test_rotator_restores_fifth_429_and_daily_counts(service, monkeypatch, tmp_path):
    state_file = tmp_path / "rotator.json"
    monkeypatch.setattr(account_rotator, "resolve_rotator_state_file", lambda: state_file)
    monkeypatch.setattr(account_rotator.settings, "persist_rotator", True)
    account = save(service)
    original = AccountRotator(service._store)
    for _ in range(4):
        original.record_rate_limited(account.id, "models/gemini-flash")
    restored = AccountRotator(service._store)
    assert restored.get_all_stats()[account.id]["model_requests"] == {"gemini-flash": 4}
    assert restored.is_account_available(account.id, "gemini-flash")
    restored.record_rate_limited(account.id, "gemini-flash")
    again = AccountRotator(service._store)
    assert not again.is_account_available(account.id, "models/gemini-flash")
    assert again.get_all_stats()[account.id]["model_requests"] == {"gemini-flash": 5}


@pytest.mark.asyncio
async def test_automatic_switch_keeps_backup_four_429s(service, monkeypatch):
    active = save(service)
    backup = save(service, email="backup@example.com")
    session, state = session_state(service)
    rotator = AccountRotator(service._store)
    for _ in range(5):
        rotator.record_rate_limited(active.id, "gemini-flash")
    for _ in range(4):
        rotator.record_rate_limited(backup.id, "gemini-flash")
    monkeypatch.setattr(runtime_state, "account_service", service)
    monkeypatch.setattr(runtime_state, "client", state.client)
    monkeypatch.setattr(runtime_state, "rotator", rotator)
    monkeypatch.setattr(
        "aistudio_api.application.account_orchestrator._last_switch_time", 0.0
    )
    assert await try_switch_account("gemini-flash", failed_account_id=active.id)
    assert service.get_active_account().id == backup.id
    assert rotator.get_all_stats()[backup.id]["model_rate_limited"] == {"gemini-flash": 4}
    assert rotator.get_all_stats()[backup.id]["model_requests"] == {"gemini-flash": 4}
    session.switch_auth.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_missing_auth_keeps_old_active(service):
    old = save(service)
    target = save(service, email="target@example.com")
    service.get_account_auth_path(target.id).unlink()
    session, _ = session_state(service)
    assert await service.activate_account(target.id, session) is None
    assert service.get_active_account().id == old.id
    session.switch_auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_active_with_missing_backup_does_not_lose_credentials(service):
    active = save(service)
    backup = save(service, email="backup@example.com")
    service.get_account_auth_path(backup.id).unlink()
    _, state = session_state(service)
    with pytest.raises(HTTPException) as exc:
        await delete_account(active.id, service, state)
    assert exc.value.status_code == 503
    assert service.get_account(active.id) is not None
    assert service.get_active_account().id == active.id


@pytest.mark.asyncio
async def test_concurrent_activation_serializes_browser_and_registry(service):
    save(service)
    first = save(service, email="first@example.com")
    second = save(service, email="second@example.com")
    session, _ = session_state(service)
    warming = asyncio.Event()
    release = asyncio.Event()

    async def warm():
        if session.switch_auth.await_count == 1:
            warming.set()
            await release.wait()

    session.ensure_botguard_service.side_effect = warm
    first_task = asyncio.create_task(service.activate_account(first.id, session))
    await warming.wait()
    second_task = asyncio.create_task(service.activate_account(second.id, session))
    await asyncio.sleep(0)
    assert session.switch_auth.await_count == 1
    release.set()
    await asyncio.gather(first_task, second_task)
    assert service.get_active_account().id == second.id
    assert session.switch_auth.await_args_list[-1].args == (
        str(service.get_account_auth_path(second.id)),
    )
