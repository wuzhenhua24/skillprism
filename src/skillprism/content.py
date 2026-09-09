"""内容来源。

管理系统把 skill 内容存在数据库或对象存储里，形态未定，因此这里只定义协议。
骨架提供一个从本地目录读取的实现，让整条链路可以先跑起来；接入时替换成
真实的存储客户端即可，worker 不需要改。

目前有三种实现：本地目录（开发用）、管理系统的 zip 下载接口、GitLab 的
归档接口。三者都归一到"一个 skill 一组文件"，下游（物化、算 hash、缓存
复用）看不出内容是从哪来的。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlencode

import httpx

from skillprism.archive import ArchiveError, read_skill_bundle, read_skill_zip
from skillprism.domain import ContentSource
from skillprism.materialize import (
    MAX_BUNDLE_MEMBERS,
    MAX_FILE_BYTES,
    SKILL_MANIFEST,
    SkillBundle,
    SkillFile,
    UnsafePathError,
    safe_relative_path,
)


class SkillNotFoundError(LookupError):
    """管理系统中不存在该 skill 或该版本。"""


class SkillContentSource(Protocol):
    def fetch(self, skill_id: str, version: str | None = None) -> list[SkillFile]:
        """取回一个 skill 的全部文件。路径为仓库内相对路径，未经校验。"""
        ...

    def fetch_bundle(self, skill_id: str, version: str | None = None) -> SkillBundle:
        """取回一组耦合 skill。路径相对 bundle 根，未经校验。

        与 :meth:`fetch` 分开而不是靠内容形态自动判别：调用方知道自己注册的
        是一个 skill 还是一套工作流，说出来才能在不一致时报错。让内容形态
        决定语义的话，``skill_id`` 少写一层子目录就会静默变成评另一批东西。
        """
        ...


class LocalDirectorySource:
    """开发用实现：把 <root>/<skill_id> 目录当作一个 skill。

    生产实现替换为管理系统的存储客户端。注意无论哪种实现，返回的 path
    都被视为不可信输入，由 materialize 层统一校验。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def fetch(self, skill_id: str, version: str | None = None) -> list[SkillFile]:
        base = self.root / skill_id
        if not base.is_dir():
            raise SkillNotFoundError(f"找不到 skill：{skill_id}")

        files: list[SkillFile] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            files.append(
                SkillFile(
                    path=path.relative_to(base).as_posix(),
                    data=path.read_bytes(),
                )
            )
        if not files:
            raise SkillNotFoundError(f"skill 内容为空：{skill_id}")
        return files

    def fetch_bundle(self, skill_id: str, version: str | None = None) -> SkillBundle:
        """把 ``<root>/<skill_id>`` 当作一组 skill 的父目录。"""
        base = self.root / skill_id
        if not base.is_dir():
            raise SkillNotFoundError(f"找不到 bundle：{skill_id}")
        if (base / SKILL_MANIFEST).is_file():
            raise SkillNotFoundError(
                f"{skill_id} 根上有 {SKILL_MANIFEST}：这是单个 skill，不是一组"
            )

        files: list[SkillFile] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            files.append(SkillFile(path=path.relative_to(base).as_posix(), data=path.read_bytes()))

        members = sorted(
            child.name for child in base.iterdir()
            if child.is_dir() and not child.is_symlink() and (child / SKILL_MANIFEST).is_file()
        )
        if not members:
            raise SkillNotFoundError(f"{skill_id} 下没有任何含 {SKILL_MANIFEST} 的子目录")
        return SkillBundle(files=files, members=members)


class ContentFetchError(RuntimeError):
    """无法从管理系统取回内容。与“skill 不存在”区分开——前者应当重试。"""


