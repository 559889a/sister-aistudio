"""流式 SSE 心跳包装器。

上游（尤其是 bypass/预填充链路）在首块正文之前可能长时间没有任何事件：浏览器里
整段生成完才吐字节、或者 capture/签名阶段卡住几十秒。客户端的「流式空闲计时器」
一到期就主动断开，网关这边按客户端断开处理，已经花掉的生成全部白费。

这里用 producer task + queue 把「上游事件」和「时间」解耦：主循环等 1 秒还没事件
就产出一个 :data:`HEARTBEAT_EVENT`，由路由层换成各自协议的合法心跳帧
（OpenAI：空 delta 的 chat.completion.chunk；原生 Gemini：`\": ping\\n\\n\"` 注释行）。

间隔固定 1 秒、不做设置项：它没有「该关掉」的场景（与主项目 SSE_HEARTBEAT_MS 同思路，
但主项目 bypass 用 500ms、假流式用 1000ms；这里统一 1s）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator

HEARTBEAT_EVENT = "__heartbeat__"
HEARTBEAT_INTERVAL_S = 1.0

_DONE = object()


class _ProducerFailure:
    """producer 里冒出来的异常：原样交给消费方重新抛出（保留原类型/堆栈）。"""

    __slots__ = ("error",)

    def __init__(self, error: BaseException) -> None:
        self.error = error


async def stream_with_heartbeat(
    source: AsyncIterator[tuple[str, object]],
    *,
    interval_s: float | None = None,
) -> AsyncGenerator[tuple[str, object], None]:
    """把上游事件流包一层心跳：超过 ``interval_s`` 没有事件就 yield 心跳事件。

    - 正常事件原样透传，顺序不变；
    - 上游异常在消费方再次抛出（错误帧由上层决定怎么渲染）；
    - 消费方提前退出（客户端断开）时取消 producer，避免上游继续读。
    """
    interval = HEARTBEAT_INTERVAL_S if interval_s is None else float(interval_s)
    queue: asyncio.Queue[tuple[str, object] | _ProducerFailure | object] = asyncio.Queue()

    async def _produce() -> None:
        try:
            async for item in source:
                await queue.put(item)
        except Exception as exc:
            await queue.put(_ProducerFailure(exc))
        finally:
            try:
                close = getattr(source, "aclose", None)
                if close is not None:
                    await close()
            except Exception as exc:
                queue.put_nowait(_ProducerFailure(exc))
            finally:
                queue.put_nowait(_DONE)

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield (HEARTBEAT_EVENT, None)
                continue
            if item is _DONE:
                break
            if isinstance(item, _ProducerFailure):
                raise item.error
            if isinstance(item, tuple):
                yield item
    finally:
        if not producer.done():
            producer.cancel()
        with contextlib.suppress(BaseException):
            await producer
        if producer.done() and not producer.cancelled():
            # 从没被消费过的异常别留给事件循环（pytest 会把 warning 当错误）
            with contextlib.suppress(BaseException):
                producer.exception()


__all__ = [
    "HEARTBEAT_EVENT",
    "HEARTBEAT_INTERVAL_S",
    "stream_with_heartbeat",
]
