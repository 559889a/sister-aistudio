"""AB 实验：流式 vs 非流式 的抗网络波动能力对比（2026-10-20，用户指派）。

方法：隔离实例（临时 data 目录 + 端口 9527 + 拷贝的账号凭据，profile 自然分家，
不碰 8080/9222 上的生产服务）。浏览器挂一次性本地代理（AISTUDIO_PROXY），波动 =
代理对已建立连接发 RST 强杀（等效 Clash 切节点/线路重置——CDP offline 模拟实测
拦不住已建立连接的数据流，已弃用）。两臂（stream / non-stream）各跑 N 个样本，
统计成功率 / 失败类别 / 已送达字符数。

用法：
  .venv/Scripts/python.exe tools/ab_network_resilience.py --samples 10            # 正式
  .venv/Scripts/python.exe tools/ab_network_resilience.py --samples 2 --no-flap   # 冒烟
产物：%TEMP%\\ab-net-test\\report.jsonl + stdout 汇总表
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import shutil
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_BASE = Path(tempfile.gettempdir()) / "ab-net-test"
_DATA_DIR = _BASE / "data"
_ACCOUNT_DIR = _BASE / "account"
_BROWSER_PORT = 9527
_MODEL = "gemini-3.8-flash"  # 用户指定；禁止换 argon
_PROMPT = "用中文写一段大约400字的散文，主题：海边的清晨。直接输出正文，不要标题。"

# 隔离环境变量必须在 import aistudio_api 之前生效
os.environ["AISTUDIO_DATA_DIR"] = str(_DATA_DIR)
os.environ["AISTUDIO_BROWSER_PORT"] = str(_BROWSER_PORT)
_PROXY_PORT = 9590
os.environ["AISTUDIO_PROXY"] = f"http://127.0.0.1:{_PROXY_PORT}"

sys.path.insert(0, str(_REPO / "src"))


def _prepare_isolated_account() -> Path:
    """把生产活跃账号的凭据拷到临时目录（profile 目录随 auth 路径派生，自然隔离）。"""
    registry_path = _REPO / "data" / "accounts" / "registry.json"
    active_id = None
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        active_id = registry.get("active_account_id")
    if not active_id:
        raise SystemExit(f"找不到活跃账号（registry: {registry_path}）")
    src_dir = _REPO / "data" / "accounts" / str(active_id)
    if not (src_dir / "auth.json").is_file():
        raise SystemExit(f"活跃账号缺 auth.json: {src_dir}")
    if _ACCOUNT_DIR.exists():
        shutil.rmtree(_ACCOUNT_DIR, ignore_errors=True)
    _ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_dir / "auth.json", _ACCOUNT_DIR / "auth.json")
    meta_src = src_dir / "meta.json"
    if meta_src.exists():
        shutil.copyfile(meta_src, _ACCOUNT_DIR / "meta.json")
    print(f"[setup] 已拷贝账号 {active_id} -> {_ACCOUNT_DIR}")
    return _ACCOUNT_DIR / "auth.json"


def _make_flap_schedule(rng: random.Random, duration_s: float) -> list[float]:
    """波动计划：每次 RST 的间隔秒数序列，覆盖整个生成窗口。"""
    schedule: list[float] = []
    total = 0.0
    while total < duration_s:
        interval = rng.uniform(3.0, 6.0)
        schedule.append(interval)
        total += interval
    return schedule


class ResetProxy:
    """一次性 CONNECT 隧道代理；``kill_all()`` 对已建立连接发 RST（abortive close）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = _PROXY_PORT) -> None:
        self._host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._client_conns: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self.port)

    async def _handle(self, client_reader, client_writer) -> None:
        upstream_writer = None
        try:
            request_line = await asyncio.wait_for(client_reader.readline(), 20.0)
            parts = request_line.split()
            if len(parts) < 2 or parts[0] != b"CONNECT":
                return
            while True:
                header = await asyncio.wait_for(client_reader.readline(), 10.0)
                if header in (b"\r\n", b"\n", b""):
                    break
            host, _, port = parts[1].decode().rpartition(":")
            _up_reader, upstream_writer = await asyncio.open_connection(host, int(port))
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
            self._client_conns.add(client_writer)

            async def pump(reader, writer) -> None:
                try:
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            break
                        writer.write(data)
                        await writer.drain()
                except Exception:
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        writer.close()

            await asyncio.gather(
                pump(client_reader, upstream_writer),
                pump(_up_reader, client_writer),
            )
        except Exception:
            pass
        finally:
            for writer in (client_writer, upstream_writer):
                if writer is not None:
                    with contextlib.suppress(Exception):
                        writer.close()
            self._client_conns.discard(client_writer)

    def kill_all(self) -> int:
        """强杀全部已建立隧道（SO_LINGER=0 → RST），返回杀掉的连接数。"""
        killed = 0
        for writer in list(self._client_conns):
            try:
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    sock.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                writer.close()
                killed += 1
            except Exception:
                pass
        self._client_conns.clear()
        return killed


