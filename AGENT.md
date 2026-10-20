# 智能体协作与开发规范 (AGENT.md)

本文档面向所有参与本项目的代码代理（Coding Agents）与开发者，明确核心架构共识、运行环境约束与代码规范。

---

## 目录

- [1. 核心架构与设计原则](#1-核心架构与设计原则)
- [2. 运行时与环境约束（仅 Windows）](#2-运行时与环境约束仅-windows)
- [3. 代码质量与类型规范](#3-代码质量与类型规范)
- [4. 并发安全与多账号守则](#4-并发安全与多账号守则)
- [5. 代码结构索引](#5-代码结构索引)

---

## 1. 核心架构与设计原则

`aistudio-api` 是 Google AI Studio 反向代理网关，提供原生 **Gemini API 兼容接口**（`/v1beta/...`）。

- **原生 Gemini 协议专一性**：服务于 Gemini 官方 API 规范（包含 thinking、multimodal、image-generation、function calling 等），不引入额外的跨厂商协议转译层。
- **轻量 CDP 驱动**：使用纯 Python 异步 WebSocket 直连 Chrome DevTools Protocol（CDP），不依赖 Node.js、Playwright 或 Selenium。
- **浏览器内 XHR Replay**：在 MakerSuite 页面上下文中执行携带 `withCredentials = true` 的异步 XHR 请求，天然复用完整的 Cookie 会话、BotGuard 快照与环境指纹。
- **Proof 与输入框绑定（binding，别删）**：WAA proof 与页面提示词输入框状态强绑定 —— 签名前必须把**哈希原文**写进 `ms-prompt-box textarea`（原生 setter + `input`/`change` 事件 + 焦点扰动），请求结束清空。只写值：**绝不点发送、绝不按 Enter**。细节见 `docs/BOTGUARD_VERIFICATION_CHAIN.md` §4.3。
- **引导不需要发消息**：BotGuardService 通过 Angular DI 探针接住 —— 包 `default_MakerSuite.n`（页面自己的 `inject`），把**形状匹配**（`service.A.snapshot` 是函数）的注入结果当服务；旧构建的 `TB.prototype.initialize|getMetadata` 包装保留但**不再是前提**（2026-10-20 的页面构建不再导出 `dms.TB`，依赖它会让探针永远装不上、每次启动白等 35 秒才回退 page_send）。请求头从页面任意 `/$rpc/` 请求采集；探针签名通过才走 binding 引导，否则回退 `say '1'`；连续 3 次 `service_unavailable` 提前回退（`botguard_bootstrap.SERVICE_UNAVAILABLE_BAIL`）。
- **代码即事实标准（Code as Ground Truth）**：通过全量测试验证的代码是系统的事实标准。当技术规范文档与真实代码逻辑出现分歧时以代码为准，并在重构过程中同步更新文档。
- **启动期配置 vs 运行期设置**：监听/鉴权/浏览器/模型默认档案属于 `config.yaml`（手工编辑，改完重启生效）；「参数覆写 / 预填充兼容 / 工具调用 bypass / 签名哨兵」属于**运行期设置**，落在 `data/settings.json`，WebUI「运行期设置」页或 `PUT /settings` 改完**立即生效**。新增这类开关必须走 `application/runtime_settings.py` 的 schema，别再往 `config.yaml` 里加。
- **两条对外端口共用一条上游链路**：Gemini 原生 `/v1beta/...` 与 OpenAI 兼容 `/v1/chat/completions` 都走 `prepare_gemini_request → capture → replay → parse`；格式转换只在 `openai_bridge.py`，参数覆写只在 `overrides.py`，网关层（bypass/prefill/哨兵）对两条端口同时生效，别为某一条端口单独复制一套。
- **流式不做生成看门狗，只发心跳**（用户明确要求）：不存在「单次生成最长等待 / 流式空闲超时」这类会掐断长回答的计时器——`streaming_init.js`、`hooked_request.js`、`transport.py` 里都不许加回来。需要保活就在 SSE 层发心跳（`application/stream_heartbeat.py`，1 秒间隔；OpenAI 发空 delta chunk、Gemini 原生发 `: ping` 注释行），它只写字节、不改变上游等待逻辑。基础设施类超时（浏览器启动/Cdp 连接/BotGuard 签名）保留。
- **流式 JSON 修复只救截断、不改内容**：`IncrementalJSONStreamParser` 的 `feed()` 对解析失败的完整分块先补全再解析，`finish()` 抢救流末尾被截断的最后一块（走与 `feed()` 同一个 `_consume()`）；补全只做「闭合字符串/数组/对象、处理末尾反斜杠」，不得改写已有正文。`wire_parser.parse_response_part` 里「2 元素且 index1 是角色名」的守卫只拦 `[parts, role]` 形状，别扩大成按角色名一律丢弃（正文恰好是 model/user/system/assistant 时会真丢字）。
- **页面 fetch/XHR 的网络失败是 `BrowserTransportError`，不是 500**：`transport.py` 把 `Failed to fetch`、页面关闭、CDP 命令超时统一抛这个类型；`handle_attempt_exception` 在**还没吐任何正文**时清模板缓存原地重试（页面不可用则重建会话），已吐数据或重试耗尽报 502 `UNAVAILABLE`。别退回「未知严重异常 → 500」，也**不要**因为传输失败去切换账号——账号本身没坏。
- **网关层零自动重试（用户 2026-10-20 拍板，别加回来）**：网关对**当次请求**不做任何自动重试——不清模板原地重打、不切账号重试（204、偶发 Ambiguous 404 抖动也不重试）；失败按真实错误返回，由客户端决定重发。`handle_attempt_exception` 返回 `NoReturn`：归类 + 记账 + 唯一自愈动作（特定故障重启浏览器）+ 抛错。调度层动作不是重试、保留：`ensure_active_account` 请求前置挑号、429 冷却、SessionExpired 账号隔离 24h。
- **单浏览器 + 账号池，只有 429 换号且第 5 次才入冷宫（用户 2026-10-20 拍板）**：多浏览器 worker 池（`client_pool.py`、`AISTUDIO_BROWSER_POOL_SIZE`）已整体移除，**别再加回来**；全部请求跑在一台 Chromium 上，多账号靠 `BrowserSession.switch_auth` 蓝绿热切轮换（在途流不中断）。换号**只由 429 驱动**：`account_rotator.RATE_LIMITED_COOLDOWN_THRESHOLD = 5`——单账号/模型累计第 5 次 429 才冷却到美西午夜（前 4 次只计数不冷却，成功一次清零计数；防误杀）。403/401、传输失败、会话挂死是浏览器故障：清模板 + 杀进程（下次请求 `ensure_ready` 重建），**不记账不换号**；SessionExpired 按账号隔离 24h 并重启浏览器。
- **bypass 请求的上游是「整段返回」，不是网关丢事件**：bypass 会注入合成 functionCall，Gemini 不分片下发函数参数，**上游第一帧就是整段工具调用**，在此之前连 HTTP 响应头都不到（2026-10-20 真机复测：bypass 开 首包 5.4s/总 5.5s、正文 1 块；bypass 关 首包 3.1s/总 7.8s、19 块逐块到达）。**周期性「上游 Ns 无任何数据」WARN 已按用户要求移除（2026-10-20，几百秒的请求只会刷屏）——不要加回来**；判据是完成日志的「首包 X s / 总 Y s」，失败归因靠 CDP 网络层证据 WARN。**也不要因此加看门狗、提前 502、或伪造分块**。
- Flake：页面 `fetch` 在长等待后抛 `Failed to fetch` 是真实的网络/上游断开（`BrowserTransportError`），不是本项目的 abort（`abort_stream` 目前没有任何调用点）；按 worker 故障重启 + 有备用就切，没备用才 502。主项目（`C:\build2api`）同通道记录里也有一次 183s 的 `Failed to fetch`。
- **架构事实优先（Fact & Architecture over Brittle Tests）**：保持架构整洁与低耦合。避免为了迁就耦合了私有变量的脆弱 mock 测试而妥协生产设计；遇冲突时应重构测试以对齐公共接口契约，消除抽象泄露。
- **协议索引常量化（Specification Constants & No Magic Numbers）**：Protobuf-over-JSON 逆向数组索引统一提取为具备明确语义的规范枚举或常量（如 `wire_spec.py`），杜绝裸数字散落业务逻辑中。

## 2. 运行时与环境约束（仅 Windows）

**唯一受支持的平台是 Windows 宿主**（2026-10-20 用户拍板；Termux/Linux/Docker/
macOS 支持与 Android 伴侣端已整体移除，别加回来）。Python 工具链用 `uv` 管虚拟环境，
Chromium（CloakBrowser）由 `browser_engine.py` 直接子进程拉起（Job Object 看护），
CDP 经 `cdp_client.py` WebSocket 直连。

### 2.1 环境准备与启动流程

```powershell
# 1. 安装 uv 并同步依赖
uv sync

# 2. 准备受控浏览器运行时（仅首次执行）
scripts\install-browser.bat

# 3. 启动服务（前台运行，Ctrl+C 即停；或直接用 start.cmd）
uv run python main.py server --port 8080
```

> [!NOTE]
> - `start.cmd`：前台运行（选/建 venv → 装依赖 → 起服务），日志直接打在终端，**Ctrl+C 即停**。
> - 终端日志只保留 LLM 请求/上游重试/错误（健康检查、面板轮询、uvicorn access、httpx 逐请求都静音）；日志格式是 `[INFO] 2026-10-19T10:54:15.499Z [aistudio-api] …`，请求带短 id（`→` / `← 完成` / `请求失败` / `客户端断开`）。要协议细节把 `logging.level` 设 DEBUG。

### 2.2 前端构建与管理

- 前端位于 `web/` 目录，基于 Vite + Vue 3 + TypeScript + Tailwind CSS。
- 依赖管理与构建使用 `bun`（`bun run dev`、`bun run build`、`bun run lint`、`bun run type-check`）。
- 生产构建产物直接输出至 `src/aistudio_api/static/`，由 FastAPI 静态托管。

### 2.3 常见排错

| 现象 | 原因 | 处理方案 |
|---|---|---|
| 端口被占用 | 上一次服务未完全退出 | 关掉旧 `start.cmd` 窗口（Ctrl+C）后重试，或指定 `--port <other_port>` |
| `Chromium CDP endpoint on port N not ready` | CloakBrowser 未安装或路径不对 | 重新运行 `scripts\install-browser.bat`，或设 `AISTUDIO_BROWSER_EXECUTABLE` |
| `pydantic-core` 导入失败 | 使用了系统 pip 安装而非 `uv` | 清理 `.venv/` 后重新执行 `uv sync` |

### 2.4 临时文件与目录规范

- **避免硬编码 `/tmp`**：跨平台惯例，统一用 `tempfile.gettempdir()` / `tempfile.mkdtemp()`
  创建临时目录（Windows 下解析到 `%TEMP%`），并在 `try...finally` + `shutil.rmtree` 及时清理。
---

## 3. 代码质量与类型规范

本项目启用了严格的静态类型检查与代码质量工具链：

### 3.1 类型安全要求

- **避免 `Any` 滥用**：在公共接口、领域模型与核心流程中避免无约束的 `Any`。
- **未知数据收窄**：对于动态解析的数据，使用 `object` 配合 `isinstance` 进行类型收窄。
- **结构化定义**：数据结构使用具名 dataclass（如 `NormalizedGeminiRequest`、`AccountMeta`）或明确泛型类型标注（如 `dict[str, object]`）。

### 3.2 Ruff 代码风格与规范配置

项目采用 Ruff 作为统一的 Python 代码风格与 Lint 工具，并在 `pyproject.toml` 中开启了严格的规则检查：

- **激活规则集**：
  - `E` / `W` (pycodestyle 错误与警告)
  - `F` (Pyflakes 代码逻辑错误)
  - `I` (isort 自动 import 排序)
  - `B` (flake8-bugbear 常见陷阱与 Bug 预防)
  - `C4` (flake8-comprehensions 列表推导式优化)
  - `UP` (pyupgrade Python 3.11+ 语法升级)
  - `SIM` (flake8-simplify 代码精简，强制 `contextlib.suppress` 替代 `try-except-pass`)
  - `PTH` (flake8-use-pathlib 规范使用 Pathlib)
  - `RUF` (Ruff 专有规则检查)
  - `ASYNC` (flake8-async 异步编程反模式检查，如 `asyncio.Event` 替代轮询睡眠)
  - `PIE` (flake8-pie 冗余代码消除)
  - `Q` / `RSE` / `RET` (引号、raise 括号与返回值规范)
  - `A` (内建函数命名防覆盖)
  - `YTT` (flake8-2020 版本兼容性检查)
- **全局豁免（均有明确理由）**：
  - `B008`：FastAPI `Depends()` 注入参数的标准模式
  - `RUF001` / `RUF002` / `RUF003`：注释与文档字符串中的中文全角标点（项目文档语言为中文）
  - `ASYNC109` / `ASYNC230` / `ASYNC240`：异步函数中的超时参数与本地文件访问（单机自托管场景）
  - `E501`：行宽由 formatter 统一处理
- **按文件豁免**：`main.py` 豁免 `E402`（启动时需先注入 `sys.path`）；`tests/**` 豁免动态殊性规则
- **检查与格式化指令**：
  ```bash
  # 代码风格与 Lint 检查
  ruff check .
  # 自动格式化与 import 排序修复
  ruff check . --fix
  ```
### 3.3 智能体工具调用与开发工作守则

为保障代码修改的准确性、交互效率并最大化利用大上下文模型能力，智能体与开发者必须严格遵循以下高优先级操作准则：

1. **优先读取完整文件，严禁滥用范围截断与 raw（Full Reads Only）**：
   - 查看代码、测试及文档文件时，**一律使用完整的相对文件路径读取完整文件**。
   - **严禁无端添加行号范围选择器（如 `:1-50`）或 `:raw`**，避免局部切片截断导致上下文断裂、误判函数边界，或由于行号漂移造成后续 `edit` 命中失败被拦截。
2. **批量并发读取（Batch Parallel Reads）**：
   - 涉及多个独立文件、测试文件或模块的调研比对时，在单个响应轮次中同时并发发起多个 `read` 调用。
   - 避免逐个串行读取导致多轮调用消耗上下文与拖慢交互。
3. **严禁滥用 `eval` 执行文件查看（No Eval Abuse）**：
   - 查看文件内容、定位符号与阅读代码必须使用专业的 `read` / `grep` 工具，**严禁使用 `eval` 编写 Python 文件读取脚本来替代 `read`**。
   - `eval` 仅限用于必要的复杂内存原型运算、即时逻辑求值或重现动态执行结果，非必要绝不滥用。
4. **统一强制使用 `uv` 工具链（Enforce uv）**：
   - 执行 Python 脚本、测试套件（`uv run pytest`）、代码检查（`uv run ruff check .`）与包同步（`uv sync`）**一律通过 `uv` 调用**。
   - 严禁调用系统裸 `python`、`python3` 或 `pip`，确保所有依赖与环境均在受控的虚拟环境内运行。
5. **任务清单 (todo) 保持敏捷与批量操作**：
   - 待办事项完成时，**允许并鼓励一次性全部批量划掉**，严禁机械地一轮交互只标记一个任务。
   - 专注推动实质性工程进展，避免频繁产生仅包含待办状态翻转的无效上下文。
6. **精炼克制（No Bloat）**：
   - 方案设计、代码修改与文档更新直击痛点。
   - 避免引入形式主义的胶水层、过度抽象与冗余样板代码，保持代码与文档紧凑精炼。
7. **交付前全量审查完整 diff（Review Full Diff）**：
   - 每次提交或报告完成前，**必须完整查看整个 `git diff`（不带行数折叠与省略）**，逐行确认所有改动准确无误、没有残留调试代码或意外破损。
   - 确保 `uv run ruff check .`、`bun x pyright` 和 `uv run pytest` 全部零报错通过后再行提交。
8. **微创编辑优先与 Diff 审计守则（Prefer Edit & Mandatory Diff Inspection）**：
   - 局部功能调整或代码重构**一律优先使用精准微创的 `edit`**，避免全文件重写引入非预期的代码格式漂移与无关变动。
   - 仅在新建文件、大型嵌入脚本提取（`src/.../js/*.js`）或结构彻底颠覆时使用 `write`。**使用 `write` 进行文件覆盖后，必须在第一时间执行 `git diff` 进行全量逐行审计**，确保无意外功能遗漏、截断或回退。
9. **零 Shim 容忍与干净重构（Clean Cutover Refactoring）**：
   - 重构内部函数或类方法签名时，必须实施**一步到位的原子切换（Atomic Cutover）**。
   - **严禁保留 `@deprecated` 向下兼容存根、无意义的历史 shim 别名包装或 `_unused_*` 废弃形参**；全库一次性完整迁移所有调用方和对应测试断言。

### 3.4 自动化检查命令与执行守则

在提交代码改动前，必须确保以下工具链检查通过：

| 工具 / 阶段 | 命令 | 判定标准 |
|---|---|---|
| **Python 代码风格与 Lint** | `uv run ruff check .` | 0 errors |
| **Python 代码格式化** | `uv run ruff format --check .` | 72 files already formatted |
| **Python 类型检查** | `bun x pyright src tests` | 0 errors |
| **Python 单元测试** | `uv run pytest` | 全部通过（Windows 本地基线：**567 passed / 7 failed**，均为既有的环境类失败：`test_browser_engine`×1~2（随环境闪动）、`test_browser_scripts_syntax`×1、`test_response_parser`×3、`test_wire_codec`×2；CI Linux 上不存在） |
| **浏览器 JS 语法校验** | `for f in src/aistudio_api/infrastructure/browser/js/*.js; do bun build "$f" --no-bundle >/dev/null; done` | 0 errors |
| **前端代码规范** | `cd web && bun run lint` | 0 errors, 0 warnings |
| **前端类型检查** | `cd web && bun run type-check` | 0 errors |
| **前端生产构建** | `cd web && bun run build` | 构建成功并更新 static 产物 |
> [!NOTE]
> **跨平台原生二进制依赖与开发工具**：
> Windows-only 后这条只剩历史价值：`uv.lock` 仍在 `.gitignore`（无跨平台 wheel 源冲突需要锁定了，
> 想锁定也可以移回来）；`ruff` 在 dev 依赖里的 `sys_platform != 'android'` 平台标记已无意义，
> 后续清理 `pyproject.toml` 时可一并去掉。

> [!TIP]
> **避免无效重跑**：若在当前交互轮次中未发生代码或配置文件的实质性变动，无需重复执行全量类型与测试套件检查。

## 4. 并发安全与多账号守则

> [!IMPORTANT]
> 1. **原子化文件持久化**：所有持久化文件（`registry.json`、`auth.json`、`meta.json` 等）操作必须使用 `account_store._atomic_write_json`（写入临时文件后通过 `os.replace` 原子替换），防止并发写入截断。
2. **蓝绿会话平滑热切与就绪门机制**：切换账号时，`BrowserSession` 在后台创建独立新标签页，注入 Cookie、完成页面导航与 BotGuard 握手验证；验证通过后原子翻转 `SessionReadinessGate` 并将新上下文设为就绪。旧会话上下文加入退休队列，优雅等待在途流式请求排干（`decr_in_flight()` 触发 `drain_event`）后再行关闭，保障流式请求平滑过渡零中断。
3. **缓存与模板隔离**：账号切换、429 限流或 403 鉴权重试时，调用 `client.clear_templates()`，避免跨账号复用请求模板。快照签名按每次请求哈希实时生成，绝不跨请求共享。初次握手截获基线模板后，任意目标模型通过 `capture_template(model)` 纯内存动态适配参数。
4. **鉴权/服务端/传输故障：一律「记账 + 重启浏览器 + 真实错误返回」（零重试，别再退回去）**：403/401、页面 fetch/XHR 传输失败、会话挂死**都不是账号问题**——不写 `auth_cooldown`（不要调 `record_auth_error`），按浏览器故障处理：清模板 + 杀掉浏览器进程（下次请求 `ensure_ready` 重建）；本次请求按真实状态码返回（传输失败 502）。只有 429 驱动换号（第 5 次才冷却，见 §1）；只有 `SessionExpiredError`（真的被跳到登录页）按账号隔离 24h。
5. **无全局 DOM 污染**：页面内 JavaScript 交互使用局部闭包 `Promise` 返回数据，不在 `window` 对象上遗留全局共享状态。
6. **Cookie 智能精简与天然协商**：外部导入 Cookie 时自动过滤旧设备或跨 IP 绑定的易腐败凭据（`OSID`、`__Secure-OSID`、`SIDCC` 及 `_ga` 等追踪标记），保留核心认证项（`SID`、`SAPISID`、`1PSIDTS`）。浏览器访问 AI Studio 时自动协商出绑定当前网络/TLS 的全新有效 `OSID`，杜绝 403 权限拒绝。
7. **真实环境伪装与低内存协同**：保留 `--renderer-process-limit=1`、`--in-process-gpu` 与 128MB V8 内存限制控制内存占用；移除 `--disable-software-rasterizer` 并启用 `--use-gl=angle --use-angle=swiftshader` 恢复软件 WebGL 上下文，保留 `--mute-audio` 并移除 `--disable-audio` 保护 AudioContext；统一注入 `--fingerprint-platform=windows` 伪装为 Windows 桌面环境，并通过原生 `--fingerprint-timezone` 与 Wire 协议层保持时区/位置严格一致。
8. **账号接口解耦与 SPA 缓存隔离**：对外标准 REST 接口统一位于 `/api/accounts`（保持 `/accounts` 向下兼容），并在 SPA 单页应用与 API 响应中下发 `Vary: Accept` 与 `Cache-Control: no-store`，杜绝浏览器与代理网关发生缓存错乱。

## 5. 代码结构索引

```text
aistudio-api/
├── web/                           # Web 管理控制台 (Vite + Vue 3 + TS + Tailwind)
│   ├── src/
│   │   ├── api/                   # REST 请求封装 (auth, accounts, system, models)
│   │   ├── types/                 # TypeScript 类型契约
│   │   ├── stores/                # Pinia 状态管理
│   │   ├── components/            # UI 与业务解耦组件
│   │   ├── views/                 # 页面路由视图 (Dashboard, Accounts, Settings, Login)
│   │   └── router/                # Vue Router 路由与守卫
│   └── vite.config.ts             # 构建配置 (输出至 src/aistudio_api/static)
├── src/aistudio_api/
│   ├── api/                       # HTTP 接口层 (FastAPI)
│   │   ├── app.py                 # 应用生命周期与静态托管
│   │   ├── dependencies.py        # API Key 与 Token 鉴权注入
│   │   ├── routes_gemini.py       # /v1beta/models/*:generateContent 路由
│   │   ├── routes_openai.py       # OpenAI 兼容端口 (/v1/chat/completions, /v1/models)
│   │   ├── routes_models.py       # /v1beta/models 模型发现路由
│   │   ├── routes_accounts.py     # 账号管理与探活路由
│   │   ├── routes_system.py       # 监控指标、日志配置、系统配置与 /settings
│   │   ├── state.py               # 运行时全局单例容器 (RuntimeState)
│   │   ├── schemas.py             # API 请求与校验模型 (含 OpenAI 请求模型)
│   │   ├── responses.py           # Gemini API 响应格式化辅助工具
│   │   └── response_models.py     # API Pydantic 响应模型
│   ├── application/               # 业务应用层
│   │   ├── account_rotator.py     # 黏性账号调度器 (按模型记录 429 与太平洋午夜重置)
│   │   ├── account_service.py     # 账号存储与激活上下文协调
│   │   ├── api_service_gemini.py  # 请求生命周期 (normalize→overrides→capture→replay→parse)
│   │   ├── openai_bridge.py       # OpenAI ↔ 内部 Gemini 请求/响应转换 + 流式转换器
│   │   ├── overrides.py           # 参数覆写（thinking 档位收敛 / 思考回传 / max_tokens / 采样）
│   │   ├── runtime_settings.py    # 运行期设置（schema + data/settings.json 持久化）
│   │   ├── bypass.py              # 工具调用抗截断：合成工具、指令注入、正文还原、流式累积折叠
│   │   ├── prefill.py             # 预填充兼容：续写指令、COT 闭合守卫、响应拼回与流式去重
│   │   ├── stream_heartbeat.py    # 流式 SSE 心跳（1s；OpenAI 空 delta / Gemini `: ping`）
│   │   ├── account_orchestrator.py# _switch_lock 防雪崩切号与故障转移编排
│   │   └── chat_service.py        # 消息规范化与多模态数据处理
│   ├── domain/                    # 纯净领域模型与异常体系
│   │   ├── errors.py              # 业务异常定义 (AuthError, UsageLimitExceeded 等)
│   │   └── models.py              # 领域数据结构 (Candidate, ModelOutput 等)
│   ├── infrastructure/            # 基础设施层
│   │   ├── account/               # Cookie 解析、会话刷新与凭据持久化 (account_store, cookie_parser)
│   │   ├── browser/               # 异步 CDP 客户端与 CloakBrowser 进程管理 (cdp_client, browser_engine)
│   │   └── gateway/               # 会话状态机、蓝绿就绪门、鉴权凭据、WAA 引导 (botguard_bootstrap)、
│   │                              # Wire 编解码与流式管道、签名哨兵 (signatures.py)、
│   │                              # 单浏览器 + 账号池（client.py / session.py 蓝绿热切）
├── config.yaml                    # 模型规则与工具默认行为配置（手工编辑，重启生效）
├── main.py                        # 本地统一启动入口
└── tests/                        # 单元测试套件 (模块化轻量架构，全量通过 <5s)
│   ├── conftest.py               # 气密性隔离配置 (monkeypatch AISTUDIO_* 数据目录)
│   ├── fixtures/                 # 真实 Protobuf JSON 请求与响应报文
│       └── unit/                     # 30+ 个按职责严格划分的单元测试模块（含 bypass/prefill/signatures/overrides/openai_bridge/runtime_settings）
│       ├── test_account_*        # 账号调度、并发轮换防雪崩与凭据导入
│       ├── test_api_*            # API 鉴权、响应序列化与模型/系统路由
│       ├── test_browser_*        # Chromium 进程看门狗、CDP 会话与页面生命周期
│       └── test_wire_*           # Protobuf-over-JSON 编解码、流式解析与模型规则
```

## 6. 交接（2026-10-20）：当前状态、证据与遗留问题

> 这一节是给下一个接手的 agent 的。先读完再动代码；用户要的是「稳定的 2API 网关」，
> 讨厌看门狗、讨厌无依据的猜测，改完必须能说清楚证据。

### 6.1 本轮已完成（代码 + 真机验证）

| 改动 | 文件 | 验证 |
|---|---|---|
| binding 引导不再依赖 `dms.TB`：包 `default_MakerSuite.n`，按形状（`service.A.snapshot` 是函数）接服务 | `infrastructure/browser/js/install_hooks.js` | 空 profile 真机 binding 引导 **4.2s**（`capture_via=inject(shape)`） |
| 连续 3 次 `service_unavailable` 提前回退 page_send（`SERVICE_UNAVAILABLE_BAIL`），不再白等 35s × worker 数 | `infrastructure/gateway/botguard_bootstrap.py` | 新增单测；日志「binding 探针连续 3 次…提前回退」 |
| 备用 worker 判定包含空闲休眠（STOPPED）：`_standby_worker()`，日志写 `切备用=#N（空闲休眠，将拉起）` | `infrastructure/gateway/client_pool.py` | 新增单测 `test_standby_includes_idle_reclaimed_worker` |
| 长静默 WARN 带 request id；bypass 时附「整段返回属预期」说明 | ~~`api/routes_openai.py`、`api/service_gemini.py`~~ **已于 2026-10-20 第三轮按用户要求移除**（几百秒请求刷屏）；判据改为完成日志「首包/总时长」 | real curl 复测（bypass 开 5.4s/5.5s 1 块；关 3.1s/7.8s 19 块） |
| **（第二轮）auth.json / meta.json 原子写**：`save_browser_cookies` 与身份探测回写全改 `atomic_write_json`，两台 worker 共用账号时不再可能读到半截 JSON | `infrastructure/gateway/session_auth.py` | 定向测试通过（改动机械） |
| **（第二轮）池模式账号重绑**：`ClientPool.activate_account` / `redistribute_accounts`；WebUI 手动切换/删除/probe-import 同步所有 worker | `infrastructure/gateway/client_pool.py`、`api/routes_accounts.py` | 新增单测 ×5（池重绑 / 未知账号 / 删除重排 / 路由 pool 分支 ×2） |
| **（第二轮）传输失败网络层证据**：`$rpc/` 请求记 `loadingFailed`（errorText/canceled/blocked/resp），`Failed to fetch` 时 WARN 归因 | `infrastructure/gateway/transport.py`、`infrastructure/browser/cdp_client.py` | 新增单测 `test_replay_failure_logs_cdp_net_layer_evidence` |
| **（第三轮）重试耗尽不再静默空流/谎报 429**：`stream_raw_events` 抛出最后的错误渲染错误帧；非流式兜底按真实错误分类 | `application/api_service_gemini.py` | 全量 567/7 |
| **（第三轮）debug+精简**：删死代码 `get_next_account_with_stats`（50 行）、`AIStudioClient.replay/_captured`；status_map 提常量、page_send 收尾去重、clamp_pool_size 复用 | `account_rotator.py`、`client.py`、`botguard_bootstrap.py`、`app.py` 等 | 全量 567/7，净 -50 行 |
| **（第三轮）独立逆向复核后修复 digest**：`generate_snapshot` 剔除 executableCode/codeExecutionResult（页面 `_.Tm` 只算 text/inline/fileId，多算是 403[7] 诱因）；`wire_spec` 语义标注修正（14=responseModalities 等） | `infrastructure/gateway/session.py`、`wire_spec.py` | 新增 `test_generate_snapshot_digest_ignores_code_parts`；完整报告在 `逆向相关内容/re-verification-2026-10-20.md`（本地，不入库） |
| **（第四轮，2026-10-20）网关零自动重试**：用户拍板移除全部当次请求重试（切备用 worker / 单浏览器原地重试 / 切号重试 / 204 与 Ambiguous 404 抖动重试、`MAX_RETRIES`、备用判定 `_standby_worker`）；自愈仅剩「特定故障后台重启浏览器」（403/401、5xx、传输失败、会话挂死、登录页重定向），429 只记账不重启 | `application/api_service_gemini.py`、`infrastructure/gateway/client_pool.py`、`application/account_orchestrator.py` + 5 个测试文件 | 全量 **563 passed / 7 failed**（7 条全是 §6.3.7 环境类既有失败）；ruff 0 errors；pyright 86 = 基线持平 0 新增 |
| **（第五轮，2026-10-20）单浏览器 + 429 冷宫规则 + 一手证据修 bug**：① 移除多浏览器 worker 池（`client_pool.py` / `AISTUDIO_BROWSER_POOL_SIZE` / `clamp_pool_size` / 池分支路由全删），账号池保留（蓝绿热切）；② 只有 429 换号，`RATE_LIMITED_COOLDOWN_THRESHOLD=5`（第 5 次 429 才冷却到午夜，成功清零计数；drip 模式删除）；③ 常驻逆向 agent 一手复核（`逆向相关内容/independent-re-2026-10-20.md`）修了 3 个真 bug：seed 槽位（genConfig idx18，此前两端口都静默丢 seed）、finishReason 5/6 互换与 15/16/17 错位 + 补 18=MISSING_THOUGHT_SIGNATURE、idx11 语义更正（会话 token `v1_…`，非 cachedContent）；④ 硬编码 `gemini-3.7-flash` → `gemini-3.8-flash`；⑤ 流式预填充泄漏修复（非 200 不吐 prefill） | `app.py`、`routes_accounts.py`、`account_rotator.py`、`wire_spec/types/codec`、`schemas.py`、`chat_service.py`、`openai_bridge.py`、`streaming.py`、web 前端 | 全量 **533 passed / 7 failed**（基线不变）；ruff 0；pyright 78（比基线 86 少 8，0 新增）；web lint/type-check/build 全过 |
| 文档：binding 新构建、bypass 整段返回、池 standby 语义 | `README.md`、`AGENT.md`、`docs/BOTGUARD_VERIFICATION_CHAIN.md` | — |
| **（第六轮，2026-10-20）Windows-only 裁剪 + 全量 review 收尾**：① 移除 Termux/Linux/Docker/macOS/android（`Dockerfile`、compose×2、`scripts/cloakbrowser_termux|setup-*.sh`、`start.sh`、`android/`、docker workflow；CI backend 切 `windows-latest`）；② `browser_engine.py` 砍掉 proot/Darwin/POSIX 看门狗（~200 行）；③ 修 7 条「环境失败」真 bug——测试读 fixture/子进程输出没写 `encoding="utf-8"`（GBK 崩），全量 **533 passed / 0 failed**；④ 孤儿清理：`abort_stream`、`transport.close`、`is_transient_rpc_error`、`auth_error` 写入链（`record_auth_error`/`try_switch_account` 的 `is_auth_error`/`is_session_expired` 参数、`get_next_account` 同名参数）；⑤ 继续全量 review（botguard_bootstrap/replay/session_state/signatures/stream_parser/wire_parser/transport/capture/streaming 已审，未发现新 bug） | 全仓多处 | 533/0；ruff 0；pyright 78 持平 |
| **（第七轮，2026-10-20）全量 review 收官**：剩余文件全部过目——`cdp_client`、`browser/scripts`、`session_auth`、`account_store`、`cookie_parser`、`account_service`、`routes_system·models`、`dependencies`、`chat_service`、`overrides`、`runtime_settings`、`stream_heartbeat`、`model_defaults`、`model_discovery`、`responses`、`utils`、`wire_codec` 全文、`streaming_init/hooked_request/snapshot_generate/stream_cleanup` 等核心 JS。修复：`_FALLBACK_MODELS` 重复 id（3.7→3.8 替换引入的真重复）、`model_discovery` 的 `for _ in [0]` 费解过滤写法；删除 5 个零调用的 `runtime_settings` 语义 helper。确认 `stream_cleanup.js` 的页面侧 abort 注册表有真实调用方（非孤儿）。未审仅剩 web/src 前端视图与 tools/reverse（低风险） | 全仓多处 | 533/0；ruff 0；pyright 78 持平 |


上一轮（同一会话）已完成、已被第五轮部分取代：~~2 台 worker 池~~（已移除）、
403/401/5xx/502/transport 按 worker 故障重启（现为单浏览器杀进程）、429 保留配额冷却
（现收紧为第 5 次才冷却）、SessionExpired 隔离 24h、`start.cmd` 前台 Ctrl+C、
终端 LLM 日志、streaming JSON 修复、OpenAI 兼容端口、bypass/预填充/签名哨兵。

### 6.2 关键真机证据（复现方法见 6.5）

1. **bypass 请求是整段返回，不是网关攒包**（隔离探针，独立浏览器 + 独立 data dir）：
   - bypass **开**：`status=200` 与首个 body 都在 **7.08s**，正文 **1 块 438 字**，总 7.11s；
   - bypass **关**：`status=200` 1.83s、首块 1.83s，正文 **19 块**、块间隔约 0.3s，总 7.95s。
   → 同一通道普通请求是真流式；bypass 的 functionCall 参数不分片，上游第一帧=整段工具调用。
2. **用户现场那发请求（60 条消息 / 333KB、argon）**：心跳 1s 一拍共 8 条、`已发` 全是心跳；
   522s 时页面 `fetch` 抛 `Failed to fetch` → 502。主项目同通道记录：TTFT 462s / 576s 成功，
   另有一次 183s `Failed to fetch`；express 同模型 25~35s、65s 后 429。
3. **curl（运行中的 8080 网关）**：HTTP 头立即返回；05:44:26.04 / 27.04 / 28.04 三拍心跳；
   05:44:28.20~28.24 正文+usage+`[DONE]` 一次性到（5 个 TCP 读）。== bypass 的「非流式」体感是真实的。==
4. **「切备用=否」是误判（已修）**：当时另一台 worker 空闲 30 分钟被回收（STOPPED），
   `_has_standby` 只认 READY → 不重试直接 502；现在会写 `切备用=#N（空闲休眠，将拉起）` 并重试。

### 6.3 未解决 / 需要下一个 agent 决定的事

1. **`Failed to fetch` 在长请求（数百秒）后的根因未定**：是 Google 前置/代理/Chrome 在长时间
   无字节时断连，还是 BotGuard proof 时效，尚未证明。**已埋好证据采集（2026-10-20 第二轮）**：
   `transport.py` 的 `_ensure_net_trace` 给每个页面订阅 `Network.requestWillBeSent` / `responseReceived` /
   `loadingFailed`（只记 `$rpc/` 请求，监听在 `init_domains` 的 `Network.enable` 上，零额外开销），
   传输失败时 `_log_rpc_net_evidence` 打 WARN：`err=net::ERR_xxx canceled=… blocked=… resp=none|200 age=…`。
   **等下一次真实复现看这行日志即可归因**（ERR_CONNECTION_RESET/EMPTY_RESPONSE=上游断；
   canceled=true=页面 JS 自己中止=我方代码问题）；不要再起隔离实例压测 argon。无记录本身也是线索
   （= 页面 JS 层中止）。
2. ~~bypass 长请求要不要给客户端「正在生成」信号~~ **用户已拍板（2026-10-20）：不加，维持只有
   1s 心跳**（原话「那就等着」）。同时按用户要求删除了周期性「上游 Ns 无任何数据」WARN
   （`routes_openai.py` / `api_service_gemini.py`，理由：单请求可能连跑几百秒，WARN 只会刷屏）。
   bypass 期间**原理上无法**区分「正在生成」与「无响应」：上游完成前 0 字节（连响应头都不来，
   已实测），网关侧任何「进度」信号都只能是自造的非标准事件。
3. ~~超长请求失败后的重试窗口~~ **已拍板（2026-10-20 第四轮）：网关不做任何自动重试**——
   A（切备用重试）/ B（快速失败）都不选，自愈仅限「特定问题 → 后台重启无头浏览器」，
   当次请求按真实错误返回，重发与否由客户端决定。相关代码已全部移除（见 §6.1 第四轮）。
4. ~~单账号时两台 worker 共用 cookie~~ **已修（2026-10-20 第二轮）**：`save_browser_cookies` 与
   `verify_account_identity` 的 meta.json 写入原来**并不是**原子写（交接文档说错了）——直接
   `write_text` 会让另一台 worker / 传输层兜底读到半截 JSON 误判 Cookie 失效。现全部改走
   `atomic_write_json`（temp+os.replace）。后写覆盖先写仍是预期行为（两台都是活会话），接受。
5. ~~WebUI 手动「切换账号」只影响主 worker~~ **已修（2026-10-20 第二轮）**：`ClientPool.activate_account`
   重绑**所有** worker（READY 蓝绿热切、休眠的改绑定待拉起）；删除账号走 `redistribute_accounts`
   把仍指向已删账号的 worker 换到剩余账号；`probe-import` 首个账号激活同样走池。
   绑定是手动的、不自动回落；`detect-email` 的浏览器兜底切号在池模式直接跳过（HTTP 探测已覆盖）。
6. **真实上游 403/500/502 自愈**只做过单测 + 真机 kill Chromium 冒烟（同请求切备用 200）；
   没等到真实上游 403 现场。遇到时留意日志里的 `worker #N 故障 (auth/http_403…)`。
7. **全量 pytest 的 Windows 既有失败**（`test_browser_engine`、`test_browser_scripts_syntax`×1、
   `test_response_parser`×3、`test_wire_codec`×2）是环境/基线问题，不是回归；别去「修」成通过
   （browser_engine 有一条会随环境状态闪通/闪挂，2026-10-20 第二轮为 1 failed 而非 2）。
8. **仓库状态**：改动已提交并推送到 `origin`（`559889a/sister-aistudio`）。
   不要再生成/依赖补丁文件；要看差异直接 `git diff upstream/master..origin/master`。
9. 全量 pytest（2026-10-20 第三轮）：**568 passed / 7 failed**（7 条全是 §6.3.7 的环境类既有
   失败）；`ruff check src tests` 0 errors；pyright 与改前基线持平（本机 86 条既有，0 新增）。
10. **独立逆向复核的遗留发现**（完整报告：`逆向相关内容/re-verification-2026-10-20.md`，
    本地不入库）：snapKey「首匹配」启发式是最脆一环（建议升级为试签验证后采用）；
    candidates 只取 `[0]`（多 candidate 会静默丢）；binding 请求缺 F12/F14 字段（指纹差，
    当前被接受）；usage[6] 未解析；WIRE_SPEC.md §7.1 示例拼造、§7.4 hCb 说法无据；
    EVERGREEN=7 与 penalties/logprobs 槽位无当前构建证据；BOTGUARD 文档 API key 陈旧
    （key 不承担鉴权，风险低）。这些都**未修**，动前先读报告。

### 6.4 环境、命令与坑

```powershell
# 项目根（唯一副本；没有别的 clone/venv 目录）
cd C:\sister-aistudio
$env:PYTHONPATH = 'C:\sister-aistudio\src'
$env:PYTHONIOENCODING = 'utf-8'

# 全量测试（Windows 基线：560 passed / 8 failed）
& 'C:\sister-aistudio\.venv\Scripts\python.exe' -m pytest tests -q

# ruff
& 'C:\sister-aistudio\.venv\Scripts\ruff.exe' check src tests

# JS 语法
node --check src\aistudio_api\infrastructure\browser\js\install_hooks.js

# 启动/停止：start.cmd 前台，Ctrl+C
.\start.cmd
```

- `.venv` 是项目自己的虚拟环境（Python 3.14，`uv` 创建；`start.cmd` 用的就是它）。测试依赖
  `pytest` / `pytest-asyncio` / `ruff` / `google-genai` 已装好；缺东西就
  `uv pip install --python .venv\Scripts\python.exe <pkg>`。
- 运行期数据：`data/settings.json`（WebUI/`PUT /settings`，立即生效）、`data/accounts/<id>/auth.json`、
  `data/accounts/<id>/profile-wN`（worker profile，**同一 auth_file 不许共用**）。
- 单浏览器：CDP `9222`；**不要在服务运行时再起第二个网关碰这个端口**。做隔离实验时用
  `AISTUDIO_DATA_DIR=<临时目录>` + `AISTUDIO_BROWSER_PORT=95xx`。
- 主项目 `C:\build2api` 在 **8889** 跑着（用户自己的），**不要动它**；只读对比其
  `/__b2a/requests`（`Authorization: Bearer sk-123`）可以看到同通道的真实首包/总时长。
- 额度：`gemini-4-argon` 日桶很小，**别拿它压测**；诊断用 `gemini-3.8-flash`。
- **改完代码要重启 `start.cmd` 才生效**：用户 2026-10-20 05:5x 时 8080 上还跑着改前的进程；
  停服务只按用户的方式（Ctrl+C），别去 kill 进程号或动 9222/9223 的 Chromium。

### 6.5 诊断脚本（已随临时目录删除）

排查时用的是项目外的临时目录 `C:\sister-aistudio-local`（agent 工作台：3.12 测试 venv + 一次性脚本 +
日志 + 补丁），**2026-10-20 已整体删除**；项目里现在只有 `C:\sister-aistudio` 一个目录，测试统一用
项目 `.venv`。要复现当时的实验，按 6.2 的结论重写等价脚本即可，关键做法：

| 实验 | 做法 |
|---|---|
| 页面 DI 探针状态 | 起一台隔离浏览器（独立 `AISTUDIO_DATA_DIR` + `AISTUDIO_BROWSER_PORT=95xx`），装 document-start hooks 后每秒读 `window.__AISTUDIO__` 的 `service/captureVia/snapKey`，只读不发请求 |
| bypass 开/关对照 | 同一隔离实例里，用 `client.stream_generate_content` 直接打一发 flash；bypass 开时 `status` 与正文同时到、正文 1 块；关掉后 19 块 / 0.3s 间隔 |
| 网关 SSE 逐块计时 | `curl.exe -N --trace-time --trace-ascii <log>` 打运行中的 `/v1/chat/completions`，看心跳与正文到齐的时间 |
| 300KB binding 是否冻页面 | 页面里原生 setter 写 300KB 进 `ms-prompt-box textarea`，随后每 250ms 测一次 `Runtime.evaluate` 往返；实测 RTT 3~65ms，无冻结 |

### 6.6 一句话结论（可以直接转述给用户）

- `binding 引导未通过…回退 page_send` = **真 bug（已修）**：新页面构建不再导出 `dms.TB`，DI 探针装不上。
- `上游 Ns 无任何数据（响应头都没收到）` = **不是 bug**：bypass 的 functionCall 上游整段返回，
  出第一帧前没有任何字节（也就没有响应头）；心跳一直在发。这条周期 WARN 已按用户要求移除
  （2026-10-20），别加回来；判据用完成日志的「首包 X s / 总 Y s」。
- `Failed to fetch` → 502 = **不是误判**：页面 fetch 真的被上游/网络断开（主项目同通道也有）。
  2026-10-20 第四轮起网关**零自动重试**：传输失败只做「后台重启那台浏览器 + 本次请求 502」，
  不再切备用重打。失败时会跟一条 `CDP 网络层证据 … err=net::ERR_xxx` 的 WARN，直接拿它归因。
- WebUI 手动切号后「一半请求还在旧账号」= **真 bug（已修，2026-10-20 第二轮）**：池模式现在
  `activate_account` 重绑所有 worker；之前只切主 worker。
- auth.json 偶发读成半截 JSON → 误判 Cookie 失效 = **真隐患（已修）**：cookie/meta 回写改成原子替换了。
