#!/usr/bin/env bash
# aistudio-api 启动脚本（Linux / macOS / Termux / Windows Git Bash 通用）
#
# 做四件事：进目录 → 找/建 Python 环境 → 装依赖 → 起服务。
# 浏览器（CloakBrowser）不在这里装：首次请先跑 scripts/install-browser.sh / install-browser.ps1，
# 或把已有 chrome 的路径写进 AISTUDIO_BROWSER_EXECUTABLE。
#
# 常用环境变量：
#   PORT=8080                 监听端口
#   AISTUDIO_BROWSER_HEADLESS=false   有头启动（默认无头）
#   AISTUDIO_BROWSER_EXECUTABLE=/path/to/chrome
#   AISTUDIO_DATA_DIR=/path/to/data   账号/设置/统计的落盘目录
#   AISTUDIO_API_KEY / AISTUDIO_WEB_PASSWORD  对外鉴权与面板密码
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8080}"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { echo "找不到 python，请先安装 Python 3.11+"; exit 1; }

# 1) 准备环境：优先用已有 venv（可用 VENV=/path/to/venv 指定），否则 uv / venv 新建
#    支持 Windows 布局（Scripts/python.exe）与 POSIX 布局（bin/python）
if [ -n "${VENV:-}" ]; then
    :
elif [ -x ".venv/bin/python" ] || [ -f ".venv/Scripts/python.exe" ]; then
    VENV=".venv"
elif [ -x "venv/bin/python" ] || [ -f "venv/Scripts/python.exe" ]; then
    VENV="venv"
else
    VENV=".venv"
    if command -v uv >/dev/null 2>&1; then
        echo "[start] 用 uv 建环境: $VENV"
        uv venv "$VENV"
    else
        echo "[start] 用 venv 建环境: $VENV"
        "$PY" -m venv "$VENV"
    fi
fi

if [ -f "$VENV/bin/python" ]; then
    PYTHON_BIN="$VENV/bin/python"
elif [ -f "$VENV/Scripts/python.exe" ]; then
    PYTHON_BIN="$VENV/Scripts/python.exe"
else
    echo "[start] 环境不可用，退回到系统 python"; PYTHON_BIN="$PY"
fi

# 2) 依赖：requirements.txt 存在且没装过 fastapi 就装一遍
if ! "$PYTHON_BIN" -c "import fastapi" >/dev/null 2>&1; then
    echo "[start] 安装依赖..."
    if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$PYTHON_BIN" -r requirements.txt
    else
        "$PYTHON_BIN" -m pip install --upgrade pip >/dev/null 2>&1 || true
        "$PYTHON_BIN" -m pip install -r requirements.txt
    fi
fi

# 3) 起服务
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING="utf-8"

# 监听地址：默认 0.0.0.0（局域网/其它设备可访问）；只本机用 HOST=127.0.0.1
HOST="${HOST:-0.0.0.0}"

# 打印本机在局域网里的地址，方便手机/别的电脑直接访问
LAN_IPS=""
if command -v hostname >/dev/null 2>&1; then
    LAN_IPS="$(hostname -I 2>/dev/null || true)"
fi
if [ -z "$LAN_IPS" ] && command -v ipconfig >/dev/null 2>&1; then
    LAN_IPS="$(ipconfig 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | grep -vE '^(127\.|255\.)' | sort -u | tr '\n' ' ' || true)"
fi

echo "[start] aistudio-api 启动：监听 $HOST:$PORT（无头=${AISTUDIO_BROWSER_HEADLESS:-true}）"
echo "[start] 本机面板   http://127.0.0.1:$PORT/"
for ip in $LAN_IPS; do
    echo "[start] 局域网面板 http://$ip:$PORT/"
done
echo "[start] 接口       http://$HOST:$PORT/v1beta/models   （OpenAI: /v1/chat/completions）"
exec "$PYTHON_BIN" main.py server --host "$HOST" --port "$PORT"
