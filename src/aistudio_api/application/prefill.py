"""预填充（prefill）兼容。

问题：SillyTavern 一类前端习惯让对话以一条 assistant/model 消息结尾，用它锚定
思维链格式（`<thinking>` 已开个头），期望模型从那里接着写。但 Gemini 3.x 会以
400 拒绝「以 model 回合结尾」的请求。

做法：保留那条 model 回合，再追加一条 **新的 user 回合** 作为续写指令（绝不能并进
上一轮——工具结果轮混进 text part 会被签名校验器拒掉）。模型接着往下写，网关再把
预填充内容拼回响应开头，客户端看到的是一段连续文本。

对应主项目 build2api 的 `src/prefill.js`，语义逐条对齐；本模块只做纯函数，不发请求。
"""

from __future__ import annotations

import re

from aistudio_api.infrastructure.gateway.wire_types import AistudioContent, AistudioPart

DEFAULT_NUDGE = (
    "[继续] 从你上一条消息的断点处无缝往下写，不要重复已经写过的内容，"
    "不要任何前言、解释或复述。直接续上。"
)

# 内部 wire 用 "model"；OpenAI 兼容端口可能原样带来 "assistant"
ASSISTANT_ROLES = frozenset({"model", "assistant"})

# 开标签 / 闭标签统一匹配：`</`、标签名、属性段、自闭合斜杠。
# 属性段不许跨行：正文里的裸尖括号经常凑出一个假开标签（`<peip\n后续 -> 呼应`）。
TAG_RE = re.compile(r"<(\/?)([A-Za-z][A-Za-z0-9_\-.:]*)((?:[ \t][^>\n]*)?)(\/?)>")

# HTML 空元素：本来就没有闭标签，写成 `<br>` 不算未闭合
VOID_TAGS = frozenset(
    {
        "br",
        "hr",
        "img",
        "input",
        "meta",
        "link",
        "area",
        "base",
        "col",
        "embed",
        "source",
        "track",
        "wbr",
    }
)

# 判定重叠的最小长度：漏掉一段很短的重复只是观感问题，误判会真的删掉模型写的内容
MIN_OVERLAP = 8
MAX_DEDUP_WINDOW = 4096

# 前一个字符是词字符时，那个 `<` 不是标签开头（`a<b`、`List<String>`）
_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9_]")


def _parts_of(content: object) -> list[AistudioPart]:
    """contents 来自客户端，parts 不保证是数组。"""
    parts = getattr(content, "parts", None)
    return list(parts) if isinstance(parts, list) else []


def _is_assistant(content: object) -> bool:
    return str(getattr(content, "role", "") or "") in ASSISTANT_ROLES


def is_text_only_content(content: object) -> bool:
    """这条 content 是「纯文本的模型回合」吗——能不能当预填充续写。

    思维链 part（thought=True）不算内容也不算障碍：客户端偶尔会把上一轮的推理
    一起回传，但请求仍然以 model 回合结尾，3.x 照样 400。工具调用、图片结尾仍然
    不算：那不是「让模型接着写文本」。
    """
    parts = _parts_of(content)
    if not parts:
        return False
    saw_text = False
    for part in parts:
        if not isinstance(part, AistudioPart):
            return False
        if part.thought:
            continue
        if part.text is None:
            return False
        saw_text = True
    return saw_text


def join_text(content: object) -> str:
    """拼预填充正文；思维链不是预填充内容（拼进去会被当成正文回给客户端）。"""
    return "".join(
        str(part.text)
        for part in _parts_of(content)
        if isinstance(part, AistudioPart)
        and part.text is not None
        and not part.thought
    )


def _has_substance(part: object) -> bool:
    if not isinstance(part, AistudioPart):
        return False
    if part.text is not None and str(part.text).strip() != "":
        return True
    return any(
        getattr(part, name, None) is not None
        for name in (
            "inline_data",
            "file_id",
            "function_call",
            "function_response",
        )
    )


def last_non_empty_content(
    contents: list[AistudioContent],
) -> tuple[int, AistudioContent | None]:
    """找最后一条非空 content 及其下标。"""
    for index in range(len(contents) - 1, -1, -1):
        content = contents[index]
        if any(_has_substance(part) for part in _parts_of(content)):
            return index, content
    return -1, None


