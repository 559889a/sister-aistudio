# aistudio-api

Google AI Studio 反向代理网关：一台受控 Chromium（CDP 直连，无 Node/Playwright/Selenium）承载 AI Studio Web 会话，
对外提供 **Gemini `v1beta`** 与 **OpenAI 兼容**两套 API，另附 Web 管理控制台。
基于 [DeconstructedCube/aistudio-api](https://github.com/DeconstructedCube/aistudio-api) 二次开发，**仅支持 Windows 宿主**。

## 目录

- [核心特性](#核心特性)
- [架构](#架构)
- [快速开始](#快速开始)
- [三上游与模型前缀路由](#三上游与模型前缀路由)
- [配置与环境变量](#配置与环境变量)
- [运行期设置](#运行期设置)
- [单浏览器与账号池（零自动重试）](#单浏览器与账号池零自动重试)
- [流式语义](#流式语义)
- [API 调用示例](#api-调用示例)
- [Web 管理控制台](#web-管理控制台)
- [开发与测试](#开发与测试)

---

## 核心特性

| 功能模块 | 说明 |
|---|---|
| **双协议端口** | Gemini 原生 `/v1beta/...`（`google-genai` SDK 直连；`generateContent` / `streamGenerateContent` / `models`）；OpenAI 兼容 `POST /v1/chat/completions`（非流式 + SSE，工具调用、`reasoning_content`、`usage`）与 `GET /v1/models` |
| **三上游前缀路由** | `pg/`（默认，受控浏览器）/ `express/`（Vertex Express 官方 Key 直连）/ `cookie/`（Cloud 控制台 Cookie 直连），按模型名前缀选路，详见[下文](#三上游与模型前缀路由) |
| **多账号自动轮换** | 单浏览器账号池：只有 429 驱动换号，且**第 5 次** 429 才把账号/模型打入冷宫（防误杀，冷却到美西午夜），成功一次清零计数；网关对当次请求**零自动重试**，失败按真实错误返回 |
| **工具调用抗截断 bypass** | 让模型把正文写进一次合成函数调用的 `content` 参数，网关再还原成普通正文流；用于绕开只扫描正文流的外部审查截断。默认关，非流式/流式都支持，`required` 模式没调工具会报错 |
| **无消息引导（WAA binding）** | 默认不往对话里发探针消息：探针接住页面 BotGuardService、复用页面自身请求头，签名前把内容写入提示词输入框（只写值不发送）；探针签名失败自动回退 `say '1'` 引导 |
| **签名哨兵** | 发往上游的思考签名 / 工具签名默认换成官方逃生口 `skip_thought_signature_validator`，避免上游「校验后替换内容」导致的死循环 |
| **流式可靠性与心跳** | 没有任何「生成等待时长」看门狗，长回答不会被网关掐断；流式响应每 1 秒一条 SSE 心跳（OpenAI 空 delta chunk / Gemini `: ping`），bypass 首块前的长静默不会触发客户端空闲超时；上游流被截断时用 JSON 修复抢救最后一块正文 |
| **运行期设置（WebUI 可改）** | 参数覆写（思考控制 / 档位 / 采样参数 / max_tokens）、预填充兼容（含思维链闭合守卫）、bypass 开关，落在 `data/settings.json`，改完立即生效、无需重启 |
| **下游参数容错** | 自动过滤下游传入的非标准安全分类（如 `HARM_CATEGORY_CIVIC_INTEGRITY`）与不兼容工具，避免 400 阻断 |
| **批量子账号探活** | 导入单个包含多个 Google 身份的 Cookie 后，自动递归探活子账号（`u/0`, `u/1`...）并分别建档 |
| **运行状态持久化** | 模型调用量与 Token 统计（`stats.json`）、账号 429 冷却状态（`rotator_state.json`）自动落盘，重启不丢 |
| **Web 管理面板** | 仪表盘（请求量 / 成功率 / 429 指标）、账号管理（文本与 JSON 批量导入）、系统设置与运行期设置 |

> [!NOTE]
> **内存占用参考**：纯 Python 后端常驻约 35-45 MB；拉起受控 Chromium 后总物理内存 (PSS) 约 350-450 MB。

---

## 架构

```
┌──────────────────────────────────────────────────┐
│      Client（cURL / google-genai / OpenAI SDK）   │
└────────────────────────┬─────────────────────────┘
                         │  /v1beta/...  /v1/chat/completions
┌────────────────────────▼─────────────────────────┐
│            FastAPI HTTP Gateway (:8080)           │
│      账号池 / 429 冷却 / 参数覆写 / bypass / 心跳   │
└─────────┬──────────────────┬──────────────────┬───┘
     pg/（默认）          express/           cookie/
┌─────────▼────────┐ ┌──────▼───────────┐ ┌───▼──────────────┐
│  受控 Chromium    │ │  Vertex Express  │ │  Cloud Console   │
│  （CDP + WAA 签名）│ │  （官方 API Key） │ │（batchGraphql）  │
└──────────────────┘ └──────────────────┘ └──────────────────┘
```

`pg/` 通道：网关把请求在页面上下文里重放（`withCredentials` XHR，WAA/BotGuard 由页面亲签），命中 AI Studio 的 playground 配额桶（含 `gemini-4-argon`）。
`express/` 与 `cookie/` 无浏览器、无账号池语义，错误原样透传，同样零自动重试。

---

## 快速开始

前置：[Python 3.11+](https://www.python.org/downloads/)（勾选 `Add python.exe to PATH`）。

```powershell
# 1. 安装 uv（已装跳过）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. 同步依赖
uv sync

# 3. 安装受控浏览器（仅首次；或自备并设 AISTUDIO_BROWSER_EXECUTABLE）
scripts\install-browser.bat

# 4. 启动
.\start.ps1
```

`start.ps1` 在前台跑、日志实时输出，`Ctrl+C` 停止；自动复用 `.venv` / `venv`，缺依赖时按 `requirements.txt` 补装。
执行策略受限时先跑一次 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

```powershell
$env:PORT='8081'; .\start.ps1                            # 换端口
$env:HOST='127.0.0.1'; .\start.ps1                       # 只本机
$env:AISTUDIO_BROWSER_HEADLESS='false'; .\start.ps1      # 有头浏览器（调试 WAA）
```

等效命令：`uv run python main.py server --port 8080`。

---

## 三上游与模型前缀路由

按**模型名前缀**选通道（`infrastructure/upstreams/registry.py`）：

| 前缀 | 通道 | 凭据 | 说明 |
|---|---|---|---|
| `pg/`（默认） | 受控浏览器 + 账号池 | AI Studio Cookie（WebUI 导入） | playground 配额桶，支持 argon；无前缀裸模型名也走这里（除非 `AISTUDIO_UPSTREAM` 覆盖默认通道） |
| `express/` | Vertex Express | `VERTEX_EXPRESS_API_KEY` | 官方 Key 直连，无浏览器；真流式（含 `streamFunctionCallArguments` 工具参数增量） |
| `cookie/` | Cloud 控制台 batchGraphql | `AISTUDIO_CONSOLE_COOKIE` | Cookie 直连，无浏览器；配额金贵 |

- `/v1/models` 走本地模型表 `data/models.json`（带前缀别名的模型清单）；裸模型名不出现在列表里但**仍可调用**（走默认通道）。
- `AISTUDIO_UPSTREAM=vertex_express|console_graphql` 可把**默认通道**切到无浏览器上游（`pg/` 前缀仍强制走浏览器）。
- `express/` 锁 `locations/global`：无位置入口会被 Google 路由到项目默认区（实测 `asia-southeast1`），新模型只在 global 发布，表现为「模型名写错」式的 404；钉 global 需配合 `GOOGLE_PROJECT_ID` 项目号。

---

## 配置与环境变量

服务启动后可访问 `http://localhost:8080` 进入 Web 控制台管理账号、日志与运行期设置。
凭据类配置放 `.env`（已 gitignore，不入库）。

| 变量名 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `AISTUDIO_PORT` | int | `8080` | API 服务监听端口 |
| `AISTUDIO_HOST` | string | `0.0.0.0` | 监听地址；只允许本机访问就设 `127.0.0.1` |
| `AISTUDIO_WEB_PASSWORD` | string | `""` | Web 控制台访问密码（亦可 `AISTUDIO_ADMIN_PASSWORD`） |
| `AISTUDIO_PROXY` | string | 自动探测 | 出口代理（`http://` / `https://` / `socks5://`），浏览器与无浏览器上游共用；不设时自动探测本机 `7890/7897/10808` 端口 |
| `AISTUDIO_UPSTREAM` | string | `aistudio` | 默认通道覆盖：`vertex_express` / `console_graphql`（`pg/` 前缀不受影响） |
| `VERTEX_EXPRESS_API_KEY` | string | `""` | express/ 通道官方 Key |
| `GOOGLE_PROJECT_ID` | string | `""` | express/ 通道项目号（钉 global 位置必需） |
| `AISTUDIO_EXPRESS_LOCATION` | string | `global` | express/ 通道位置（保持默认） |
| `AISTUDIO_CONSOLE_COOKIE` | string | `""` | cookie/ 通道控制台 Cookie |
| `AISTUDIO_CONSOLE_PROJECT_ID` | string | `""` | cookie/ 通道项目号 |
| `AISTUDIO_DATA_DIR` | string | `data/` | 主数据持久化根目录 |
| `AISTUDIO_ACCOUNTS_DIR` | string | `data/accounts` | 账号持久化凭据目录 |
| `AISTUDIO_STATS_FILE` | string | `data/stats.json` | 模型调用与 Token 统计文件 |
| `AISTUDIO_ROTATOR_STATE_FILE` | string | `data/rotator_state.json` | 账号调度与 429 冷却状态文件 |
| `AISTUDIO_CONFIG_FILE` | string | `config.yaml` | 模型规则与默认工具配置 |
| `AISTUDIO_PERSIST_STATS` | bool | `true` | 是否持久化调用统计 |
| `AISTUDIO_PERSIST_ROTATOR` | bool | `true` | 是否持久化账号冷却状态 |
| `AISTUDIO_BROWSER_EXECUTABLE` | string | 自动探测 | 自定义 Chromium 可执行文件路径 |
| `AISTUDIO_BROWSER_PORT` | int | `9222` | Chromium CDP 远程调试端口（单浏览器独占） |
| `AISTUDIO_BROWSER_HEADLESS` | bool | `true` | 是否无头运行 Chromium |
| `AISTUDIO_BROWSER_IDLE_TIMEOUT` | int | `1800` | 浏览器空闲自动回收秒数（0 禁用） |
| `AISTUDIO_DEFAULT_TEXT_MODEL` | string | `gemini-3.8-flash` | 默认文本模型 |
| `AISTUDIO_DEFAULT_IMAGE_MODEL` | string | `gemini-3.1-flash-image-preview` | 默认图像生成模型 |
| `AISTUDIO_TIMEZONE` | string | 自动探测 | 受控浏览器指纹时区（默认 `America/Los_Angeles`） |
| `AISTUDIO_LOCALE` | string | `en-US` | 受控浏览器区域语言 |
| `AISTUDIO_DUMP_REQUESTS` | bool | `false` | 终端转储每次 API 交互报文（支持 `DEBUG=1` 触发） |
| `AISTUDIO_DUMP_FILE` | bool | `false` | 报文详情落盘 |
| `AISTUDIO_DUMP_DIR` | string | `dumps/` | 报文转储目录 |
| `AISTUDIO_SETTINGS_FILE` | string | `data/settings.json` | 运行期设置持久化文件 |

---

## 运行期设置（WebUI「运行期设置」页 / `GET|PUT /settings`）

与 `config.yaml` 的分工：`config.yaml` 是**启动期配置**（监听、鉴权、模型默认档案，改完需重启）；
运行期设置落在 `data/settings.json`，**改完立即生效**。

| 分组 | 设置项 | 说明 |
|---|---|---|
| 参数覆写 | `thinkingMode` / `thinkingLevel` | 思考控制：跟随客户端 / 强制关到最低 / 强制指定档位（按模型实际支持收敛） |
| 参数覆写 | `includeThoughts` | 关掉后上游不再回传思维链（OAI 端口 `reasoning_content` 为空） |
| 参数覆写 | `dropMaxTokens` / `maxOutputTokens` | 丢掉客户端写死的 max_tokens / 用固定上限覆写 |
| 参数覆写 | `temperature` / `topP` / `topK` | 留空用客户端值；填了无条件覆盖（topK 收敛到 63） |
| 预填充兼容 | `prefillEnabled` / `prefillCotGuard` / `prefillNudge` | 请求以 assistant 轮结尾时补续写指令并拼回响应；预填充停在未闭合标签时要求模型先闭合 |
| 工具调用 bypass | `bypassEnabled`（默认关） / `bypassMode` / `bypassMergeOutside` / `bypassInstructionPosition` / `bypassInstruction` / `bypassToolPrefix` | 正文写进合成函数调用再还原；`required` 模式在模型没调工具时直接报错，用于排查预设 |
| 工具调用 bypass | `sigSentinelThought` / `sigSentinelTool` | 签名哨兵：换成官方逃生口 `skip_thought_signature_validator`（默认开） |

命令行读写（与 WebUI 等价）：

```bash
curl -s http://127.0.0.1:8080/settings | jq
curl -s -X PUT http://127.0.0.1:8080/settings \
  -H 'Content-Type: application/json' \
  -d '{"patch":{"bypassEnabled":true,"thinkingMode":"console","thinkingLevel":"low"}}'
```

---

## 单浏览器与账号池（零自动重试）

单台受控 Chromium 承载全部请求；账号池在同一台浏览器上轮换，切换走蓝绿热切（在途流式不中断）。

- **只有 429 驱动换号，且防误杀**：单账号/模型累计第 5 次 429 才打入冷宫（冷却到美西午夜配额重置）；前 4 次只计数不冷却、不换号，成功一次即清零。
- **故障自愈（零自动重试）**：403/401、页面 fetch/XHR 传输失败、会话挂死、登录页重定向都是**浏览器故障**——杀掉浏览器进程（下次请求自动重建 20~40s），**本次请求不重试**，按真实错误返回（传输失败 = 502），要不要重发由客户端决定。这些错误不给账号加冷却、不换号。
- **登录态失效**（真被跳到登录页）：凭据已死，按账号隔离 24h 并重启浏览器。
- 传输失败时日志附 CDP 网络层证据（`net::ERR_*`）便于归因断连。

---

## 流式语义

网关对外**始终是 SSE**：请求开始就给 `200 text/event-stream`，上游无事件时每秒一条心跳。
但**上游流不流，取决于请求形态**：

| 场景 | 上游行为 | 客户端看到 |
|---|---|---|
| 普通请求（bypass 关） | MakerSuite `GenerateContent` 逐块下发正文 | 心跳 → 正文分块（实测约 0.3s 块间隔） |
| bypass 开 | 模型输出一个合成 `functionCall`；Gemini 不分片下发函数参数，第一帧即整段 | 心跳（可达数十秒到数分钟）→ 正文一次性到达 → usage / finish |

判据看完成后请求日志的 **`首包 X s / 总 Y s`**：首包 ≈ 总时长 = bypass 整段返回；首包 ≪ 总时长 = 真流式。
express/ 通道（Vertex Express）是上游真流式，工具参数还有增量下发。
静默期日志里只有心跳属正常——项目没有任何「生成等待时长」看门狗，bypass 静默多久都不中止。

上游在第一帧前断开（页面 `fetch` 抛 `TypeError: Failed to fetch`）时：记一次 `BrowserTransportError`、杀浏览器救后续请求；**本次直接返回 502**，不自动重试。

---

## API 调用示例

支持的鉴权方式：query `?key=`、请求头 `x-goog-api-key` / `x-api-key` / `Authorization: Bearer`。

### cURL

```bash
# 模型列表（本地表，带通道前缀别名）
curl http://localhost:8080/v1beta/models -H "x-goog-api-key: your-api-key"

# 文本生成（默认通道 = pg/）
curl http://localhost:8080/v1beta/models/gemini-3.8-flash:generateContent \
  -H "x-goog-api-key: your-api-key" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"用一句话介绍量子计算。"}]}]}'

# 流式 + 指定通道（express/ 走 Vertex Express）
curl http://localhost:8080/v1beta/models/express/gemini-4-argon:streamGenerateContent?alt=sse \
  -H "x-goog-api-key: your-api-key" -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}'

# OpenAI 兼容端口
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

### Python SDK（google-genai）

```python
from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={"api_version": "v1beta", "base_url": "http://localhost:8080"},
)

for chunk in client.models.generate_content_stream(
    model="gemini-3.8-flash", contents="解释一下反应式编程？"
):
    print(chunk.text, end="", flush=True)
```

### 工具调用

Gemini 端口与 OpenAI 端口均支持 Function Calling（`tools` / `function_declarations`，OAI 端口自动翻译）。
`express/` 通道另有 Vertex 专有的工具参数增量流式（`streamFunctionCallArguments`）。

---

## Web 管理控制台

访问 `http://localhost:8080`（Vue 3 + Vite 构建产物内置于后端）：

- **仪表盘**：服务健康、各模型请求量、成功率与 429 冷却指标。
- **账号管理**：Cookie 单条与凭据包批量导入、多账号自动探测、手动切换 / 重置冷却。
- **系统设置**：日志级别、请求转储、API Key 管理；运行期参数在独立「运行期设置」页。
- **访问鉴权**：设 `AISTUDIO_WEB_PASSWORD` 后自动开启登录守卫。

---

## 开发与测试

前端源码在 `web/`（Vue 3 + TypeScript + Vite + Tailwind）：

```bash
cd web
bun install && bun run dev        # 开发服务（:3000，代理至 8080）
bun run build                     # 产物输出至 src/aistudio_api/static
```

Python 侧：

```bash
uv run ruff check .               # Lint
uv run pytest                     # 单元测试（基线 533 passed / 0 failed）
```

> 开发规范与交接状态见 [AGENT.md](./AGENT.md)。改完代码需重启 `start.ps1` 才生效。
