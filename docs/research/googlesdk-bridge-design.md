# google-genai 官方 SDK 桥接层设计调研

日期：2026-10-20 · 调研代号：sdk-bridge · 证据版本：google-genai **2.31.0**（wheel：`google_genai-2.31.0-py3-none-any.whl`，源码解压于 `%TEMP%\genai-sdk-src\extracted`；与 `C:\sister-aistudio\.venv\Lib\site-packages\google_genai-2.31.0` 同版本）

---

## 一、结论（TL;DR）

1. **可行，且比预想的更干净：不需要"魔改"SDK。** 用户设想的是把 SDK 传输层换掉、留用其协议语义层——这个理解基本正确，但实现上连 monkeypatch 都不需要：SDK 的 `HttpOptions` **官方公开支持**注入自定义 `httpx_client` / `httpx_async_client` / `aiohttp_client` 和覆盖 `base_url`。我们只需写一个自定义 `httpx.AsyncBaseTransport`（把 SDK 发出的官方 REST/JSON 请求翻译成 AI Studio wire 数组、走 CDP XHR、把 wire 响应翻译回官方 JSON/SSE），SDK 就完整接管模型语义：请求校验、默认值、官方 JSON 构建、响应解析、SSE 拆帧、错误模型。
2. **一个前提不成立：SDK 里没有 OpenAI↔Gemini 转换代码。** 全库检索（`openai` / `ChatCompletion` / `chat.completions`）仅 `tests/tunings/test_tune.py` 一处无关命中。OpenAI 兼容是**服务端端点**（`generativelanguage.googleapis.com/v1beta/openai/`），SDK 只是它的客户端，不带翻译层。因此 OpenAI 端口的请求侧翻译（openai_bridge.py 前半部分）**无法被 SDK 替换**，仍需自维护；SDK 的收益集中在 Gemini 语义侧（类型系统、响应解析、流式拆帧）。
3. **项目已埋好半条路**：`tests/unit/test_genai_sdk_compat.py` 已用 `httpx.ASGITransport(app=app)` 把官方 SDK 指向我们的 FastAPI，5 个用例（非流式/流式/函数调用/错误/工具配置）全链路验证了"SDK ↔ 我们的官方 JSON 端口"格式互操作。且 `google-genai>=1.0.0` 已在 pyproject 的 dev/test extras 里，venv 已装 2.31.0，**依赖零新增**（httpx 0.28.1、pydantic 2.12.5、tenacity 9.1.4、google-auth 2.62.0 全部满足 SDK 的 Requires-Dist 区间）。
4. **净收益评估：短期代码量约持平（新增桥接 ~400-600 行、删减相当），长期收益是字段覆盖与上游同步。** SDK 的 `types.py` 由官方 API 生成，覆盖 groundingMetadata、logprobs、thinkingConfig 等我们手搓层没实现/会漏掉的字段；换 SDK 后这些自动获得。License 为 Apache-2.0，直接依赖引入无合规问题。
5. **推荐路径**：先在 OpenAI 端口内部试点（feature flag 双轨），Gemini /v1beta 端口现有实现稳定可暂不动。

---

## 二、用户理解的校正

| 用户设想 | 核实结果 |
|---|---|
| 引入官方 SDK 魔改成桥接 | 方向对；但"魔改"不必要，官方扩展口（HttpOptions 注入 httpx 客户端 + 自定义 transport）足够 |
| SDK 负责模型和协议语义，传输走浏览器会话 | 完全成立；见 §三架构证据 |
| 少维护手搓 wire 编解码 | **部分成立**：请求侧官方 JSON→wire 的翻译仍要自己做（SDK 只产出官方 JSON，不懂 JSPB）；响应侧 wire→官方 JSON 是新增转换器，但可复用 wire_parser 现有原语 |
| 少维护 OpenAI↔Gemini 翻译 | **不成立**：SDK 无此代码。收益仅限"翻译的目标/来源从手搓 dict 变成 SDK 强类型对象"，翻译本身仍在 |
| 复用 SDK 流式解析器喂自己的字节流 | 成立且自动获得：SDK 的 SSE 拆帧器 `_aiter_response_stream` 消费的就是 httpx.Response 的 `aiter_lines()`，我们只要把 wire 流转成 SSE 字节流装进 httpx.Response |

