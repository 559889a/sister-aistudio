"""Unit tests for native async CDP client and protocol handling."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistudio_api.infrastructure.browser.cdp_client import (
    BLOCKED_URL_PATTERNS,
    CDPConnection,
    CDPError,
    CDPPage,
)


def test_blocked_url_patterns_coverage():
    """Verify kernel-level asset pruning covers images, fonts, and trackers."""
    assert "*.png" in BLOCKED_URL_PATTERNS
    assert "*.jpg" in BLOCKED_URL_PATTERNS
    assert "*.woff2" in BLOCKED_URL_PATTERNS
    assert "*google-analytics.com*" in BLOCKED_URL_PATTERNS
    assert "*play.google.com/log*" in BLOCKED_URL_PATTERNS
    assert "*SavePrompt*" in BLOCKED_URL_PATTERNS
    assert "*CreatePrompt*" in BLOCKED_URL_PATTERNS


@pytest.mark.asyncio
async def test_cdp_connection_send_and_receive():
    """Test CDPConnection request-response matching and error handling."""
    conn = CDPConnection("ws://127.0.0.1:9222/devtools/page/test")
    fake_ws = AsyncMock()
    conn.ws = fake_ws

    # Mock sending and receiving
    async def fake_send(text):
        data = json.loads(text)
        req_id = data["id"]
        # Simulate response in future
        fut = conn._futures.get(req_id)
        if fut and not fut.done():
            fut.set_result({"result": {"value": 42}})

    fake_ws.send = fake_send

    res = await conn.send("Runtime.evaluate", {"expression": "1 + 1"})
    assert res == {"result": {"value": 42}}


@pytest.mark.asyncio
async def test_cdp_connection_error_handling():
    """Test CDPConnection properly raises CDPError on JSON-RPC error."""
    conn = CDPConnection("ws://127.0.0.1:9222/devtools/page/test")
    fake_ws = AsyncMock()
    conn.ws = fake_ws

    async def fake_send(text):
        data = json.loads(text)
        req_id = data["id"]
        fut = conn._futures.get(req_id)
        if fut and not fut.done():
            fut.set_exception(CDPError(code=-32000, message="Target closed"))

    fake_ws.send = fake_send

    with pytest.raises(CDPError) as exc_info:
        await conn.send("Page.navigate", {"url": "https://example.com"})
    assert "Target closed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_cdp_page_blocked_urls_configuration():
    """Test CDPPage sets blocked URLs on Network domain."""
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={})
    conn.on = MagicMock()

    page = CDPPage(conn, target_id="target-1")
    await page.set_blocked_urls(BLOCKED_URL_PATTERNS)

    conn.send.assert_called_with(
        "Network.setBlockedURLs", {"urls": BLOCKED_URL_PATTERNS}
    )


@pytest.mark.asyncio
async def test_cdp_page_evaluate_argument_wrapping():
    """Test CDPPage.evaluate correctly wraps expressions with arguments and unwraps value."""
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(
        return_value={"result": {"type": "string", "value": "hello world"}}
    )

    page = CDPPage(conn, target_id="target-1")
    res = await page.evaluate("(args) => args.val", {"val": "hello world"})

    assert res == "hello world"
    call_args = conn.send.call_args[0]
    assert call_args[0] == "Runtime.evaluate"
    expr = call_args[1]["expression"]
    assert "hello world" in expr
    assert call_args[1]["awaitPromise"] is True


@pytest.mark.asyncio
async def test_cdp_page_evaluate_trailing_semicolon_handling():
    """Test CDPPage.evaluate strips trailing semicolons before wrapping into expression."""
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={"result": {"type": "string", "value": "ok"}})
    page = CDPPage(conn, target_id="target-1")

    # 1. Function with trailing semicolon and arguments
    await page.evaluate("(args) => { return args.val; };\n", {"val": "test"})
    call_args = conn.send.call_args[0]
    expr1 = call_args[1]["expression"]
    assert ";)" not in expr1
    assert expr1.startswith("((args) => { return args.val; })(")

    # 2. Arrow function with trailing semicolon and without arguments
    await page.evaluate("() => { return 42; };;;", None)
    call_args2 = conn.send.call_args[0]
    expr2 = call_args2[1]["expression"]
    assert ";)" not in expr2
    assert expr2 == "(() => { return 42; })()"


@pytest.mark.asyncio
async def test_cdp_page_wait_for_selector_and_nested_iife():
    """Test CDPPage.wait_for_selector evaluates the check expression correctly without regex false positives."""
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={"result": {"type": "boolean", "value": True}})
    page = CDPPage(conn, target_id="target-1")

    res = await page.wait_for_selector("textarea", timeout_s=1.0)
    assert res is True
    call_args = conn.send.call_args[0]
    expr = call_args[1]["expression"]
    assert "textarea" in expr
    assert expr.startswith("(() => !!(")
    assert expr.endswith("))()")


@pytest.mark.asyncio
async def test_cdp_page_set_cookies_normalization():
    """Test cookie normalization handles __Host- prefixes and domains properly."""
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={})

    page = CDPPage(conn, target_id="target-1")
    cookies = [
        {
            "name": "SID",
            "value": "sid123",
            "domain": ".google.com",
            "path": "/",
            "sameSite": "None",
        },
        {
            "name": "__Host-GAPS",
            "value": "gaps123",
            "domain": "accounts.google.com",
            "path": "/",
        },
    ]

    await page.set_cookies(cookies)

    conn.send.assert_called()
    payload = conn.send.call_args[0][1]["cookies"]
    assert len(payload) == 2

    sid = next(c for c in payload if c["name"] == "SID")
    assert sid["domain"] == ".google.com"
    assert sid["sameSite"] == "None"

    host_cookie = next(c for c in payload if c["name"] == "__Host-GAPS")
    # Must NOT have domain attribute with leading dot, must have url
    assert "domain" not in host_cookie
    assert host_cookie["url"] == "https://accounts.google.com/"


@pytest.mark.asyncio
async def test_cdp_page_is_alive():
    """Test CDPPage.is_alive fast liveness probe."""
    conn = MagicMock(spec=CDPConnection)
    conn._closed = False
    conn.send = AsyncMock(return_value={"result": {"type": "number", "value": 1}})

    page = CDPPage(conn, target_id="target-1")
    assert await page.is_alive(timeout_s=0.5) is True

    # When evaluate throws (CDP disconnected)
    conn.send.side_effect = RuntimeError("CDP closed")
    assert await page.is_alive(timeout_s=0.5) is False


@pytest.mark.asyncio
async def test_cdp_connection_fast_fail_on_target_crashed():
    """Verify Target.targetCrashed immediately rejects pending futures without 30s delay."""
    conn = CDPConnection("ws://127.0.0.1:9222/devtools/page/test")
    fake_ws = AsyncMock()
    conn.ws = fake_ws

    async def fake_send(_text):
        # Send crash event into recv loop
        conn.fail_pending_futures(RuntimeError("Target.targetCrashed"))

    fake_ws.send = fake_send

    with pytest.raises(RuntimeError, match=r"Target\.targetCrashed"):
        await conn.send("Runtime.evaluate", {"expression": "test"}, timeout_s=30.0)


@pytest.mark.asyncio
async def test_cdp_connection_liveness_checker_fail_fast():
    """Verify liveness_checker detects dead process and fails within milliseconds."""
    conn = CDPConnection("ws://127.0.0.1:9222/devtools/page/test")
    fake_ws = AsyncMock()
    conn.ws = fake_ws
    fake_ws.send = AsyncMock()

    # Process is already dead
    conn.set_liveness_checker(lambda: False)

    start_t = asyncio.get_running_loop().time()
    with pytest.raises(RuntimeError, match=r"Browser process has died"):
        await conn.send("Runtime.evaluate", {"expression": "1+1"}, timeout_s=30.0)
    elapsed = asyncio.get_running_loop().time() - start_t
    assert elapsed < 0.5, f"Took too long to detect dead process: {elapsed}s"


@pytest.mark.asyncio
async def test_cdp_client_connect_page_success_and_prune():
    """Verify connect_page connects to main page and prunes extra spare pages."""
    from aistudio_api.infrastructure.browser.cdp_client import CDPClient

    client = CDPClient(port=9222)
    client.wait_until_ready = AsyncMock()

    fake_targets = [
        {"id": "t1", "type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/p1"},
        {"id": "t2", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/p2"},
    ]
    client.get_targets = AsyncMock(return_value=fake_targets)
    client.close_target = AsyncMock()

    with patch(
        "aistudio_api.infrastructure.browser.cdp_client.CDPConnection"
    ) as mock_conn_cls:
        mock_conn = MagicMock()
        mock_conn.connect = AsyncMock()
        mock_conn_cls.return_value = mock_conn

        with patch(
            "aistudio_api.infrastructure.browser.cdp_client.CDPPage.init_domains",
            new_callable=AsyncMock,
        ):
            page = await client.connect_page(block_assets=True)
            assert page.target_id == "t1"
            client.close_target.assert_awaited_once_with("t2")
            client.get_targets.reset_mock()
            mock_conn._closed = False
            assert await client.connect_page() is page
            client.get_targets.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s", [None, 0])
async def test_cdp_send_without_deadline_waits_for_response(timeout_s):
    """timeout_s=None/0 = 不设总截止：生成等多久都不会被 CDP 自己掐断。"""
    conn = CDPConnection("ws://127.0.0.1:9222/devtools/page/test")
    conn.ws = AsyncMock()
    conn.ws.send = AsyncMock()
    conn.set_liveness_checker(lambda: True)

    async def resolve_later():
        while not conn._futures:
            await asyncio.sleep(0.01)
        # 比「旧实现里 0 秒截止」长得多：现在必须继续等
        await asyncio.sleep(0.1)
        fut = next(iter(conn._futures.values()))
        if not fut.done():
            fut.set_result({"result": {"value": 1}})

    resolver = asyncio.create_task(resolve_later())
    result = await conn.send("Runtime.evaluate", {"expression": "1"}, timeout_s=timeout_s)
    await resolver

    assert result == {"result": {"value": 1}}


@pytest.mark.asyncio
async def test_cdp_send_positive_timeout_still_times_out():
    """基础设施超时保留：显式给正数截止时仍然按截止失败。"""
    conn = CDPConnection("ws://127.0.0.1:9222/devtools/page/test")
    conn.ws = AsyncMock()
    conn.ws.send = AsyncMock()
    conn.set_liveness_checker(lambda: True)

    with pytest.raises(TimeoutError, match=r"timed out after 0\.05s"):
        await conn.send("Runtime.evaluate", {"expression": "1"}, timeout_s=0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_during_send", [False, True])
async def test_cancelled_cdp_command_releases_future(cancel_during_send):
    conn = CDPConnection("ws://fake/page")
    dispatched = asyncio.Event()
    conn.ws = AsyncMock()

    async def send(_payload):
        dispatched.set()
        if cancel_during_send:
            await asyncio.Event().wait()

    conn.ws.send = send
    conn.set_liveness_checker(lambda: True)
    task = asyncio.create_task(conn.send("Runtime.evaluate", timeout_s=None))
    await dispatched.wait()
    future = next(iter(conn._futures.values()))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert conn._futures == {}
    assert future.cancelled()
    assert not conn._closed


@pytest.mark.asyncio
async def test_navigation_cancellation_removes_listener_without_location_probe():
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={})
    unsub = MagicMock()
    conn.on.return_value = unsub
    page = CDPPage(conn, "page")
    dispatched = asyncio.Event()

    async def send(*_args, **_kwargs):
        dispatched.set()
        return {}

    conn.send.side_effect = send
    page.evaluate = AsyncMock()
    task = asyncio.create_task(page.goto("https://example.test"))
    await dispatched.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    unsub.assert_called_once()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("domain failed"), asyncio.CancelledError()])
async def test_isolated_tab_disposes_only_its_cookie_context(failure):
    from aistudio_api.infrastructure.browser.cdp_client import CDPClient

    client = CDPClient(9222)
    client.wait_until_ready = AsyncMock()
    client.get_version = AsyncMock(return_value={"webSocketDebuggerUrl": "ws://fake/browser"})
    client.get_targets = AsyncMock(return_value=[
        {"id": "isolated", "webSocketDebuggerUrl": "ws://fake/isolated"}
    ])
    browser = MagicMock(spec=CDPConnection)
    browser.connect = AsyncMock()
    browser.close = AsyncMock()
    browser.send = AsyncMock(side_effect=[
        {"browserContextId": "cookie-jar"}, {"targetId": "isolated"}, {},
    ])
    target = MagicMock(spec=CDPConnection)
    target.connect = AsyncMock()
    target.close = AsyncMock()
    target.send = AsyncMock(return_value={})
    with (
        patch("aistudio_api.infrastructure.browser.cdp_client.CDPConnection", side_effect=[browser, target]),
        patch.object(CDPPage, "init_domains", AsyncMock(side_effect=failure)),
    ):
        if failure is None:
            page = await client.create_new_page()
            assert page.browser_context_id == "cookie-jar"
            assert client.page is None
            await page.close()
        else:
            with pytest.raises(type(failure)):
                await client.create_new_page()
    browser.send.assert_any_await("Target.createBrowserContext")
    browser.send.assert_any_await(
        "Target.createTarget", {"url": "about:blank", "browserContextId": "cookie-jar"}
    )
    browser.send.assert_any_await(
        "Target.disposeBrowserContext", {"browserContextId": "cookie-jar"}
    )
    target.close.assert_awaited_once()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cookie_clear_targets_only_this_browser_context():
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={})
    page = CDPPage(conn, "isolated")
    page.browser_context_id = "old-account-jar"
    await page.clear_cookies()
    conn.send.assert_awaited_once_with(
        "Storage.clearCookies", {"browserContextId": "old-account-jar"}
    )


@pytest.mark.asyncio
async def test_cdp_close_rejects_waiter_without_running_receive_loop():
    conn = CDPConnection("ws://fake/page")
    conn.ws = AsyncMock()
    dispatched = asyncio.Event()

    async def send(_payload):
        dispatched.set()

    conn.ws.send = send
    task = asyncio.create_task(conn.send("Runtime.evaluate", timeout_s=None))
    await dispatched.wait()
    await conn.close()
    with pytest.raises(RuntimeError, match="CDP connection closed"):
        await task
    assert not conn._futures


@pytest.mark.asyncio
async def test_control_enter_needs_target_and_dispatches_one_key_pair():
    conn = MagicMock(spec=CDPConnection)
    conn.send = AsyncMock(return_value={})
    page = CDPPage(conn, "page")
    page.evaluate = AsyncMock(return_value=False)
    assert await page.send_control_enter() is False
    conn.send.assert_not_awaited()
    page.evaluate.return_value = True
    assert await page.send_control_enter() is True
    assert conn.send.await_count == 2
    assert "KeyboardEvent" not in page.evaluate.call_args.args[0]
    assert conn.send.call_args_list[0].args[1]["type"] == "rawKeyDown"
    assert conn.send.call_args_list[1].args[1]["type"] == "keyUp"
