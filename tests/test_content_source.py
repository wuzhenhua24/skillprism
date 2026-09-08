"""管理系统 zip 下载接口的客户端测试。"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest

from skillprism.content import (
    ContentFetchError,
    GitLabArchiveSource,
    LocalDirectorySource,
    SkillNotFoundError,
    ZipArchiveSource,
    build_content_sources,
    enabled_sources,
    join_skill_id,
    split_skill_id,
    validate_ref,
)
from skillprism.config import Settings
from skillprism.domain import ContentSource

TEMPLATE = "https://mgmt.example/api/skills/{skill_id}/download"
GITLAB = "https://gitlab.internal"
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
    sources = build_content_sources(settings)
    assert isinstance(sources[ContentSource.ZIP], ZipArchiveSource)


def test_factory_falls_back_to_local_directory():
    settings = Settings(content_url_template="")
    sources = build_content_sources(settings)
    assert isinstance(sources[ContentSource.LOCAL], LocalDirectorySource)


# ---- GitLab 归档接口 ----


def gitlab_zip(entries) -> bytes:
    """模拟 GitLab 的归档形态：所有条目在 <repo>-<ref>-<sha>/ 下。"""
    return make_zip([(f"repo-main-abc123/{name}", data) for name, data in entries])


def test_gitlab_builds_archive_url(patch_client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("private-token")
        return httpx.Response(200, content=gitlab_zip([("SKILL.md", MANIFEST)]))

    patch_client(handler)
    files = GitLabArchiveSource(GITLAB, token="glpat-x").fetch("group/repo", "v1.2.0")

    assert [f.path for f in files] == ["SKILL.md"]
    assert seen["url"] == (
        "https://gitlab.internal/api/v4/projects/group%2Frepo"
        "/repository/archive.zip?sha=v1.2.0"
    )
    assert seen["token"] == "glpat-x"


def test_gitlab_uses_default_ref_when_version_missing(patch_client):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=gitlab_zip([("SKILL.md", MANIFEST)]))

    patch_client(handler)
    GitLabArchiveSource(GITLAB, default_ref="master").fetch("group/repo")
    assert "sha=master" in seen["url"]


def test_gitlab_monorepo_filters_by_path_and_strips_it(patch_client):
    """monorepo 必须只取一个子树：整仓取档会撞上条目数上限，
    还会把同仓其他 skill 算进 content_hash。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            content=gitlab_zip(
                [
                    ("skills/log-triage/SKILL.md", MANIFEST),
                    ("skills/log-triage/ref/a.md", b"a"),
                ]
            ),
        )

    patch_client(handler)
    files = GitLabArchiveSource(GITLAB).fetch("group/repo:skills/log-triage", "main")

    assert {f.path for f in files} == {"SKILL.md", "ref/a.md"}
    assert "path=skills%2Flog-triage" in seen["url"]