def download_capped(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
    not_found_message: str,
) -> bytes:
    """流式下载，超过 ``max_bytes`` 立刻中断。

    体积上限卡在**流式读取时**，而不是先收完再检查——否则一个超大响应就能
    把 worker 的内存吃光，压根走不到解归档那一步。

    404 收敛成 :class:`SkillNotFoundError`（不重试），其余 4xx/5xx 与传输层
    故障收敛成 :class:`ContentFetchError`（退避重试）。401/403 归在后者：
    令牌过期要靠运维改配置，重试窗口正好给这个修复留时间，而把它判成
    "skill 不存在"会让任务直接终结、错误信息还指错方向。
    """
    chunks: list[bytes] = []
    total = 0
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    raise SkillNotFoundError(not_found_message)
                if response.status_code in (401, 403):
                    response.read()
                    raise ContentFetchError(
                        f"鉴权失败 HTTP {response.status_code}（令牌缺失、过期或权限不足）："
                        f"{response.text[:200]}"
                    )
                if response.status_code >= 400:
                    response.read()
                    raise ContentFetchError(
                        f"下载失败 HTTP {response.status_code}：{response.text[:200]}"
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ContentFetchError(f"下载体积超限：> {max_bytes} 字节")
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ContentFetchError(f"下载失败：{type(exc).__name__}: {exc}") from exc
    return b"".join(chunks)


class ZipArchiveSource:
    """从管理系统下载 zip 并解出文件。

    管理系统按一个 skill 一个 zip 提供内容。下载与解归档分开：这里只负责
    把字节安全地取回来，归档本身的风险由 :mod:`skillprism.archive`
    处理。

    下载体积在**流式读取时**卡上限，而不是先收完再检查——否则一个超大响应
    就能把 worker 的内存吃光，压根走不到解归档那一步。
    """

    def __init__(
        self,
        url_template: str,
        *,
        token: str = "",
        timeout: float = 60.0,
        max_bytes: int = 64 * 1024 * 1024,
        max_bundle_members: int = MAX_BUNDLE_MEMBERS,
    ) -> None:
        if "{skill_id}" not in url_template:
            raise ValueError("url_template 必须包含 {skill_id} 占位符")
        self.url_template = url_template
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_bundle_members = max_bundle_members

    def _url(self, skill_id: str) -> str:
        # skill_id 可能含 /（如 team/name），整体编码避免它改变路径结构。
        return self.url_template.format(skill_id=quote(skill_id, safe=""))

    def _download(self, url: str) -> bytes:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return download_capped(
            url,
            headers=headers,
            timeout=self.timeout,
            max_bytes=self.max_bytes,
            not_found_message=f"管理系统中不存在该 skill：{url}",
        )

    def fetch(self, skill_id: str, version: str | None = None) -> list[SkillFile]:
        data = self._download(self._url(skill_id))
        try:
            return read_skill_zip(data)
        except ArchiveError as exc:
            # 归档内容有问题是 skill 的问题，不是取回失败，重试没有意义。
            raise SkillNotFoundError(f"归档无法解出（{skill_id}）：{exc}") from exc

    def fetch_bundle(self, skill_id: str, version: str | None = None) -> SkillBundle:
        data = self._download(self._url(skill_id))
        try:
            return read_skill_bundle(data, max_members=self.max_bundle_members)
        except ArchiveError as exc:
            raise SkillNotFoundError(f"归档无法解出（{skill_id}）：{exc}") from exc


#: skill_id 里分隔项目与子目录的字符。GitLab 的项目路径只允许字母数字与
#: ``_ - . /``，冒号不可能出现在里面，因此拿它做分隔不会有歧义。
SUBDIR_SEPARATOR = ":"

#: git ref 名里不允许出现的字符（见 git-check-ref-format）。空格单列在下面。
_REF_FORBIDDEN_CHARS = "~^:?*[\\"


def split_skill_id(skill_id: str) -> tuple[str, str | None]:
    """把 ``<project>[:<subdir>]`` 拆成项目路径与仓库内子目录。

    单仓单 skill（SKILL.md 在仓库根）写 ``group/repo``；monorepo 写
    ``group/repo:skills/foo``。项目位置也接受数字项目 ID。

    这是在不动对外契约的前提下把 (项目, 子目录) 塞进 ``skill_id`` 的办法。
    要脱离这个编码，得给 SubmitRequest 加字段，那是要和管理系统一起改的。
    """
    raw = skill_id.strip()
    if not raw:
        raise SkillNotFoundError("skill_id 为空")

    project, sep, subdir = raw.partition(SUBDIR_SEPARATOR)
    project = project.strip().strip("/")
    if not project:
        raise SkillNotFoundError(f"skill_id 里没有 GitLab 项目路径：{skill_id!r}")
    if any(ord(ch) < 0x20 or ch in " \t" for ch in project) or ".." in project:
        raise SkillNotFoundError(f"GitLab 项目路径不合法：{project!r}")

    if not sep:
        return project, None

    subdir = subdir.strip().strip("/")
    if not subdir:
        raise SkillNotFoundError(
            f"skill_id 带了 {SUBDIR_SEPARATOR!r} 却没给子目录：{skill_id!r}"
        )
    if SUBDIR_SEPARATOR in subdir:
        raise SkillNotFoundError(f"子目录里不允许出现 {SUBDIR_SEPARATOR!r}：{skill_id!r}")
    try:
        # 复用物化层的路径校验：../、绝对路径、反斜杠、控制字符一并挡掉。
        subdir = safe_relative_path(subdir).as_posix()
    except UnsafePathError as exc:
        raise SkillNotFoundError(f"子目录不合法（{skill_id!r}）：{exc}") from exc
    return project, subdir


def join_skill_id(project: str, subdir: str | None) -> str:
    """把 (项目, 子目录) 拼成内部用的 ``skill_id``，:func:`split_skill_id` 的逆。

    对外接口收的是 ``project`` 与 ``subdir`` 两个字段，拼装由服务端做——
    冒号编码是内部约定，不该要求调用方懂。拼完立刻用 ``split_skill_id``
    校验一遍：写错的项目路径或子目录在提交时就报 422，而不是十秒后变成一条
    "取不到内容"的任务错误。
    """
    raw = project.strip().strip("/")
    if subdir and subdir.strip().strip("/"):
        raw = f"{raw}{SUBDIR_SEPARATOR}{subdir.strip().strip('/')}"
    split_skill_id(raw)
    return raw


def member_skill_id(bundle_skill_id: str, member: str) -> str:
    """一组耦合 skill 里，某个成员对外的 ``skill_id``。

    ``group/repo:skills`` + ``code-review`` → ``group/repo:skills/code-review``，
    正是这个 skill 单独提交时会用的那个 ID。管理系统因此不需要第二套查询
    方式：查一个成员的结论和查任何别的 skill 完全一样。

    规则放在这里而不是 worker 里，是因为它有**两个**使用者：worker 落库时
    按它给成员命名，查询侧按它反过来找出"这次 bundle 任务产出了哪几条结论"
    （见 :func:`skillprism.repository.bundle_member_results`）。两边各写一遍
    的话，改了一处就会让任务接口漏报成员——漏报不报错，只是列表短了一截。
    """
    return f"{bundle_skill_id.rstrip('/')}/{member}"


def validate_ref(ref: str) -> str:
    """校验 git ref。写错的 ref 重试多少次都一样，所以当作"不存在"处理。"""
    candidate = ref.strip()
    if not candidate:
        raise SkillNotFoundError("ref 为空")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == " " for ch in candidate):
        raise SkillNotFoundError(f"ref 含空白或控制字符：{ref!r}")
    if any(ch in _REF_FORBIDDEN_CHARS for ch in candidate):
        raise SkillNotFoundError(f"ref 含非法字符：{ref!r}")
    if ".." in candidate or "@{" in candidate:
        raise SkillNotFoundError(f"ref 含非法序列：{ref!r}")
    if candidate.startswith(("-", "/")) or candidate.endswith(("/", ".lock")):
        raise SkillNotFoundError(f"ref 形式不合法：{ref!r}")
    return candidate