def detect_unclosed_tag(text: str) -> str | None:
    """找出文本结束时还开着的最内层标签（如 `<thinking>`）。

    闭标签**隐式关掉它上面所有还开着的标签**，和浏览器解析 HTML 一致：思维链里
    经常出现「输出 `<content>` 再 `<details>`」这类提到标签名但不闭合的写法，
    它们到本块的闭标签处就算收了口。只有真写到一半被截断，末尾才会留下还开着的标签。

    开标签前面紧挨着字母数字时一律不认：`if a<b and b>c`、`List<String>` 都能凑出
    形式合法的开标签，而误判的代价是守卫命令模型输出一个 `</b>` 这样的假闭标签。
    """
    stack: list[str] = []
    # lower_tag → 该标签在栈里的下标（递增）。配对查 O(1)，别从栈顶往下扫：
    # 预填充文本客户端可控，`<a>`×N + `</b>`×N 曾让 28KB 输入卡住事件循环。
    indices_of: dict[str, list[int]] = {}

    for match in TAG_RE.finditer(text):
        closing = match.group(1)
        tag = match.group(2)
        self_closing = match.group(4)
        lower = tag.lower()

        if closing:
            positions = indices_of.get(lower)
            if positions:
                at = positions[-1]
                # 闭标签隐式关掉它上面所有还开着的标签；每个开标签最多被弹一次
                while len(stack) > at:
                    popped = stack.pop().lower()
                    popped_positions = indices_of[popped]
                    popped_positions.pop()
                    if not popped_positions:
                        del indices_of[popped]
            continue

        # 闭标签紧跟正文是正常的（`hello</p>`），只对开标签做这个判断
        if match.start() > 0 and _WORD_CHAR_RE.fullmatch(text[match.start() - 1]):
            continue
        if self_closing or lower in VOID_TAGS:
            continue
        indices_of.setdefault(lower, []).append(len(stack))
        stack.append(tag)

    return stack[-1] if stack else None


def build_nudge(
    prefill: str,
    *,
    nudge: str = "",
    cot_guard: bool = True,
) -> str:
    """构造续写指令；预填充停在未闭合标签里时追加「先补完并闭合它」。"""
    base = (nudge or "").strip() or DEFAULT_NUDGE
    if not cot_guard:
        return base
    tag = detect_unclosed_tag(prefill)
    if not tag:
        return base
    return (
        f"{base}\n注意：你上一条消息停在未闭合的 <{tag}> 内部。"
        f"请先在 <{tag}> 里把内容写完并输出 </{tag}>，然后再继续写正文。"
    )


def _nudge_content(text: str) -> AistudioContent:
    return AistudioContent(role="user", parts=[AistudioPart(text=text)])


def _ensure_user_last(
    contents: list[AistudioContent],
    *,
    nudge: str,
    cot_guard: bool,
    requires_user_last_turn: bool,
) -> None:
    """没有预填充可用、但请求仍以 model 轮结尾时，补一条续写指令。

    典型形状是「只有空白的 model 结尾」：它当不了预填充，可 3.x 看到「以 model
    结尾」照样 400，而这个 400 会原样打给用户。
    """
    if not requires_user_last_turn or not contents:
        return
    if not _is_assistant(contents[-1]):
        return
    contents.append(_nudge_content(build_nudge("", nudge=nudge, cot_guard=cot_guard)))


def apply_prefill(
    contents: list[AistudioContent],
    *,
    enabled: bool = True,
    cot_guard: bool = True,
    nudge: str = "",
    requires_user_last_turn: bool = True,
) -> str:
    """就地改写 contents 以适配预填充。

    @returns 预填充文本；空字符串表示本次请求没有预填充。
    """
    if not enabled:
        return ""

    index, content = last_non_empty_content(contents)
    if index == -1 or content is None or not _is_assistant(content):
        _ensure_user_last(
            contents,
            nudge=nudge,
            cot_guard=cot_guard,
            requires_user_last_turn=requires_user_last_turn,
        )
        return ""
    # 工具调用、图片结尾不算预填充
    if not is_text_only_content(content):
        return ""

    prefill = join_text(content)
    if not prefill.strip():
        _ensure_user_last(
            contents,
            nudge=nudge,
            cot_guard=cot_guard,
            requires_user_last_turn=requires_user_last_turn,
        )
        return ""

    # 2.5 及更早的模型原生支持以 model 结尾，直接放行让它自己续写
    if not requires_user_last_turn:
        return prefill

    # 末尾之后若还有空 content，先截掉，保证 nudge 紧跟预填充
    if index < len(contents) - 1:
        del contents[index + 1 :]

    # 必须**另起一轮 user**：把文本并进最后一条（可能是工具结果轮）会破坏交替/签名
    contents.append(_nudge_content(build_nudge(prefill, nudge=nudge, cot_guard=cot_guard)))
    return prefill