---

## 三、SDK 源码级证据（google-genai 2.31.0）

### 3.1 传输层与可注入性

- HTTP 客户端：**httpx 为默认**；aiohttp 是可选 extra（METADATA：`aiohttp; extra == "aiohttp"`）。
- 官方注入点 `google/genai/types.py:2712` `class HttpOptions`，公开字段：
  - `base_url`（2715 行）— 自定义端点；`api_version`（2723 行）
  - `client_args` / `async_client_args`（2734/2737 行）— 透传给 httpx client 构造函数
  - **`httpx_client`（2750 行）/ `httpx_async_client`（2754 行）/ `aiohttp_client`（2758 行）—"A custom httpx (async) client to be used for the request"，官方文档化的扩展口**
  - `retry_options`（2746 行）— `HttpRetryOptions(attempts=1)` 即关闭重试（types.py:2657-2661："If 0 or 1, it means no retries. If not specified, default to 5."）
- 注入生效机制 `google/genai/_api_client.py`：
  - `BaseApiClient.__init__` 910-921 行：`http_options.httpx_async_client` 存在则直接用作 `self._async_httpx_client`，否则 `httpx.AsyncClient(**async_client_args)`
  - `_use_aiohttp()`（1368-1375 行）：`has_aiohttp and async_client_args.get('transport') is None and httpx_async_client is None` —— **通过 HttpOptions 传入 httpx_async_client 可强制锁定 httpx 路径**，杜绝 aiohttp 分支
- URL 构建 `_build_request`（1449 行起）：`base_url + api_version + path`，path 由 models 层给出（如 `models/{model}:generateContent`、流式 `models/{model}:streamGenerateContent?alt=sse`）；headers 注入 `x-goog-api-key`（我们的 transport 直接忽略即可）
- Client 构造（`client.py:363`）：`custom_base_url` 检测在 `_api_client.py:823-828`——设了 `base_url` 即跳过鉴权充分性检查，`api_key` 可传任意占位值（仅进 header，不到达网络）

### 3.2 请求/响应/流式的内部路径（桥接要对接的精确位置）

- 非流式：`models.py:6895` `AsyncModels._generate_content` → `_GenerateContentParameters_to_mldev(...)` 产出官方 camelCase JSON dict → `self._api_client.async_request('post', 'models/{model}:generateContent', request_dict)`（6949 行）→ `_async_httpx_client.request(...)`（2387 行）→ **httpx transport**
- 流式：`models.py:6991` `_generate_content_stream` → `async_request_streamed`（`_api_client.py:1894`）→ `_async_request(stream=True)` → `build_request` + `_async_httpx_client.send(req, stream=True)`（2540-2547 行）→ **httpx transport**
- 响应包装 `_api_client.py:320` `class HttpResponse`：对 `response_stream` 做 **isinstance 检查必须是真 `httpx.Response`**（`_HTTPX_RESPONSE_TYPES`，106-108 行），随后：
  - SSE 拆帧 `_aiter_response_stream`（474 行）：`aiter_lines()` 逐行，剥 `data: ` 前缀、花括号配平拼完整 JSON，最后 `aclose()`
  - 错误 `errors.py:209` `raise_for_async_response`：200 直接放行；否则 `aread()` + `.json()` 解析 `{"error": {"code", "message", "status"}}` 升级为 `errors.APIError`
- 结论：**自定义 transport 只需返回手工构造的 `httpx.Response`**——非流式塞 JSON bytes；流式塞一个产出 `data: {单行 JSON}\n\n` 帧的 async bytes 迭代器（`httpx.Response(200, headers=..., content=async_iter, request=req)` 原生支持 AsyncIterable content）。现有 ASGITransport 测试已证明该消费路径端到端可用。

### 3.3 OpenAI 兼容层：不存在

