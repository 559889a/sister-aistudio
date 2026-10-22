"""PG 通道隐式缓存 AB 黑箱采样 —— 脚本 A（冷前缀臂）+ 共用引擎。

问题：AI Studio 浏览器通道（pg/）到底吃不吃隐式上下文缓存？
后端是黑箱，只能看两样客户端可见的东西：curl 侧的 TTFT/总耗时，和响应里公开的
`usage.prompt_tokens_details.cached_tokens`。

设计（2026-10-22 用户指派，具体采样口径由本脚本定）：
- A 臂（冷）：每轮生成一份**全新**的长前缀 + 新问题 —— 前缀不可能命中缓存。
- B 臂（热）：全部轮次复用**同一份**长前缀，只换末尾片段与问题 —— 前缀可命中。
- 两臂规模相同、都走同一条 pg 浏览器链路、同一份上传 gzip 规则（体 >96KB 自动压），
  唯一变量就是前缀是否复用。
- B 臂先预热一发（写入缓存），随后各 10 发按 ABBA / BAAB 分块交错，消除时间顺序干扰，
  并让 B 的两发相邻（避免被 A 的冷前缀挤掉缓存）。
- TTFT = curl 进程启动到第一条非空正文 delta；HTTP 头、`[DONE]`、以及 1s 心跳帧
  （空 delta 的合法 chunk）都不算正文。零重试，失败就记失败。

隔离：临时 data 目录 + 临时 config/settings + 95xx 端口 + 拷贝的生产活跃账号凭据
（profile 随 auth 路径分家）。不碰生产 8080/9222，也不碰副本 C:\\sister-aistudio-copy。

用法：
  .venv/Scripts/python.exe tools/pg_cache_a.py                 # 只跑 A 臂 10 发
  .venv/Scripts/python.exe tools/pg_cache_a.py --interleave     # A+B 交错 20 发（推荐）
  .venv/Scripts/python.exe tools/pg_cache_a.py --rounds 2 --interleave   # 冒烟
产物：%TEMP%\\pg-cache-ab\\report_a.jsonl（或 report_ab.jsonl）+ server.log + stdout 汇总表
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BASE = Path(tempfile.gettempdir()) / "pg-cache-ab"
_DATA_DIR = _BASE / "data"
_CONFIG_FILE = _BASE / "ab-config.yaml"
_SETTINGS_FILE = _DATA_DIR / "settings.json"
_HTTP_PORT = 9560
_BROWSER_PORT = 9561
_API_KEY = "sk-123"
_MODEL = "gemini-3.8-flash"
_PREFIX_CHARS = 120_000  # 前缀字符数：>96KB 上传体，顺带压到 gzip 分支
_MAX_TOKENS = 64  # 小输出：让耗时由 prefill 主导
_ROUND_TIMEOUT_S = 600.0

# 隔离运行用的 config.yaml：干净文本生成（不注入 google_search 等默认工具，
# 避免联网检索把 TTFT 噪声混进来）。profiles=[] 会整段替换内置档案。
_AB_CONFIG_YAML = """\
api_keys:
- name: '1'
  key: sk-123
  created_at: '2026-10-22 00:00:00'
logging:
  level: INFO
  dump_requests: false
  dump_to_file: false
  dump_dir: dumps
model_defaults:
  drop_unsupported_params: false
  profiles: []
  models: {}
