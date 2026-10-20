# vertex-streaming-anti-truncation 流式工具调用抗截断机制研究

研究日期：2026-10-20
克隆位置：`%TEMP%\vertex-at`（`C:\Users\Administrator\AppData\Local\Temp\vertex-at`，浅克隆 main）
本仓库对照代码：`src/aistudio_api/application/bypass.py`、`src/aistudio_api/infrastructure/gateway/streaming.py`、`src/aistudio_api/application/stream_heartbeat.py`

---

## 结论（先看这里）

1. **对方能做到真流式，靠的不是解析技巧，而是官方 API 的一个 Preview 开关**：Vertex 原生
   `streamGenerateContent` 支持 `toolConfig.functionCallingConfig.streamFunctionCallArguments = true`
   （Gemini 3+、v1/v1beta1、REST，官方标注 Preview）。开启后响应里的 `functionCall` part 不再整块到达，
   而是携带 `partialArgs: [{jsonPath: "$.content", stringValue: "片段", willContinue: true}]` 逐片到达，
   最终以一个空 `functionCall` 收尾。网关把每片 `stringValue` 重新 JSON 转义后拼成合法的 OpenAI
   `tool_calls.function.arguments` 增量帧，再用增量 JSON 状态机实时解出正文按 `delta.content` 吐给客户端。
2. **官方 API 默认（不开这个开关）时 functionCall 也是整块到达的**——vertex-at 自己的代码注释和 README
   明说：兼容端点「等完整 tool 参数再返回」、原生流「function calls arrive whole」。这与我们观察到的
   AI Studio 页面 wire 行为（生成结束才整块吐 functionCall part）**完全一致**，不是我们解析器的缺陷。
   我们 `BypassStreamExtractor` 里「args 累积增量」的注释是从 build2api 移植的设计假设，对应的测试
   fixture（`tests/unit/test_bypass.py` 的累积帧用例）是模拟数据，不代表 AI Studio 实际行为。
3. **我们能否让 bypass 真流式？分层结论**：
   - **方案 A（治本，需一次实验验证）**：在 MakerSuite wire 请求体里注入等价于
     `streamFunctionCallArguments` 的字段。可行性未知但有一个正面信号：页面 bundle 的 functionResponse
     容器 schema 里已经存在 `will_continue`（field 4），说明流式函数调用 proto 家族存在于 MakerSuite
     的 schema 里。若 400 则说明该 RPC 未暴露此能力，止损成本低。
   - **方案 B（治标，零 wire 改动，立即可试）**：把「一次调用交全文」改成「多次小调用各交一段」。
     每个调用整块到达，但小而频繁，天然形成分片流。我们的 `ContentMerger`/`collapse_cumulative`
     已经能按序合并多个调用，只需改指令文案。
   - **方案 C（备选，风险较高）**：thinking 通道承载正文。thought part 走的是和正文一样的 text 传输，
     应当天然增量到达，但模型配合度与审查行为均未验证。
   - 三者都失败时，1 秒心跳（现状）就是正确兜底——vertex-at 在同样处境（兼容端点回退）下也是整块交付，
     只是文档说明而不做额外处理。
4. **置信度**：第 1、2 点高（双方代码 + 官方文档三重印证）；方案 A 中（存在正反两向信号，需实验）；
   方案 B 中（机制上通，模型配合度待验）；方案 C 低。

---

## 一、vertex-streaming-anti-truncation 的核心机制

