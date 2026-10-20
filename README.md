# aistudio-api

<p align="center">
  <a href="https://github.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/API-Gemini%20v1beta-4285F4?style=flat-square&logo=google" alt="Gemini API"></a>
  <a href="https://hub.docker.com/r/bedrockblock/aistudio-api"><img src="https://img.shields.io/badge/Docker-bedrockblock%2Faistudio--api-2496ED?style=flat-square&logo=docker" alt="Docker Hub"></a>
  <a href="https://deepwiki.com/DeconstructedCube/aistudio-api"><img src="https://img.shields.io/badge/DeepWiki-DeconstructedCube%2Faistudio--api-5B68E4?style=flat-square&logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==" alt="DeepWiki"></a>
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/Framework-FastAPI-009688?style=flat-square&logo=fastapi" alt="FastAPI"></a>
  <a href="https://vuejs.org/"><img src="https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Vite-4FC08D?style=flat-square&logo=vuedotjs" alt="Vue 3"></a>
  <a href="https://docs.astral.sh/uv/"><img src="https://img.shields.io/badge/Python-3.11%2B%20%7C%20uv-261230?style=flat-square&logo=python" alt="Python uv"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="License"></a>
</p>

<p align="center">
  Google AI Studio 反向代理服务，将 Web 端 AI Studio 转换为标准 Google Gemini API 协议接口。
</p>

<p align="center">
  <a href="./README_EN.md">English Documentation</a>
</p>

---

## 目录

