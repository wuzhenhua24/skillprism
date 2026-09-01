"""内容来源。

管理系统把 skill 内容存在数据库或对象存储里，形态未定，因此这里只定义协议。
骨架提供一个从本地目录读取的实现，让整条链路可以先跑起来；接入时替换成
真实的存储客户端即可，worker 不需要改。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

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