def test_gitlab_token_header_is_configurable(patch_client):
    """CI 的 CI_JOB_TOKEN 只认 JOB-TOKEN，头名写死一种会表现为"全都不存在"。"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, content=gitlab_zip([("SKILL.md", MANIFEST)]))

    patch_client(handler)
    GitLabArchiveSource(GITLAB, token="jt", token_header="JOB-TOKEN").fetch("group/repo")
    assert seen["headers"]["job-token"] == "jt"
    assert "private-token" not in seen["headers"]


def test_gitlab_404_message_mentions_permission(patch_client):
    """GitLab 对无权限的项目也返回 404（防枚举），文案必须把两种可能都写上。"""
    patch_client(lambda request: httpx.Response(404, text="404 Project Not Found"))
    with pytest.raises(SkillNotFoundError, match="无权限"):
        GitLabArchiveSource(GITLAB).fetch("group/repo")


def test_gitlab_401_is_retryable_fetch_error(patch_client):
    """令牌过期要靠运维改配置，重试窗口正好留给这个修复。"""
    patch_client(lambda request: httpx.Response(401, text="401 Unauthorized"))
    with pytest.raises(ContentFetchError, match="鉴权失败"):
        GitLabArchiveSource(GITLAB).fetch("group/repo")


def test_gitlab_bad_ref_does_not_hit_network(patch_client):
    """写错的 ref 重试多少次都一样，在发请求之前就该判掉。"""

    def handler(request):
        raise AssertionError("不该发出请求")

    patch_client(handler)
    with pytest.raises(SkillNotFoundError, match="ref"):
        GitLabArchiveSource(GITLAB).fetch("group/repo", "main; rm -rf /")


@pytest.mark.parametrize(
    "skill_id",
    ["", ":skills/foo", "group/repo:", "group/repo:../../etc", "group/../repo"],
)
def test_gitlab_rejects_malformed_skill_id(skill_id, patch_client):
    """解析失败必须收敛成 SkillNotFoundError：抛别的异常会让 worker 走兜底
    分支，任务在队列里反复重领。"""

    def handler(request):
        raise AssertionError("不该发出请求")

    patch_client(handler)
    with pytest.raises(SkillNotFoundError):
        GitLabArchiveSource(GITLAB).fetch(skill_id)


def test_split_skill_id():
    assert split_skill_id("group/repo") == ("group/repo", None)
    assert split_skill_id("group/sub/repo:skills/foo") == ("group/sub/repo", "skills/foo")
    # 数字项目 ID 也是合法的 GitLab 项目标识
    assert split_skill_id("42:skills/foo") == ("42", "skills/foo")
    # 多余的斜杠不改变含义
    assert split_skill_id("/group/repo/:/skills/foo/") == ("group/repo", "skills/foo")


def test_validate_ref_accepts_normal_refs():
    for ref in ("main", "v1.2.0", "release/2026-09", "a" * 40):
        assert validate_ref(ref) == ref


def test_factory_picks_gitlab_source_when_configured():
    settings = Settings(gitlab_base_url=GITLAB, gitlab_token="x")
    sources = build_content_sources(settings)
    assert isinstance(sources[ContentSource.GITLAB], GitLabArchiveSource)


def test_both_sources_can_be_enabled_at_once():
    """两种接入不再互斥。

    曾经互斥是因为一次触发说不清自己是哪种：两边对 skill_id 的解释不同，
    服务端只能按配置猜。现在来源由入口声明、随任务落库，理由就没有了。
    """
    settings = Settings(gitlab_base_url=GITLAB, content_url_template=TEMPLATE)
    sources = build_content_sources(settings)

    assert isinstance(sources[ContentSource.ZIP], ZipArchiveSource)
    assert isinstance(sources[ContentSource.GITLAB], GitLabArchiveSource)
    assert ContentSource.LOCAL not in sources, "配了真来源就不该再挂着开发用的本地目录"


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (Settings(gitlab_base_url=GITLAB), (ContentSource.GITLAB,)),
        (Settings(content_url_template=TEMPLATE), (ContentSource.ZIP,)),
        (
            Settings(gitlab_base_url=GITLAB, content_url_template=TEMPLATE),
            (ContentSource.ZIP, ContentSource.GITLAB),
        ),
        (Settings(), (ContentSource.LOCAL,)),
    ],
)
def test_enabled_sources_maps_config_to_the_enum(settings, expected):
    """判定只有这一处：API 用它决定入口开不开、缺省取哪个，worker 用它造
    客户端表。各判各的早晚会分叉，那种分叉的表现是"结论存在 A 名下、
    查询去 B 名下找"。"""
    assert enabled_sources(settings) == expected


@pytest.mark.parametrize(
    ("project", "subdir", "expected"),
    [
        ("group/repo", None, "group/repo"),
        ("group/repo", "skills/foo", "group/repo:skills/foo"),
        ("group/repo/", "/skills/foo/", "group/repo:skills/foo"),
        ("42", "skills", "42:skills"),
        ("group/repo", "", "group/repo"),
    ],
)
def test_join_skill_id_encodes_the_internal_identity(project, subdir, expected):
    """对外是两个字段，对内仍是那一列 skill_id。落库形态不变是有意的：
    已有的结论、查询与报告地址都按它寻址。"""
    assert join_skill_id(project, subdir) == expected


@pytest.mark.parametrize(
    ("project", "subdir"),
    [
        ("group/repo", "../etc"),
        ("group/repo", "a:b"),
        ("../repo", None),
        ("", None),
    ],
)
def test_join_skill_id_rejects_what_split_would_reject(project, subdir):
    """拼完立刻按 split_skill_id 校验一遍：写错的位置当场报错，
    不用等 worker 去取内容才暴露。"""
    with pytest.raises(SkillNotFoundError):
        join_skill_id(project, subdir)


def test_version_selects_content_is_a_property_of_the_source():
    """``skill_version`` 是不是"选内容"取决于来源，不取决于部署。

    zip 下它是用户手填的标签，GitLab 下它是 ref。挂在配置上的写法只在
    "一个进程一种来源"时成立。
    """
    assert ContentSource.GITLAB.version_selects_content is True
    assert ContentSource.ZIP.version_selects_content is False
    assert ContentSource.LOCAL.version_selects_content is False
