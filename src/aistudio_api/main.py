"""Unified CLI entrypoint for local development and installed usage."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio API entrypoint")
    subparsers = parser.add_subparsers(dest="command")

    server_parser = subparsers.add_parser("server", help="启动 Gemini 兼容 API 服务")
    server_parser.add_argument("--port", type=int, default=settings.port)
    server_parser.add_argument(
        "--host",
        default=settings.host,
        help="监听地址；默认 0.0.0.0（局域网可访问），只本机用 127.0.0.1",
    )
    server_parser.add_argument(
        "--browser-port", type=int, default=settings.browser_port
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    import sys

    from aistudio_api.api.app import main as server_main

    port = getattr(args, "port", None)
    browser_port = getattr(args, "browser_port", None)
    host = getattr(args, "host", None)

    from aistudio_api.config import settings

    port_val = port if port is not None else settings.port
    browser_port_val = (
        browser_port if browser_port is not None else settings.browser_port
    )
    host_val = host if host is not None else settings.host

    sys.argv = [
        "aistudio-api-server",
        "--host",
        str(host_val),
        "--port",
        str(port_val),
        "--browser-port",
        str(browser_port_val),
    ]
    server_main()


if __name__ == "__main__":
    main()
