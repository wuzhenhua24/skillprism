"""管理系统 zip 下载接口的客户端测试。"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from skillprism.content import (
    ContentFetchError,
    LocalDirectorySource,
    SkillNotFoundError,
    ZipArchiveSource,
    build_content_source,
)
from skillprism.config import Settings

TEMPLATE = "https://mgmt.example/api/skills/{skill_id}/download"
MANIFEST = b"---\nname: demo\ndescription: A demo skill.\n---\n\n# Demo\n"


def make_zip(entries) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


@pytest.fixture
def patch_client(monkeypatch):
    """把 ZipArchiveSource 内部的 httpx.Client 换成 MockTransport 版本。"""

    def apply(handler):
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr("skillprism.content.httpx.Client", factory)

    return apply


def test_downloads_and_extracts(patch_client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=make_zip([("SKILL.md", MANIFEST)]))

    patch_client(handler)
    files = ZipArchiveSource(TEMPLATE, token="t0ken").fetch("demo")

    assert [f.path for f in files] == ["SKILL.md"]
    assert seen["url"] == "https://mgmt.example/api/skills/demo/download"
    assert seen["auth"] == "Bearer t0ken"


def test_skill_id_with_slash_is_url_encoded(patch_client):
    """skill_id 可能形如 team/name，不能让它改变 URL 的路径结构。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=make_zip([("SKILL.md", MANIFEST)]))

    patch_client(handler)
    ZipArchiveSource(TEMPLATE).fetch("team-infra/log-triage")
    assert "team-infra%2Flog-triage" in seen["url"]
    assert seen["url"].count("/skills/") == 1


def test_404_is_not_found(patch_client):
    patch_client(lambda request: httpx.Response(404, text="no such skill"))
    with pytest.raises(SkillNotFoundError):
        ZipArchiveSource(TEMPLATE).fetch("missing")


def test_500_is_fetch_error(patch_client):
    """服务端错误与“不存在”要分开：前者应当重试。"""
    patch_client(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(ContentFetchError):
        ZipArchiveSource(TEMPLATE).fetch("demo")


def test_transport_error_is_fetch_error(patch_client):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    patch_client(handler)
    with pytest.raises(ContentFetchError, match="下载失败"):
        ZipArchiveSource(TEMPLATE).fetch("demo")


def test_download_size_cap(patch_client):
    """超大响应必须在流式读取时截断，而不是先收完再判断。"""
    patch_client(lambda request: httpx.Response(200, content=b"A" * 5000))
    with pytest.raises(ContentFetchError, match="体积超限"):
        ZipArchiveSource(TEMPLATE, max_bytes=1000).fetch("demo")


def test_bad_archive_is_not_retryable(patch_client):
    """归档内容有问题是 skill 的问题，不是取回失败，重试没有意义。"""
    patch_client(lambda request: httpx.Response(200, content=b"not a zip"))
    with pytest.raises(SkillNotFoundError, match="无法解出"):
        ZipArchiveSource(TEMPLATE).fetch("demo")


def test_template_must_have_placeholder():
    with pytest.raises(ValueError, match="skill_id"):
        ZipArchiveSource("https://mgmt.example/download")


def test_factory_picks_zip_source_when_configured():
    settings = Settings(content_url_template=TEMPLATE, content_token="x")
    assert isinstance(build_content_source(settings), ZipArchiveSource)


def test_factory_falls_back_to_local_directory():
    settings = Settings(content_url_template="")
    assert isinstance(build_content_source(settings), LocalDirectorySource)