- `grep -ri openai` 全库仅 `tests/tunings/test_tune.py` 命中（与其无关）；无 `ChatCompletion`/`chat.completions` 任何代码。OpenAI 兼容是 Gemini API 的服务端端点，SDK 只是该端点的薄客户端。
- 附带发现：`HttpResponse.to_json_dict`（`_common.py:661`）用 `model_dump(exclude_none=True)` **无 by_alias**，输出 snake_case；要回发官方 camelCase JSON 须自行 `model_dump(by_alias=True, exclude_none=True)`。

### 3.4 License 与依赖

- METADATA：`License-Expression: Apache-2.0`。
- Requires-Dist：anyio>=4.8, google-auth[requests]>=2.56, httpx>=0.28.1, pydantic>=2.12.5, requests, tenacity>=8.2.3,<9.2, websockets, typing-extensions——**项目 venv 现状全部满足，无需任何升级**。

---

## 四、项目现状盘点：SDK 能换什么、必须留什么

| 模块 | 行数 | 现职责 | SDK 桥接后 |
|---|---|---|---|
| `application/openai_bridge.py` | 1101 | OpenAI↔Gemini 双向翻译 + OpenAIStreamConverter | **保留但改薄**：请求侧翻译目标从手搓 dict 改为 SDK `types.GenerateContentConfig`/`Content`；响应侧改为消费 SDK `types.GenerateContentResponse` 强类型对象（text/function_calls/usage_metadata/finish_reason 全部已解析好），OpenAIStreamConverter 从 ~250 行事件元组处理缩为类型化直译 |
| `application/chat_service.py` | 855 | Gemini 请求归一化 + schema→wire 编码 | **大头可被 SDK 替换**：`normalize_gemini_request` + `encode_schema_to_wire`/`encode_function_declaration_to_wire` 的前半段（官方 JSON→内部表示）由 SDK `types.*` 校验与 `to_mldev` 接管；schema→wire 下标编码保留在桥接层（wire_codec 已有） |
| `api/schemas.py` + `api/response_models.py` | 384 | Gemini 端口请求/响应 pydantic 模型 | **可整体删除**（被 `google.genai.types` 取代，字段覆盖更全）——仅当 /v1beta 端口也切 SDK 时 |
| `application/api_service_gemini.py` | 829 | SSE 组帧、错误渲染、finish reason 映射 | SSE 组帧职责**转移到桥接 transport**（对 SDK 侧）；对外端口的 SSE 渲染若走 SDK 路径则由类型化对象直出。`to_gemini_finish_reason`（wire 码→枚举名）**保留**，被 wire→官方 JSON 转换器复用 |
| `infrastructure/gateway/wire_codec.py` | 536 | JSPB 请求编解码（模板改写） | **100% 保留**——桥接层的请求侧出口。官方 JSON 字段→`AistudioContent`/`AistudioPart`/generation_config 下标的映射复用 `rewrite()` 的既有参数面 |
| `infrastructure/gateway/wire_parser.py` | 628 | JSPB 响应解码→内部 Candidate | **原语 100% 保留**（`parse_response_part`/`parse_usage_metadata`/全部下标常量）；新增一个薄转换器把这些原语输出组装成官方 `GenerateContentResponse` JSON dict（~150-250 行） |
| `infrastructure/gateway/streaming.py` + `stream_parser.py` | 540 | XSSI+JSON 数组增量解析、事件流 | **100% 保留**；桥接在其上包一层"事件→SSE 帧"（每帧一个官方 JSON chunk） |
| `infrastructure/gateway/*`（session/client/signatures/capture/replay/botguard/…） | — | CDP 传输、WAA 签名、模板捕获 | **100% 保留**，SDK 完全不触碰 |
| `api/routes_openai.py` / `routes_gemini.py` | — | 两端口入口 | 保留，内部改调 SDK client（flag 控制） |

**wire 与官方格式的根本差异（桥接必须吸收的）**：
- 上游响应 = XSSI 前缀 `)]}'` + JSPB 数组（字段号=下标+1）；SDK 期待 = 官方 REST JSON（camelCase）/ SSE `data:` 帧。**方向性转换不可避免**，但两个方向的映射经验与解析原语项目都已具备。
- wire 请求独有字段：`snapshot`(field5)、`request_flag`(field10)、`session_token`(field12)、`location/timezone`(field14)——**官方 JSON 无对应物**，必须继续从捕获模板注入（capture.py `_ensure_template` 流程不变）。
- SDK 会发出的部分官方字段 wire 不认（generationConfig 下标 9-12 penalties/logprobs 当前构建无写入点等）→ 桥接需要显式 drop 列表 + 告警日志，防静默丢失。

