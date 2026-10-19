# Google AI Studio BotGuard 验证机制技术规范

本文档记录 Google AI Studio 官方 Web 客户端与 Google WAA (Web Attestation & Anti-abuse / BotGuard) 服务端的交互协议、数据链路与验证逻辑。

---

## 目录

- [1. 系统架构与交互拓扑](#1-系统架构与交互拓扑)
- [2. 阶段一：页面加载与服务初始化](#2-阶段一页面加载与服务初始化)
- [3. 阶段二：WAA 挑战握手与 Wasm 运行时加载](#3-阶段二waa-挑战握手与-wasm-运行时加载)
- [4. 阶段三：请求内容哈希与快照签名](#4-阶段三请求内容哈希与快照签名)
- [5. 阶段四：Wire 协议组包与浏览器 Fetch/XHR 重放](#5-阶段四wire-协议组包与浏览器-fetchxhr-重放)
- [6. 阶段五：服务端校验与统一报错响应](#6-阶段五服务端校验与统一报错响应)
- [7. 生命周期与会话约束](#7-生命周期与会话约束)
- [8. 核心报文结构参考](#8-核心报文结构参考)

---

## 1. 系统架构与交互拓扑

浏览器与 Google 后端服务集群的三方交互拓扑关系：

```mermaid
flowchart TD
    subgraph Browser ["用户浏览器环境 (Client Browser)"]
        UI["Google AI Studio UI<br/>(Angular SPA)"]
        BGSvc["BotGuardService<br/>(WAA 前端容器)"]
        BGWasm["BotGuard Wasm Runtime<br/>(动态加载的沙箱虚拟机)"]
        UI <--> BGSvc
        BGSvc <--> BGWasm
    end

    subgraph GoogleInfra ["Google 后端服务集群 (Google Infrastructure)"]
        MakerSuite["MakerSuite RPC 网关<br/>alkalimakersuite-pa.clients6.google.com"]
        WaaServer["WAA 反作弊网关<br/>waa-pa.clients6.google.com"]
        StaticCDN["Google 静态 CDN<br/>www.google.com/js/bg/..."]
    end

    UI -- "(1) 页面资源 & GenerateContent RPC" --> MakerSuite
    BGSvc -- "(2) 挑战初始化握手 (/Waa/Ping)" --> WaaServer
    BGWasm -- "(3) 下载与执行专属 Wasm 字节码" --> StaticCDN
```

### 完整交互时序

```mermaid
sequenceDiagram
    autonumber
    actor User as 用户 (User)
    participant Browser as AI Studio (Angular)
    participant WaaClient as BotGuardService
    participant WaaServer as WAA 网关 (waa-pa)
    participant StaticCDN as Google CDN (Static)
    participant Gateway as MakerSuite RPC (alkali)

    Note over User,Gateway: 【阶段一 & 二：页面启动与 WAA 挑战握手】
    User->>Browser: 访问 aistudio.google.com/u/0/prompts/new_chat
    Browser->>Browser: Angular 初始化并实例化 BotGuardService
    Browser->>WaaClient: initialize()
    WaaClient->>WaaServer: POST /Waa/Ping (携带 X-Goog-AuthUser 与 SAPISIDHASH)
    WaaServer-->>WaaClient: 下发会话挑战 Nonce 与 Wasm 资源文件名
    WaaClient->>StaticCDN: GET /js/bg/{hash}.js
    StaticCDN-->>WaaClient: 返回动态 BotGuard 虚拟机代码 (绑定 u/0 身份)

    Note over User,Gateway: 【阶段三 & 四：用户提问与动态签名】
    User->>Browser: 输入 Prompt 并点击 Run
    Browser->>Browser: 计算 content_hash = SHA256(Prompt + Images)
    Browser->>WaaClient: snapshot(content_hash)
    WaaClient->>WaaClient: 收集环境/时钟指纹 + HMAC(内容哈希 + 凭据)
    WaaClient-->>Browser: 返回加密快照 Token (!dXaldhL...)
    Browser->>Browser: 组装 Wire 数据包 (body[4] = snapshot)
    Browser->>Gateway: POST /GenerateContent (Fetch credentials='include')
    Note over Gateway: 【阶段五：服务端校验与响应】
    Gateway->>Gateway: 解密 Body[4] 快照，比对 HMAC 内容指纹
    Gateway->>Gateway: 校验快照内嵌 GAIA ID 与请求头 X-Goog-AuthUser
    Gateway-->>Browser: 校验通过，分块流式返回推理响应 (200 OK)
    Browser-->>User: 实时渲染并展示模型生成结果
```

---

## 2. 阶段一：页面加载与服务初始化

1. **页面与路由加载**：
   - 用户访问 `https://aistudio.google.com/prompts/new_chat?model=gemini-3.8-flash`（或子账号路由 `/u/{auth_user}/...`）。
   - HTML 页面引入 `boq-makersuite` JavaScript Bundle。
2. **依赖注入容器初始化**：
   - Angular 根组件启动时，DI 容器解析并实例化 `BotGuardService`（符号随版本变化，如 `_.QC`、`_.Vv`，注入令牌挂载于 root 作用域）：
     ```javascript
     // 结构参考 (类混淆名以实际运行环境为准):
     _.QC = class {
         constructor() {
             this.qb = _.n(_.Aq);
             this.F = !1;
             this.H = new Promise(a => {
                 var b = this;
                 return _.y(function*() {
                     b.qb.la && (yield b.initialize());
                     a();
                 });
             });
         }
     };
     ```
   - 从 Cookie 中读取 `SAPISID`，并根据当前 UNIX 时间戳计算 `SAPISIDHASH`。

---

### 2.3 引导：不需要往对话里发探针消息（binding 模式）

早期实现靠「往输入框写 `say '1'` 再点 Run」，让页面自己发一次 GenerateContent，借此同时拿到
`BotGuardService` 单例与真实请求头。这条路线有两个代价：往用户对话里塞一条假消息、烧掉一次生成配额。

现在的默认引导（`botguard_bootstrap.py`）改成三步，全程不碰发送：

1. **DI 探针**：`install_hooks.js` 包住 `default_MakerSuite.n`（页面自己的 Angular `inject`），
   在每次 `inject()` 返回时按**形状**认服务（`service.A.snapshot` 是函数）；旧构建另外包
   `TB.prototype.initialize / getMetadata`。**不能把 `dms.TB` 当前提**（2026-10-20 的构建已不导出它），
   否则探针一次都装不上；
2. **请求头采集**：页面自己的任意 `/$rpc/` 请求带的会话级头（`x-goog-api-key`、`x-aistudio-*`、
   `x-goog-ext-*`）合并保留，拼进请求模板（`content-length` / `host` / `connection` 丢掉）；
3. **探针签名**：用固定摘要签一次 proof —— 签得出来才认为这条链路可用；签不出来（连续 3 次
   `service_unavailable`，或 35 秒预算用尽）就**自动回退**老的 `say '1'` 引导，并把模式记在会话上，
   后续切号不再重复试探。

钩子必须用 `Page.addScriptToEvaluateOnNewDocument` 在 **document-start** 装上：页面脚本先跑的话，
Angular DI 已经造完服务单例，探针就再也接不到它了（`session.py` 在导航前调 `add_init_script`）。

#### 2.3.1 实测：移植 binding 到本仓库时踩到的四个坑（2026-10-19，Windows x64 + CloakBrowser 146）

真机（headless CloakBrowser 146.0.7680.177.4 + 真实账号 cookie）跑通的完整链路，四处必须一起改，
少一处就表现为「引导看起来成功、打请求必失败」：

1. **binding 引导成功后必须关掉 `interceptBootstrap`**（`botguard_bootstrap.try_binding_bootstrap`）。
   那个开关是给 page_send 路线拦页面第一条 GenerateContent 用的；binding 模式没有这一步，
   开关一直开着，于是我们自己用页面 XHR 重放 GenerateContent 时被自己的 fetch/XHR 钩子拦掉——
   现场是 `replay failed: timeout`（XHR 被 abort、status=0），五轮重试全部失败。
2. **`AistudioWireCodec.decode` 要容忍短模板**。binding 模板只有 `["models/xxx"]` 一个元素，
   而 decode 按固定下标取 contents/safety/generation_config，直接 `list index out of range`。
   现在 decode 先 `_ensure_len` 补齐，并对 generation_config 做 `None → []` 归一。
3. **没有统计对象的新账号要按「可用」处理**（`AccountRotator.is_account_available`）。
   原实现 `stats is None → False`，于是每个刚导入账号的**第一次**请求都会被编排器判成
   「当前账号不可用」，触发一次强制重建会话（白白多花一次导航+引导，然后失败）。
4. **binding 等待预算给足 35 秒**。页面冷启动时 BotGuard Wasm 要从 CDN 下载并完成握手，
   实测第一个探针 12 秒超时、第二次（约 21 秒处）才成功；预算给 20 秒会在 VM 刚就绪前放弃。

真机结果（同一台机器、同一份 cookie、headless）：引导 **3.8~9.8 秒**、`bootstrap_mode=binding`、
`capture_via=TB.initialize`、页面头 9 个（含 `x-goog-ext-*` 与 `x-aistudio-g1-tier`）；
`gemini-3.8-flash` 与 `gemini-4-argon` 的 `generateContent` / `streamGenerateContent` **全部 200**。
请求结束后页面输入框 `value` 长度为 0、页面里 `ms-chat-turn` 节点数为 0 —— 也就是确实
**没有往对话里发过任何消息**，只写过输入框。

#### 2.3.2 2026-10-20：页面构建不再导出 `dms.TB`，探针改按形状认服务

现场：`start.cmd` 启动两台 worker（profile `-w0`/`-w1` 都是新目录），每台都刷 4 条
`binding 引导探针签名失败: service_unavailable`，35 秒预算用尽后各自回退 page_send ——
每次启动白等 **35 秒 × worker 数**，还会往页面里发一条 `1` 探针。

页面侧只读诊断（空 profile 与旧 `profile` 的副本都试过，结果一致）：

```
dms=1  n=1  TB=0  tap=0  box=1  hdr=9  svc=0   # 40 秒内始终抓不到服务
dms.n(dms.TB)  ->  err:NG0203                  # inject() 不能在注入上下文外调用
```

结论：`default_MakerSuite.TB` 已经不在新构建的导出里，`installServiceTap` 里
`typeof dms.TB !== 'function'` 直接 return false —— **旧 DI 探针在新构建上永远装不上**，
`service` 只能等页面自己发消息触发 snapshot 时才被包装器顺手接住（也就是 page_send 兜底能工作、
binding 模式必失败的原因）。

修复（`install_hooks.js`）：只要求 `dms.n`；包 `inject()` 后对**返回值**做形状匹配
（`obj.A` 是对象且 `obj.A.snapshot` 是函数即认为是 BotGuardService），`capture_via=inject(shape)`；
`TB.prototype` 包装保留给旧构建。另加 `SERVICE_UNAVAILABLE_BAIL = 3`：连续 3 次
`service_unavailable` 直接回退，不再等满 35 秒（防以后再改构建时又白等）。

真机验证（2026-10-20 04:2x，headless，空 profile）：

- `bootstrap_mode=binding`、`capture_via=inject(shape)`、**引导 4.2 秒**、页面头 7 个；
- 300KB binding 的 proof 生成 **0.68 秒**（`snapshot=1512 chars`、`box=synced`）；
- 全程没有点发送 / 按 Enter，输入框在验证后清空。

顺带排掉一个猜测：把 300KB binding 写进输入框后，页面主线程 RTT 仍为 3~65ms（60 秒观测无 >1s
的卡顿），所以「写 binding 把页面冻住、导致请求迟迟发不出去」不成立。

---

## 3. 阶段二：WAA 挑战握手与 Wasm 运行时加载

在 `BotGuardService.initialize()` 中，前端与 Google WAA 服务完成握手：

```javascript
initialize() {
    var a = this;
    return _.z(function*() {
        var b = new QVa; // waa-pa 远程 RPC 通信客户端
        a.A = new LUa({
            fetcher: new ura(b, () => a.getMetadata())
        });
        yield a.A.u6a; // 等待 Wasm 运行时握手完成并就绪
    });
}
getMetadata() {
    var a;
    return {
        "X-Goog-Api-Key": "AIzaSyBGb5fGAyC-pRcRU6MUHb__b_vKha71HRE",
        Authorization: (a = _.bq([])) != null ? a : "",
        "X-Goog-AuthUser": this.wb.Bo.toString()
    };
}
```
### 3.1 握手信令交互 (`/Waa/Ping`)

请求发往 `https://waa-pa.clients6.google.com/$rpc/google.internal.waa.v1.Waa/Ping`：

- **请求头示例**：
  ```http
  X-Goog-Api-Key: AIzaSyBGb5fGAyC-pRcRU6MUHb__b_vKha71HRE
  Authorization: SAPISIDHASH <timestamp>_<sha1_hash> ...
  X-Goog-AuthUser: 0
  Content-Type: application/json+protobuf
  ```
- **请求负载**：包含客户端接入 ID（如 `client: "lmnUSbltwc5ULv48iKLX"`）与环境参数。
- **响应载荷**：WAA 服务返回会话挑战 Nonce 与专属 JS/Wasm 资源哈希。

### 3.2 动态加载 Wasm 虚拟机

前端向 Google CDN 请求对应哈希的脚本：

```http
GET https://www.google.com/js/bg/gBetl7I-09yp6c3Nmm4ajwTxhDHStoNbVEOK3L3hfg4.js
```

脚本执行并实例化核心虚拟机。此时虚拟机内部已绑定当前会话的 GAIA ID、Nonce 与 `auth_user` 凭据。

---

## 4. 阶段三：请求内容哈希与快照签名

当用户提交提示词触发请求时，AI Studio 底层执行管线：

```javascript
// 请求组装与签名管线
t = yield _.Nv(requestProto);        // 1. 递归提取 contents 并计算 SHA-256 内容哈希
t = yield _.Dp(this.zc, t);          // 2. 调用快照函数生成 Token
_.l(requestProto, 5, t);             // 3. 将 Token 注入 Protobuf 字段 5 (Wire 数组 body[4])
```

1. **计算内容哈希 (`_.Nv`)**：
   提取请求 `contents` 数组中的所有文本与内联图片数据，通过 `_.rj` 映射为字符串，按顺序以单个空格 `" "` 拼接后计算 SHA-256 Hex 摘要：
   ```javascript
   _.Nv = function(requestProto) {
       return _.z(function*() {
           var b = _.Mv(requestProto).flatMap(c => c.eg()).map(_.rj);
           return _.dv(b.join(" "));
       });
   };

   _.rj = function(part) {
       switch(_.oj(part)) {
           case 2: // text 文本块
               return part.getText();
           case 3: // inline_data 图片数据 (Base64 字符串)
               let b, c;
               return (b = _.pj(part)) == null ? void 0 : (c = b.getData()) == null ? void 0 : c.Xe();
           case 6: // file_data 引用资源
               let d;
               return (d = _.qj(part)) == null ? void 0 : d.getId();
           default: // functionCall / functionResponse 等非文本
               return "";
       }
   };

   _.dv = function(str) {
       var a = (new TextEncoder).encode(str);
       var b = new _.pqa; // SHA-256 算法实现
       b.update(a);
       return maa(b.digest()); // 转换为 16 进制 hex string
   };
   ```

2. **生成 BotGuard 快照 (`_.jp` / `_.Dp` / `snapKey`)**：
   前端调用被检测到的专属签名函数（在 `default_MakerSuite` 中动态匹配注册）：
   ```javascript
   // 实测最新版本 (2026-09-28 live 验证):
   _.jp = function(a, b) {
       return _.y(function*() {
           yield a.H; // 等待 BotGuardService 初始化 Promise
           return a.A
               ? (yield Vqb(a.A), a.A.snapshot({ Bcc: { content: b } }))
               : "";
       });
   };
   // 历史版本混淆符号参考:
   _.Dp = function(service, contentHash) {
       return _.z(function*() {
           yield service.H;
           return service.A
               ? (yield JUa(service.A), service.A.snapshot({ e9b: { content: contentHash } }))
               : "";
       });
   };
   ```
   > **符号混淆与动态匹配**：Google 构建版本更新时会重置混淆名（如类名 `_.QC`、签名函数 `_.jp`、参数键 `Bcc` 等），因此系统通过源码特征（`.snapshot`、`content`、`yield`）动态定位函数，避免硬编码混淆符号。
   >
   > **生命周期与状态**：
   > - 快照 Token 与单次请求内容及时间戳绑定，不跨请求复用。
   > - 会话凭据与 Nonce 由服务端动态下发，账号切换时重新初始化。
3. **虚拟机内部处理**：
   - 采集浏览器指纹（屏幕分辨率、语言列表、时钟精度、WebGL 上下文特征等）；
   - 使用握手会话秘钥对 `contentHash` 与环境特征进行加密并计算 HMAC 签名；
   - 生成以 `!` 开头、长度约为 1500~1650 字符的 Base64 快照 Token（例如 `!dXaldhLNAAa1dSU5lXVCmOax...`）。

### 4.3 输入框 binding：WAA proof 与输入框状态强绑定（2026-10-19 实测）

把 `sha256(contents)` 交给快照函数**并不足以**让上游认这份 proof —— 签名器还要求页面的
提示词输入框里此刻就是那段内容。机制来源是参考实现
`Catiecli Playground Executor 2.4.9` 与同作者的 `gemini2api` 执行器 `v2.0.0`，两边都是同一句话：

> binding 写入输入框（WAA proof 与输入框状态强绑定）

省掉这一步的后果不是报错，而是两种更难查的形态：

| 现象 | 说明 |
|---|---|
| `403 [7] The caller does not have permission` | proof 签出来了，但上游判定与输入框状态不一致 |
| `snapshot()` 静默挂死 | 页面 zone 冻结/状态不一致时该 Promise 可能永不回调（实测挂过 5 分钟） |

实现（`src/aistudio_api/infrastructure/browser/js/snapshot_generate.js`）：

1. **写值走原生 setter**：`Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(box, binding)`。
   直接赋 `.value` 时 Angular 的受控组件收不到变更。
2. **补事件**：派发 `InputEvent('input', { inputType: 'insertText', data: binding })` 与 `Event('change')`。
3. **确认页面认了**：`box.value === binding` 才算同步成功；8 秒预算内每 150ms 重试，
   每两次重试做一次**焦点扰动**（`focus → focusin → blur → focusout`）——长时间后台运行的页面，
   Angular 的合成事件不再触发变更检测，表现为「页面卡住，用户手动点一下输入框才恢复」。
4. **binding 就是哈希原文**：`digest = sha256(binding)`。写进输入框的字符串与算摘要的原文必须
   逐字一致，否则服务端的 HMAC 比对不过。
5. **超时与重试**：`snapshot()` 可能永不 resolve，所以用 20s / 30s / 30s 三轮硬超时，
   每轮失败先做焦点扰动、再补一次 binding，然后重签。
6. **生命周期**：写值 → 签名 → 发请求 → 请求结束清空（`js/clear_binding.js`）。
   **只写值与清值：绝不点发送、绝不按 Enter、绝不替页面发任何消息。**

> 输入框不存在（停在 dashboard / apps 页）时不做 binding、照旧签名；写入失败（框架锁值）
> 也只在日志/状态里如实记 `sync_failed`，不因此让请求失败——但那样可能换来上面的 403，
> 所以 `prompt_state.js` 把「输入框在不在、页面可见/焦点、服务与公共头就绪没」一起报出来。

---

## 5. 阶段四：Wire 协议组包与浏览器 Fetch/XHR 重放

```json
[
  "models/gemini-3.8-flash",
  [
    [
      [[null, "用户输入的提示词内容"]],
      "user"
    ]
  ],
  [[null, null, 7, 5], [null, null, 8, 5], [null, null, 9, 5], [null, null, 10, 5]],
  [null, null, null, 128, 0.5, 0.8, 16],
  "!dXaldhLNAAa1dSU5lXVCmOax...",
  null,
  null,
  null,
  null,
  null,
  1,
  null,
  null,
  [[null, null, "Asia/Tokyo"]]
]
```

数组索引与 Google Protobuf 字段序号的精确映射（`Index = FieldNumber - 1`）：

| 数组索引 | Proto 字段号 | 字段名称 | 类型与说明 |
|---|---|---|---|
| **`0`** | `Field 1` | `model` | 目标模型名称（如 `"models/gemini-3.8-flash"`、`"models/gemini-3.7-flash"`） |
| **`1`** | `Field 2` | `contents` | 结构化对话轮次与多模态数据数组 |
| **`2`** | `Field 3` | `safety_settings` | 安全审核阈值配置数组 |
| **`3`** | `Field 4` | `generation_config` | 生成控制参数（temperature, topP, topK, maxTokens 等） |
| **`4`** | `Field 5` | `snapshot` | **BotGuard 快照签名 Token（以 `!` 开头）** |
| **`5`** | `Field 6` | `system_instruction` | 系统指令角色与提示词块 |
| **`6`** | `Field 7` | `tools` | 工具声明列表（Google 搜索、代码执行、Google Maps 等） |
| **`7`** | `Field 8` | `evergreen_model_uri` | 动态常青模型 URI（如 `"models/gemini-..."`） |
| **`10`** | `Field 11` | `request_flag` | 请求行为标记（固定值 `1`） |
| **`11`** | `Field 12` | `cached_content` | 上下文缓存资源名称 |
| **`13`** | `Field 14` | `location` | 用户位置与时区声明（如 `[[null, null, "Asia/Tokyo"]]`） |
### 浏览器内 Fetch/XHR 发送

```http
POST https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent
Host: alkalimakersuite-pa.clients6.google.com
Content-Type: application/json+protobuf
X-User-Agent: grpc-web-javascript/0.1
X-Goog-Api-Key: AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs
X-Goog-AuthUser: 0
Authorization: SAPISIDHASH 1789397237_0093f004...
Origin: https://aistudio.google.com
Referer: https://aistudio.google.com/
Cookie: SID=...; HSID=...; SSID=...; SAPISID=...; __Secure-1PAPISID=...
```

- 设置 `credentials: 'include'`（或 `xhr.withCredentials = true`），自动附加浏览器内存储的完整会话 Cookie。

---

## 6. 阶段五：服务端校验与统一报错响应

服务端网关接收到请求后的审计与校验链路：

```mermaid
flowchart TD
    Req["GenerateContent 请求<br/>(Header + Body)"] --> Step1

    subgraph GatewayValidation ["服务端校验流程"]
        Step1["1. 解密 Body[4] 快照载荷"]
        Step2["2. 校验 HMAC(Body[1] Contents 哈希)"]
        Step3["3. 比对快照 GAIA ID 与 X-Goog-AuthUser 会话"]
        Step4["4. 校验环境与时钟指纹"]

        Step1 -->|解密成功| Step2
        Step2 -->|哈希匹配| Step3
        Step3 -->|身份一致| Step4
    end

    Step1 -->|解密失败| Reject["统一拒绝：HTTP 403<br/>[,[7,'The caller does not have permission']]"]
    Step2 -->|哈希不匹配| Reject
    Step3 -->|身份冲突| Reject
    Step4 -->|指纹异常| Reject
    Step4 -->|校验通过| Accept["HTTP 200 OK<br/>分块流式返回推理数据"]
```

### 6.1 服务端报错特征与网关自愈机制

当 BotGuard 校验失败（快照缺失、快照过期、内容哈希不一致或身份冲突）时，服务端返回 **HTTP 403**，响应体为 JSON 数组 `[,[7,"The caller does not have permission"]]`（对应 gRPC 状态码 7 `PERMISSION_DENIED`）。

网关针对该报错建立两级自愈机制：
1. **多账号故障转移（Failover）**：网关自动对触发 403 的故障账号设置 300 秒短时隔离（`auth_cooldown`），优先调度下一个健康账号，避免在受限账号上无效重试。
2. **单账号在位重建（In-Place Recovery）**：当无备用账号可用时，网关清空请求模板缓存，重载 Cookie 并通过 `ensure_botguard_service(force_refresh=True)` 重新执行页面导航与 WAA 挑战握手，恢复服务可用性。

---

## 7. 生命周期与会话约束

1. **时效性与内容绑定**：快照 Token 内包含时间戳与挑战 Nonce，且与特定请求的内容哈希强绑定。内容修改后需重新计算哈希并生成新快照。
2. **后台休眠与保活**：当标签页处于后台时，虚拟机可能进入休眠。页面重新切回前台时，`visibilitychange` 事件会触发恢复（`service.A.CE()`）；若已失效则重新执行握手。
3. **多账号身份隔离**：每个子账号（`u/0`, `u/1`...）对应独立的身份上下文与 Wasm 实例，切换账号需通过 `/u/{auth_user}/` 专属路由切换并加载对应账号上下文。
4. **输入框 binding 的生命周期**：proof 与输入框状态强绑定（见 §4.3），所以「写 binding → 签名 → 发请求」这一小段必须独占输入框：
   `generate_snapshot` 用 `_snapshot_lock` 串行化签名窗口，请求结束在 `finally` 里清空输入框。
   并发请求不会互相覆盖（覆盖只会发生在签名窗口内，那正是锁保护的范围），但**别把输入框里的内容当成请求参数**——
   它只服务于 proof，请求内容始终是 wire body 里的 `contents`。

## 8. 核心报文结构参考

### 8.1 SAPISIDHASH 鉴权格式

```text
Authorization: SAPISIDHASH <TS>_<HASH1> SAPISID1PHASH <TS>_<HASH2> SAPISID3PHASH <TS>_<HASH3>
```

计算逻辑：
```text
HASH = SHA1(timestamp + " " + SAPISID_VALUE + " https://aistudio.google.com")
```

### 8.2 快照签名 Token 特征

| 属性 | 特征 |
|---|---|
| **前缀** | 固定以感叹号 `!` 开头 |
| **编码** | URL-Safe Base64 |
| **长度** | 约 1500 ~ 1650 字符 |
| **内容** | 包含硬件与环境指纹、内容哈希 HMAC 与会话 Nonce |