项目定位：本地 OpenAI 兼容网关（Node.js，无依赖），把 SillyTavern 等 OpenAI 客户端的请求转发到
Vertex AI。合成工具传输（synthetic text-tool transport）的设计来自
[Antigravity-gateway](https://github.com/Xeltra233/Antigravity-gateway)（README.en.md L42 致谢），
本项目的增量是接上 Vertex 原生的参数流。

### 1.1 请求侧：合成工具 + 强制选择

`src/anti-truncation.mjs` `prepareAntiTruncation()`（L27-68）：

- 工具名 `router_emit_` + 24 位随机 hex（L41），与我们 `bypass.py:118-121` 的
  `emit_<16 hex>` 同构；
- 追加一条 user 消息作为「传输格式」指令（L52-58），语境与我们一致：「`{toolName}` only
  transports text and performs no external action」；
- 声明唯一参数 `content: {type: "string"}` 的函数（L59-63）；
- `tool_choice` 强制到该函数；有客户端工具时改 `required` 并允许真调用透传（L44-64）；
- **与我们最大的差异一**：它同时支持「续写」模式（L9 `CONTINUATION_INSTRUCTION`，用于 SillyTavern
  Continue），我们按用户决策已收敛为单轮不续写。

跳过条件（L28-35）：已有 `functions`、强制 tool_choice、结构化输出、多候选、带工具历史 →
不套传输层。我们等价逻辑在 `bypass.py` 的 prefer/required 与 `merge_outside`。

### 1.2 三种 transport（关键差异所在）

`src/gateway.mjs` L222-227：按模型档案 `route.mode` 分 normal / buffered / streaming 三档：

| transport | 上游 | 参数到达方式 | 客户端体感 |
|---|---|---|---|
| `tool-transport-native-streaming` | Vertex 原生 `streamGenerateContent?alt=sse`，请求体带 `toolConfig.functionCallingConfig.streamFunctionCallArguments = true` | `partialArgs` 逐片（jsonPath 定位 + stringValue 片段 + willContinue） | **真流式** |
| `tool-transport-buffered-fields` | OpenAI 兼容端点 `/chat/completions`（或自定义上游/带客户端工具时的回退） | `tool_calls[].function.arguments` 逐**字符串片段**增量（标准 OpenAI 流式语义） | 取决于上游是否真的分片转发；官方兼容端点=整块 |
| `tool-transport-buffered` | 任意（buffered 档） | 整块 | 整块交付（SSE 只是包装） |

- 原生开关：`src/vertex-native.mjs` `buildNativeTextBody()` L258-262，核心一行
  `body.toolConfig.functionCallingConfig.streamFunctionCallArguments = true;`（L260）。
- 回退判定：`src/anti-truncation.mjs` L37-40（`requested && native && !clientTools.length &&
  supportsNativeTextStream(payload)`）；带客户端工具/工具历史的请求只能走 buffered-fields
  （README.en.md L264 明说：native 参数流只承载合成工具）。
- 原生流的转换器：`src/vertex-text-stream.mjs` `wrapNativeTextStream()` L9-76——
  - `fc.args`（整块对象，收尾帧）与 `fc.partialArgs`（增量）二选一，混用即 fail（L26-37）；
  - 只认 `part.jsonPath === "$.content"` 的 string 片段；首片先发 `{"content":"` 前缀，
    每片 `JSON.stringify(part.stringValue).slice(1, -1)` 重转义后作为 arguments 增量发出（L33-35）；
  - `willContinue !== true` 时补闭合引号（L36）；`fc.willContinue !== true` 时补 `}` 并封口（L38-42）；
  - thought part 单独映射成 `reasoning_content` 增量（L59）。
- 官方 API 默认整块的旁证：`src/vertex-protocol.mjs` L39-41 注释「Real tool requests do not
  enable partialArgs... complete function calls」；同文件 L16 遇到 `fc.partialArgs` 直接
  `invalid_native_tool`（普通原生流不应出现）。

### 1.3 增量提取：ContentDecoder（流式 JSON 状态机）

`src/anti-truncation.mjs` L72-214。这是对方真正的「partial JSON 解析器」：

- 单字符流式喂入 `feed(text)`（L126-212），维护 JSON 语法栈（stack/mode/stringKind/hex/escape），
  **从不累积原始参数字符串**（L70-71 注释），内存 O(语法深度)；
- 只在「根对象第一层、key === `"content"`」时把字符串值当作正文（L208
  `startValue(ch, this.stack.length === 1 && frame.type === '{' && frame.key === 'content')`），
  其余 key 的值只走语法不输出（L89-94 `emitChar` 里 `stringKind` 过滤）；
- 逐字符处理 `\uXXXX`、代理对（`high` 暂存，L96-102）、非法转义/裸控制字符直接 fail（L142-161）；
- `decoder.valid` = 语法完整 && 找到过 content && content 字符串已闭合（L213）。
- 字符串片段输入场景（兼容端点/OpenAI 协议）：`StreamRestorer.tool()` L262-304 把每个
  `delta.tool_calls[].function.arguments` 片段直接 `slot.decoder.feed(args)`（L292-296），解出的
  字符立即作为 `delta.content` 重发（L342）。工具名本身也可能分片到达（L272-273 `slot.name +=
  call.function.name`），靠随机前缀在名字完整前区分真/合成调用（L276-282）。
- 测试佐证分片粒度：`test/anti-truncation.test.mjs` L162 逐字符喂 `arguments: char`。

### 1.4 截断检测与「抗」的具体对象

- **抗 MAX_TOKENS（finish_reason=length）与审查（content_filter）**：`cutShort = reason =>
  "length" || "content_filter"`（anti-truncation.mjs L216）。finish 时若 JSON 未闭合但 reason 是
  cutShort，**接受不完整参数、交付已解出的部分正文**（L231、L316）；否则
  `anti_truncation_incomplete_arguments` 报错。审查截断的「抗」本体是**传输层**：正文藏在工具参数
  里，绕开只扫 `delta.content`/text 通道的过滤（与我们 bypass.py L1-5 的动机相同）。
- **抗网络断流**：不自动恢复。`wrapAntiTruncationStream` 结束时未见 `[DONE]` 即
  `anti_truncation_stream_interrupted`（L391）；`guardCompletionStream`（completion-integrity.mjs
  L80-182）要求每个 choice 有合法 finish_reason、流以 `[DONE]` 结束，否则 `incomplete_stream`/
  `empty_stream`。README L266 明说「Interrupted streams fail; the gateway does not continue or
  retry them automatically」——与我们的「零自动重试」决策一致。
- **完整性审计**：`completion-integrity.mjs` 把结果归类 complete/length/content_filter/tool_calls/
  incomplete/empty/error/cancelled，length/content_filter 下跳过工具参数 JSON 完整性校验（L103-110）。

---

## 二、官方 API 的 functionCall 流式行为（对我们最关键的事实）

官方文档（Vertex AI function calling，`streaming function call arguments` 小节，2026-10-20 抓取确认）：

- 状态 **Preview**；
- 请求：`streamGenerateContent` 时在 `toolConfig.functionCallingConfig` 里设
  `streamFunctionCallArguments: true`；
- 响应：中间帧的 `functionCall` 带 `partialArgs` 与 `willContinue`；每个 `PartialArg` 含
  `jsonPath`（该片段在参数对象中的 JSONPath）+ `numberValue/stringValue/boolValue/nullValue` 之一；
  `willContinue` 有两层语义：PartialArg 级=该 string 还会有后续块，functionCall 级=后续流式响应里
  还有更多 partialArgs；结束信号是一个**空的 functionCall 对象**；
- 模型：Gemini 3 及以后（3.1 Flash-Lite 不支持）；v1 与 v1beta1 都有。

**推论**：官方 API 的 functionCall part 默认（不开开关）在流式响应里**不是**逐块增量——这与
AI Studio 页面 wire 的整块行为同源（同一套 serving 栈）。差异只在于官方 API 暴露了一个页面前端
从不设置的 opt-in 开关。

---

## 三、我们这边的现状（代码级对照）

| 环节 | vertex-at | 我们（aistudio-api） |
|---|---|---|
| 合成工具 | `router_emit_<hex>`，tool_choice 强制 | `bypass.py:118-121` `emit_<hex>`；ANY 靠「工具表只剩合成工具」逼近（`bypass.py:267-277`，注释明说 MakerSuite 无独立 toolConfig 槽位） |
| 指令注入 | 追加 user 消息（anti-truncation.mjs L52-58） | `bypass.py:161-216` `inject_instruction`（tail/system/head 三档，处理工具结果轮例外） |
| 流式提取 | 增量 JSON 状态机，片段即解即吐 | `bypass.py:903-969` `BypassStreamExtractor`：**假设每帧 args 是累积快照**，`collapse_cumulative`（L691-712）切尾巴；`feed()`（L931-969）逐 part 处理 |
| 坏 JSON 修复 | 只在 cutShort 时宽容不完整；否则报协议错 | `repair_args`（L334-363）→ 剥围栏 → `_repair_structure`（L395-461，补引号/平衡括号/转义裸控制符）→ `_scan_content`（L468-546，手工扫 `"content": "..."`，容忍未闭合） |
| 去重合并 | 首个非空通道获胜，其余抑制（StreamRestorer L298-303） | `ContentMerger`（L580-633）+ `is_cumulative` O(1) 探针（L552-566）+ `strip_overlap` |
| 断流处理 | 报错，不重试 | `streaming.py:205-209` `parser.finish()` 抢救半截尾块；完成判定只记日志（`assess_completion` L839-855） |
| 空闲兜底 | 无（回退 transport 就整块交付） | `stream_heartbeat.py:36-80` 1 秒心跳（`HEARTBEAT_INTERVAL_S=1.0`），OpenAI 帧见 `api/routes_openai.py:78-81,176-181` |

关键差异定位：

1. **我们的提取器吃不下「字符串片段」输入**。`_call_payload`（bypass.py L661-672）取到 `arguments`
   字符串时走 `repair_args`——那是为「整段坏 JSON」设计的一次性修复；若上游真的逐片段发
   `arguments`，每个片段单独 `repair_args` 会解析失败被丢弃（只置 `repaired=True`）。换言之：
   **就算 wire 突然开始发增量片段，我们也接不住**；接累积快照没问题（`collapse_cumulative`），
   接片段需要 ContentDecoder 式状态机。
2. **我们的 wire 请求体目前根本不带 toolConfig**。`chat_service.py:387-426`
   `_normalize_gemini_tool_config` 产出 `[None, [mode_code, allowed_names, include_invocations]]`，
   但 `wire_codec.py:206-250` 的 `encode()` 只写 model/contents/safety/generation_config/snapshot/
   system/tools/evergreen/flag/session_token/location 这些下标，`rewrite()`（L329-345）拿 tool_config
   **只做工具过滤**（NONE 置空、allowedFunctionNames 筛声明），从不落进 body。
   `wire_spec.py:12-31` 的 `TopLevelIndex` 也没有 TOOL_CONFIG 槽位：field 8「无当前构建写入点、
   语义未证实」，field 14 实测承载时区结构（bundle 语义标注为 ToolConfig 类字段，按真机原样回写）。
3. **AI Studio wire 整块到达的既有证据**：`stream_heartbeat.py:1-13` 模块注释明说「上游（尤其是
   bypass/预填充链路）在首块正文之前可能长时间没有任何事件：浏览器里整段生成完才吐字节」；
   `tests/unit/test_bypass.py:253-257、330-344` 的累积帧 fixture 是从 build2api 移植的模拟，仓库内
   无任何真实 dump 证明 AI Studio 发过累积帧。
4. **正面信号**：`逆向相关内容/independent-re-2026-10-20.md` L157 记录页面 bundle 的 functionResponse
   容器 `_.ct` 字段为 `name(1)/response(2)/id(3)/will_continue(4)`——`will_continue` 正是流式函数
   调用 proto 家族的字段，说明 MakerSuite schema 里这套结构**存在**，functionCall 侧有对应字段
   （partialArgs/willContinue）的概率不低。

---

## 四、可行性结论与设计方案

问题：「bypass 开启时仍然是真流式（逐块吐正文）」能不能做到？

### 方案 A：在 wire 请求上启用参数流（治本，一次实验定生死）

思路：MakerSuite RPC 与官方 v1/v1beta1 GenerateContent 同栈；官方既然有
`toolConfig.functionCallingConfig.streamFunctionCallArguments`，wire 数组编码里大概率存在对应槽位。

实验步骤（全部不动 src/，可先用脚本/手工改 body 验证）：

1. **找槽位**：候选一是 `_normalize_gemini_tool_config` 已产出的形状在某个未验证顶层下标
   （8/9/12）落位；候选二是 field 14 结构里嵌 functionCallingConfig（bundle 把 field 14 标注为
   ToolConfig 类，但真机 body 只有时区——需要抓一份**页面自身启用函数调用/结构化输出时**的真机
   body 对照，看它往哪写）。注意 schema 边界教训（WIRE_SPECIFICATION §6.3：注入未知下标会
   `HTTP 400: invalid argument`）——所以这是**廉价的单点探测**：400 即止损。
2. **看响应**：开启后观察 functionCall part（响应侧主槽位 index 3 / 变体 10，wire_parser.py
   L32-38）是否出现更长的数组（partialArgs/willContinue 追加在 `[name, args, call_id]` 之后），
   或是否开始发**累积帧**。用 `settings.dump_raw_response` 落盘原始流即可取证。
3. **若成功，改造点**（届时才动 src/）：wire 请求侧加 toolConfig 写入；wire_parser 识别
   partialArgs；BypassStreamExtractor 增加 ContentDecoder 式状态机（可直接移植对方 L72-214 的
   算法——单字符流、O(语法深度) 内存、只认根层 `content` 键）；`collapse_cumulative` 保留兼容
   累积快照帧。

置信度：中。正信号：will_continue 已在页面 schema 中、同一 serving 栈、官方功能覆盖 v1/v1beta1。
负信号：页面前端从不设置该开关，MakerSuite 的请求 proto 可能根本没映射这个字段（页面不需要）。
一次实验可定。

### 方案 B：多次小调用分片传输（治标，零 wire 改动）

思路：不指望 args 增量，改让模型**把回复拆成多次合成调用**，每次调用携带下一段正文。每个
functionCall part 仍然整块到达，但生成完一段就到一段，粒度=分段长度。

- 我们已有的支撑：`BypassStreamExtractor.feed()` 对同一回合内多次调用天然按序合并
  （bypass.py L937-945，每次 `merger.add_delta(_call_delta(...))`）；`collapse_cumulative`
  的 `strip_overlap` 分支处理「非累积的独立片段」的边界衔接。
- 需要改的只有指令文案（`bypass.py:78-100` `INSTRUCTION_SINGLE`）：从「call it once per reply」
  改为「每写完约 N 字就调用一次，content 只装新写的这一段；全部写完后再调用一次空收尾（或以
  finish 自然结束）」。注意保留「不要在调用之间写正文」的约束。
- 风险：模型配合度（分段纪律、不重复、不提前收尾）需要实测；每段有封帧开销；MakerSuite 对单回
  合调用次数是否有上限未知。vertex-at 反向选择（强制恰好一次、`parallel_tool_calls: false`）是
  为了可靠性，说明多次调用在官方端点是合法形状。

置信度：机制上通（中）；效果取决于模型（待实测）。**这是不动 wire 的最快验证路径。**

### 方案 C：thinking 通道承载正文（备选，先验证再考虑）

思路：thought part 与正文 part 走同一条 text 传输（wire_parser.py L148-161 的 thought 标志 +
L455-506 的 `candidate.thinking` 逐 chunk 累积），若思维链在我们的流里本来就是逐块到达的
（`streaming.py:151-152` 每 chunk yield 一次 "thinking" 事件——高概率增量），则让模型把完整回复
写进思考、可见正文给最小占位，网关在 bypass=thinking 模式下把 thinking 事件映射为 body 输出。

- 前提验证（无代码改动）：开 thinking 抓一次 dump，确认 thought text 是多个小帧而非一帧；
  确认审查/截断对 thought 通道的行为（这是关键未知数：若上游过滤同样扫 thought，此路不通）；
  确认模型愿意把全文塞进思考且可见答案不重复生成（否则双倍 token）。
- 置信度：低-中。不建议首选，作为 A/B 都失败后的探索项。

### 不做 / 保持不变

- 心跳机制保留：任何方案落地前它都是唯一兜底；vertex-at 在 buffered 回退下同样整块交付且无心跳。
- 不做自动续写/重试（与既有用户决策一致；vertex-at 同样明确不自动续写）。
- 不引入对方的三档 transport 配置面：我们只有单轮 single 口径，A/B 任一成功即可删掉「假流式」体感。

---

## 五、证据索引

对方仓库（`%TEMP%\vertex-at`）：
- `src/anti-truncation.mjs` L27-68（请求改写）、L72-214（ContentDecoder）、L216（cutShort）、
  L248-351（StreamRestorer）、L353-393（SSE 包装/断流报错）
- `src/vertex-native.mjs` L258-262（`streamFunctionCallArguments = true`）、L105-109（alt=sse URL）
- `src/vertex-text-stream.mjs` L9-76（partialArgs→OpenAI arguments 增量；L59 thought→reasoning）
- `src/vertex-protocol.mjs` L16、L39-41（不开关时 function calls arrive whole 的注释证据）
- `src/gateway.mjs` L222-227（三档 transport 判定）、L292-299（流式包装链）
- `src/completion-integrity.mjs` L80-182（完整性守卫）、L103-110（cutShort 跳过 JSON 校验）
- `test/anti-truncation.test.mjs` L162（逐字符 arguments fixture）
- `README.en.md` L42（Antigravity-gateway 致谢）、L73-82（"How streaming works"：兼容端点等完整
  参数则一次性返回 + 官方 Preview 链接）、L159、L264、L266

官方文档：Vertex AI "Introduction to function calling" → Streaming function call arguments 小节
（Preview；`streamFunctionCallArguments`；`partialArgs`/`PartialArg{jsonPath, stringValue, ...}`；
双层 `willContinue`；空 functionCall 收尾；Gemini 3+ 除 3.1 Flash-Lite；v1/v1beta1）。
2026-10-20 经 docs.cloud.google.com 抓取核实。

本仓库：
- `src/aistudio_api/application/bypass.py` L1-18（原理）、L78-112（指令/声明）、L118-121（工具名）、
  L161-216（注入）、L229-277（apply_bypass/wire tools/wire toolConfig 注释）、L334-546（修复链）、
  L552-633（去重合并）、L661-672（`_call_payload`：arguments 字符串只走一次性修复）、
  L691-712（collapse_cumulative）、L903-969（BypassStreamExtractor 及其累积快照假设）
- `src/aistudio_api/infrastructure/gateway/streaming.py` L94-150（流式 bypass 消费）、L205-209（尾块抢救）
- `src/aistudio_api/application/stream_heartbeat.py` L1-13（「整段生成完才吐字节」的注释证据）、L36-80
- `src/aistudio_api/infrastructure/gateway/wire_parser.py` L28-45（part 下标）、L131-232（parse_response_part）、
  L235-261（string args→`arguments` 键）、L455-506（thinking 逐 chunk）
- `src/aistudio_api/infrastructure/gateway/wire_spec.py` L12-31（顶层下标；无 toolConfig 槽位）
- `src/aistudio_api/infrastructure/gateway/wire_codec.py` L206-250（encode 不写 toolConfig）、L329-345（只过滤）
- `src/aistudio_api/application/chat_service.py` L387-426（toolConfig 语义形态）
- `src/aistudio_api/infrastructure/gateway/capture.py` L115-172（bypass 改写链路）
- `tests/unit/test_bypass.py` L253-257、L330-344（累积帧 fixture 为模拟数据）
- `逆向相关内容/independent-re-2026-10-20.md` L157（functionResponse `will_continue(4)` 一手证据）
