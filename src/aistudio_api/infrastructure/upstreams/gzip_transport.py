"""上传层 gzip：超过阈值的请求体自动压缩 + Content-Encoding 头。

只动传输层：服务端（Google GFE）解压后看到的 body 与原始 JSON 逐字节一致，
body 哈希/校验不受影响。仅挂在无浏览器通道（express / cookie）；
pg 走页面 XHR，浏览器不会自动压请求体，页面内压缩另行处理。

2026-10-22 真机探针验收：Vertex Express 面接受 gzip 请求体。
"""
import gzip

import httpx

GZIP_MIN_BYTES = 96 * 1024


def gzip_request_body(request: httpx.Request, *, min_bytes: int) -> bool:
    """就地压缩超阈值请求体并改写头；返回是否压缩（幂等，已设编码头则跳过）。"""
    if request.headers.get("content-encoding", "identity") != "identity":
        return False
    body = request.content
    if body is None or len(body) < min_bytes:
        return False
    data = gzip.compress(body, 6)
    # httpx 没有公开的改体 API：read()/content 走构造时缓存的 _content，
    # 传输层发送按它取字节——只换 stream 会造成头(gzip)身(原文)不一致
    request._content = data
    request.stream = httpx.ByteStream(data)
    request.headers["content-encoding"] = "gzip"
    request.headers["content-length"] = str(len(data))
    return True


class GzipUploadTransport(httpx.AsyncHTTPTransport):
    """httpx 异步传输：发送前对大请求体 gzip（阈值默认模块级 GZIP_MIN_BYTES）。"""

    def __init__(self, *, min_bytes: int | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._min_bytes = GZIP_MIN_BYTES if min_bytes is None else min_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gzip_request_body(request, min_bytes=self._min_bytes)
        return await super().handle_async_request(request)
