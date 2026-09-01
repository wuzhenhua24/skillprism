"""内容来源。

管理系统把 skill 内容存在数据库或对象存储里，形态未定，因此这里只定义协议。
骨架提供一个从本地目录读取的实现，让整条链路可以先跑起来；接入时替换成
真实的存储客户端即可，worker 不需要改。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from skill_eval_service.archive import ArchiveError, read_skill_zip
from skill_eval_service.materialize import MAX_FILE_BYTES, SkillFile


class SkillNotFoundError(LookupError):
    """管理系统中不存在该 skill 或该版本。"""


class SkillContentSource(Protocol):
    def fetch(self, skill_id: str, version: str | None = None) -> list[SkillFile]:
        """取回一个 skill 的全部文件。路径为仓库内相对路径，未经校验。"""
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


class ContentFetchError(RuntimeError):
    """无法从管理系统取回内容。与“skill 不存在”区分开——前者应当重试。"""


class ZipArchiveSource:
    """从管理系统下载 zip 并解出文件。

    管理系统按一个 skill 一个 zip 提供内容。下载与解归档分开：这里只负责
    把字节安全地取回来，归档本身的风险由 :mod:`skill_eval_service.archive`
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
    ) -> None:
        if "{skill_id}" not in url_template:
            raise ValueError("url_template 必须包含 {skill_id} 占位符")
        self.url_template = url_template
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes

    def _url(self, skill_id: str) -> str:
        # skill_id 可能含 /（如 team/name），整体编码避免它改变路径结构。
        return self.url_template.format(skill_id=quote(skill_id, safe=""))

    def _download(self, url: str) -> bytes:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        chunks: list[bytes] = []
        total = 0
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 404:
                        raise SkillNotFoundError(f"管理系统中不存在该 skill：{url}")
                    if response.status_code >= 400:
                        response.read()
                        raise ContentFetchError(
                            f"下载失败 HTTP {response.status_code}：{response.text[:200]}"
                        )
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ContentFetchError(
                                f"下载体积超限：> {self.max_bytes} 字节"
                            )
                        chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ContentFetchError(f"下载失败：{type(exc).__name__}: {exc}") from exc
        return b"".join(chunks)

    def fetch(self, skill_id: str, version: str | None = None) -> list[SkillFile]:
        data = self._download(self._url(skill_id))
        try:
            return read_skill_zip(data)
        except ArchiveError as exc:
            # 归档内容有问题是 skill 的问题，不是取回失败，重试没有意义。
            raise SkillNotFoundError(f"归档无法解出（{skill_id}）：{exc}") from exc


def build_content_source(settings) -> SkillContentSource:
    """按配置选内容来源。

    配了 ``SES_CONTENT_URL_TEMPLATE`` 就走管理系统的 zip 下载接口，
    否则退回本地目录——后者只用于开发调试。
    """
    if settings.content_url_template:
        return ZipArchiveSource(
            settings.content_url_template,
            token=settings.content_token,
            timeout=settings.content_timeout_seconds,
            max_bytes=settings.max_download_bytes,
        )
    return LocalDirectorySource(settings.local_skills_root)
