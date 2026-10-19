"""BotGuard WAA bootstrap probe and base request template interception.

两条引导路线：

1. **binding 模式（新增，默认优先）** —— 不替页面发任何消息：
   页面加载后用 DI 探针接住 BotGuardService 单例（`install_hooks.js`），
   从页面自己的 `/$rpc/` 请求里采集公共头，再用一次**探针签名**确认
   「不点发送也能签出 WAA proof」，然后把请求模板拼出来就完事。
   签名（proof）与页面输入框状态强绑定这件事在 `generate_snapshot` 里处理：
   每次签名前把内容哈希的原文写进 `ms-prompt-box textarea`（binding）。

2. **page_send 模式（兜底，原实现）** —— 往输入框写一句 `say '1'` 并点 Run /
   Ctrl+Enter，让页面自己发一次 GenerateContent，靠这次真实请求把服务和模板
   都带出来。只在 binding 模式验证不过、或调用方显式要求时使用。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from hashlib import sha256

from aistudio_api.domain.errors import SessionExpiredError
from aistudio_api.infrastructure.browser.cdp_client import CDPPage
from aistudio_api.infrastructure.browser.scripts import (
    DIALOG_CLEANUP_JS,
    DOM_GC_CLEANUP_JS,
    INSTALL_HOOKS_JS,
    PROMPT_STATE_JS,
    SNAPSHOT_PROBE_JS,
    STOP_GENERATION_JS,
    build_snapshot_probe_args,
)
from aistudio_api.infrastructure.gateway.session_auth import is_login_page_url
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("session.botguard")

BOTGUARD_BOOTSTRAP_PROMPT = "say '1'"

# binding 模式的引导预算：等「服务 + 公共头 + 探针签名成功」。
# 冷启动那一发要等 BotGuard Wasm 从 CDN 下来并完成握手（实测 15~25 秒），
# 预算给小了会在 VM 刚就绪前一秒放弃、白白回退到 page_send。
BINDING_WAIT_S = 35.0
# 探针签名单次上限：页面 zone 冻结时 snapshot 是静默挂死，只能靠超时兜
PROBE_TIMEOUT_MS = 12000
# 探针签名用的固定原文（只用来验证签名链路，与任何真实请求无关）
PROBE_BINDING_INPUT = "aistudio-api/waa-probe"
# 连续这么多次 service_unavailable 就提前回退 page_send。
# 服务是页面加载时由 Angular inject 出来的（延迟初始化只发生在「页面自己发消息」之后），
# 等下去不会自己出现；每条 WARN 之间隔 1 秒，等满 35 秒纯属白等。
SERVICE_UNAVAILABLE_BAIL = 3

DEFAULT_BOOTSTRAP_TEMPLATE: dict[str, object] = {
    "url": "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent",
    "headers": {
        "Content-Type": "application/json+protobuf",
        "X-User-Agent": "grpc-web-javascript/0.1",
        "X-Goog-Api-Key": "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs",
        "X-Goog-AuthUser": "0",
        "Referer": "https://aistudio.google.com/",
    },
    "body": '["models/gemini-3.7-flash"]',
}
HEADER_DENY = ("content-length", "host", "connection")

OPEN_PLAYGROUND_JS = """(() => {
    const clickable = Array.from(document.querySelectorAll('button, mat-card, a, [role="button"], [role="tab"], .nav-item'));
    const targets = ['Playground', 'Code and Chat', 'Chat prompt', 'New chat', 'Start building', 'Create prompt'];
    for (const term of targets) {
        const target = clickable.find(el => (el.innerText || el.textContent || '').includes(term));
        if (target) { target.click(); return term; }
    }
    return '';
})()"""


async def install_page_hooks(page: CDPPage) -> str:
    """Install WAA service hooks on page and extract snapshot function key."""
    initial_url = getattr(page, "url", "") or ""
    if is_login_page_url(initial_url):
        raise SessionExpiredError(
            f"Cookie 认证失效，当前页面为 Google 登录页: {initial_url}"
        )

    result = await page.evaluate(INSTALL_HOOKS_JS)
    if result == "already_hooked":
        snap_key = await page.evaluate("() => window.__AISTUDIO__?.snapKey || ''")
        if snap_key and isinstance(snap_key, str):
            return snap_key
        return "snapKey"
    if isinstance(result, str) and result.startswith("hooked:"):
        return result.split(":", 1)[1]

    for _ in range(3):
        await page.wait_for_timeout(1500)
        curr_url = getattr(page, "url", "") or ""
        if is_login_page_url(curr_url):
            raise SessionExpiredError(
                f"Cookie 认证失效，当前页面为 Google 登录页: {curr_url}"
            )
        result = await page.evaluate(INSTALL_HOOKS_JS)
        if result == "already_hooked":
            snap_key = await page.evaluate("() => window.__AISTUDIO__?.snapKey || ''")
            if snap_key and isinstance(snap_key, str):
                return snap_key
            return "snapKey"
        if isinstance(result, str) and result.startswith("hooked:"):
            return result.split(":", 1)[1]

    page_url = getattr(page, "url", "(unknown)")
    if is_login_page_url(str(page_url)):
        raise SessionExpiredError(
            f"Cookie 认证失效，当前页面为 Google 登录页: {page_url}"
        )
    page_title = await page.title()
    if (
        isinstance(page_title, str)
        and "sign in - google accounts" in page_title.lower()
    ):
        raise SessionExpiredError(
            f"Cookie 认证失效，当前页面为 Google 登录页: {page_url} (title={page_title!r})"
        )
    raise RuntimeError(
        f"Hook install failed: {result} (url={page_url}, title={page_title!r})"
    )


async def enable_temporary_chat(page: CDPPage) -> bool:
    """Enable Temporary Chat mode in AI Studio to prevent polluting user history."""
    try:
        return bool(
            await page.evaluate(
                """(() => {
                    const clickable = Array.from(document.querySelectorAll('button, [role="button"], mat-slide-toggle, a'));
                    const tempBtn = clickable.find(el => {
                        const s = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('mattooltip') || '')).toLowerCase();
                        return (s.includes('temporary chat') || s.includes('temporary mode') || s.includes('turn on temporary'))
                            && !s.includes('data use policy');
                    });
                    if (tempBtn) {
                        tempBtn.click();
                        return true;
                    }

                    const moreBtn = clickable.find(el => {
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const txt = (el.innerText || '').trim();
                        return aria === 'view more actions' || txt === 'more_vert';
                    });
                    if (moreBtn && moreBtn.getAttribute('aria-expanded') !== 'true') {
                        moreBtn.click();
                        const menuItem = Array.from(document.querySelectorAll('[role="menuitem"], .mat-mdc-menu-item')).find(item => {
                            const s = ((item.innerText || '') + ' ' + (item.getAttribute('aria-label') || '')).toLowerCase();
                            return s.includes('temporary');
                        });
                        if (menuItem) {
                            menuItem.click();
                            return true;
                        }
                        moreBtn.click();
                    }
                    return false;
                })()"""
            )
        )
    except Exception as e:
        log.debug("尝试激活 Temporary chat 异常: %s", e)
        return False


async def click_run_button(page: CDPPage) -> bool:
    """Find and trigger Run / Build button or dispatch Control+Enter shortcut."""
    clicked = await page.evaluate(
        """(() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const runBtn = buttons.find(b => {
                const t = (b.innerText || b.textContent || '').trim();
                return t === 'Run' || t.startsWith('Run\\n') || t.startsWith('Run Ctrl')
                    || t === 'Build' || t.startsWith('Build\\n') || t.startsWith('BuildCtrl')
                    || b.classList.contains('build-button')
                    || b.classList.contains('ctrl-enter-submits');
            });
            if (runBtn) {
                runBtn.click();
                return true;
            }
            return false;
        })()"""
    )
    if clicked:
        return True

    if await page.send_control_enter("textarea"):
        return True

    if await page.click("button.build-button"):
        return True
    if await page.click("button.ctrl-enter-submits"):
        return True
    if await page.click("button:has-text('Build')"):
        return True
    if await page.click("button:has-text('Run')"):
        return True
    return await page.click("button:has(mat-icon)")


async def read_prompt_state(page: CDPPage) -> dict[str, object]:
    """读取输入框/服务/请求头状态（binding 模式的就绪判据，也用于日志）。"""
    try:
        state = await page.evaluate(PROMPT_STATE_JS)
    except Exception as e:
        log.debug("读取输入框状态失败: %s", e)
        return {}
    return state if isinstance(state, dict) else {}


async def read_rpc_headers(page: CDPPage) -> dict[str, str]:
    """页面自己发 `/$rpc/` 请求时带的公共头（已由 install_hooks.js 采集）。"""
    try:
        raw = await page.evaluate(
            "() => (window.__AISTUDIO__ && window.__AISTUDIO__.rpcHeaders) || null"
        )
    except Exception as e:
        log.debug("读取页面请求头失败: %s", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    headers: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).lower()
        if name in HEADER_DENY:
            continue
        headers[name] = str(value)
    return headers


def build_binding_template(headers: dict[str, str]) -> dict[str, object]:
    """用默认模板 + 页面采集到的公共头拼出请求模板（不依赖页面发过生成请求）。"""
    merged = {
        str(key).lower(): str(value)
        for key, value in dict(DEFAULT_BOOTSTRAP_TEMPLATE.get("headers") or {}).items()
    }
    # 页面采集到的头优先：里面是当前会话的 x-goog-api-key / x-aistudio-* / x-goog-ext-*
    merged.update(headers)
    merged.setdefault("content-type", "application/json+protobuf")
    return {
        "url": str(DEFAULT_BOOTSTRAP_TEMPLATE.get("url") or ""),
        "headers": merged,
        "body": str(DEFAULT_BOOTSTRAP_TEMPLATE.get("body") or ""),
        "bootstrap_mode": "binding",
    }


async def probe_waa_snapshot(page: CDPPage, timeout_ms: int = PROBE_TIMEOUT_MS) -> tuple[bool, str]:
    """探针签名：确认「不替页面发消息」也能签出 proof。

    这是 binding 模式的放行条件。失败就退回 page_send 引导，避免把一个
    签不出来的会话交上去、让每个真实请求都吃 403 [7]。
    """
    probe_hash = sha256(PROBE_BINDING_INPUT.encode("utf-8")).hexdigest()
    try:
        result = await page.evaluate(
            SNAPSHOT_PROBE_JS,
            args=build_snapshot_probe_args(content_hash=probe_hash, timeout_ms=timeout_ms),
            timeout_s=(timeout_ms / 1000) + 5.0,
        )
    except Exception as e:
        return False, f"evaluate_error: {e}"
    if isinstance(result, dict):
        if result.get("ok"):
            return True, f"proof {result.get('len')} chars"
        return False, str(result.get("reason") or "unknown")
    return False, f"unexpected result: {result!r}"


async def try_binding_bootstrap(
    page: CDPPage,
    *,
    is_proc_alive_cb: Callable[[], bool] | None = None,
    max_wait_s: float = BINDING_WAIT_S,
) -> dict[str, object] | None:
    """binding 模式引导：不发消息，等「输入框 + 服务 + 公共头」齐了再探针签名。"""
    deadline = time.time() + max_wait_s
    last_reason = "not_ready"
    probe_failures = 0
    service_unavailable_streak = 0
    while time.time() < deadline:
        if is_proc_alive_cb is not None and not is_proc_alive_cb():
            return None
        if page.is_closed():
            return None

        state = await read_prompt_state(page)
        if not state.get("service") or not state.get("snap_key"):
            # 页面模块可能比钩子晚到：重装一次（幂等），下一轮再看
            with suppress(Exception):
                await page.evaluate(INSTALL_HOOKS_JS)
        if not state.get("has_box"):
            last_reason = "prompt_box_missing"
            await page.wait_for_timeout(500)
            continue

        headers = await read_rpc_headers(page)
        if not headers or not (headers.get("x-goog-api-key") or headers.get("x-aistudio-visit-id")):
            last_reason = "rpc_headers_missing"
            await page.wait_for_timeout(500)
            continue

        ok, detail = await probe_waa_snapshot(page)
        if ok:
            # 关键：binding 模式没有「等页面自己发一次」的环节，必须手动关掉
            # install_hooks.js 里的 bootstrap 拦截开关。否则我们后面用页面 XHR
            # 重放 GenerateContent 时会被自己的钩子拦掉（表现是
            # `replay failed: timeout`，XHR 被 abort、status=0）。
            with suppress(Exception):
                await page.evaluate(
                    "() => { if (window.__AISTUDIO__) "
                    "window.__AISTUDIO__.interceptBootstrap = false; return true; }"
                )
            log.info(
                "binding 引导就绪: 探针=%s, 输入框=%s, 页面头=%d 个, 页面可见=%s, 服务来源=%s",
                detail,
                state.get("has_box"),
                len(headers),
                state.get("visible"),
                state.get("capture_via") or "-",
            )
            return build_binding_template(headers)

        last_reason = f"probe_failed({detail})"
        probe_failures += 1
        service_unavailable_streak = (
            service_unavailable_streak + 1 if detail == "service_unavailable" else 0
        )
        # 不能每轮（1s）刷一条 WARN：冷启动时会连刷 35 行。
        # 第一轮 + 每 10 轮各报一次，其余降到 DEBUG。
        if probe_failures == 1 or probe_failures % 10 == 0:
            log.warning(
                "binding 引导探针签名失败: %s（已等待 %d 秒，继续）",
                detail,
                probe_failures,
            )
        else:
            log.debug("binding 引导探针签名失败: %s（第 %d 次）", detail, probe_failures)
        if service_unavailable_streak >= SERVICE_UNAVAILABLE_BAIL:
            # 服务没被接住等多久都不会出现（页面只在「自己发消息」时才注入），
            # 提前走 page_send，别让每次启动白等 35 秒 × worker 数。
            log.info(
                "binding 探针连续 %d 次抓不到签名服务（service_unavailable），提前回退 page_send",
                service_unavailable_streak,
            )
            return None
        await page.wait_for_timeout(1000)

    log.info("binding 引导未就绪（%s），%.0f 秒预算用尽", last_reason, max_wait_s)
    return None


async def _wait_prompt_box(
    page: CDPPage,
    reload_cookies_cb: Callable[[CDPPage], Awaitable[bool]] | None,
) -> None:
    """等 playground 的输入框出现；顺带处理登录页重定向与地区限制。"""
    try:
        try:
            await page.wait_for_selector("ms-prompt-box textarea", timeout_s=15.0)
        except Exception:
            await page.wait_for_selector("textarea", timeout_s=5.0)
    except Exception as err:
        dbg_url = getattr(page, "url", "") or ""
        if is_login_page_url(dbg_url):
            log.warning("等待输入框期间检测到登录页重定向: %s，尝试重新注入 Cookie...", dbg_url)
            if reload_cookies_cb and await reload_cookies_cb(page):
                await page.wait_for_selector("textarea", timeout_s=15.0)
            else:
                raise SessionExpiredError(
                    f"Cookie 认证失效，已被重定向到 Google 登录页: {dbg_url}"
                ) from err
        if "available-regions" in dbg_url:
            raise RuntimeError(
                "Google AI Studio 地区限制: 访问被重定向至 available-regions"
            ) from err
        dbg_title = await page.title()
        raise RuntimeError(
            f"textarea not found while capturing BotGuardService; url={dbg_url}, title={dbg_title}"
        ) from err


async def _bootstrap_with_page_send(
    page: CDPPage,
    *,
    is_proc_alive_cb: Callable[[], bool] | None = None,
    max_wait_seconds: int = 45,
) -> dict[str, object]:
    """兜底路线：往输入框写一句 `say '1'` 并让页面自己发一次，靠它带出服务与模板。"""
    captured: dict[str, object] = {}
    original_text = ""

    def on_req(req: dict[str, object]) -> None:
        url = str(req.get("url") or "")
        if "GenerateContent" not in url or "Count" in url or captured:
            return
        body = str(req.get("post_data") or "")
        if not body:
            return
        captured["url"] = url
        captured["headers"] = req.get("headers") or {}
        captured["body"] = body

    unsub = page.on_request(on_req)

    async def harvest_in_page_template() -> None:
        if captured:
            return
        in_page_tpl = await page.evaluate(
            "() => window.__AISTUDIO__?.capturedTemplate || null"
        )
        if (
            isinstance(in_page_tpl, dict)
            and in_page_tpl.get("url")
            and in_page_tpl.get("body")
        ):
            captured["url"] = str(in_page_tpl["url"])
            captured["headers"] = in_page_tpl.get("headers") or {}
            captured["body"] = str(in_page_tpl["body"])

    t0 = time.time()
    try:
        original_text = str(
            (
                await page.evaluate(
                    "() => document.querySelector('textarea')?.value || ''"
                )
            )
            or ""
        )
        await enable_temporary_chat(page)
        await page.fill("textarea", BOTGUARD_BOOTSTRAP_PROMPT)
        await page.wait_for_timeout(600)
        await page.evaluate(DIALOG_CLEANUP_JS)

        if not await click_run_button(page):
            raise RuntimeError(
                "failed to trigger run button while capturing BotGuardService"
            )

        for i in range(max_wait_seconds):
            if is_proc_alive_cb is not None and not is_proc_alive_cb():
                raise RuntimeError("Browser process died during BotGuard capture")
            if page.is_closed():
                raise RuntimeError("Browser page closed during BotGuard capture")

            curr_url = getattr(page, "url", "") or ""
            if is_login_page_url(curr_url):
                raise SessionExpiredError(
                    f"Cookie 认证失效，已跳转至登录页: {curr_url}"
                )

            await page.wait_for_timeout(1000)
            # 判据必须是「模板到手」而不是「服务出现」：DI 探针在页面加载时就把
            # 服务接住了，拿 __AISTUDIO__.service 当「页面已经发出请求」的信号会
            # 在页面还没发之前就提前收工（实测表现为 wire template missing）。
            with suppress(Exception):
                await harvest_in_page_template()
            if captured:
                with suppress(Exception):
                    await page.evaluate(STOP_GENERATION_JS)
                await page.fill("textarea", original_text)
                with suppress(Exception):
                    await page.evaluate(DOM_GC_CLEANUP_JS)

                log.info(
                    "BotGuard 握手成功并截获模板（page_send 模式），耗时 %d 秒 (累计 %.1f 秒)",
                    i + 1,
                    time.time() - t0,
                )
                captured["bootstrap_mode"] = "page_send"
                return captured

            if await page.evaluate("() => !!window.__AISTUDIO__?.service"):
                # 服务已在：页面这一次请求应该已经发出去了，再给 2.5 秒收模板
                for _ in range(25):
                    if captured:
                        break
                    with suppress(Exception):
                        await harvest_in_page_template()
                    if captured:
                        break
                    await page.wait_for_timeout(100)
                if captured:
                    with suppress(Exception):
                        await page.evaluate(STOP_GENERATION_JS)
                    await page.fill("textarea", original_text)
                    with suppress(Exception):
                        await page.evaluate(DOM_GC_CLEANUP_JS)
                    log.info(
                        "BotGuard 握手成功并截获模板（page_send 模式），耗时 %d 秒 (累计 %.1f 秒)",
                        i + 1,
                        time.time() - t0,
                    )
                    captured["bootstrap_mode"] = "page_send"
                    return captured

        raise RuntimeError(f"BotGuardService capture timeout after {max_wait_seconds}s")
    finally:
        unsub()
        with suppress(Exception):
            await page.evaluate(
                "() => { if (window.__AISTUDIO__) window.__AISTUDIO__.interceptBootstrap = false; }"
            )
        with suppress(Exception):
            await page.fill("textarea", original_text)


async def bootstrap_botguard_and_template(
    page: CDPPage,
    reload_cookies_cb: Callable[[CDPPage], Awaitable[bool]] | None = None,
    is_proc_alive_cb: Callable[[], bool] | None = None,
    max_wait_seconds: int = 45,
    allow_page_send: bool = True,
    prefer_binding: bool = True,
) -> tuple[str, dict[str, object]]:
    """引导 BotGuard：优先 binding 模式（不替页面发消息），失败才回退 page_send。

    返回 `(snap_key, captured)`；`captured["bootstrap_mode"]` 标明走了哪条路。
    """
    current_url = getattr(page, "url", "") or ""
    if "available-regions" in current_url:
        raise RuntimeError(
            "Google AI Studio 地区限制: 访问被重定向至 available-regions"
        )
    if is_login_page_url(current_url):
        raise SessionExpiredError(
            f"Cookie 认证失效，当前页面为 Google 登录页: {current_url}"
        )

    snap_key = await install_page_hooks(page)
    t0 = time.time()

    await page.evaluate(DIALOG_CLEANUP_JS)
    await page.evaluate(OPEN_PLAYGROUND_JS)
    await page.wait_for_timeout(800)
    await _wait_prompt_box(page, reload_cookies_cb)

    if prefer_binding:
        template = await try_binding_bootstrap(
            page,
            is_proc_alive_cb=is_proc_alive_cb,
            max_wait_s=min(BINDING_WAIT_S, float(max_wait_seconds)),
        )
        if template is not None:
            log.info(
                "BotGuard 引导完成（binding 模式，未替页面发消息，累计 %.1f 秒）",
                time.time() - t0,
            )
            return snap_key, template
        if not allow_page_send:
            raise RuntimeError(
                "BotGuard binding 引导失败，且调用方禁止替页面发消息（allow_page_send=False）"
            )
        log.info("binding 引导未通过，回退到「页面发一条探针消息」的引导方式")

    captured = await _bootstrap_with_page_send(
        page,
        is_proc_alive_cb=is_proc_alive_cb,
        max_wait_seconds=max_wait_seconds,
    )
    return snap_key, captured
