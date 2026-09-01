"""Shim 测试。

最关键的两条：
- 发给方舟的每个请求都不超过 10 条（否则 400）。
- 合并后的 index 必须是 0..N-1 的唯一整数且顺序与输入一致——
  SkillEvaluator 对此有严格校验，错了会直接报错。
"""

from __future__ import annotations

import json

import httpx
import pytest

from skill_eval_service.config import Settings
from skill_eval_service.embedding_shim import EmbeddingsRequest, ShimError, embed_batched


def _settings(**kw) -> Settings:
    base = {
        "ark_base_url": "https://ark.example/api/coding/v3",
        "ark_batch_size": 10,
        "shim_concurrency": 4,
        "shim_retries": 2,
        "shim_timeout_seconds": 5.0,
    }
    base.update(kw)
    return Settings(**base)


def _vector_for(text: str) -> list[float]:
    """用输入文本推导出可识别的向量，便于验证顺序没有错乱。"""
    return [float(len(text)), float(sum(ord(c) for c in text))]


class Recorder:
    """记录发往方舟的每个分片请求。"""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        texts = payload["input"]
        self.batches.append(texts)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": payload["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": _vector_for(t)}
                    for i, t in enumerate(texts)
                ],
                "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
            },
        )


async def _run(texts, handler, settings=None):
    settings = settings or _settings()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        return await embed_batched(
            EmbeddingsRequest(model="doubao-embedding-vision", input=texts, encoding_format="float"),
            settings=settings,
            api_key="k",
            client=client,
        )


@pytest.mark.asyncio
async def test_splits_into_chunks_of_ten():
    rec = Recorder()
    texts = [f"text-{i}" for i in range(25)]
    body = await _run(texts, rec.handler)

    assert [len(b) for b in rec.batches] == [10, 10, 5]
    assert all(len(b) <= 10 for b in rec.batches), "任何一个分片超过 10 条都会被方舟拒绝"
    assert len(body["data"]) == 25


@pytest.mark.asyncio
async def test_index_is_globally_reassigned_and_order_preserved():
    """跨分片必须重编号；上游要求 index 是 0..N-1 的唯一整数。"""
    rec = Recorder()
    texts = [f"text-{i}" for i in range(25)]
    body = await _run(texts, rec.handler)

    indices = [d["index"] for d in body["data"]]
    assert indices == list(range(25))
    assert len(set(indices)) == 25

    # 向量必须仍与原始输入一一对应，不能因为并发而错位
    for i, text in enumerate(texts):
        assert body["data"][i]["embedding"] == _vector_for(text)


@pytest.mark.asyncio
async def test_exact_multiple_of_batch_size():
    rec = Recorder()
    body = await _run([f"t{i}" for i in range(20)], rec.handler)
    assert [len(b) for b in rec.batches] == [10, 10]
    assert [d["index"] for d in body["data"]] == list(range(20))


@pytest.mark.asyncio
async def test_single_string_input():
    rec = Recorder()
    transport = httpx.MockTransport(rec.handler)
    async with httpx.AsyncClient(transport=transport) as client:
        body = await embed_batched(
            EmbeddingsRequest(model="m", input="just one"),
            settings=_settings(), api_key="k", client=client,
        )
    assert len(body["data"]) == 1
    assert body["data"][0]["index"] == 0


@pytest.mark.asyncio
async def test_out_of_order_chunk_response_is_sorted():
    """方舟若乱序返回，分片内也要按 index 排好再拼接。"""

    def handler(request: httpx.Request) -> httpx.Response:
        texts = json.loads(request.content)["input"]
        data = [
            {"object": "embedding", "index": i, "embedding": _vector_for(t)}
            for i, t in enumerate(texts)
        ]
        return httpx.Response(200, json={"data": list(reversed(data)), "usage": {}})

    texts = [f"t{i}" for i in range(5)]
    body = await _run(texts, handler)
    for i, text in enumerate(texts):
        assert body["data"][i]["embedding"] == _vector_for(text)


@pytest.mark.asyncio
async def test_usage_is_summed():
    rec = Recorder()
    body = await _run([f"t{i}" for i in range(15)], rec.handler)
    assert body["usage"]["total_tokens"] == 15


@pytest.mark.asyncio
async def test_upstream_400_propagates_without_partial_result():
    """任一分片失败即整体失败，不能返回数量不符的部分结果。"""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(400, json={"error": {"message": "bad input"}})
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={
            "data": [{"index": i, "embedding": _vector_for(t)} for i, t in enumerate(texts)],
            "usage": {},
        })

    with pytest.raises(ShimError) as exc:
        await _run([f"t{i}" for i in range(25)], handler)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_transient_503_is_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, text="upstream busy")
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={
            "data": [{"index": i, "embedding": _vector_for(t)} for i, t in enumerate(texts)],
            "usage": {},
        })

    body = await _run(["a", "b"], handler)
    assert attempts["n"] == 2
    assert len(body["data"]) == 2


@pytest.mark.asyncio
async def test_transport_error_is_retried_then_fails():
    """实测方舟端点偶发 TLS 握手失败，重试耗尽后要明确报错。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("TLS handshake failed")

    with pytest.raises(ShimError, match="传输失败"):
        await _run(["a"], handler, _settings(shim_retries=2))
    assert attempts["n"] == 3  # 首次 + 2 次重试


@pytest.mark.asyncio
async def test_chunk_returning_wrong_count_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}], "usage": {}})

    with pytest.raises(ShimError, match="数量不符"):
        await _run(["a", "b", "c"], handler)


@pytest.mark.asyncio
async def test_encoding_format_is_forwarded():
    """SkillEvaluator 固定传 encoding_format=float，实测方舟接受该字段。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.update(payload)
        texts = payload["input"]
        return httpx.Response(200, json={
            "data": [{"index": i, "embedding": [0.0]} for i in range(len(texts))],
            "usage": {},
        })

    await _run(["a"], handler)
    assert seen["encoding_format"] == "float"
    assert seen["model"] == "doubao-embedding-vision"