def longest_overlap(
    prefill: str,
    output: str,
    min_overlap: int = MIN_OVERLAP,
    window: int = MAX_DEDUP_WINDOW,
) -> int:
    """prefill 的后缀与 output 的前缀的最长重叠长度（KMP，O(窗口)）。

    别写成「从长到短逐个 endswith」——那是二次方的，4000 字 37ms、64000 字 11.4 秒，
    而两个串都由客户端提供，一个请求就能卡死事件循环。
    """
    if not prefill or not output:
        return 0
    size = min(window, len(prefill), len(output))
    if size < min_overlap:
        return 0

    head = output[:size]
    tail = prefill[len(prefill) - size :]
    combined = head + "\u0000" + tail
    fail = [0] * len(combined)
    for i in range(1, len(combined)):
        k = fail[i - 1]
        while k > 0 and combined[i] != combined[k]:
            k = fail[k - 1]
        if combined[i] == combined[k]:
            k += 1
        fail[i] = k

    length = fail[-1]
    # 分隔符万一出现在正文里，匹配可能跨过它，长度就不可信了
    if length > size:
        return 0
    return length if length >= min_overlap else 0


def strip_overlap(prefill: str, output: str) -> str:
    """去掉模型重复的那段预填充（模型经常无视「不要重复」把尾巴抄一遍）。"""
    if not prefill or not output:
        return output
    # 整段重复：直接切掉
    if output.startswith(prefill):
        return output[len(prefill) :]
    length = longest_overlap(prefill, output)
    return output[length:] if length > 0 else output


def rejoin(prefill: str, output: str) -> str:
    """非流式：把预填充拼回响应开头。"""
    if not prefill:
        return output
    return prefill + strip_overlap(prefill, output or "")


class StreamDeduper:
    """流式去重器。

    预填充已经先发给客户端了，接下来判断模型开头有没有重复它。判断需要看到足够多
    的字符，但又不能一直缓冲（拖慢首字延迟）：只在「还可能构成更长重叠」时继续缓冲。
    最终的重叠必然是 prefill 的某个后缀，而 buffer 是模型输出的前缀，所以
    「还有可能」等价于 **buffer 出现在 tail（prefill 尾部窗口）里**。
    """

    def __init__(self, prefill: str) -> None:
        self.prefill = prefill or ""
        self.buffer = ""
        self.done = not self.prefill
        self.window = min(len(self.prefill) + 32, MAX_DEDUP_WINDOW)
        # 重叠必然是 prefill 的后缀，最长也就一个窗口，所以只看尾巴这一段
        self.tail = (
            self.prefill[-self.window :]
            if len(self.prefill) > self.window
            else self.prefill
        )

    def feed(self, text: str) -> str:
        """喂入一段模型输出，返回可以立即发给客户端的文本。"""
        if self.done:
            return text
        self.buffer += text
        if len(self.buffer) >= self.window or not self._still_ambiguous():
            return self._resolve()
        return ""

    def flush(self) -> str:
        """流结束：把缓冲里剩下的放出来。"""
        if self.done:
            return ""
        return self._resolve()

    @property
    def buffered(self) -> int:
        return 0 if self.done else len(self.buffer)

    def _resolve(self) -> str:
        self.done = True
        out = strip_overlap(self.prefill, self.buffer)
        self.buffer = ""
        return out

    def _still_ambiguous(self) -> bool:
        if not self.buffer:
            return True
        if len(self.buffer) > len(self.tail):
            return False
        return self.buffer in self.tail


__all__ = [
    "ASSISTANT_ROLES",
    "DEFAULT_NUDGE",
    "MAX_DEDUP_WINDOW",
    "MIN_OVERLAP",
    "TAG_RE",
    "VOID_TAGS",
    "StreamDeduper",
    "apply_prefill",
    "build_nudge",
    "detect_unclosed_tag",
    "is_text_only_content",
    "join_text",
    "last_non_empty_content",
    "longest_overlap",
    "rejoin",
    "strip_overlap",
]
