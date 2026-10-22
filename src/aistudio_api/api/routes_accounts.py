"""账号管理路由。"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends, HTTPException, Path as FastApiPath
from pydantic import BaseModel, Field

from aistudio_api.api.dependencies import (
    get_account_service,
    get_runtime_state,
)
from aistudio_api.infrastructure.account.account_store import AccountMeta
from aistudio_api.infrastructure.account.cookie_parser import (
    parse_cookie_string,
    probe_account_benefit_tier,
    probe_account_email,
)
from aistudio_api.infrastructure.utils.common import mask_email

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState
    from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.utils.logger import get_logger

log = get_logger("accounts")

router = APIRouter(prefix="/accounts")


class AccountResponse(BaseModel):
    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None
    auth_user: str = "0"
    cookie_id: str | None = None
    tier: str = "Free"


def to_account_response(account: AccountMeta) -> AccountResponse:
    cid = account.cookie_id or f"cookie_{account.id}"
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at or "",
        last_used=account.last_used,
        auth_user=getattr(account, "auth_user", "0") or "0",
        cookie_id=cid,
        tier=getattr(account, "tier", "Free") or "Free",
    )


class UpdateAccountRequest(BaseModel):
    name: str


class ImportCookiesRequest(BaseModel):
    cookies: str
    name: str | None = None
    email: str | None = None
    account_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_\-\.@]+$")
    auth_user: str = "0"
    tier: str | None = None


class ImportCookiesResponse(BaseModel):
    account_id: str
    name: str
    cookie_count: int
    domain_summary: dict[str, int]
    auth_user: str = "0"
    tier: str = "Free"


class ProbeAndImportRequest(BaseModel):
    cookies: str
    name_prefix: str | None = None


class ProbeAndImportResponse(BaseModel):
    imported_count: int
    accounts: list[AccountResponse]


class BundleAccountItem(BaseModel):
    id: str | None = None
    name: str | None = None
    email: str | None = None
    cookies: str
    auth_user: str = "0"
    tier: str | None = None


class ImportBundleRequest(BaseModel):
    file_path: str | None = None
    content: str | None = None
    accounts: list[BundleAccountItem] | None = None


class ImportBundleResponse(BaseModel):
    imported_count: int
    accounts: list[AccountResponse]


@router.get("", response_model=list[AccountResponse])
@router.get("/", response_model=list[AccountResponse])
async def list_accounts(
    account_service: AccountService = Depends(get_account_service),
) -> list[AccountResponse]:
    """列出所有账号。"""
    accounts = account_service.list_accounts()
    return [to_account_response(a) for a in accounts]


@router.get("/active", response_model=AccountResponse)
async def get_active_account(
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """获取当前活跃账号。"""
    account = account_service.get_active_account()
    if account is None:
        raise HTTPException(status_code=404, detail="没有活跃账号")
    return to_account_response(account)


@router.post("/{account_id}/activate", response_model=AccountResponse)
async def activate_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> AccountResponse:
    """切换到指定账号。"""
    client = runtime_state.client
    browser_session = client._session if client else None

    if browser_session is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    account = await account_service.activate_account(account_id, browser_session)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在或切换失败")
    if runtime_state.rotator:
        runtime_state.rotator.add_account(account.id)
        runtime_state.rotator.clear_cooldown(account.id)
    log.info("手动激活账号: %s (%s)", account.id, account.name)
    return to_account_response(account)


@router.post("/{account_id}/detect-email", response_model=AccountResponse)
async def detect_account_email(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> AccountResponse:
    """自动提取并更新指定账号的真实邮箱与订阅权益等级（优先无感 HTTP 探测，必要时回退受控浏览器）。"""
    import json

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    target_auth = account_service.get_account_auth_path(account_id)
    browser_session = runtime_state.client._session if runtime_state.client else None
    email: str | None = None
    detected_tier: str | None = None

    # 1. 若受控浏览器当前正好处于该账号上下文，优先直接从当前页面提取（免切号）
    is_current_session = bool(
        browser_session is not None
        and browser_session.page is not None
        and (
            not target_auth
            or not getattr(browser_session, "_auth_file", None)
            or str(target_auth) == browser_session._auth_file
        )
    )
    if is_current_session and browser_session is not None:
        with contextlib.suppress(Exception):
            email = await browser_session.extract_account_email()
        if hasattr(browser_session, "extract_account_tier"):
            with contextlib.suppress(Exception):
                detected_tier = await browser_session.extract_account_tier()

    # 2. 通过 auth.json 凭据执行高速 HTTP 探测（无需切号，即使浏览器休眠也可用）
    if (
        (not email or not detected_tier or detected_tier == "Free")
        and target_auth
        and target_auth.is_file()
    ):
        try:
            auth_data = json.loads(target_auth.read_text(encoding="utf-8"))
            raw_cookies = auth_data.get("cookies") or []
            cookie_dict = {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in raw_cookies
                if isinstance(c, dict) and c.get("name")
            }
            if cookie_dict:
                if not email:
                    email = await probe_account_email(
                        cookie_dict, auth_user=acc.auth_user or "0"
                    )
                if not detected_tier or detected_tier == "Free":
                    http_tier = await probe_account_benefit_tier(
                        cookie_dict, auth_user=acc.auth_user or "0"
                    )
                    if (http_tier and http_tier != "Free") or not detected_tier:
                        detected_tier = http_tier
        except Exception as e:
            log.debug("通过 HTTP 探测账号 %s 邮箱/等级失败: %s", account_id, e)

    # 3. 若 HTTP 仍未取到邮箱且受控浏览器可用，回退切换浏览器会话提取
    if (
        not email
        and browser_session is not None
        and browser_session.page is not None
    ):
        original_auth = browser_session.auth_file
        switched = target_auth is not None and str(target_auth) != original_auth
        try:
            if switched:
                await browser_session.switch_auth(str(target_auth))
            email = await browser_session.extract_account_email()
            if (not detected_tier or detected_tier == "Free") and hasattr(
                browser_session, "extract_account_tier"
            ):
                with contextlib.suppress(Exception):
                    detected_tier = await browser_session.extract_account_tier()
        finally:
            if switched:
                await browser_session.switch_auth(original_auth)

    if email:
        account_service.update_account_email(account_id, email)
        log.info("已自动识别并更新账号 %s 邮箱: %s", account_id, mask_email(email))

    if detected_tier:
        account_service.update_account_tier(account_id, detected_tier)
        log.info("已自动识别并更新账号 %s 订阅等级: %s", account_id, detected_tier)

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(acc)


@router.post("/{account_id}/detect-tier", response_model=AccountResponse)
async def detect_account_tier(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> AccountResponse:
    """探测并更新指定账号的订阅权益等级 (Free / Pro / Ultra / Plus)。"""
    import json

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    detected_tier: str | None = None
    target_auth = account_service.get_account_auth_path(account_id)
    browser_session = runtime_state.client._session if runtime_state.client else None
    if (
        browser_session is not None
        and browser_session.page is not None
        and target_auth
        and str(target_auth) == browser_session._auth_file
        and hasattr(browser_session, "extract_account_tier")
    ):
        detected_tier = await browser_session.extract_account_tier()

    if not detected_tier and target_auth and target_auth.is_file():
        try:
            auth_data = json.loads(target_auth.read_text(encoding="utf-8"))
            raw_cookies = auth_data.get("cookies") or []
            cookie_dict = {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in raw_cookies
                if isinstance(c, dict) and c.get("name")
            }
            if cookie_dict:
                detected_tier = await probe_account_benefit_tier(
                    cookie_dict, auth_user=acc.auth_user or "0"
                )
        except Exception as e:
            log.debug("通过 HTTP 探测账号 %s 订阅等级失败: %s", account_id, e)

    if detected_tier:
        account_service.update_account_tier(account_id, detected_tier)
        log.info("已更新账号 %s 订阅等级: %s", account_id, detected_tier)

    acc = account_service.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    return to_account_response(acc)


async def _prepare_active_delete(
    account_service: AccountService,
    runtime_state: RuntimeState,
    deleted_ids: set[str],
) -> None:
    """先切到保留账号，切换失败时不删除现有凭据。"""
    remaining = [a for a in account_service.list_accounts() if a.id not in deleted_ids]
    new_active = remaining[0] if remaining else None
    client = runtime_state.client
    browser_session = client._session if client else None
    if new_active is not None:
        if browser_session is not None:
            account = await account_service.activate_account(new_active.id, browser_session)
            if account is None:
                raise HTTPException(status_code=503, detail="备用账号凭据不存在，未删除当前账号")
    elif browser_session is not None:
        await browser_session.clear_auth()


@router.delete("/{account_id}")
async def delete_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, bool]:
    """删除账号。"""
    active_account = account_service.get_active_account()
    is_active = active_account is not None and active_account.id == account_id
    if is_active:
        await _prepare_active_delete(account_service, runtime_state, {account_id})

    success = account_service.delete_account(account_id)
    if not success:
        raise HTTPException(status_code=404, detail="账号不存在")

    if runtime_state.rotator:
        runtime_state.rotator.remove_account(account_id)

    log.info("已删除账号: %s", account_id)
    return {"ok": True}


@router.delete("/group/{cookie_id}")
async def delete_cookie_group(
    cookie_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> dict[str, int]:
    """按 Cookie 组批量删除该 Cookie 下的所有子账号。"""
    active_account = account_service.get_active_account()
    active_id = active_account.id if active_account is not None else None

    accounts = account_service.list_accounts()
    deleted_ids = {a.id for a in accounts if to_account_response(a).cookie_id == cookie_id}
    if active_id in deleted_ids:
        await _prepare_active_delete(account_service, runtime_state, deleted_ids)
    deleted_count = 0
    rotator = runtime_state.rotator
    for a in accounts:
        acc_cid = to_account_response(a).cookie_id
        if acc_cid == cookie_id and account_service.delete_account(a.id):
            deleted_count += 1
            if rotator:
                rotator.remove_account(a.id)
            log.info("已删除组 %s 下的子账号 %s", cookie_id, a.id)


    return {"deleted": deleted_count}


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str = FastApiPath(..., pattern=r"^[a-zA-Z0-9_\-\.@]+$"),
    req: UpdateAccountRequest = Body(...),
    account_service: AccountService = Depends(get_account_service),
) -> AccountResponse:
    """更新账号名称。"""
    account = account_service.update_account(account_id, req.name)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    log.info("账号已更新: %s -> %s", account.id, req.name)
    return to_account_response(account)


async def _refresh_imported_active(
    accounts: list[AccountMeta],
    account_service: AccountService,
    runtime_state: RuntimeState,
) -> None:
    active = account_service.get_active_account()
    client = runtime_state.client
    if (
        active is None
        or not any(a.id == active.id for a in accounts)
        or client is None
    ):
        return
    try:
        await account_service.activate_account(active.id, client.session)
    except Exception as exc:
        log.warning("凭据已保存，但刷新活跃账号 %s 失败: %s", active.id, exc)


@router.post("/import-cookies", response_model=ImportCookiesResponse)
async def import_cookies(
    req: ImportCookiesRequest,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ImportCookiesResponse:
    """从 cookie 导入账号（支持 JSON 数组、Netscape 或 KV）。"""
    storage_state = parse_cookie_string(req.cookies)
    raw_cookies = storage_state.get("cookies")
    cookie_list: list[dict[str, object]] = (
        raw_cookies if isinstance(raw_cookies, list) else []
    )
    cookie_count = len(cookie_list)

    if cookie_count == 0:
        raise HTTPException(status_code=400, detail="未解析到有效 cookie")

    domain_summary: dict[str, int] = {}
    for cookie in cookie_list:
        domain = str(cookie.get("domain", ""))
        domain_summary[domain] = domain_summary.get(domain, 0) + 1
    name = req.name or (
        f"Google Account (u/{req.auth_user})" if req.auth_user != "0" else "导入的账号"
    )

    import secrets

    cid = f"cookie_{secrets.token_hex(4)}"
    detected_email = req.email
    detected_tier = req.tier
    if not detected_email or not detected_tier:
        raw_cookies_list = storage_state.get("cookies")
        if isinstance(raw_cookies_list, list):
            cookie_dict = {
                str(c.get("name") or ""): str(c.get("value") or "")
                for c in raw_cookies_list
                if isinstance(c, dict) and c.get("name")
            }
            if cookie_dict:
                if not detected_email:
                    with contextlib.suppress(Exception):
                        detected_email = await probe_account_email(
                            cookie_dict, auth_user=req.auth_user or "0"
                        )
                if not detected_tier:
                    with contextlib.suppress(Exception):
                        probed = await probe_account_benefit_tier(
                            cookie_dict, auth_user=req.auth_user or "0"
                        )
                        if probed and probed != "Free":
                            detected_tier = probed
    detected_tier = detected_tier or "Free"
    account = account_service.save_account_from_cookies(
        name=name,
        email=detected_email,
        storage_state=storage_state,
        account_id=req.account_id,
        auth_user=req.auth_user or "0",
        cookie_id=cid,
        tier=detected_tier,
    )
    if runtime_state.rotator:
        runtime_state.rotator.add_account(account.id)
        runtime_state.rotator.clear_cooldown(account.id)
    await _refresh_imported_active([account], account_service, runtime_state)
    return ImportCookiesResponse(
        account_id=account.id,
        name=account.name,
        cookie_count=cookie_count,
        domain_summary=domain_summary,
        auth_user=account.auth_user,
        tier=account.tier,
    )


@router.post("/probe-import", response_model=ProbeAndImportResponse)
async def probe_and_import(
    req: ProbeAndImportRequest,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ProbeAndImportResponse:
    """单份 Cookie 无限向下探活多账号并一键批量导入。"""
    from aistudio_api.infrastructure.account.cookie_parser import (
        parse_cookie_string,
        probe_google_accounts_infinite,
    )

    probed = await probe_google_accounts_infinite(req.cookies)
    if not probed:
        raise HTTPException(status_code=400, detail="未探测到有效已登录 Google 账号")

    storage_state = parse_cookie_string(req.cookies)
    prefix = req.name_prefix.strip() if req.name_prefix else "Google Account"
    import secrets

    cid = f"cookie_{secrets.token_hex(4)}"
    metas = account_service.batch_import_accounts(
        probed_list=probed,
        storage_state=storage_state,
        prefix=prefix,
        cookie_id=cid,
    )
    rotator = getattr(runtime_state, "rotator", None)
    if rotator:
        for meta in metas:
            rotator.add_account(meta.id)
            rotator.clear_cooldown(meta.id)

    imported_accounts = [to_account_response(account) for account in metas]

    await _refresh_imported_active(metas, account_service, runtime_state)
    log.info("探活与导入完成: 成功导入 %d 个账号", len(imported_accounts))
    return ProbeAndImportResponse(
        imported_count=len(imported_accounts),
        accounts=imported_accounts,
    )


@router.post("/import-bundle", response_model=ImportBundleResponse)
async def import_bundle(
    req: ImportBundleRequest,
    account_service: AccountService = Depends(get_account_service),
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> ImportBundleResponse:
    """批量导入多个账号的凭据 Bundle（支持传入 JSON 文本、直接解析列表或服务器本地文件路径）。"""
    import json
    import secrets
    from pathlib import Path

    from aistudio_api.infrastructure.account.cookie_parser import parse_cookie_string

    items_to_import: list[dict[str, object]] = []

    if req.accounts:
        for a in req.accounts:
            items_to_import.append(
                {
                    "id": a.id,
                    "name": a.name or "Google Account",
                    "email": a.email,
                    "cookies": a.cookies,
                    "auth_user": a.auth_user or "0",
                    "tier": a.tier or "Free",
                }
            )
    elif req.content:
        try:
            raw_data = json.loads(req.content)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"JSON 内容解析失败: {e}"
            ) from e
        if isinstance(raw_data, list):
            raw_list = raw_data
        elif isinstance(raw_data, dict):
            raw_list = raw_data.get("accounts") or raw_data.get("profiles") or []
        else:
            raw_list = []
        for item in raw_list:
            if isinstance(item, dict) and item.get("cookies"):
                items_to_import.append(item)
    elif req.file_path:
        fpath = Path(req.file_path).expanduser().resolve()
        if not fpath.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {fpath}")
        try:
            raw_text = fpath.read_text(encoding="utf-8")
            raw_data = json.loads(raw_text)
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"读取或解析文件失败: {e}"
            ) from e
        if isinstance(raw_data, list):
            raw_list = raw_data
        elif isinstance(raw_data, dict):
            raw_list = raw_data.get("accounts") or raw_data.get("profiles") or []
        else:
            raw_list = []
        for item in raw_list:
            if isinstance(item, dict) and item.get("cookies"):
                items_to_import.append(item)
    else:
        raise HTTPException(
            status_code=400,
            detail="请提供 accounts 列表、content JSON 文本或 file_path 本地文件路径",
        )

    if not items_to_import:
        raise HTTPException(
            status_code=400, detail="未解析到任何包含有效 cookies 的账号条目"
        )

    imported_accounts: list[AccountResponse] = []
    for idx, item in enumerate(items_to_import):
        raw_cookie_str = item.get("cookies")
        if not raw_cookie_str:
            continue
        storage_state = parse_cookie_string(raw_cookie_str)
        if not storage_state.get("cookies"):
            continue

        acc_name = str(item.get("name") or f"Account {idx + 1}")
        acc_email = str(item["email"]) if item.get("email") else None
        acc_auth_user = str(item.get("auth_user") or "0")
        acc_tier = str(item.get("tier") or "")
        if not acc_email or not acc_tier:
            raw_cookies_list = storage_state.get("cookies")
            if isinstance(raw_cookies_list, list):
                cookie_dict = {
                    str(c.get("name") or ""): str(c.get("value") or "")
                    for c in raw_cookies_list
                    if isinstance(c, dict) and c.get("name")
                }
                if cookie_dict:
                    if not acc_email:
                        with contextlib.suppress(Exception):
                            acc_email = await probe_account_email(
                                cookie_dict, auth_user=acc_auth_user
                            )
                    if not acc_tier:
                        with contextlib.suppress(Exception):
                            probed = await probe_account_benefit_tier(
                                cookie_dict, auth_user=acc_auth_user
                            )
                            if probed and probed != "Free":
                                acc_tier = probed
        acc_tier = acc_tier or "Free"
        acc_id = str(item["id"]) if item.get("id") else None
        cid = f"cookie_{secrets.token_hex(4)}"

        account = account_service.save_account_from_cookies(
            name=acc_name,
            email=acc_email,
            storage_state=storage_state,
            account_id=acc_id,
            auth_user=acc_auth_user,
            cookie_id=cid,
            tier=acc_tier,
        )
        rotator = getattr(runtime_state, "rotator", None)
        if rotator:
            rotator.add_account(account.id)
            rotator.clear_cooldown(account.id)
        imported_accounts.append(to_account_response(account))
    if not imported_accounts:
        raise HTTPException(
            status_code=400, detail="未解析到任何包含有效 cookies 的账号条目"
        )
    imported_metas = [
        account for item in imported_accounts
        if (account := account_service.get_account(item.id)) is not None
    ]
    await _refresh_imported_active(imported_metas, account_service, runtime_state)
    log.info("批量文件导入完成: 成功入库 %d 个账号", len(imported_accounts))
    return ImportBundleResponse(
        imported_count=len(imported_accounts),
        accounts=imported_accounts,
    )
