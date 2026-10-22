"""GzipUploadTransport 的纯函数层：阈值 / 头改写 / 幂等 / 小体直传。"""
import gzip

import httpx

from aistudio_api.infrastructure.upstreams.gzip_transport import (
    GZIP_MIN_BYTES,
    gzip_request_body,
)


def _request(body: bytes) -> httpx.Request:
    return httpx.Request("POST", "https://example.com/v1", content=body)


def test_large_body_is_gzipped_and_roundtrips():
    body = b'{"k": "' + b"x" * (GZIP_MIN_BYTES + 1) + b'"}'
    req = _request(body)
    assert gzip_request_body(req, min_bytes=GZIP_MIN_BYTES) is True
    assert req.headers["content-encoding"] == "gzip"
    assert int(req.headers["content-length"]) == len(req.read())
    assert gzip.decompress(req.read()) == body  # 服务端解压后逐字节一致


def test_small_body_passes_through():
    req = _request(b'{"hi": 1}')
    assert gzip_request_body(req, min_bytes=GZIP_MIN_BYTES) is False
    assert "content-encoding" not in req.headers
    assert req.read() == b'{"hi": 1}'


def test_is_idempotent():
    req = _request(b"y" * (GZIP_MIN_BYTES + 1))
    assert gzip_request_body(req, min_bytes=GZIP_MIN_BYTES) is True
    assert gzip_request_body(req, min_bytes=GZIP_MIN_BYTES) is False  # 已带编码头


def test_respects_existing_encoding_header():
    req = _request(b"z" * (GZIP_MIN_BYTES + 1))
    req.headers["content-encoding"] = "br"
    assert gzip_request_body(req, min_bytes=GZIP_MIN_BYTES) is False