---

## 五、分层设计

```
┌────────────────────────────────────────────────────────────────────┐
│ API 层（保留）：routes_openai / routes_gemini / routes_models        │
│  OpenAI 端口：openai_bridge 请求侧（OpenAI→SDK types，改薄保留）      │
│  Gemini 端口：构造 SDK 调用（可选切换，默认走原路径）                  │
├────────────────────────────────────────────────────────────────────┤
│ SDK 层（google-genai 2.31.0，锁定版本，零魔改）                       │
│  types.*：请求校验/默认值/字段全集                                    │
│  AsyncModels.generate_content(_stream)：官方 JSON 构建(to_mldev)     │
│  与响应解析(from_mldev)；SSE 拆帧；错误模型 APIError；重试(需显式关)  │
├────────────────────────────────────────────────────────────────────┤
│ 桥接层（新增，~400-600 行，1-2 个文件）                               │
│  AistudioTransport(httpx.AsyncBaseTransport)                        │
│   .handle_async_request(httpx.Request) -> httpx.Response            │
│   ├ 路径解析：/v1beta/models/{model}:(stream)generateContent         │
│   ├ 官方JSON→Aistudio 请求：复用 RequestCaptureService.capture       │
│   │  + wire_codec.rewrite（snapshot/session_token 等从模板注入）     │
│   ├ wire→官方JSON：新转换器（复用 wire_parser 原语 +                 │
│   │  to_gemini_finish_reason + parse_usage_metadata）               │
│   ├ 流式：StreamingGateway 事件 → `data: {...}\n\n` async 迭代器     │
│   └ 错误：classify_error → {"error":{code,message,status}} 包装      │
├────────────────────────────────────────────────────────────────────┤
│ 传输层（保留 100%）：BrowserSession / cdp_client / signatures /       │
│  capture / replay / botguard_bootstrap / session_auth / …           │
└────────────────────────────────────────────────────────────────────┘
```

SDK 客户端构造（伪代码，全部为官方公开 API）：

```python
from google import genai
from google.genai import types

sdk = genai.Client(
    api_key="aistudio-bridge",  # 占位，只进 header，不到达网络
    http_options=types.HttpOptions(
        base_url="http://aistudio-bridge.internal",   # 任意假地址，仅用于路径拼装
        api_version="v1beta",
        httpx_async_client=httpx.AsyncClient(transport=AistudioTransport(gateway)),
        retry_options=types.HttpRetryOptions(attempts=1),  # 用户决策：零自动重试
    ),
)
resp = await sdk.aio.models.generate_content(model=..., contents=..., config=...)
```

---

## 六、"魔改点"清单（精确到模块/函数）

**结论：零魔改、零 monkeypatch、零 fork。** 唯一的对接面是两个官方公开构造：

| # | 位置 | 用途 | 性质 |
|---|---|---|---|
| 1 | `google/genai/types.py:2754` `HttpOptions.httpx_async_client` | 注入挂载我们 transport 的 httpx 客户端，同时把 `_use_aiohttp()` 锁死为 False | 官方公开字段 |
| 2 | `httpx.AsyncBaseTransport.handle_async_request`（httpx 官方抽象） | 拦截 SDK 全部请求（`client.request()` 与 `client.send(stream=True)` 两条路径都过 transport） | httpx 官方扩展口 |

配套设置：`HttpOptions.base_url`（路径拼装）、`api_version='v1beta'`、`retry_options=HttpRetryOptions(attempts=1)`（关闭 SDK 默认 5 次重试，对齐"零自动重试"决策）。

**不采用的备选**：现有测试的做法（构造后覆写 `aio_client._api_client._async_httpx_client`，test_genai_sdk_compat.py:49）——依赖私有属性 `_api_client`（其模块 docstring 自述"intended to be a private module and is subject to change"，_api_client.py:19），且不设 `http_options.httpx_async_client` 时一旦环境装了 aiohttp 会翻转传输路径。仅测试代码可继续用。