"""

# 关掉全部运行期改写（bypass/预填充/签名哨兵）：这轮只测缓存，别的变量一律不动。
_AB_RUNTIME_SETTINGS = {
    "bypassEnabled": False,
    "prefillEnabled": False,
    "prefillCotGuard": False,
    "includeThoughts": False,
    "thinkingMode": "request",
    "dropMaxTokens": False,
    "sigSentinelThought": False,
    "sigSentinelTool": False,
}

_VOCAB = (
    "alpha",
    "anchor",
    "atlas",
    "batch",
    "beacon",
    "bloom",
    "cabin",
    "canvas",
    "cascade",
    "cedar",
    "cipher",
    "clause",
    "cobalt",
    "comet",
    "coral",
    "delta",
    "drift",
    "ember",
    "fabric",
    "falcon",
    "ferry",
    "forge",
    "gauge",
    "glacier",
    "granite",
    "harbor",
    "helix",
    "hollow",
    "indigo",
    "ivory",
    "jasper",
    "kernel",
    "lagoon",
    "lantern",
    "ledge",
    "linen",
    "lumen",
    "marble",
    "meadow",
    "mesa",
    "nimbus",
    "nocturne",
    "oasis",
    "onyx",
    "orbit",
    "pebble",
    "pivot",
    "prism",
    "quartz",
    "quill",
    "raven",
    "reef",
    "relay",
    "ridge",
    "rivet",
    "saffron",
    "sequoia",
    "shard",
    "sierra",
    "signal",
    "slate",
    "solstice",
    "spindle",
    "summit",
    "talon",
    "tundra",
    "umbra",
    "vale",
    "vertex",
    "vessel",
    "willow",
    "zenith",
)

_QUESTIONS = (
    "上面材料里反复出现的自然类词汇，请只列出三个，用逗号分隔。",
    "材料中出现了哪些与地质或地貌相关的词？只列两个。",
    "把材料里出现的颜色/矿石类词挑一个写出来。",
    "材料里有没有和天空、天气有关的词？有就写一个，没有就写“无”。",
    "从材料里挑一个长度超过六个字母的词写出来。",
    "材料里反复出现的词里，哪个最像地名？只写一个。",
    "材料里是否出现了和航海有关的词？写出来或写“无”。",
    "把材料里出现的所有动物类词去掉，剩下的挑一个写出来。",
    "材料里出现次数最多的那类词是什么？只写类别名。",
    "材料里有和金属有关的词吗？有就写出来。",
)


# ── 文本生成：同一 seed 必得同一份文本（arm B 靠它复用前缀） ──────────────


def _make_blob(seed: int, chars: int, nonce: str) -> str:
    """确定性伪材料：不同 seed 内容不同、规模一致；nonce 让每次运行的前缀都不同。"""
    rng = random.Random(seed)
    lines = [f"RUN-{nonce} 材料片段（seed={seed}）"]
    total = len(lines[0])
    while total < chars:
        sentence = " ".join(rng.choice(_VOCAB) for _ in range(rng.randint(9, 15)))
        lines.append(sentence + ".")
        total += len(sentence) + 2
    return "\n".join(lines)


def _question(index: int) -> str:
    return f"（第 {index + 1} 问）{_QUESTIONS[index % len(_QUESTIONS)]}"


def build_payload(
    arm: str, index: int, *, prefix_chars: int, nonce: str, max_tokens: int
) -> dict:
    """arm=a 用 seed=index+1（每轮换前缀）；arm=b 固定 seed=0（前缀恒定）。"""
    blob = _make_blob(0 if arm == "b" else index + 1, prefix_chars, nonce)
    tail = f"\n\n[末段 {index + 1}] " + " ".join(
        _VOCAB[(index * 7 + i) % len(_VOCAB)] for i in range(24)
    )
    text = f"{blob}{tail}\n\n{_question(index)}"
    return {
        "model": _MODEL,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": text}],
    }


# ── 隔离服务 ────────────────────────────────────────────────────────────


def _prepare_isolated_env() -> str:
    """拷贝生产活跃账号到临时 data 目录，写临时 config/settings，返回 auth.json 路径。"""
    src_accounts = _REPO / "data" / "accounts"
    registry_path = src_accounts / "registry.json"
    if not registry_path.is_file():
        raise SystemExit(f"找不到账号注册表: {registry_path}")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    active_id = registry.get("active_account_id")
    metas = registry.get("accounts") or {}
    if not active_id or active_id not in metas:
        raise SystemExit(f"注册表里没有活跃账号: {registry_path}")
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR, ignore_errors=True)
    dst = _DATA_DIR / "accounts" / active_id
    dst.mkdir(parents=True)
    for name in ("auth.json", "meta.json"):
        src = src_accounts / active_id / name
        if src.is_file():
            shutil.copyfile(src, dst / name)
    (_DATA_DIR / "accounts" / "registry.json").write_text(
        json.dumps(
            {"accounts": {active_id: metas[active_id]}, "active_account_id": active_id},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(_AB_RUNTIME_SETTINGS, ensure_ascii=False), encoding="utf-8"
    )
    _CONFIG_FILE.write_text(_AB_CONFIG_YAML, encoding="utf-8")
    print(f"[setup] 隔离账号 {active_id} → {dst}（profile 随 auth 路径分家）")
    return str(dst / "auth.json")


def _start_server(auth_file: str) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "AISTUDIO_DATA_DIR": str(_DATA_DIR),
            "AISTUDIO_AUTH_FILE": auth_file,
            "AISTUDIO_CONFIG_FILE": str(_CONFIG_FILE),
            "AISTUDIO_SETTINGS_FILE": str(_SETTINGS_FILE),
            "AISTUDIO_BROWSER_PORT": str(_BROWSER_PORT),
            "AISTUDIO_PORT": str(_HTTP_PORT),
            "PYTHONPATH": str(_REPO / "src"),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    log = (_BASE / "server.log").open("w", encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable,
            "main.py",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(_HTTP_PORT),
            "--browser-port",
            str(_BROWSER_PORT),
        ],
        cwd=str(_REPO),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _wait_http(timeout_s: float = 120.0) -> None:
    url = f"http://127.0.0.1:{_HTTP_PORT}/v1/models"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {_API_KEY}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1.0)
    raise SystemExit(f"隔离服务 {timeout_s:.0f}s 内未就绪，看 {_BASE / 'server.log'}")


def _stop_server(proc: subprocess.Popen) -> None:
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=30)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()


# ── 采样 ────────────────────────────────────────────────────────────────


def _curl_round(payload: dict, *, timeout_s: float = _ROUND_TIMEOUT_S) -> dict:
    """一发 curl，逐行读 SSE 打时间戳。返回 TTFT / 总耗时 / usage 观测。"""
    body_path = _BASE / "payload.json"
    body_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    cmd = [
        "curl.exe",
        "-sS",
        "-N",
        "--no-buffer",
        "-X",
        "POST",
        f"http://127.0.0.1:{_HTTP_PORT}/v1/chat/completions",
        "-H",
        f"Authorization: Bearer {_API_KEY}",
        "-H",
        "Content-Type: application/json",
        "-d",
        f"@{body_path}",
        "-w",
        "\n__META__ %{http_code} %{time_total} %{size_upload}\n",
    ]
    rec: dict = {
        "ok": False,
        "ttft_s": None,
        "total_s": None,
        "chars": 0,
        "http_status": None,
        "upload_bytes": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_tokens": None,
        "error": "",
    }
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    killer = threading.Timer(timeout_s, proc.kill)
    killer.start()
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            now = time.perf_counter() - t0
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("__META__"):
                parts = line.split()
                if len(parts) >= 4:
                    rec["http_status"] = int(parts[1])
                    rec["upload_bytes"] = int(parts[3])
                continue
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                rec["total_s"] = round(now, 2)
                rec["ok"] = True
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                rec["error"] = json.dumps(obj["error"], ensure_ascii=False)[:240]
                continue
            usage = obj.get("usage")
            if isinstance(usage, dict):
                rec["prompt_tokens"] = usage.get("prompt_tokens")
                rec["completion_tokens"] = usage.get("completion_tokens")
                details = usage.get("prompt_tokens_details")
                if isinstance(details, dict):
                    rec["cached_tokens"] = details.get("cached_tokens")
            choices = obj.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    if rec["ttft_s"] is None:
                        rec["ttft_s"] = round(now, 2)
                    rec["chars"] += len(text)
    finally:
        killer.cancel()
        stderr = ""
        with contextlib.suppress(Exception):
            assert proc.stderr is not None
            stderr = proc.stderr.read().decode("utf-8", "replace").strip()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
    if rec["total_s"] is None:
        rec["total_s"] = round(time.perf_counter() - t0, 2)
    if rec["ok"] and rec["ttft_s"] is None:
        rec["ok"] = False
        rec["error"] = rec["error"] or "没有正文 delta"
    if not rec["ok"] and not rec["error"]:
        rec["error"] = stderr[:240] or "没有 [DONE]"
    return rec


def _sequence(rounds: int) -> list[tuple[str, int]]:
    """ABBA / BAAB 分块交错：块内 B 的两发相邻，块间起手臂交替。"""
    seq: list[tuple[str, int]] = []
    block = 0
    start = 0
    while start < rounds:
        has_next = start + 1 < rounds
        if block % 2 == 0:
            block_seq = [("a", start), ("b", start)]
            if has_next:
                block_seq += [("b", start + 1), ("a", start + 1)]
        else:
            block_seq = [("b", start), ("a", start)]
            if has_next:
                block_seq += [("a", start + 1), ("b", start + 1)]
        seq += block_seq
        start += 2
        block += 1
    return seq


def _run(
    arms: tuple[str, ...],
    *,
    rounds: int,
    prefix_chars: int,
    max_tokens: int,
    keep_data: bool,
    report_name: str,
) -> list[dict]:
    _BASE.mkdir(parents=True, exist_ok=True)
    nonce = time.strftime("%m%d-%H%M%S")
    auth_file = _prepare_isolated_env()
    server = _start_server(auth_file)
    records: list[dict] = []
    report = _BASE / report_name.replace(".jsonl", f"-{nonce}.jsonl")
    report.write_text("", encoding="utf-8")
    try:
        _wait_http()
        print(
            f"[setup] 隔离服务就绪 http://127.0.0.1:{_HTTP_PORT}（浏览器端口 {_BROWSER_PORT}）"
        )

        warm = _curl_round(
            build_payload(
                "b", 0, prefix_chars=prefix_chars, nonce=nonce, max_tokens=max_tokens
            )
        )
        warm.update({"arm": "warmup", "round": -1, "warmup": True})
        records.append(warm)
        print(
            f"[预热] {'OK ' if warm['ok'] else 'FAIL'} ttft={warm['ttft_s']}s "
            f"总={warm['total_s']}s in={warm['prompt_tokens']} cached={warm['cached_tokens']} "
            f"{warm['error'][:80]}"
        )

        order = (
            _sequence(rounds)
            if len(arms) > 1
            else [(arms[0], i) for i in range(rounds)]
        )
        for step, (arm, index) in enumerate(order, 1):
            payload = build_payload(
                arm,
                index,
                prefix_chars=prefix_chars,
                nonce=nonce,
                max_tokens=max_tokens,
            )
            rec = _curl_round(payload)
            rec.update({"arm": arm, "round": index, "warmup": False, "order": step})
            records.append(rec)
            with report.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(
                f"[{step:>2}/{len(order)}] {arm.upper()} r{index:<2} "
                f"{'OK ' if rec['ok'] else 'FAIL'} ttft={rec['ttft_s']}s 总={rec['total_s']}s "
                f"in={rec['prompt_tokens']} out={rec['completion_tokens']} "
                f"cached={rec['cached_tokens']} up={rec['upload_bytes']}B {rec['error'][:70]}"
            )
    finally:
        _stop_server(server)
        if not keep_data:
            shutil.rmtree(_DATA_DIR, ignore_errors=True)
    print(f"\n明细: {report}\n服务日志: {_BASE / 'server.log'}")
    return records


def summarize(records: list[dict]) -> None:
    print("\n===== 汇总（TTFT / 总耗时，排除预热发）=====")
    print(
        f"{'臂':<4}{'成功':<8}{'TTFT中位':<12}{'TTFT均值':<12}{'总耗时中位':<12}{'cached中位':<12}"
    )
    for arm, label in (("a", "A 冷"), ("b", "B 热")):
        arm_recs = [r for r in records if r.get("arm") == arm and not r.get("warmup")]
        if not arm_recs:
            continue
        ok = [r for r in arm_recs if r["ok"] and r["ttft_s"] is not None]
        cached = [
            r["cached_tokens"]
            for r in arm_recs
            if isinstance(r.get("cached_tokens"), int)
        ]
        ttft = [r["ttft_s"] for r in ok]
        total = [r["total_s"] for r in ok]
        print(
            f"{label:<4}{f'{len(ok)}/{len(arm_recs)}':<8}"
            f"{f'{statistics.median(ttft):.2f}s' if ttft else '-':<12}"
            f"{f'{statistics.fmean(ttft):.2f}s' if ttft else '-':<12}"
            f"{f'{statistics.median(total):.2f}s' if total else '-':<12}"
            f"{f'{statistics.median(cached)}' if cached else '未报告':<12}"
        )
    fails = [r for r in records if not r["ok"] and not r.get("warmup")]
    if fails:
        print(
            f"失败 {len(fails)} 发: "
            + "; ".join(f"{r['arm']}{r['round']}={r['error'][:50]}" for r in fails)
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PG 隐式缓存 AB 采样（脚本 A：冷前缀）"
    )
    parser.add_argument(
        "--interleave", action="store_true", help="A+B 交错 20 发（推荐）"
    )
    parser.add_argument("--rounds", type=int, default=10, help="每臂轮数（默认 10）")
    parser.add_argument("--prefix-chars", type=int, default=_PREFIX_CHARS)
    parser.add_argument("--max-tokens", type=int, default=_MAX_TOKENS)
    parser.add_argument("--keep-data", action="store_true", help="保留临时 data 目录")
    args = parser.parse_args()
    arms = ("a", "b") if args.interleave else ("a",)
    records = _run(
        arms,
        rounds=args.rounds,
        prefix_chars=args.prefix_chars,
        max_tokens=args.max_tokens,
        keep_data=args.keep_data,
        report_name="report_ab.jsonl" if len(arms) > 1 else "report_a.jsonl",
    )
    summarize(records)


def main_b() -> None:
    """脚本 B 入口：热前缀臂（同一份引擎，预热一发后 10 发复用长前缀）。"""
    parser = argparse.ArgumentParser(
        description="PG 隐式缓存 AB 采样（脚本 B：热前缀）"
    )
    parser.add_argument(
        "--interleave", action="store_true", help="A+B 交错 20 发（推荐）"
    )
    parser.add_argument("--rounds", type=int, default=10, help="每臂轮数（默认 10）")
    parser.add_argument("--prefix-chars", type=int, default=_PREFIX_CHARS)
    parser.add_argument("--max-tokens", type=int, default=_MAX_TOKENS)
    parser.add_argument("--keep-data", action="store_true", help="保留临时 data 目录")
    args = parser.parse_args()
    arms = ("a", "b") if args.interleave else ("b",)
    records = _run(
        arms,
        rounds=args.rounds,
        prefix_chars=args.prefix_chars,
        max_tokens=args.max_tokens,
        keep_data=args.keep_data,
        report_name="report_ab.jsonl" if len(arms) > 1 else "report_b.jsonl",
    )
    summarize(records)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