async def _flap(proxy: ResetProxy, schedule: list[float], lead_in_s: float = 4.0) -> None:
    """引导期过后按节奏 kill_all()——一次 RST = 一次「线路重置」。"""
    await asyncio.sleep(lead_in_s)
    for interval in schedule:
        await asyncio.sleep(interval)
        proxy.kill_all()


async def _run_sample(client, mode: str, flap_schedule, *, no_flap: bool, proxy) -> dict:
    from aistudio_api.infrastructure.gateway.wire_types import AistudioContent

    contents = [AistudioContent.from_user_prompt(_PROMPT)]
    flap_task = None if no_flap else asyncio.create_task(_flap(proxy, flap_schedule))
    t0 = time.perf_counter()
    record = {"mode": mode, "ok": False, "chars": 0, "error_class": "", "error": ""}
    try:
        if mode == "non-stream":
            out = await client.generate_content(
                model=_MODEL, capture_prompt=_PROMPT, contents=contents
            )
            record["ok"] = bool(out.text)
            record["chars"] = len(out.text)
        else:
            finish = None
            async for event, payload in client.stream_generate_content(
                model=_MODEL, capture_prompt=_PROMPT, contents=contents
            ):
                if event == "body" and isinstance(payload, str):
                    record["chars"] += len(payload)
                elif event == "finish_reason" and payload is not None:
                    finish = payload
            record["ok"] = finish is not None or record["chars"] > 0
            record["finish_reason"] = finish
    except Exception as exc:
        record["error_class"] = type(exc).__name__
        record["error"] = str(exc)[:300]
    finally:
        if flap_task is not None:
            flap_task.cancel()
    record["elapsed_s"] = round(time.perf_counter() - t0, 1)
    return record


async def _ensure_alive(client) -> object:
    """零重试口径下，失败后浏览器可能已坏：ensure 一下，不行就整体重建。"""
    try:
        await client.warmup()
        return client
    except Exception as exc:
        print(f"[recover] 会话自愈失败（{exc}），重建浏览器…")
        with contextlib.suppress(Exception):
            await client._session._close_internal()
        from aistudio_api.infrastructure.gateway.client import AIStudioClient

        new_client = AIStudioClient(
            port=_BROWSER_PORT, auth_file=str(_ACCOUNT_DIR / "auth.json")
        )
        await new_client.warmup()
        return new_client


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10, help="每臂样本数")
    parser.add_argument("--no-flap", action="store_true", help="不制造波动（对照组/冒烟）")
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--kill-every", default="3-6", help="RST 间隔区间 秒 (min-max)")
    args = parser.parse_args()

    auth_file = _prepare_isolated_account()
    from aistudio_api.infrastructure.gateway.client import AIStudioClient

    proxy = ResetProxy()
    await proxy.start()
    print(f"[setup] 波动代理 127.0.0.1:{_PROXY_PORT}（RST 模式），浏览器端口 {_BROWSER_PORT}，模型 {_MODEL}")
    client = AIStudioClient(port=_BROWSER_PORT, auth_file=str(auth_file))
    t0 = time.perf_counter()
    await client.warmup()
    print(f"[setup] BotGuard 就绪，耗时 {time.perf_counter() - t0:.0f}s")

    rng = random.Random(args.seed)
    report_path = _BASE / "report.jsonl"
    _BASE.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    modes = ("non-stream", "stream")
    for idx in range(args.samples):
        for mode in modes:
            # 同一样本号两臂共用同一份波动计划（配对设计，公平比较）
            schedule = _make_flap_schedule(rng, duration_s=45.0)
            client = await _ensure_alive(client)
            rec = await _run_sample(
                client, mode, schedule, no_flap=args.no_flap, proxy=proxy
            )
            rec["idx"] = idx
            rec["flapped"] = not args.no_flap
            records.append(rec)
            with report_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            status = "OK " if rec["ok"] else "FAIL"
            print(
                f"[{idx + 1}/{args.samples}] {mode:<10} {status} chars={rec['chars']:<5} "
                f"{rec['elapsed_s']:>5.1f}s {rec['error_class']} {rec['error'][:80]}"
            )
            await asyncio.sleep(2.0)

    print("\n===== 汇总 =====")
    print(
        f"波动: {'RST 连接强杀，间隔 ' + args.kill_every + 's' if not args.no_flap else '无（对照）'}"
    )
    for mode in modes:
        arm = [r for r in records if r["mode"] == mode]
        ok = sum(1 for r in arm if r["ok"])
        fails: dict[str, int] = {}
        for r in arm:
            if not r["ok"]:
                fails[r["error_class"] or "?"] = fails.get(r["error_class"] or "?", 0) + 1
        chars_ok = [r["chars"] for r in arm if r["ok"]]
        print(
            f"{mode:<10} 成功 {ok}/{len(arm)}"
            f"  平均耗时 {sum(r['elapsed_s'] for r in arm) / max(1, len(arm)):.1f}s"
            f"  成功平均字符 {sum(chars_ok) / max(1, len(chars_ok)):.0f}"
            f"  失败类别 {fails or '-'}"
        )
    print(f"明细: {report_path}")

    with contextlib.suppress(Exception):
        await client._session.close()
    if proxy._server is not None:
        proxy._server.close()


if __name__ == "__main__":
    asyncio.run(main())
