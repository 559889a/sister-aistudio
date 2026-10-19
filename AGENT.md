# 智能体协作与开发规范 (AGENT.md)

本文档面向所有参与本项目的代码代理（Coding Agents）与开发者，明确核心架构共识、运行环境约束与代码规范。

---

## 目录

- [1. 核心架构与设计原则](#1-核心架构与设计原则)
- [2. 运行时与环境约束 (Termux)](#2-运行时与环境约束-termux)
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
- **浏览器 worker 池：worker 隔离、单请求绑定、失败切另一台（别退回单浏览器语义）**：`AISTUDIO_BROWSER_POOL_SIZE>1` 时由 `infrastructure/gateway/client_pool.py` 管 N 台 Chromium——第 N 台 CDP 端口 = `browser_port+N`、profile 目录按 `-wN` 后缀隔离（同一 auth_file 也不许共用目录）；一个请求由 `pick()` 绑定到一台 worker（ContextVar `current_worker/current_account_id`），**中途不换 worker**。403/401、5xx、502、页面传输失败都按 **worker 故障**处理：后台 kill+重启那台，未吐字节时本次请求立刻切另一台重试；这些错误**不给账号加冷却**（只有 429 保留配额冷却、SessionExpiredError 才按账号隔离）。单浏览器路径（`pool_size=1`）保持旧行为，两种模式别混用判据。
- **失败切换的「备用」包含空闲休眠（STOPPED）worker**：`_standby_worker()` 认 `READY` + `STOPPED`（`pick()` 会把它重新拉起，20~40s），日志写 `切备用=#N（空闲休眠，将拉起）`。**别改回只认 READY**：2026-10-20 现场就是另一台空闲 30 分钟被回收，8 分钟长请求失败时日志写「切备用=否」、请求直接 502 —— 其实那台可以拉起来再战。
- **bypass 请求的上游是「整段返回」，不是网关丢事件**：bypass 会注入合成 functionCall，Gemini 不分片下发函数参数，**上游第一帧就是整段工具调用**，在此之前连 HTTP 响应头都不到。所以流式日志里的 `上游 Ns 无任何数据（响应头都没收到…）` 在 bypass 下是**预期**（已自动附说明）；`已发 N B` 全是 1s 心跳帧、正文 0 字节。**不要因此加看门狗、提前 502、或伪造分块**——普通请求（bypass 关）在同一通道实测是逐块流式的（19 块 / 0.3s），可以拿它区分「通道坏了」还是「bypass 整段返回」。
- Flake：页面 `fetch` 在长等待后抛 `Failed to fetch` 是真实的网络/上游断开（`BrowserTransportError`），不是本项目的 abort（`abort_stream` 目前没有任何调用点）；按 worker 故障重启 + 有备用就切，没备用才 502。主项目（`C:\build2api`）同通道记录里也有一次 183s 的 `Failed to fetch`。
- **架构事实优先（Fact & Architecture over Brittle Tests）**：保持架构整洁与低耦合。避免为了迁就耦合了私有变量的脆弱 mock 测试而妥协生产设计；遇冲突时应重构测试以对齐公共接口契约，消除抽象泄露。
- **协议索引常量化（Specification Constants & No Magic Numbers）**：Protobuf-over-JSON 逆向数组索引统一提取为具备明确语义的规范枚举或常量（如 `wire_spec.py`），杜绝裸数字散落业务逻辑中。

## 2. 运行时与环境约束 (Termux)

Python 工具链运行在 Termux 宿主（通过 `uv` 管理虚拟环境）；Chromium（CloakBrowser）由于 glibc/bionic ABI 差异，运行在 `proot-distro` Linux 容器中。CDP 通过内置的 `cdp_client.py` 经由 WebSocket 直连。

### 2.1 环境准备与启动流程

```bash
# 1. 宿主依赖环境配置与同步
bash scripts/setup-env.sh
uv sync

# 2. 准备受控浏览器运行时（仅首次执行）
bash scripts/setup-browser.sh

# 3. 启动服务
uv run python3 main.py server --port 8080
```

> [!NOTE]
> - Windows 下用仓库根目录的 `start.cmd`：前台运行（选/建 venv → 装依赖 → 起服务），日志直接打在终端，**Ctrl+C 即停**；`start.sh` 保留给 Linux / macOS / Termux / Git Bash。两者行为一致。
> - 终端日志只保留 LLM 请求/上游重试/错误（健康检查、面板轮询、uvicorn access、httpx 逐请求都静音）；日志格式是 `[INFO] 2026-10-19T10:54:15.499Z [aistudio-api] …`，请求带短 id（`→` / `← 完成` / `请求失败` / `客户端断开`）。要协议细节把 `logging.level` 设 DEBUG。

> [!NOTE]
> - `uv` 会解析 `uv.lock` 中的跨平台镜像索引与预编译 wheel，避免在 Termux 环境因编译 Rust 消耗过多内存导致 OOM。
> - `scripts/cloakbrowser_termux/run-chrome.sh` 是 Termux 下的 Chromium 包装入口，内部调用 `proot-distro login aistudio-api -- /opt/cloakbrowser/chrome "$@"` 并自动绑定路径。

### 2.2 前端构建与管理

- 前端位于 `web/` 目录，基于 Vite + Vue 3 + TypeScript + Tailwind CSS。
- 依赖管理与构建使用 `bun`（`bun run dev`、`bun run build`、`bun run lint`、`bun run type-check`）。
- 生产构建产物直接输出至 `src/aistudio_api/static/`，由 FastAPI 静态托管。

### 2.3 Android 伴侣端 (android/)

- 源码位于 `android/` 目录，基于 Jetpack Compose + Material 3 构建，用于移动端多账号 Cookie 提取。
- 包含多 Profile 隔离、按需启动 WebView（不常驻内存）以及 Bitwarden 自动填充适配。
- 代码规范检查使用 `ktlint`，规则配置于 `android/.editorconfig`。
- 检查命令：`ktlint "android/app/src/main/java/**/*.kt"`。

### 2.4 常见排错

| 现象 | 原因 | 处理方案 |
|---|---|---|
| `[Errno 98] address already in use` | 端口被占用或上一次服务未完全退出 | 执行 `pkill -f 'main.py server'` 后重试，或指定 `--port <other_port>` |
| `Chromium CDP endpoint on port N not ready` | proot 容器未启动或缺少 apt 依赖 | 重新运行 `bash scripts/setup-browser.sh` |
| `proot-distro login: container 'aistudio-api' is missing` | 容器尚未创建或被改名 | 运行安装脚本创建容器，或通过 `AISTUDIO_PROOT_NAME` 指定现有容器 |
| `pydantic-core` 报 `GLIBC_X.Y not found` | 使用了系统 pip 安装而非 `uv` | 清理 `.venv/` 后重新执行 `uv sync` |

### 2.5 临时文件与目录规范 (Termux)

- **避免硬编码 `/tmp`**：Android Termux 环境中不存在系统级 `/tmp` 目录。在代码、测试、脚本或临时分析中严禁硬编码 `/tmp/`，否则会引发 `FileNotFoundError: [Errno 2] No such file or directory: '/tmp'`。
- **统一使用 `tempfile` / `$TMPDIR`**：所有临时目录或文件操作必须通过 `tempfile.gettempdir()` 或 `tempfile.mkdtemp()` 创建（自动安全解析至 `$TMPDIR` 即 `/data/data/com.termux/files/usr/tmp`），并在操作完成后通过 `try...finally` 与 `shutil.rmtree` 及时清理。
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
| **Python 单元测试** | `uv run pytest` | 全部通过（Windows 本地基线：**555 passed / 8 failed**，8 条都是既有的环境类失败：`test_browser_engine`×2、`test_browser_scripts_syntax`×1、`test_response_parser`×3、`test_wire_codec`×2；CI Linux 上不存在） |
| **浏览器 JS 语法校验** | `for f in src/aistudio_api/infrastructure/browser/js/*.js; do bun build "$f" --no-bundle >/dev/null; done` | 0 errors |
| **前端代码规范** | `cd web && bun run lint` | 0 errors, 0 warnings |
| **前端类型检查** | `cd web && bun run type-check` | 0 errors |
| **前端生产构建** | `cd web && bun run build` | 构建成功并更新 static 产物 |
| **Android 静态检查 (Lint & 代码规范)** | `cd android && ./gradlew :app:lintDebug :app:ktlintCheck` 或 `ktlint "android/app/src/main/java/**/*.kt"` | 0 errors |
> [!NOTE]
> **跨平台原生二进制依赖与开发工具**：
> - `pydantic-core` 等依赖在 PyPI 上无预编译 Android wheel，而 Termux 的 TUR 源无桌面端 wheel。因此 `uv.lock` 纳入 `.gitignore`：桌面端直接从 PyPI 安装对应 wheel；Termux 端通过 `setup-env.sh` 生成本地 `uv.toml` 使用 TUR 源，避免不同平台的 wheel 源锁定冲突，各平台均直接使用二进制 wheel 避免源码编译。
> - `ruff` 作为开发阶段的 Lint 工具，在 `pyproject.toml` 的 dev 依赖中配置了平台标记 `ruff>=0.8.0; sys_platform != 'android'`；桌面平台（Linux / macOS / Windows）执行 `uv sync --extra dev` 时直接自 PyPI 下载预编译 wheel。而在 Android Termux 环境下，TUR 并未打包 ruff 的 PyPI wheel，开发者可通过 Termux 原生包管理器 `pkg install -y ruff` 直接获得编译好的 aarch64 native 二进制，`setup-env.sh` / `setup-browser.sh` 会自动建立 `.venv/bin/ruff` 软链以便在虚拟环境中直接调用，普通用户生产运行无需安装。

> [!TIP]
> **避免无效重跑**：若在当前交互轮次中未发生代码或配置文件的实质性变动，无需重复执行全量类型与测试套件检查。

## 4. 并发安全与多账号守则

> [!IMPORTANT]
> 1. **原子化文件持久化**：所有持久化文件（`registry.json`、`auth.json`、`meta.json` 等）操作必须使用 `account_store._atomic_write_json`（写入临时文件后通过 `os.replace` 原子替换），防止并发写入截断。
2. **蓝绿会话平滑热切与就绪门机制**：切换账号时，`BrowserSession` 在后台创建独立新标签页，注入 Cookie、完成页面导航与 BotGuard 握手验证；验证通过后原子翻转 `SessionReadinessGate` 并将新上下文设为就绪。旧会话上下文加入退休队列，优雅等待在途流式请求排干（`decr_in_flight()` 触发 `drain_event`）后再行关闭，保障流式请求平滑过渡零中断。
3. **缓存与模板隔离**：账号切换、429 限流或 403 鉴权重试时，调用 `client.clear_templates()`，避免跨账号复用请求模板。快照签名按每次请求哈希实时生成，绝不跨请求共享。初次握手截获基线模板后，任意目标模型通过 `capture_template(model)` 纯内存动态适配参数。
4. **鉴权/服务端/传输故障按模式分策略（别再退回去）**：
   - **单浏览器路径（`AISTUDIO_BROWSER_POOL_SIZE=1`，旧行为）**：403 把当前账号置入 `auth_cooldown` 并故障转移到健康账号；传输失败在未吐正文时重试，否则 502。
   - **worker 池路径（`>1`，默认）**：403/401、5xx、502、页面 fetch/XHR 传输失败**都不是账号问题**——按 worker 故障处理：后台 kill+重启那台 Chromium，未吐字节时本次请求立刻切另一台重试；**不写 `auth_cooldown`**（不要调 `record_auth_error`）。只有 429 保留账号/模型的配额冷却；只有 `SessionExpiredError`（真的被跳到登录页）按账号隔离。
5. **无全局 DOM 污染**：页面内 JavaScript 交互使用局部闭包 `Promise` 返回数据，不在 `window` 对象上遗留全局共享状态。
6. **Cookie 智能精简与天然协商**：外部导入 Cookie 时自动过滤旧设备或跨 IP 绑定的易腐败凭据（`OSID`、`__Secure-OSID`、`SIDCC` 及 `_ga` 等追踪标记），保留核心认证项（`SID`、`SAPISID`、`1PSIDTS`）。浏览器访问 AI Studio 时自动协商出绑定当前网络/TLS 的全新有效 `OSID`，杜绝 403 权限拒绝。
7. **真实环境伪装与低内存协同**：保留 `--renderer-process-limit=1`、`--in-process-gpu` 与 128MB V8 内存限制以保障 Android 低 RAM 运行；移除 `--disable-software-rasterizer` 并启用 `--use-gl=angle --use-angle=swiftshader` 恢复软件 WebGL 上下文，保留 `--mute-audio` 并移除 `--disable-audio` 保护 AudioContext；统一注入 `--fingerprint-platform=windows` 伪装为 Windows 桌面环境，并通过原生 `--fingerprint-timezone` 与 Wire 协议层保持时区/位置严格一致。
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
├── android/                       # Android 伴侣端：多资料凭据提取器 (Jetpack Compose + Material 3)
│   ├── app/src/main/
│   │   ├── AndroidManifest.xml    # 声明 BROWSABLE / http / https 意图，适配 Bitwarden
│   │   └── java/com/aistudio/extractor/
│   │       ├── MainActivity.kt    # 单 Activity、SAF 导出/备份导入
│   │       ├── data/              # ProfileItem / AccountBundle / ProfileManager
│   │       └── ui/                # Material 3 主题、MainScreen、按需挂载的 LoginScreen
│   ├── build.gradle.kts           # 根构建脚本
│   └── .editorconfig              # ktlint 代码规范配置
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
│   │                              # 多浏览器 worker 池与失败自愈 (client_pool.py)
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
| 长静默 WARN 带 request id；bypass 时附「整段返回属预期」说明 | `api/routes_openai.py`、`api/routes_gemini.py`（gemini 原本已带 rid） | 定向单测 + real curl |
| 文档：binding 新构建、bypass 整段返回、池 standby 语义 | `README.md`、`AGENT.md`、`docs/BOTGUARD_VERIFICATION_CHAIN.md` | — |

上一轮（同一会话）已完成、本轮继续有效：2 台 worker 池（9222/9223，`-w0`/`-w1`）、
403/401/5xx/502/transport 按 worker 故障重启 + 切备用（不加账号冷却）、429 保留配额冷却、
SessionExpired 隔离 24h、`start.cmd` 前台 Ctrl+C、终端 LLM 日志、streaming JSON 修复、
OpenAI 兼容端口、bypass/预填充/签名哨兵。

### 6.2 关键真机证据（复现步骤在 6.5 的脚本里）

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
   无字节时断连，还是 BotGuard proof 时效，尚未证明。复现时建议：给页面 CDP 挂
   `Network.requestWillBeSent` / `Network.responseReceived` / `loadingFailed` 打时间戳（**只在隔离实例上做，
   别碰用户正在跑的 9222/9223**），对比「连接何时被谁断」。**不要**用加看门狗的办法绕。
2. **bypass 长请求要不要给客户端「正在生成」信号**：现在只有 1s 心跳；用户接受「和非流式差不多」，
   但超大上下文动辄几分钟，客户端体验差。选项：SSE 里发一个非标准 `processing` 事件、
   或文档里明确告诉客户端「bypass 请求首包=整段生成时间」。**用户尚未拍板，别擅自加**。
3. **超长请求失败后的重试窗口**：备用 worker 空闲休眠时需要 20~40s 拉起，再重新生成可能又是几分钟；
   是否对「已经等了 N 分钟且无首帧」的 transport 失败改为快速失败（而不是切备用重试），待用户决定。
4. **单账号时两台 worker 共用 cookie**：`save_browser_cookies` 两台都会写同一份 `auth.json`
   （原子写，不会写坏，但可能后写覆盖先写）。目前只有 1~2 个账号，等导入 4+ 份后自然分散；
   若单账号长期跑，考虑给 cookie 保存加跨 worker 互斥或只让主 worker 保存。
5. **WebUI 手动「切换账号」只影响当前/主 worker**，不会同步两台；多账号导入后需要补「按账号重绑 worker」。
6. **真实上游 403/500/502 自愈**只做过单测 + 真机 kill Chromium 冒烟（同请求切备用 200）；
   没等到真实上游 403 现场。遇到时留意日志里的 `worker #N 故障 (auth/http_403…)`。
7. **全量 pytest 的 8 条 Windows 既有失败**（`test_browser_engine`×2、`test_browser_scripts_syntax`×1、
   `test_response_parser`×3、`test_wire_codec`×2）是环境/基线问题，不是回归；别去「修」成通过。
8. **未提交 git**：工作区有 100+ 文件改动（含新增文件）。交接前如要提交，先 `git status` 对照
   `C:\sister-aistudio-local\sister-aistudio-port.patch`；补丁生成方式见 6.5。
9. 本轮最后两个改动（standby 判定、stall 文案）只跑了定向测试与 ruff；**全量 pytest 请在交接前再跑一次**
   （见 6.4 命令），结果应是 555 passed / 8 failed。

### 6.4 环境、命令与坑

```powershell
# 项目根（不是 git clone 的 local 目录）
cd C:\sister-aistudio
$env:PYTHONPATH = 'C:\sister-aistudio\src'
$env:PYTHONIOENCODING = 'utf-8'

# 全量测试（Windows 基线：555 passed / 8 failed）
& 'C:\sister-aistudio-local\venv\Scripts\python.exe' -m pytest tests -q

# ruff
& 'C:\sister-aistudio-local\venv\Scripts\ruff.exe' check src tests

# JS 语法
node --check src\aistudio_api\infrastructure\browser\js\install_hooks.js

# 启动/停止：start.cmd 前台，Ctrl+C
.\start.cmd
```

- 运行期数据：`data/settings.json`（WebUI/`PUT /settings`，立即生效）、`data/accounts/<id>/auth.json`、
  `data/accounts/<id>/profile-wN`（worker profile，**同一 auth_file 不许共用**）。
- 池默认 2 台：CDP `9222/9223`；**不要在服务运行时再起第二个网关碰这两个端口**。做隔离实验时用
  `AISTUDIO_DATA_DIR=<临时目录>` + `AISTUDIO_BROWSER_PORT=95xx` + `AISTUDIO_BROWSER_POOL_SIZE=1`。
- 主项目 `C:\build2api` 在 **8889** 跑着（用户自己的），**不要动它**；只读对比其
  `/__b2a/requests`（`Authorization: Bearer sk-123`）可以看到同通道的真实首包/总时长。
- 额度：`gemini-4-argon` 日桶很小，**别拿它压测**；诊断用 `gemini-3.8-flash`。
- 补丁：`git add -N` 未跟踪文件 → `git diff --binary --output=C:\sister-aistudio-local\sister-aistudio-port.patch`
  → `git reset`；再用 `git clone --no-hardlinks` 到临时目录 `git apply --check` 验证后删掉 clone。
- **改完代码要重启 `start.cmd` 才生效**：用户 2026-10-20 05:5x 时 8080 上还跑着改前的进程；
  停服务只按用户的方式（Ctrl+C），别去 kill 进程号或动 9222/9223 的 Chromium。

### 6.5 诊断脚本（`C:\sister-aistudio-local`，都是这一次排查留下的）

| 脚本 | 用途 |
|---|---|
| `diag_binding.py` | 冷/热 profile 下页面 DI 探针状态（`dms/TB/tap/service` 时间线），只起浏览器不发请求 |
| `diag_binding3.py` | 证明「通用 `inject()` 形状捕获能拿到服务并签 proof」 |
| `diag_bootstrap_after_fix.py` | 端到端跑 `bootstrap_botguard_and_template`，验证 binding 修复（4.2s / 300KB binding 0.68s） |
| `probe_flash_stream.py` / `probe_flash_long.py` | 通过运行中的网关打 flash 流式请求，打印每帧到达时间（产物 `probe_*.sse`） |
| `probe_isolation.py` | **最关键的对照实验**：独立浏览器 + 临时 data dir，bypass 开/关各打一发，比较 status/chunk 时间线（产物 `probe_isolation.log`） |
| `curl_trace.log` / `curl_out.sse` | 用户要求的那发 raw curl 的逐块时间戳与原始 SSE |

### 6.6 一句话结论（可以直接转述给用户）

- `binding 引导未通过…回退 page_send` = **真 bug（已修）**：新页面构建不再导出 `dms.TB`，DI 探针装不上。
- `上游 Ns 无任何数据（响应头都没收到）` = **不是 bug**：bypass 的 functionCall 上游整段返回，
  出第一帧前没有任何字节（也就没有响应头）；心跳一直在发，60s WARN 只是报告卡点。
- `Failed to fetch` → 502 = **不是误判**：页面 fetch 真的被上游/网络断开（主项目同通道也有），
  但有备用却没切是 **真误判（已修：空闲休眠也算备用）**。
