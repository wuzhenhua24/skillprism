"""OpenAI 兼容的 embeddings 批量拆分 shim。

存在的理由：火山方舟 embeddings 接口单请求最多 10 条输入，而 SkillEvaluator
把批大小硬编码为 64（``embedding/registry.py`` 的 EMBEDDING_BATCH_SIZE 与
``constants.py`` 的 CONTENT_DEDUP_EMBEDDING_BATCH_SIZE），两处都是模块级常量，
没有环境变量或 CLI 参数可调。直连方舟会稳定返回：

    InvalidParameter: Embeddings API input limit exceeded: max 10, got 64

shim 夹在中间，对上游装成一个正常的 OpenAI 端点：接收任意大小的请求，
按 10 条切片、并发调用方舟、合并结果后返回。这样不必 fork SkillEvaluator。

把 ``SKILL_EVAL_EMBEDDING_BASE_URL`` 指向 shim（而非方舟）即可生效。

**必须注意**：SkillEvaluator 会严格校验响应里的 ``index``——要求是 0..N-1 的
唯一整数、不重复、不缺失，否则直接报错。因此跨分片重编号是本模块的核心正确性
要求，见 ``_merge``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from skillprism.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()

#: 方舟对这些响应码的失败是瞬时的，值得重试。4xx（尤其 400）是请求本身
#: 有问题，重试只会重复失败。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ShimError(RuntimeError):
    """调用方舟失败。携带可回传给调用方的状态码与说明。"""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class EmbeddingsRequest(BaseModel):
    """OpenAI embeddings 请求。未知字段原样透传给方舟。"""

    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[str]
    encoding_format: str | None = None

    def texts(self) -> list[str]:
        if isinstance(self.input, str):
            return [self.input]
        return list(self.input)

    def passthrough(self) -> dict[str, Any]:
        """除 input 外需要转发给方舟的字段。"""
        extra = dict(self.model_extra or {})
        extra["model"] = self.model
        if self.encoding_format is not None:
            extra["encoding_format"] = self.encoding_format
        return extra


def _chunks(texts: list[str], size: int) -> list[list[str]]:
    return [texts[i : i + size] for i in range(0, len(texts), size)]


async def _post_chunk(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    retries: int,
) -> dict[str, Any]:
    """发一个分片，带瞬时故障重试。返回方舟的 JSON 响应体。"""
    last: str = "未知错误"
    for attempt in range(retries + 1):
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.TransportError as exc:
            # 实测该端点偶发 TLS 握手失败，属于传输层抖动。
            last = f"传输失败：{type(exc).__name__}: {exc}"
            if attempt == retries:
                raise ShimError(last, status_code=502) from exc
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

        if response.status_code == 200:
            return response.json()

        detail = response.text[:400]
        if response.status_code in _RETRYABLE_STATUS and attempt < retries:
            last = f"HTTP {response.status_code}: {detail}"
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

        # 非瞬时错误：原样回传状态码，让调用方看到方舟的真实原因。
        raise ShimError(f"方舟返回 HTTP {response.status_code}: {detail}", status_code=response.status_code)

    raise ShimError(last, status_code=502)


def _merge(chunk_bodies: list[dict[str, Any]], chunk_sizes: list[int], model: str) -> dict[str, Any]:
    """按分片顺序合并，并把 index 重编为全局的 0..N-1。

    每个分片内部先按方舟返回的 index 排序，再按分片顺序拼接，最后统一重编号。
    分片返回数量与请求数量不符时立刻失败——返回一个数量不对的结果，
    只会让上游在更远的地方报一个更难查的错。
    """
    vectors: list[Any] = []
    prompt_tokens = 0
    total_tokens = 0

    for body, expected in zip(chunk_bodies, chunk_sizes, strict=True):
        data = body.get("data")
        if not isinstance(data, list) or len(data) != expected:
            got = len(data) if isinstance(data, list) else "非数组"
            raise ShimError(f"分片返回数量不符：期望 {expected}，实际 {got}")

        ordered = sorted(data, key=lambda item: item.get("index", 0))
        for item in ordered:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise ShimError("分片返回中缺少 embedding 向量")
            vectors.append(embedding)

        usage = body.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)

    return {
        "object": "list",
        "model": model,
        "data": [
            {"object": "embedding", "index": i, "embedding": vector}
            for i, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": total_tokens},
    }


async def embed_batched(
    request: EmbeddingsRequest,
    *,
    settings: Settings,
    api_key: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """把一个任意大小的 embeddings 请求拆成 ≤batch_size 的多次调用并合并。"""
    texts = request.texts()
    if not texts:
        return {"object": "list", "model": request.model, "data": [], "usage": {}}

    parts = _chunks(texts, settings.ark_batch_size)
    url = settings.ark_base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    base_payload = request.passthrough()

    semaphore = asyncio.Semaphore(settings.shim_concurrency)

    async def run(part: list[str]) -> dict[str, Any]:
        async with semaphore:
            return await _post_chunk(
                client, url, {**base_payload, "input": part}, headers, retries=settings.shim_retries
            )

    results = await asyncio.gather(*(run(part) for part in parts), return_exceptions=True)

    # 任一分片失败即整体失败：返回部分结果会让上游拿到数量不符的向量集，
    # 反而在更远的地方以更难懂的形式报错。
    for item in results:
        if isinstance(item, ShimError):
            raise item
        if isinstance(item, BaseException):
            raise ShimError(f"分片调用异常：{type(item).__name__}: {item}") from item

    bodies: list[dict[str, Any]] = [r for r in results if isinstance(r, dict)]
    merged = _merge(bodies, [len(p) for p in parts], request.model)
    logger.debug("shim: %d 条输入拆成 %d 个分片", len(texts), len(parts))
    return merged


@router.post("/embeddings")
async def embeddings(
    request: EmbeddingsRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """OpenAI 兼容的 embeddings 端点。

    调用方（SkillEvaluator 经 OpenAI SDK）带来的 Authorization 直接转发给方舟；
    没带时回落到服务自身配置的 key。

    注意：shim 会转发凭据到方舟，**不要暴露到公网**，只在 worker 可达的
    内网或本机监听。
    """
    settings = get_settings()

    api_key = ""
    if authorization and authorization.lower().startswith("bearer "):
        api_key = authorization[7:].strip()
    api_key = api_key or settings.ark_api_key
    if not api_key:
        raise HTTPException(status_code=401, detail="缺少 API key：请带 Authorization 头或配置 SKILLPRISM_ARK_API_KEY")

    async with httpx.AsyncClient(timeout=settings.shim_timeout_seconds) as client:
        try:
            return await embed_batched(request, settings=settings, api_key=api_key, client=client)
        except ShimError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
