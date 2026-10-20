# vertex2openai 可续传 SSE（resumable.py）机制分析与移植评估

分析日期：2026-10-20
对象：`%TEMP%\vertex2openai`（bad-woman/vertex2openai，已克隆）——`app/resumable.py`（508 行）+ `app/routes/chat_api.py`（304 行）
我方链路：`src/aistudio_api/api/routes_openai.py` → `application/openai_bridge.py`（OpenAIStreamConverter）→ `application/api_service_gemini.py::stream_raw_events`（事件元组流）+ `application/stream_heartbeat.py`（1s 心跳）

---

## 结论（先看这里）

1. **这不是「多块拼接」，确认与被否决方案无关**。它不做任何内容级合并/去重/重叠计算：上游 SSE
   帧原样追加进一个有序列表（`ResumableBuffer.events`），每帧带 `id: <数组下标>`；断线重连时
   客户端报 `Last-Event-ID: N`，服务端从 `N+1` 起重放**同一份缓冲的同一批帧**。帧序号 = 数组
   下标，单调、不变、无空洞，去重逻辑天然干净（详见 §2.4）。
2. **心跳与帧 id 完全可共存**，对方就是这么做的：心跳帧**不进缓冲**（pump 直接跳过），由面向
   客户端的读取层在静默时按连接各自生成。我们的 `stream_with_heartbeat` 在事件元组层，移植后
   心跳职责移到读取层即可，两层互不干扰。
3. **断开后烧配额的代价有明确边界**：首块真实输出**之前**断开 → 1.5 秒宽限后主动杀掉上游
   （近乎零浪费）；首块输出**之后**断开 → 上游跑到自然结束（这正是功能目的：生成不浪费，重连
   拿全文）。硬上限：卡死不结束的流 7200s 后被清扫（默认 TTL 1800s 的 4 倍取大）；内存无字节
   上限（唯一缺口，见 §3.3）。
4. **值不值得做**：它解决的是「客户端断开导致整次生成白费 + 重连拿回全文」，不解决 bypass
   假流式（那是上游整块到达的问题，见上份报告）。若当前痛点是断线浪费生成，值得做，移植量
   中等（1 个新模块 + 改 `routes_openai.py` 的流式端点）；有一个必须正视的现实约束——**收益
   取决于客户端会不会带 Last-Event-ID 重连**（EventSource 系自动重连；SillyTavern 等用 fetch
   的客户端不会自动续传，只能靠手动重试或轮询端点）。置信度：机制解读高；移植可行性高；实际
   收益中（取决于客户端行为）。

---

## 一、机制全解（代码级）

### 1.1 总体架构

请求不再「上游→客户端」直通，而是拆成两半（chat_api.py L94-144 `_resumable_stream`）：

```
客户端 ← sse_from_buffer(buf, start_seq)   ← ResumableBuffer.events[]（有序帧列表）
                                              ↑ append
              pump_stream(buf, 上游SSE迭代器)  （后台 task，不挂在客户端连接上）
```

- 后台泵：chat_api.py L135-136 `asyncio.create_task(pump_stream(...))` + `register_pump`。
- 客户端侧只是缓冲的一个 reader（resumable.py L460-509 `sse_from_buffer`）。
- 断连感知被「智能化」：`DetachedRequest`（L367-388）包装真实 request，上游层查询
  `is_disconnected()` 时：缓冲已结束→真；已有模型输出→**假**（抑制断连中止）；有 reader→假；
  其余才透传真实连接状态（L374-385）。这是「断开后继续烧」的实现开关。

### 1.2 帧缓冲窗口多大/多久

- **单个缓冲无大小上限**：`events: List[str]` 全量保留整条回复的每一帧（L138），直到被清理。
- 默认配置（app/config.py L95-97）：`resumable_enabled=True`、`resumable_ttl_seconds=1800`、
  `resumable_max_streams=200`。
- 清理（`sweeper_loop` 每 60s 跑一次，L353-361；`sweep` L327-351）：
  - error 状态且无 reader：5 秒后删（L336-339）；
  - done 且无 reader：TTL（1800s）后删（L340-343）；
  - 一直 running 不结束：`max(TTL*4, 1800)` = **7200s** 硬上限后强删并取消泵（L344-347）；
  - 有 reader 的缓冲绝不清理（L333-334）。
- 容量淘汰（`_evict_locked` L311-325，在每次 create 时触发）：超 200 个先淘汰「已完成且无人读」
  中最旧的，还不够就淘汰最旧的（连泵一起取消）。
- 进程关闭时全量取消泵（app/main.py L76 `cancel_all_pumps`）。

### 1.3 stream_id 怎么算

`compute_stream_id`（resumable.py L40-51）：

- 优先取请求头 `x-resume-id` / `x-request-id` / `x-idempotency-key` / `idempotency-key`
  （按序第一个非空），`stream_id = "cid-" + sha256(api_key \0 头值 \0 指纹)[:32]`；
- 都没有则 `"req-" + sha256(api_key \0 指纹)[:32]`；
- 指纹 = **完整请求体 JSON**（chat_api.py L38-42 `request_obj.model_dump_json()`）。

