"""Unit tests to validate JavaScript syntax of browser-injected scripts.

Ensures that even in environments where CDP is mocked (such as Termux),
syntax errors, missing braces, or evaluation wrapper mismatches in
src/aistudio_api/infrastructure/browser/js/*.js are caught during `pytest`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from aistudio_api.infrastructure.browser.scripts import (
    CHECK_IDENTITY_JS,
    CLEAR_BINDING_JS,
    DIALOG_CLEANUP_JS,
    DOM_GC_CLEANUP_JS,
    EXTRACT_EMAIL_JS,
    HOOKED_REQUEST_JS,
    INSTALL_HOOKS_JS,
    PROMPT_STATE_JS,
    SNAPSHOT_GENERATE_JS,
    SNAPSHOT_PROBE_JS,
    STOP_GENERATION_JS,
    STREAM_CLEANUP_JS,
    STREAMING_INIT_JS,
    build_document_start_hooks_js,
)

JS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "aistudio_api"
    / "infrastructure"
    / "browser"
    / "js"
)


def _check_pure_python_bracket_balance(code: str) -> None:
    """Validate brace, bracket, and parenthesis balance while ignoring strings/comments."""
    stack: list[tuple[str, int]] = []
    pairs = {"}": "{", ")": "(", "]": "["}
    i = 0
    n = len(code)
    line = 1

    while i < n:
        char = code[i]
        if char == "\n":
            line += 1
            i += 1
            continue

        # Skip line comments
        if char == "/" and i + 1 < n and code[i + 1] == "/":
            i += 2
            while i < n and code[i] != "\n":
                i += 1
            continue

        # Skip block comments
        if char == "/" and i + 1 < n and code[i + 1] == "*":
            i += 2
            while i + 1 < n and not (code[i] == "*" and code[i + 1] == "/"):
                if code[i] == "\n":
                    line += 1
                i += 1
            i += 2
            continue

        # Skip regex literals
        if char == "/":
            prev = code[:i].rstrip()
            if prev and prev[-1] in (
                "(",
                "=",
                ":",
                ",",
                "!",
                "&",
                "|",
                "?",
                ";",
                "{",
                "[",
            ):
                i += 1
                while i < n and code[i] != "/":
                    if code[i] == "\\":
                        i += 2
                        continue
                    if code[i] == "\n":
                        break
                    i += 1
                i += 1
                continue

        # Skip string literals ('...', "...", `...`)
        if char in ("'", '"', "`"):
            quote = char
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                    continue
                if code[i] == "\n" and quote != "`":
                    break
                if code[i] == quote:
                    i += 1
                    break
                i += 1
            continue

        # Check brackets
        if char in ("{", "(", "["):
            stack.append((char, line))
        elif char in ("}", ")", "]"):
            expected = pairs[char]
            if not stack:
                raise AssertionError(
                    f"Unexpected closing '{char}' at line {line} (no matching open '{expected}')"
                )
            opened, open_line = stack.pop()
            if opened != expected:
                raise AssertionError(
                    f"Mismatched bracket: opened '{opened}' at line {open_line}, closed with '{char}' at line {line}"
                )

        i += 1

    if stack:
        opened, open_line = stack[-1]
        raise AssertionError(
            f"Unclosed '{opened}' opened at line {open_line} (missing closing bracket at EOF)"
        )


def test_pure_python_js_bracket_balance():
    """Verify all browser JS scripts have balanced brackets (works offline/Termux)."""
    assert JS_DIR.is_dir(), f"JS directory not found: {JS_DIR}"
    js_files = list(JS_DIR.glob("*.js"))
    assert len(js_files) >= 10, f"Expected at least 10 JS files, found {len(js_files)}"

    for js_file in js_files:
        content = js_file.read_text(encoding="utf-8")
        try:
            _check_pure_python_bracket_balance(content)
        except AssertionError as exc:
            pytest.fail(f"Syntax/bracket error in {js_file.name}: {exc}")


def test_js_ast_syntax_with_external_runtime():
    """If node or bun is installed on the host, perform native AST syntax check."""
    runtime = shutil.which("bun") or shutil.which("node")
    if not runtime:
        pytest.skip("Neither bun nor node found in PATH; skipping external AST check.")

    is_bun = "bun" in Path(runtime).name.lower()
    for js_file in JS_DIR.glob("*.js"):
        if is_bun:
            cmd = [runtime, "build", str(js_file), "--no-bundle"]
        else:
            cmd = [runtime, "-c", str(js_file)]

        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert res.returncode == 0, (
            f"{runtime} syntax check failed for {js_file.name}:\n{res.stderr}"
        )


def test_evaluate_wrapper_expressions_parse_cleanly():
    """Test wrapping each script inside an expression as CDP evaluate() does."""
    runtime = shutil.which("bun") or shutil.which("node")
    if not runtime:
        pytest.skip("Neither bun nor node found in PATH; skipping evaluate check.")

    scripts = [
        ("INSTALL_HOOKS_JS", INSTALL_HOOKS_JS, None),
        (
            "SNAPSHOT_GENERATE_JS",
            SNAPSHOT_GENERATE_JS,
            {"binding": "hello world", "hash": "a" * 64, "bindingId": "request"},
        ),
        (
            "SNAPSHOT_PROBE_JS",
            SNAPSHOT_PROBE_JS,
            {"hash": "a" * 64, "timeoutMs": 12000},
        ),
        ("PROMPT_STATE_JS", PROMPT_STATE_JS, None),
        ("CLEAR_BINDING_JS", CLEAR_BINDING_JS, {"bindingId": "request"}),
        ("build_document_start_hooks_js", build_document_start_hooks_js(), None),
        ("STREAMING_INIT_JS", STREAMING_INIT_JS, {"rid": "r1", "url": "http://x"}),
        ("STREAM_CLEANUP_JS", STREAM_CLEANUP_JS, "r1"),
        ("DIALOG_CLEANUP_JS", DIALOG_CLEANUP_JS, None),
        ("DOM_GC_CLEANUP_JS", DOM_GC_CLEANUP_JS, None),
        ("CHECK_IDENTITY_JS", CHECK_IDENTITY_JS, {"email": "a@b.com"}),
        ("STOP_GENERATION_JS", STOP_GENERATION_JS, None),
        ("EXTRACT_EMAIL_JS", EXTRACT_EMAIL_JS, None),
        ("HOOKED_REQUEST_JS", HOOKED_REQUEST_JS, None),
    ]

    for name, script_text, arg in scripts:
        expr = script_text.strip()
        wrapped = f"({expr})({json.dumps(arg)})" if arg is not None else f"({expr})()"

        prelude = "globalThis.window = globalThis; globalThis.document = { querySelector: () => null, querySelectorAll: () => [] };"
        code = f"{prelude} try {{ new Function({wrapped!r}); }} catch (e) {{ if (e instanceof SyntaxError) throw e; }}"
        cmd = [runtime, "-e", code]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert res.returncode == 0, (
            f"SyntaxError in evaluate() wrapper for {name}:\n{res.stderr}"
        )


def test_binding_cleanup_preserves_newer_request_and_cancelled_job():
    runtime = shutil.which("bun") or shutil.which("node")
    if not runtime:
        pytest.skip("No JavaScript runtime installed")
    code = """
        const assert = (ok) => { if (!ok) throw new Error('binding regression'); };
        globalThis.window = globalThis;
        class Textarea {
            constructor() { this._value = ''; this.isConnected = true; this.events = []; }
            set value(value) { this._value = value; }
            get value() { return this._value; }
            checkVisibility() { return true; }
            dispatchEvent(event) { this.events.push(event.type); }
            focus() {}
            blur() {}
        }
        globalThis.HTMLTextAreaElement = Textarea;
        globalThis.Event = class { constructor(type) { this.type = type; } };
        globalThis.InputEvent = globalThis.Event;
        const box = new Textarea();
        globalThis.document = { querySelectorAll: () => [box] };
        let resolveProof;
        globalThis.default_MakerSuite = {
            snap: () => new Promise(resolve => { resolveProof = resolve; }),
        };
        globalThis.__AISTUDIO__ = { service: {}, snapKey: 'snap' };
    """
    code += f"const generate = ({SNAPSHOT_GENERATE_JS}); const clear = ({CLEAR_BINDING_JS});\n"
    code += """
        (async () => {
            const pending = generate({ binding: '', hash: 'hash', bindingId: 'old' });
            while (!resolveProof) await new Promise(resolve => setTimeout(resolve, 0));
            assert(__AISTUDIO__.binding.id === 'old');
            assert(box.value === '');
            assert(box.events.includes('input') && box.events.includes('change'));
            assert(clear({ bindingId: 'old' }) === 'already_empty');
            resolveProof('proof');
            let cancelled = false;
            try { await pending; } catch (error) { cancelled = error.message.includes('binding_cancelled'); }
            assert(cancelled);
            assert(Object.keys(__AISTUDIO__.bindingJobs).length === 0);
            box.value = 'new binding';
            __AISTUDIO__.binding = { id: 'new', box, text: box.value };
            assert(clear({ bindingId: 'old' }) === 'not_owned');
            assert(box.value === 'new binding');
            box.value = 'user edited';
            assert(clear({ bindingId: 'new' }) === 'not_owned');
            assert(box.value === 'user edited');
        })().catch(error => { console.error(error); process.exitCode = 1; });
    """
    result = subprocess.run([runtime, "-e", code], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_hook_headers_install_before_bundle_and_service_tap_can_retry():
    runtime = shutil.which("bun") or shutil.which("node")
    if not runtime:
        pytest.skip("No JavaScript runtime installed")
    code = """
        const assert = (ok) => { if (!ok) throw new Error('hook regression'); };
        globalThis.window = globalThis;
        globalThis.fetch = () => Promise.resolve('fetch');
        class XHR {
            open() {}
            setRequestHeader() {}
            send() {}
        }
        globalThis.XMLHttpRequest = XHR;
    """
    code += f"const install = () => ({INSTALL_HOOKS_JS});\n"
    code += """
        assert(install() === 'no_default_MakerSuite');
        fetch('/$rpc/read', { headers: { 'X-Goog-Api-Key': 'page-key' } });
        assert(__AISTUDIO__.rpcHeaders['x-goog-api-key'] === 'page-key');
        const service = { A: { snapshot() {} } };
        const bundle = {
            snapshotFn: function(service, content) { return service.A.snapshot(content); },
        };
        Object.defineProperty(bundle, 'n', { value: () => service, configurable: true, writable: false });
        globalThis.default_MakerSuite = bundle;
        assert(install() === 'no_service_tap');
        Object.defineProperty(bundle, 'n', { value: () => service, writable: true });
        assert(install().startsWith('hooked:'));
        bundle.n('unminified-shape');
        assert(__AISTUDIO__.service === service);
        assert(__AISTUDIO__.captureVia === 'inject(shape)');
    """
    result = subprocess.run([runtime, "-e", code], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