---

## 七、风险清单

1. **SDK 版本漂移**：`_api_client.py` 自述私有易变；但我们只依赖公开 `HttpOptions` + httpx 语义，实际耦合面小。对策：pyproject 锁 `google-genai==2.31.0`（从 extras 提升为运行时依赖时），升级走独立 PR + 全量测试。
2. **aiohttp 翻转**：若环境装入 aiohttp 且未通过 `HttpOptions.httpx_async_client` 注入，`_use_aiohttp()` 为 True，请求绕开我们的 transport。对策：只用 HttpOptions 注入；不安装 aiohttp extra。
3. **SDK 隐式重试**：默认 `attempts=5`（含 429/5xx），违反用户"零自动重试"决策。对策：强制 `retry_options=HttpRetryOptions(attempts=1)`，并在集成测试断言单次调用。
4. **字段覆盖差（双向）**：SDK 发出的官方字段 wire 不认（penalties/logprobs 等）→ 显式 drop 列表 + 日志；wire 独有字段（snapshot/session_token/location）从模板注入——官方 JSON 请求永远不可能是完整事实来源，**模板捕获机制不可拆**。
5. **SSE 帧细节**：必须 `data: ` 前缀、帧间空行、单行紧凑 JSON（`json.dumps` 默认无换行；注意 `ensure_ascii=False` 时字符串内控制符已转义）。SDK 的花括号配平对畸形帧容错有限，畸形帧会被静默丢弃（`_aiter_response_stream` 仅 brace 计数）。
6. **错误语义翻译**：上游 403/429/500 的非 JSON 体必须包成 `{"error":{"code","message","status"}}`，否则 `raise_for_async_response` 走 JSONDecodeError 降级分支，APIError 的 `status` 字段退化为 reason_phrase。
7. **序列化大小写陷阱**：`to_json_dict()` 无 by_alias（snake_case）；对外回发官方 camelCase 需 `model_dump(by_alias=True, exclude_none=True)`。
8. **双轨期成本**：feature flag 下新旧两条 OpenAI 路径并存，测试矩阵约翻倍；回滚开关必须保留至新路径稳定一个迭代周期。
9. **合规**：Apache-2.0 依赖无问题；对 AI Studio 私有端点的使用姿态与现状完全相同（引入 SDK 不改变合规暴露）。
10. **流式背压语义**：SDK 侧 `aiter_lines` 拉取驱动我们的 async 迭代器；CDP 侧事件到 SSE 帧的队列需要有界（防上游快、客户端慢时内存堆积）。

---

## 八、最小可行验证路径（试水计划）

- **Phase 0（已完成，零成本）**：`tests/unit/test_genai_sdk_compat.py` 证明 SDK ↔ 我方官方 JSON 端口互操作（ASGITransport 回环）。
- **Phase 1（纯新增，不接线）**：在 `%TEMP%` 或 tests 下实现 `AistudioTransport` + wire→官方 JSON 转换器；用 fake gateway（回放固定 wire 响应，可用 replay.py 的既有夹具机制）写单测：构造上述 SDK Client，断言 `generate_content` / `generate_content_stream` 的类型化输出（text/usage_metadata/finish_reason/function_calls）。不启动服务、不碰 CDP。
- **Phase 2（OpenAI 端口内试点）**：`routes_openai.py` 加 feature flag，新路径 = openai 请求 → SDK types → `sdk.aio.models.generate_content(_stream)` → `GenerateContentResponse` → OpenAI 响应；现有全部 openai 端口测试双跑两轨。
- **Phase 3（可选）**：Gemini /v1beta 端口切换（删 schemas.py/response_models.py/chat_service 大部），仅在 Phase 2 稳定后评估——现有端口工作正常，此步收益最小、扰动最大。

**试水规模预估**：Phase 1 约 400-600 行新代码 + 测试；Phase 2 改造 openai_bridge 后半部分（~600 行净变化）；Phase 3 另计。