含义：同一 api_key + 同一请求体（或同一幂等头）→ 同一缓冲。重连必须原样重发请求体才能命中。

### 1.4 断开后泵的行为与止损

- **泵本身不感知客户端**：`pump_stream`（L403-445）只管把上游迭代器读干净；正常结束补
  `[DONE]`（L416-417）、标 done（L424）；纯报错流（有 error 帧且无模型输出）标 error 并**立即
  drop 缓冲**，让下次重试重新请求上游而不是重放死错误（L419-422）；异常时往缓冲写错误帧 +
  `[DONE]` 再收尾（L430-438）。
- **首包前断开的止损**：`sse_from_buffer` 的 finally（L501-506）——最后一个 reader 离开且缓冲
  未结束且**尚无模型输出**时，起 `_cancel_if_abandoned`（L448-457）：睡 1.5 秒后复查，仍无
  reader、未结束、无输出 → `drop()`（连带取消泵，L291-294）。即**首块真实输出之前**的断开，
  上游最多多活 ~1.5 秒。
- **首包后断开**：无任何止损，上游跑到自然完，整份回复落缓冲等重连（这正是卖点）。

### 1.5 回放与去重（关键正确性）

- 帧号：`frame_with_id`（L397-400）给每条 `data:` 帧加 `id: <seq>`，seq = `buf.next_seq`
  （追加时的数组长度，L414/L417/L434-435）——**序号即下标**，追加-only，永不重排。
- 续传起点：`_resume_start_seq`（chat_api.py L45-58）读 `Last-Event-ID` 头 → `int+1`；也支持
  `?resume_from=N` 查询参数。没头则 0（全量重放）。
- 重连命中：`create_if_absent` 返回已存在缓冲 → 直接 `sse_from_buffer(buf, start)`，**不再请求
  上游**（chat_api.py L98-116），响应头带 `X-Resume-Id` / `X-Resumable-Replay: 1`（L61-65）。
- 防陈旧命中（chat_api.py L100-108）：已有缓冲若 kind 不符 / 已 `fully_delivered` / error /
  done 但无输出 → drop 重建（重新打上游）。`fully_delivered` 在 `[DONE]` 送达（resumable.py
  L508-509）或轮询读尽（chat_api.py L293-295）时置位——保证「收完的流不会被二次重放」。
- 心跳不污染序列：pump 跳过一切 keepalive 帧（L408-409，`is_keepalive_frame` L54-63 认 SSE
  注释帧和 `chatcmpl-keepalive` 空数据帧）；reader 静默时自己造心跳（L497-500，fake_streaming
  开时是合法空 chunk，否则 `: keep-alive` 注释；间隔 L472-477）。
- 旁路轮询：`GET /v1/resumable/{stream_id}?from=N&limit≤500&wait≤25`（chat_api.py L275-305）
  分片拉帧，客户端不重连 SSE 也能补齐。
- 非流式同样入缓冲（`_resumable_json` L147-257，kind=json，等 payload 或 600s 超时/断开即弃）。

结论：**没有内容拼接**。唯一的「内容嗅探」是帧分类（`is_error_frame` L66-90 / `is_real_output_frame`
L93-111，靠 JSON 里的 error 键和错误文案标记判断），只影响缓存生命周期，不改帧内容。

---

## 二、对我方四个问题的回答

### 2.1 跟被否决的「多块拼接」是不是一回事？

**不是。** 多块拼接是「让模型分多次产出、网关合并内容」（内容级 merge，有重叠/去重/顺序风险）；
这套是「同一次生成的帧落服务端缓冲、断点重放」（传输级 replay，帧原样、序号即下标、
`Last-Event-ID+1` 精确续传）。去重正确性依赖的唯一不变量是「events 只追加」，代码满足
（append L147-153 是唯一写点，snapshot/iter_from 只读）。干净。

### 2.2 与 stream_with_heartbeat 能否共存？

**能，且对方方案就是模板。** 我们现状：`routes_openai.py:176-184` 把 `HEARTBEAT_EVENT` 翻成
心跳帧。移植后职责重排：pump 消费 `stream_raw_events`（可保留 heartbeat 包装但**跳过
HEARTBEAT_EVENT 不入缓冲**——等价于对方的 `is_keepalive_frame` 过滤），reader 层静默超时发心跳。
心跳帧永不进缓冲、永不占序号，重连重放的都是真实帧。我方的事件是类型化元组（非 SSE 文本），
帧分类比对方的 JSON 嗅探更可靠：`("body", ...)`/`("thinking", ...)` 即 `has_model_output`，
错误走 `handle_attempt_exception` 的异常路径，不需要 `is_error_frame` 那套字符串匹配。

### 2.3 DetachedRequest 的代价与上限

- 首块输出前断开：≤1.5s 宽限后杀上游（resumable.py L448-457）——代价可忽略。
- 首块输出后断开：剩余生成继续烧（AI Studio 配额/浏览器占用），**收益换代价**：换来的是
  重连能拿全文、生成不白费。
