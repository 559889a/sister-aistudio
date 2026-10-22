"""Browser automation JavaScript scripts and parameterization utilities."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path

_JS_DIR = Path(__file__).resolve().parent / "js"
_GZIP_MIN_BYTES = 96 * 1024


def _load_js(filename: str) -> str:
    return (_JS_DIR / filename).read_text(encoding="utf-8")


INSTALL_HOOKS_JS = _load_js("install_hooks.js")
DIALOG_CLEANUP_JS = _load_js("dialog_cleanup.js")
STREAMING_INIT_JS = _load_js("streaming_init.js")
STREAM_CLEANUP_JS = _load_js("stream_cleanup.js")
HOOKED_REQUEST_JS = _load_js("hooked_request.js")
SNAPSHOT_GENERATE_JS = _load_js("snapshot_generate.js")
SNAPSHOT_PROBE_JS = _load_js("snapshot_probe.js")
PROMPT_STATE_JS = _load_js("prompt_state.js")
CLEAR_BINDING_JS = _load_js("clear_binding.js")
CHECK_IDENTITY_JS = _load_js("check_identity.js")
STOP_GENERATION_JS = _load_js("stop_generation.js")
DOM_GC_CLEANUP_JS = _load_js("dom_gc_cleanup.js")
EXTRACT_EMAIL_JS = _load_js("extract_email.js")


def build_document_start_hooks_js() -> str:
    """Wrap INSTALL_HOOKS_JS for Page.addScriptToEvaluateOnNewDocument.

    页面脚本比我们的注入先跑的话，Angular DI 已经造完 BotGuardService 单例，
    探针就再也抓不到那个实例了。所以钩子必须在 document-start 装上；而那一刻
    `default_MakerSuite` 往往还没出现，于是这里自带轮询，直到装上为止。
    """
    install = INSTALL_HOOKS_JS.strip()
    return (
        "(() => {\n"
        "    if (window.__aistudio_docstart_hooks) return 'already';\n"
        "    window.__aistudio_docstart_hooks = true;\n"
        "    const install = () => {\n"
        f"        try {{ return ({install}); }} catch (e) {{ return 'err:' + String(e); }}\n"
        "    };\n"
        "    const tick = () => { const state = String(install()); return state.indexOf('hooked:') === 0 || state === 'already_hooked'; };\n"
        "    if (tick()) return 'done';\n"
        "    const timer = setInterval(() => {\n"
        "        if (tick()) clearInterval(timer);\n"
        "    }, 50);\n"
        "    setTimeout(() => clearInterval(timer), 60000);\n"
        "    return 'polling';\n"
        "})()\n"
    )


def build_snapshot_args(
    *, binding: str, content_hash: str, binding_id: str
) -> dict[str, object]:
    """SNAPSHOT_GENERATE_JS 的参数：binding 与哈希输入必须是同一份文本。"""
    return {"binding": binding, "hash": content_hash, "bindingId": binding_id}


def build_snapshot_probe_args(*, content_hash: str, timeout_ms: int) -> dict[str, object]:
    """SNAPSHOT_PROBE_JS 的参数（引导期验证「不发消息也能签」）。"""
    return {"hash": content_hash, "timeoutMs": timeout_ms}


def build_streaming_init_args(
    *, url: str, headers: dict[str, str], body: str, rid: str
) -> dict[str, object]:
    """Prepare argument object for STREAMING_INIT_JS evaluation.

    没有 timeout：生成等待时长不再受看门狗约束（只保留浏览器/CDP 的基础设施超时）。
    """
    return {
        **build_hooked_request_args(url=url, headers=headers, body=body),
        "rid": rid,
    }


def build_hooked_request_args(
    *, url: str, headers: dict[str, str], body: str
) -> dict[str, object]:
    """Prepare argument object for HOOKED_REQUEST_JS evaluation（同样不传超时）。"""
    args: dict[str, object] = {"url": url, "headers": headers, "body": body}
    if any(
        key.lower() == "content-encoding" and value.lower() != "identity"
        for key, value in headers.items()
    ):
        return args
    raw = body.encode("utf-8")
    if len(raw) >= _GZIP_MIN_BYTES:
        # binding 与 WAA 已按原文完成；只压缩最终传输体，保留全部内容与 proof。
        args["body"] = base64.b64encode(gzip.compress(raw, compresslevel=6)).decode(
            "ascii"
        )
        args["headers"] = {
            **{
                key: value
                for key, value in headers.items()
                if key.lower() != "content-encoding"
            },
            "Content-Encoding": "gzip",
        }
        args["gzip"] = True
    return args


def build_selector_match_js(selector: str) -> str:
    """Generate in-page element query snippet for standard CSS, text=, or :has-text() selector."""
    return f"""(() => {{
        const sel = {json.dumps(selector)};
        if (sel.includes(':has-text(')) {{
            const m = sel.match(/^(.*?):has-text\\(['"](.*?)['"]\\)(.*)$/);
            if (m) {{
                const tag = m[1].trim() || '*';
                const text = m[2];
                return Array.from(document.querySelectorAll(tag)).find(e => (e.textContent || '').includes(text)) || null;
            }}
        }}
        if (sel.startsWith('text=')) {{
            const text = sel.slice(5).trim();
            return Array.from(document.querySelectorAll('*')).find(e => (e.textContent || '').includes(text)) || null;
        }}
        return document.querySelector(sel);
    }})()"""