- [核心特性](#核心特性)
- [技术架构与文档](#技术架构与文档)
- [快速开始](#快速开始)
  - [Android (Termux)](#android-termux)
  - [Windows](#windows)
  - [Linux / macOS](#linux--macos)
  - [Docker 部署](#docker-部署)
- [配置与环境变量](#配置与环境变量)
- [API 调用示例](#api-调用示例)
  - [cURL 调用](#curl-调用)
  - [Python SDK (google-genai)](#python-sdk-google-genai)
  - [工具调用 (Function Calling)](#工具调用-function-calling)
- [Web 管理控制台](#web-管理控制台)
- [开发与测试](#开发与测试)
- [开源协议](#开源协议)

---

## 核心特性

| 功能模块 | 说明 |
|---|---|
| **Gemini 协议兼容** | 支持 `/v1beta/...` 接口规范，包括 Thinking 思维链、多模态、Function Calling 工具调用及图片生成 |
| **动态模型发现** | 自动向上游同步可用模型列表，支持 `gemini-3.7-flash`、`gemini-3.8-flash` 等最新模型 |
| **多账号自动轮换** | 多账号管理与按模型独立记录 429 配额的冷却调度，每日美西午夜自动重置（网关对当次请求零自动重试，失败按真实错误返回） |
| **运行状态持久化** | 模型调用量、Token 消耗统计 (`stats.json`) 以及账号 429 限额与冷却状态 (`rotator_state.json`) 自动落盘，容器/服务重启不丢失状态 |
| **下游参数容错与丢弃** | 支持配置 `drop_unsupported_params` 自动过滤下游传入的非标准安全分类（如 `HARM_CATEGORY_CIVIC_INTEGRITY`）与不兼容工具，避免 400 阻断 |
| **批量子账号探活** | 导入单个包含多个 Google 身份的 Cookie 后，自动递归探活子账号（`u/0`, `u/1`...）并分别建档 |
| **内置搜索与工具** | 支持 Google Search 联网搜索、Google Maps、代码执行沙箱等官方扩展能力 |
| **轻量 CDP 驱动** | 基于纯 Python 异步 WebSocket 直连 Chrome DevTools Protocol，无需 Node.js、Playwright 或 Selenium |
| **无消息引导（WAA binding）** | 默认不往对话里发探针消息：DI 探针接住 BotGuardService、复用页面自身请求头，签名前把内容写入提示词输入框（binding，只写值不发送）；探针签名失败时自动回退 `say '1'` 引导 |
| **OpenAI 兼容端口** | 除 Gemini 原生 `/v1beta/...` 外，新增 `POST /v1/chat/completions`（非流式 + SSE，工具调用、`reasoning_content`、`usage`）与 `GET /v1/models`，与 Gemini 端口共用同一条 normalize→capture→replay→parse 链路 |
| **运行期设置（WebUI 可改）** | 参数覆写（思考控制 / 思考等级 / 返回思考内容 / 丢弃客户端 max_tokens / 采样参数）、预填充兼容（含思维链闭合守卫）、工具调用 bypass（含思考/工具签名哨兵），落在 `data/settings.json`，改完立即生效、无需重启 |
| **工具调用抗截断 bypass** | 让模型把正文写进一次合成函数调用的 `content` 参数，网关再还原成普通正文流；用于绕开只扫描正文流的外部审查截断。非流式/流式都支持，`required` 模式没调工具会报错 |
| **流式可靠性与心跳** | 没有「单次生成最长等待 / 流式空闲」看门狗，长回答不会被网关掐断；流式响应从请求开始每 1 秒发一次 SSE 心跳（OpenAI 空 delta chunk / Gemini 原生 `: ping` 注释行），bypass 首块前的长静默也不会触发客户端空闲超时；上游流被截断时用 JSON 修复抢救最后一块正文；页面 fetch 网络失败（如 `Failed to fetch`）后台重启该浏览器并明确报 502（不自动重试） |
| **签名哨兵** | 发往上游的思考签名 / 工具签名默认换成官方逃生口 `skip_thought_signature_validator`，避免上游「校验后替换内容」导致的死循环 |
| **Web 管理面板** | 提供 Web 管理控制台，支持账号管理（文本与 JSON 批量导入）、调用量监控、系统配置（日志 / API Key），以及独立的「运行期设置」页 |
| **Android 伴侣端** | 配套 Android 原生应用（`android/`），采用 Jetpack Compose 构建，支持多 Profile 隔离登录、Bitwarden 自动填充与一键导出凭据包 |
> [!NOTE]
> **内存占用参考**：纯 Python 后端服务常驻内存约 35 - 45 MB；拉起单实例受控 Chromium 后总物理内存 (PSS) 约 350 - 450 MB（宿主 Linux 下约 250 - 350 MB）。Android Termux 环境建议空闲 RAM ≥ 1 GB。

## 技术架构与文档

```
┌────────────────────────────────────────────────────────┐
│               Client (cURL / Python SDK)               │
└───────────────────────────┬────────────────────────────┘
                            │ /v1beta/models/...
┌───────────────────────────▼────────────────────────────┐
│              FastAPI HTTP Gateway (Port 8080)          │
├───────────────────────────┬────────────────────────────┤
│    Account Rotator        │    Chat & Wire Codec       │
│  (Sticky & 429 Isolation) │ (Protobuf-JSON & BotGuard) │
└───────────────────────────┼────────────────────────────┘
                            │ CDP WebSocket
┌───────────────────────────▼────────────────────────────┐
│      Controlled Chromium Instance (CloakBrowser)       │
│       In-Page XHR Replay (withCredentials=true)        │
└───────────────────────────┬────────────────────────────┘
                            │ HTTPS / RPC
┌───────────────────────────▼────────────────────────────┐
│         Google AI Studio (alkalimakersuite-pa)         │
└────────────────────────────────────────────────────────┘
```

详细的设计与协议分析文档：

- [系统架构设计 (ARCHITECTURE.md)](./docs/ARCHITECTURE.md)：分层架构、请求生命周期、Wire Codec 编解码与并发控制模型。
- [Wire 协议逆向技术规范 (WIRE_SPECIFICATION.md)](./docs/WIRE_SPECIFICATION.md)：Protobuf-JSON 数组映射、字段索引、多模态 Part 结构与 Schema 编码。
- [BotGuard 验证链路机制 (BOTGUARD_VERIFICATION_CHAIN.md)](./docs/BOTGUARD_VERIFICATION_CHAIN.md)：WAA 挑战握手、Wasm 动态签名、内容哈希与服务端校验流程。
- [智能体与协作规范 (AGENT.md)](./AGENT.md)：类型系统要求、代码质量检查与开发指南。

---

## 快速开始

### Android (Termux)

> [!TIP]
> **下载提示**：请务必从 [F-Droid](https://f-droid.org/packages/com.termux/) 或 [GitHub Releases](https://github.com/termux/termux-app/releases) 安装 Termux。**切勿使用 Google Play 商店版本**（Play 商店版本已停止维护，更新环境会报错）。

打开 Termux，依次复制执行以下命令：

```bash
# 步骤 1：更新 Termux 基础环境并安装 git
pkg update -y && pkg install -y git

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# 步骤 3：初始化依赖环境（自动配置手机专属加速源，无需本地编译）
bash scripts/setup-env.sh
uv sync

# 步骤 4：安装并配置受控浏览器（仅首次需要，耗时约 1~2 分钟）
bash scripts/setup-browser.sh

# 步骤 5：启动服务
uv run python main.py server --port 8080
```

启动成功后：
1. 打开手机自带浏览器，访问 **`http://localhost:8080`** 即可进入 Web 管理控制台；
2. 点击 **“账号管理”** 页面，粘贴你的 Google 账号 Cookie 或导入凭据包即可开始调用；
3. **后台保活提示**：下拉手机通知栏，点击 Termux 通知项上的 `Acquire Wakelock`，并在手机系统设置中为 Termux 开启“无限制后台运行/电池优化白名单”，防止锁屏休眠被系统误杀。

---

### Windows

前置要求：电脑已安装 [Python 3.11+](https://www.python.org/downloads/)（安装时勾选 `Add python.exe to PATH`）。

打开 PowerShell 或终端，依次执行：

```bat
# 步骤 1：安装 uv 包管理工具
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# 步骤 3：同步安装项目依赖
uv sync

# 步骤 4：安装 CloakBrowser 浏览器（仅首次需要）
scripts\install-browser.bat

# 步骤 5：启动服务
uv run python main.py server --port 8080
```

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### Linux / macOS

打开终端，依次执行：

```bash
# 步骤 1：安装 uv 包管理工具
curl -LsSf https://astral.sh/uv/install.sh | sh

# 步骤 2：克隆代码仓库并进入目录
git clone https://github.com/DeconstructedCube/aistudio-api.git
cd aistudio-api

# 步骤 3：同步安装项目依赖
uv sync

# 步骤 4：安装 CloakBrowser 浏览器（仅首次需要）
bash scripts/setup-browser.sh

# 步骤 5：启动服务
uv run python3 main.py server --port 8080
```

服务启动后，浏览器访问 **`http://localhost:8080`** 进入管理控制台。

---

### 一键启动（start.cmd / start.sh）

**Windows**：运行仓库根目录的 `start.cmd`，服务直接在当前终端**前台**跑，日志实时打在屏幕上；按 `Ctrl+C` 就停（cmd 若追问 `Terminate batch job (Y/N)?` 按 `Y`）。

```bat
start.cmd
set PORT=8081 && start.cmd              # 换端口
set HOST=127.0.0.1 && start.cmd         # 只本机
set AISTUDIO_BROWSER_HEADLESS=false && start.cmd   # 有头浏览器
```

脚本会自动复用 `.venv` / `venv`（都没有就用 PATH 里的 `python`），缺依赖时按 `requirements.txt` 装一遍。
日志只保留 LLM 请求 / 上游重试 / 错误（健康检查、面板轮询、uvicorn access 日志不再刷屏）；
要看更细的转换细节，把 `config.yaml` 的 `logging.level` 改成 `DEBUG`。

**Linux / macOS / Termux / Windows Git Bash**：`start.sh` 做同样的事（前台运行，Ctrl+C 停）：

```bash
./start.sh                      # 默认 8080 端口，无头模式
PORT=8081 ./start.sh            # 换端口
AISTUDIO_BROWSER_HEADLESS=false ./start.sh   # 有头启动（本机调试用）
VENV=/path/to/existing/venv ./start.sh       # 复用已有虚拟环境
```

两个脚本的行为一致：优先用已有 `.venv` / `venv`（`VENV` 可指定）；没有就用 `uv`
建环境（没有 uv 则 `python -m venv`）；依赖装过一次（能 import fastapi）就跳过。
浏览器仍需事先准备好（`scripts/setup-browser.sh` / `scripts/install-browser.ps1`，
或设置 `AISTUDIO_BROWSER_EXECUTABLE`）。

---

### Docker 部署

镜像已内置 CloakBrowser 运行时，支持开箱即用：

```bash
docker run -d \
  --name aistudio-api \
  --restart unless-stopped \
  -p 8080:8080 \
  -v aistudio-api-data:/app/data \
  ghcr.io/deconstructedcube/aistudio-api:latest
```

使用 Docker Compose 启动：

```bash
docker compose up -d
```

启动后访问 **`http://localhost:8080`**（或服务器 IP 端口）进入管理控制台。

---

## 配置与环境变量

服务启动后可访问 `http://localhost:8080` 进入 Web 控制台管理账号、日志与运行期设置。

### 环境变量支持

| 变量名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | API 服务监听端口 |
| `AISTUDIO_HOST` | string | `0.0.0.0` | 监听地址。默认对局域网/其它设备开放；只允许本机访问就设 `127.0.0.1` |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Web 控制台访问密码（亦可设置 `AISTUDIO_ADMIN_PASSWORD`） |
| `AISTUDIO_PROXY` | string | `""` | 出口代理地址，支持 `http://`、`https://` 或 `socks5://` |
| `AISTUDIO_DATA_DIR` | string | `data/` | 主数据持久化根目录（Docker 下为 `/app/data`） |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | 账号持久化凭据目录 |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | 模型调用与 Token 使用量统计持久化文件 |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | 账号调度与 429 冷却状态持久化文件 |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | 模型规则与默认工具配置文件路径 |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | 是否持久化模型调用与 Token 统计（1=开启，0=关闭） |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | 是否持久化账号 429 冷却与调度状态（1=开启，0=关闭） |
| `AISTUDIO_BROWSER_EXECUTABLE` | string | 自动探测 | 自定义 CloakBrowser 可执行文件路径（Docker 内为 `/opt/cloakbrowser/chrome`） |
| `AISTUDIO_BROWSER_PORT` | int | `9222` | Chromium 远程调试端口 (CDP)；池模式下第 N 台用 `端口 + N` |
| `AISTUDIO_BROWSER_POOL_SIZE` | int | `2` | 浏览器 worker 池大小（1=单浏览器旧路径；2..4=多台负载均衡 + 失败自愈） |
| `AISTUDIO_BROWSER_HEADLESS` | bool | `true` | 是否以无头模式运行 Chromium |
| `AISTUDIO_BROWSER_IDLE_TIMEOUT` | int | `900` / `1800` | 浏览器空闲自动回收超时秒数（Termux 下默认 900s，桌面端 1800s，0 为禁用） |
| `AISTUDIO_PROOT_NAME` | string | `aistudio-api` | Termux 环境下专用的 proot-distro 容器名称 |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.7-flash` | 默认文本模型 |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | string | `gemini-3.1-flash-image-preview` | 默认图像生成模型 |
| `AISTUDIO_TIMEZONE` | string | 自动探测 | 受控浏览器指纹与 Wire 协议交互时区（默认 `America/Los_Angeles`） |
| `AISTUDIO_LOCALE` | string | `en-US` | 受控浏览器区域语言 |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | 是否在终端转储每次 API 交互报文详情（支持 `DEBUG=1` 触发） |
| `AISTUDIO_DUMP_FILE` | bool | `false` | 是否将请求与响应报文详情落盘保存 |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | 报文转储保存目录 |
| `AISTUDIO_SETTINGS_FILE` | string | `data/settings.json` | 运行期设置持久化文件（见下节「运行期设置」） |

---

## 运行期设置（WebUI「运行期设置」页 / `GET|PUT /settings`）

和 `config.yaml` 的分工：`config.yaml` 是**启动期配置**（监听、鉴权、模型默认档案，手工编辑，改完需重启）；
下面这些是**运行期设置**，落在 `data/settings.json`，**改完立即生效、不用重启**。

| 分组 | 设置项 | 说明 |
|---|---|---|
| 参数覆写 | `thinkingMode` / `thinkingLevel` | 思考控制：跟随客户端 / 强制关到最低 / 强制用指定档位；档位按模型实际支持收敛（3 Pro 只有 low/high，3.8-flash 最低是 low） |
| 参数覆写 | `includeThoughts` | 关掉后上游不再回传思维链，OpenAI 端口 `reasoning_content` 为空、Gemini 端口没有 thought part |
| 参数覆写 | `dropMaxTokens` / `maxOutputTokens` | 丢掉客户端写死的 max_tokens / 用固定上限覆写 |
| 参数覆写 | `temperature` / `topP` / `topK` | 留空 = 用客户端值；填了无条件覆盖（topK 收敛到 63） |
| 预填充兼容 | `prefillEnabled` / `prefillCotGuard` / `prefillNudge` | 请求以 assistant 轮结尾时补续写指令并拼回响应；预填充停在未闭合标签时要求模型先闭合（思维链闭合守卫） |
| 工具调用 bypass | `bypassEnabled` / `bypassMode` / `bypassMergeOutside` / `bypassInstructionPosition` / `bypassInstruction` / `bypassToolPrefix` | 让模型把正文写进一次合成函数调用，网关再还原；用 `required` 模式可在模型没调工具时直接报错排查预设 |
| 工具调用 bypass | `sigSentinelThought` / `sigSentinelTool` | 签名哨兵：把发往上游的思考/工具签名换成官方逃生口 `skip_thought_signature_validator`（默认开） |

命令行读写（与 WebUI 等价）：

```bash
curl -s http://127.0.0.1:8080/settings | jq          # schema + 当前值 + 文件路径
curl -s -X PUT http://127.0.0.1:8080/settings \
  -H 'Content-Type: application/json' \
  -d '{"patch":{"bypassEnabled":true,"thinkingMode":"console","thinkingLevel":"low"}}'
```

> [!NOTE]
> 单轮口径：bypass 不再续写。正文最后一个 XML 标签没闭合时只会在日志里留一条
> 「输出可能不完整」，不驱动重试。
---

## 浏览器池与自愈（默认 2 台）

`AISTUDIO_BROWSER_POOL_SIZE`（默认 **2**，夹在 1..4）控制浏览器 worker 池：

- **每台一台独立 Chromium**：第 N 台 CDP 端口 = `AISTUDIO_BROWSER_PORT + N`（默认 **9222 / 9223**），
  profile 目录也按 worker 隔离（同一份 auth_file 是 `.../profile-w0`、`.../profile-w1`），
  两台不会抢 SingletonLock；内存约 **300–500 MB/台**，机器吃紧可以设回 1。
- **两台都接请求**：按 `(inflight, last_used)` 做 least-inflight 分发；账号按
  `i % n` 绑定到 worker（只有 1 份 cookie 时两台共用同一份，启动日志会 WARN 一次，属预期）。
- **故障自愈（零自动重试）**：403/401、5xx、502、页面 fetch/XHR 传输失败都按 **worker 故障**处理——
  后台 kill + 重启那台 Chromium，让**后续**请求拿到干净页面；**本次请求不重试**，
  按真实错误返回（传输失败 = 502），要不要重发由客户端决定。这些错误**不会给账号加冷却**
  （它们不是配额问题）；只有 429 保留账号/模型的配额冷却（只记账，不重启）；
  登录态失效（SessionExpiredError，真的被跳到登录页）仍按账号隔离 24h。
- **手动切号同步全池**：WebUI「切换账号」/删除账号会把**所有** worker 重绑到目标账号
  （就绪的走蓝绿热切、在途流式不中断；空闲休眠的下次拉起时生效），不会再出现
  「一半请求还挂在旧账号」；传输失败时日志附 CDP 网络层证据（`net::ERR_*`）便于归因断连。
- 被 kill 的 worker 重启期间处于 cooling（默认 15s 不再派活），连续重启失败会冷却 60s；
  **空闲回收（STOPPED）的那台仍会被下次请求重新拉起**（20~40s）。
- 设为 `1` 即回到旧的单浏览器路径（同样零重试；故障时杀掉浏览器进程，下次请求重建）。

```bash
# Windows 命令行临时启用两台（start.cmd 会读取这些环境变量）
set AISTUDIO_BROWSER_POOL_SIZE=2
start.cmd
```

> [!NOTE]
> 真实自愈冒烟会真的重启 Chromium：日志里用 `pool: worker #0/#1` 区分两台，
> 请求日志里有 `worker=#N` 与短 request id；验证完记得关服务（池会一起关掉两台浏览器）。

---

## 流式语义：什么时候是真流式，什么时候是「整段返回」

网关对外**始终是 SSE**：请求开始就给 `200 text/event-stream`，上游没有事件时每 1 秒发一条心跳
（OpenAI：空 delta chunk；Gemini 原生：`: ping`）。但**上游自己流不流，取决于请求形态**：

| 场景 | 上游行为 | 客户端会看到 |
|---|---|---|
| 普通请求（bypass 关） | MakerSuite `GenerateContent` 逐块下发正文 | 心跳 → 正文分块到达（实测 19 块、块间隔约 0.3s） |
| **bypass 开**（工具调用抗截断，默认开） | 模型输出的是一个**合成 functionCall**；Gemini 不分片下发函数参数，**第一帧就是整段工具调用**，在它准备好之前连 HTTP 响应头都不会到 | 心跳（可能持续数十秒到数分钟）→ 正文一次性到达 → usage / finish |

所以静默期的日志里**只有心跳，没有别的**（周期性「上游 Ns 无任何数据」WARN 已按用户要求
于 2026-10-20 移除——几百秒的 bypass 请求它只会刷屏）：

- 完成后请求日志里的 **`首包 X s / 总 Y s`** 是判据：首包 ≈ 总时长 = bypass 整段返回；
  首包 ≪ 总时长 = 真流式；
- 传输失败时另有 `CDP 网络层证据 … err=net::ERR_xxx` 一条 WARN 用来归因断连；
- 项目没有任何「生成等待时长」看门狗（用户明确要求），bypass 静默多久都不中止。

如果上游在第一帧之前断开（页面 `fetch` 抛 `TypeError: Failed to fetch`），网关会：

1. 记一次 `BrowserTransportError`（不是 500），后台 kill + 重启这台 worker（救后续请求）；
2. **本次请求直接返回 502 `upstream_error`，不自动重试**（网关零自动重试，2026-10-20 拍板），
   客户端要重试就重发一条新请求。

> 真机数据（2026-10-20，同一账号，flash）：
> 普通流式 19 块 / 块间隔 0.3s；bypass 请求 1 块，第一帧时间 = 整段生成时间
> （小请求 7~15s；24 万 token 的 argon 请求在主项目同通道记录里见过 **462s / 576s**）。
> 结论：**bypass 的「流式」在网关这一侧是合法的 SSE，但上游是整段返回**，不要拿心跳/静默
> 日志当故障判据。

---

## API 调用示例

支持以下鉴权方式：
- Query 参数：`?key=YOUR_API_KEY`
- 请求头：`x-goog-api-key: YOUR_API_KEY`、`x-api-key: YOUR_API_KEY` 或 `Authorization: Bearer YOUR_API_KEY`

### cURL 调用

<details>
<summary><b>1. 查询模型列表</b></summary>

```bash
curl http://localhost:8080/v1beta/models \
  -H "x-goog-api-key: your-api-key"
```
</details>

<details>
<summary><b>2. 文本生成 (Non-streaming)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:generateContent \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "用简短的一句话介绍量子计算。"}]
      }
    ]
  }'
```
</details>

<details>
<summary><b>3. 流式生成 (Server-Sent Events)</b></summary>

```bash
curl http://localhost:8080/v1beta/models/gemini-3.7-flash:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "介绍一下 Python 协程的工作原理。"}]
      }
    ]
  }'
```
</details>

---

### Python SDK (google-genai)

使用 Google 官方 `google-genai` SDK 直连本代理服务：

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 1. 流式响应
response = client.models.generate_content_stream(
    model="gemini-3.7-flash",
    contents="解释一下什么是反应式编程？",
)
for chunk in response:
    print(chunk.text, end="", flush=True)

# 2. 非流式响应
result = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="用 Python 写一个快速排序算法。",
)
print(result.text)
```

---

### 工具调用 (Function Calling)

```python
from google import genai
from google.genai import types

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "http://localhost:8080",
    },
)

# 定义工具声明
weather_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="get_current_weather",
            description="获取指定地点的实时天气状况",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "location": types.Schema(
                        type="STRING",
                        description="城市或地区名称，例如 'Beijing' 或 'Tokyo'",
                    ),
                    "unit": types.Schema(
                        type="STRING",
                        enum=["celsius", "fahrenheit"],
                        description="温度单位",
                    ),
                },
                required=["location"],
            ),
        )
    ]
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="今天杭州天气怎么样？",
    config=types.GenerateContentConfig(tools=[weather_tool]),
)

if response.function_calls:
    for call in response.function_calls:
        print(f"触发工具调用: {call.name}")
        print(f"调用参数: {call.args}")
```

---

## Web 管理控制台

系统内置静态打包的 Web 前端管理控制台（访问 `http://localhost:8080`）：

- **仪表盘 (Dashboard)**：实时展示服务健康状况、各模型请求量、成功率及 429 冷却指标。
- **账号管理 (Accounts)**：支持 Cookie 单条与凭据包批量导入、自动探测多账号、手动切换当前活跃账号或重置冷却状态。
- **系统设置 (Settings)**：服务状态、日志级别与请求转储、API Key 管理；运行期参数在独立的「运行期设置」页调整。
- **访问鉴权**：设置环境变量 `AISTUDIO_WEB_PASSWORD` 后自动开启登录鉴权守卫。

---

## 开发与测试

前端源码位于 `web/`，采用 Vue 3 + TypeScript + Vite + Tailwind CSS：

```bash
# 启动前端开发调试服务
cd web
bun install
bun run dev          # 启动开发服务（localhost:3000，代理至 8080）
bun run type-check   # 执行 TypeScript 类型校验
bun run lint         # 执行 ESLint 代码规范检查
bun run build        # 生产构建，产物输出至 src/aistudio_api/static
```

Python 端质量检查与测试：

```bash
# 执行代码风格与 Lint 检查
uv run ruff check .
# 执行静态类型检查
bun x pyright

# 执行单元测试套件
uv run pytest
```

> [!NOTE]
> Windows 本地基线是 **567 passed / 7 failed**（7 条都是环境类既有失败，CI Linux 上不存在，
> 明细见 `AGENT.md` §3.4）。交接状态、未解决问题、真机证据与诊断脚本见 **`AGENT.md` §6**；
> 项目里只有一个目录 `C:\sister-aistudio`，测试用项目 `.venv`（`pytest`/`ruff` 已装）；
> 改完代码记得重启服务才生效。

Android 伴侣端代码质量检查：

```bash
# 执行 Kotlin 代码规范检查 (已配置于 ~/.zshrc)
ktlint "android/app/src/main/java/**/*.kt"
```

---

## 开源协议

本项目基于 [MIT License](./LICENSE) 许可协议开源。