- 硬上限：卡死流 7200s 强杀（L344-347）；容量淘汰（200 流）也会杀泵（L320-325）；进程关闭
  全杀（main.py L76）。
- **缺口**：无单缓冲字节上限（`events` 无界）。我方回复可达数十 MB（bypass args 上限 32MB），
  200 个缓冲的 RAM 上限=200×整条回复。移植时必须补一个 per-buffer 字节上限（对方没有）。
- 我方特有代价：上游是**单浏览器 CDP 会话**，detached 生成会占住页面直到结束；并发断开的
  detached 流会在这一个页面上排队/争用（vertex2openai 的上游是普通 HTTP SDK，无此约束）。
  这不推翻可行性，但意味着「断开后继续烧」的并发度受单浏览器吞吐限制。

### 2.4 值不值得做 + 接入点

**值得做的条件**：痛点是「客户端空闲计时器/网络抖动导致整次生成白费」。它不改善 bypass 假流式
（那是上游整块到达，见 vertex-anti-truncation-research.md）。注意收益前提：客户端要会重连——
EventSource 系自动带 Last-Event-ID；fetch 系（SillyTavern 等）不自动续传，只能整段重试（命中
running 缓冲即秒回放）或用轮询端点。若主力客户端都不重连，收益缩水为「重试不重复烧上游」。

**接入点（届时动代码，本次不动）**：

1. 新建 `src/aistudio_api/application/resumable.py`：移植 ResumableBuffer/ResumableStore/
   pump/读取层；缓冲的是**已转换的 OpenAI SSE 帧**（字符串），追加时打 `id: seq`；增加对方没有
   的 per-buffer 字节上限与 sweep 任务（挂 app 生命周期）。
2. `api/routes_openai.py:_stream_openai_chunks`（L154-260）改为 reader：消费缓冲 + 静默心跳
   （复用 `_heartbeat_frame` L78）；`chat_completions`（L90-151）按开关分流到 resumable 路径。
3. `application/api_service_gemini.py::stream_raw_events`（L674-717）不动——pump 直接消费它；
   `handle_attempt_exception` 的异常在 pump 里翻成缓冲内错误帧（等价对方 L430-438）。
4. 断开止损：reader 的 GeneratorExit 分支（现 L225-233）改为「无 body/thinking 输出则 1.5s 后
   drop 缓冲」；`stream_heartbeat.py` 的 producer 取消逻辑（L72-75）不再决定上游生死（泵持有
   上游迭代器）。
5. 可选：`GET /v1/resumable/{id}` 轮询端点 + `X-Resume-Id` 响应头（routes_openai.py）。
6. 配置：`config.py` 加 resumable_enabled / ttl / max_streams / 字节上限。

**不建议做的部分**：对方的 `is_error_frame`/`is_real_output_frame` JSON 嗅探（我方有类型化事件，
直接判 `event_type in ("body","thinking")` 即可）；fake_streaming 心跳帧（我方已有合法心跳帧）。

---

## 三、证据索引

对方（`%TEMP%\vertex2openai`）：
- `app/resumable.py`：L12 DONE_FRAME；L26-37 TTL/容量；L40-51 compute_stream_id；L54-63
  is_keepalive_frame；L66-111 错误/真实输出帧分类；L114-123 心跳帧生成；L126-248
  ResumableBuffer（append L147-153、wait_next L193-207、iter_from L209-226、mark_delivered
  L228-230）；L251-361 ResumableStore（drop 连带取消泵 L291-294、容量淘汰 L311-325、sweep
  L327-351、sweeper_loop L353-361）；L367-388 DetachedRequest；L397-400 frame_with_id；
  L403-445 pump_stream（跳过心跳 L408-409、补 DONE L416-417、纯错流即弃 L419-422、异常收尾
  L430-438）；L448-457 _cancel_if_abandoned（1.5s）；L460-509 sse_from_buffer（reader 计数、
  finally 止损调度 L501-506、delivered 标记 L508-509）
- `app/routes/chat_api.py`：L38-42 请求体指纹；L45-58 Last-Event-ID/resume_from；L61-65 响应头；
  L94-144 _resumable_stream（重放命中 L98-116、泵启动 L135-136）；L147-257 非流式缓冲；
  L260-272 端点分流；L275-305 轮询端点
- `app/config.py` L95-97 默认值；`app/main.py` L69 sweeper 启动、L76 关闭清泵

我方：
- `src/aistudio_api/api/routes_openai.py` L78-87 心跳帧、L90-151 chat_completions、L154-260
  _stream_openai_chunks（L176-184 心跳消费、L225-233 断开分支）
- `src/aistudio_api/application/stream_heartbeat.py` L36-80（L72-75 消费方退出即取消 producer——
  移植后此语义要移交给泵）
- `src/aistudio_api/application/api_service_gemini.py` L674-717 stream_raw_events（pump 的消费
  对象，单次不重试）
- `src/aistudio_api/application/openai_bridge.py` OpenAIStreamConverter（帧转换，缓冲对象即其输出）
