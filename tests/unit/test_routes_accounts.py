"""Tests for accounts routes, cache isolation headers, and /api/accounts prefix."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.api.app import app
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.account_store import AccountMeta


@pytest.fixture(autouse=True)
def setup_mock_account_service():
    mock_svc = MagicMock(spec=AccountService)
    sample_acc = AccountMeta(
        id="acc_1",
        name="Test Account 1",
        email="test1@gmail.com",
        created_at="2026-10-17T00:00:00Z",
        last_used=None,
        auth_user="0",
        cookie_id="cookie_1",
    )
    mock_svc.list_accounts.return_value = [sample_acc]
    mock_svc.get_active_account.return_value = sample_acc

    orig = runtime_state.account_service
    runtime_state.account_service = mock_svc
    try:
        yield mock_svc
    finally:
        runtime_state.account_service = orig


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_accounts_html_navigation_headers():
    """Verify that visiting /accounts as HTML returns the SPA HTML with cache isolation headers."""
    async with _client() as client:
        for path in ("/accounts", "/accounts/"):
            resp = await client.get(path, headers={"Accept": "text/html,application/xhtml+xml"})
            assert resp.status_code == 200
            assert "<!DOCTYPE html>" in resp.text
            assert "text/html" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")


@pytest.mark.asyncio
async def test_accounts_json_api_headers():
    """Verify that calling /accounts with Accept: application/json returns JSON with cache isolation headers."""
    async with _client() as client:
        resp = await client.get("/accounts", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_endpoint_headers():
    """Verify that the dedicated /api/accounts endpoint returns JSON with cache isolation headers."""
    async with _client() as client:
        for path in ("/api/accounts", "/api/accounts/"):
            resp = await client.get(path, headers={"Accept": "application/json"})
            assert resp.status_code == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            assert "Accept" in resp.headers.get("Vary", "")
            assert "no-store" in resp.headers.get("Cache-Control", "")
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == 1
            assert data[0]["id"] == "acc_1"


@pytest.mark.asyncio
async def test_api_accounts_and_legacy_accounts_parity():
    """Verify that /api/accounts and /accounts return identical payload for JSON clients."""
    async with _client() as client:
        resp_legacy = await client.get("/accounts", headers={"Accept": "application/json"})
        resp_api = await client.get("/api/accounts", headers={"Accept": "application/json"})
        assert resp_legacy.status_code == 200
        assert resp_api.status_code == 200
        assert resp_legacy.json() == resp_api.json()


@pytest.mark.asyncio
async def test_issue_5_cache_isolation_regression():
    """Regression test for Issue #5:
    Ensure that a client requesting HTML followed by JSON (or vice-versa) receives distinct
    content types and both responses carry Vary: Accept and Cache-Control: no-store headers.
    """
    async with _client() as client:
        html_resp = await client.get("/accounts", headers={"Accept": "text/html"})
        assert html_resp.status_code == 200
        assert "<!DOCTYPE html>" in html_resp.text
        assert "Accept" in html_resp.headers.get("Vary", "")
        assert "no-store" in html_resp.headers.get("Cache-Control", "")

        json_resp = await client.get("/accounts", headers={"Accept": "application/json"})
        assert json_resp.status_code == 200
        assert "application/json" in json_resp.headers.get("Content-Type", "")
        assert "Accept" in json_resp.headers.get("Vary", "")
        assert "no-store" in json_resp.headers.get("Cache-Control", "")
        assert isinstance(json_resp.json(), list)


@pytest.mark.asyncio
async def test_api_accounts_active_endpoint():
    """Verify subroutes under /api/accounts like /api/accounts/active work."""
    async with _client() as client:
        resp = await client.get("/api/accounts/active", headers={"Accept": "application/json"})
        assert resp.status_code == 200
        assert "Accept" in resp.headers.get("Vary", "")
        assert "no-store" in resp.headers.get("Cache-Control", "")
        data = resp.json()
        assert data["id"] == "acc_1"


@pytest.mark.asyncio
async def test_activate_account_pool_mode_rebinds_all_workers(
    setup_mock_account_service,
):
    """池模式手动激活要走 ClientPool.activate_account 重绑所有 worker，而非只切主 worker。"""
    from unittest.mock import AsyncMock

    pool = MagicMock()
    pool.is_pool = True
    pool.activate_account = AsyncMock(return_value=True)
    setup_mock_account_service.get_account.return_value = (
        setup_mock_account_service.get_active_account.return_value
    )
    orig_client = runtime_state.client
    runtime_state.client = pool
    try:
        async with _client() as client:
            resp = await client.post(
                "/accounts/acc_1/activate", headers={"Accept": "application/json"}
            )
    finally:
        runtime_state.client = orig_client

    assert resp.status_code == 200
    pool.activate_account.assert_awaited_once_with("acc_1")
    setup_mock_account_service.set_active_account.assert_called_once_with("acc_1")


@pytest.mark.asyncio
async def test_delete_active_account_pool_mode_redistributes(
    setup_mock_account_service,
):
    """池模式删除活跃账号要触发 redistribute_accounts，把仍指向已删账号的 worker 换掉。"""
    from unittest.mock import AsyncMock

    pool = MagicMock()
    pool.is_pool = True
    pool.redistribute_accounts = AsyncMock(return_value=None)
    setup_mock_account_service.delete_account.return_value = True
    setup_mock_account_service.list_accounts.return_value = []
    orig_client = runtime_state.client
    runtime_state.client = pool
    try:
        async with _client() as client:
            resp = await client.delete(
                "/accounts/acc_1", headers={"Accept": "application/json"}
            )
    finally:
        runtime_state.client = orig_client

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    pool.redistribute_accounts.assert_awaited_once()
