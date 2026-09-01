"""报告存储。

骨架用本地文件系统实现，生产替换成对象存储：只要实现 ReportStorage
协议，worker 与 API 都不需要改。报告按 content_hash 寻址，天然去重。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol


class ReportStorage(Protocol):
    def put(self, content_hash: str, name: str, source: Path) -> str:
        """存入一份报告，返回可回读的 URI。"""
        ...

    def resolve(self, uri: str) -> Path | None:
        """把 URI 还原成本地路径；对象存储实现可返回 None 并改用签名 URL。"""
        ...


class LocalReportStorage:
    """本地文件系统实现。"""

    def __init__(self, root: Path) -> None:
        # 必须绝对化：URI 无法表达相对路径，而 report_root 默认是 ./var/reports。
        self.root = Path(root).resolve()

    def _dir_for(self, content_hash: str) -> Path:
        # 去掉 "sha256:" 前缀，按前两位分桶，避免单目录文件过多。
        digest = content_hash.split(":", 1)[-1]
        return self.root / digest[:2] / digest

    def put(self, content_hash: str, name: str, source: Path) -> str:
        target_dir = self._dir_for(content_hash)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        shutil.copy2(source, target)
        return target.as_uri()

    def resolve(self, uri: str) -> Path | None:
        if not uri.startswith("file://"):
            return None
        path = Path(uri.removeprefix("file://"))
        return path if path.exists() else None