class GitLabArchiveSource:
    """从 GitLab 的归档接口取 skill。

    走 ``GET /api/v4/projects/:id/repository/archive.zip?sha=&path=`` 而不是
    ``git clone``：拿到的仍然是一个 zip，:mod:`skillprism.archive` 那四道
    防线原样继续生效。clone 的话它们全部作废，还要额外面对 ``.git/hooks``、
    ``.gitattributes`` 的 filter driver、submodule 和没有上限的仓库体积——
    物化层的路径校验挡不住其中任何一样，因为它们的路径本身是合法的。

    身份的两个部分分别来自：

    * ``skill_id`` → ``<project>[:<subdir>]``，见 :func:`split_skill_id`；
    * ``version`` → git ref（分支、tag 或 commit sha），留空则用默认分支。

    注意 ``version`` 在 zip 接入下是"用户手填的版本号"，在这里被重新解释成
    ref。这个重载之所以不产生歧义，是因为每个任务都记着自己的来源
    （:class:`~skillprism.domain.ContentSource`），取内容时按它选实现——
    而不是靠"一个部署只启用一种接入"。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        token_header: str = "PRIVATE-TOKEN",
        default_ref: str = "main",
        timeout: float = 60.0,
        max_bytes: int = 64 * 1024 * 1024,
        max_bundle_members: int = MAX_BUNDLE_MEMBERS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url:
            raise ValueError("gitlab_base_url 不能为空")
        self.token = token
        self.token_header = token_header
        self.default_ref = default_ref
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_bundle_members = max_bundle_members

    def _headers(self) -> dict[str, str]:
        """按配置的头名发令牌。

        PAT / group / project token 用 ``PRIVATE-TOKEN``，CI 的
        ``CI_JOB_TOKEN`` 只认 ``JOB-TOKEN``，OAuth 才是 ``Authorization``。
        头名写死一种，换令牌类型时会表现为"所有 skill 都不存在"，很难查。
        """
        return {self.token_header: self.token} if self.token else {}

    def _url(self, project: str, ref: str, subdir: str | None) -> str:
        params = {"sha": ref}
        if subdir:
            # 让服务端只打这一棵子树：整仓归档很容易撞上 max_bytes 下载上限，
            # 白下几十 MB 也是浪费。
            #
            # 但这是**优化，不是保证**：archive.zip 的 path 是较新版本才加的，
            # 老实例不报错、只是当没看见，照样返回整仓。真正保证"只取这棵子树"
            # 的是解归档那边按 subdir 的筛选（见 archive._prefix_for_subdir），
            # 那里两种服务端得到的文件集一样，content_hash 也就不随版本变。
            params["path"] = subdir
        # quote_via=quote 让空格编成 %20 而不是 +。目录名带空格很少见，但
        # `+` 只在 form-urlencoded 的解读下才是空格，换个服务端就变成字面加号。
        query = urlencode(params, quote_via=quote)
        return (
            f"{self.base_url}/api/v4/projects/{quote(project, safe='')}"
            f"/repository/archive.zip?{query}"
        )

    def fetch(self, skill_id: str, version: str | None = None) -> list[SkillFile]:
        project, ref, subdir, data = self._download_archive(skill_id, version)
        try:
            return read_skill_zip(data, subdir=subdir)
        except ArchiveError as exc:
            # 归档内容有问题是 skill 的问题，不是取回失败，重试没有意义。
            raise SkillNotFoundError(f"归档无法解出（{project}@{ref}）：{exc}") from exc

    def fetch_bundle(self, skill_id: str, version: str | None = None) -> SkillBundle:
        """``skill_id`` 指向仓库里装着多个 skill 的父目录，例 ``group/repo:skills``。

        成员的兄弟目录会一并取回来，这正是 bundle 的意义：跨 skill 的相对
        链接要能解析。
        """
        project, ref, subdir, data = self._download_archive(skill_id, version)
        try:
            return read_skill_bundle(data, subdir=subdir, max_members=self.max_bundle_members)
        except ArchiveError as exc:
            raise SkillNotFoundError(f"归档无法解出（{project}@{ref}）：{exc}") from exc

    def _download_archive(
        self, skill_id: str, version: str | None
    ) -> tuple[str, str, str | None, bytes]:
        project, subdir = split_skill_id(skill_id)
        ref = validate_ref(version or self.default_ref)
        url = self._url(project, ref, subdir)

        data = download_capped(
            url,
            headers=self._headers(),
            timeout=self.timeout,
            max_bytes=self.max_bytes,
            # GitLab 对"无权限"也返回 404 而不是 403（防项目枚举），所以这条
            # 文案必须把两种可能都写上，否则令牌配漏了会被当成 skill 不存在。
            not_found_message=(
                f"GitLab 上取不到 {project}@{ref}"
                + (f" 的 {subdir}" if subdir else "")
                + "：项目/ref/路径不存在，或令牌对该项目无权限"
            ),
        )
        return project, ref, subdir, data


def enabled_sources(settings) -> tuple[ContentSource, ...]:
    """本部署启用了哪些内容来源。

    配了 ``SKILLPRISM_CONTENT_URL_TEMPLATE`` 就启用 zip 接入，配了
    ``SKILLPRISM_GITLAB_BASE_URL`` 就启用 GitLab 接入，**两个都配就两个都
    启用**。都没配则退回本地目录，那只用于开发调试。

    两者曾经互斥，因为那时一次触发说不清自己是哪种接入：``skill_id`` 和
    ``skill_version`` 在两边的含义不同，服务端只能按进程配置猜一个，猜错
    不会报错、只会默默评错东西。现在来源由入口声明、随任务落库
    （见 :class:`~skillprism.domain.ContentSource`），互斥的理由就没有了。
    """
    kinds = []
    if settings.content_url_template:
        kinds.append(ContentSource.ZIP)
    if settings.gitlab_base_url:
        kinds.append(ContentSource.GITLAB)
    return tuple(kinds) if kinds else (ContentSource.LOCAL,)


def build_content_sources(settings) -> dict[ContentSource, SkillContentSource]:
    """按配置造出所有启用了的内容来源客户端，按来源索引。

    worker 拿着这张表按 ``task.source`` 取实现，而不是全局只有一个客户端。
    """
    return {kind: _build_one(settings, kind) for kind in enabled_sources(settings)}


def _build_one(settings, kind: ContentSource) -> SkillContentSource:
    if kind is ContentSource.GITLAB:
        return GitLabArchiveSource(
            settings.gitlab_base_url,
            token=settings.gitlab_token,
            token_header=settings.gitlab_token_header,
            default_ref=settings.gitlab_default_ref,
            timeout=settings.content_timeout_seconds,
            max_bytes=settings.max_download_bytes,
            max_bundle_members=settings.max_bundle_members,
        )
    if kind is ContentSource.ZIP:
        return ZipArchiveSource(
            settings.content_url_template,
            token=settings.content_token,
            timeout=settings.content_timeout_seconds,
            max_bytes=settings.max_download_bytes,
            max_bundle_members=settings.max_bundle_members,
        )
    return LocalDirectorySource(settings.local_skills_root)
