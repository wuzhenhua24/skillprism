"""报告存储。

骨架用本地文件系统实现，生产替换成对象存储：只要实现 ReportStorage
协议，worker 与 API 都不需要改。报告按 (content_hash, context_hash) 寻址，
天然去重。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol


class ReportStorage(Protocol):
    def put(
        self, content_hash: str, name: str, source: Path, *, context_hash: str | None = None
    ) -> str:
        """存入一份报告，返回可回读的 URI。

        ``context_hash`` 是这次评测所在的一组耦合 skill 的整体指纹，单独评的
        为 None。同一份内容在两种上下文下的报告内容不同（跨 skill 链接一边
        是死链一边不是），寻址不带上它，后跑的那次会覆盖前一次，而前一次的
        结果行还指着这个地址——界面上就会看到一份和结论对不上的报告。
        """
        ...

    def resolve(self, uri: str) -> Path | None:
        """把 URI 还原成本地路径；对象存储实现可返回 None 并改用签名 URL。"""
        ...


class LocalReportStorage:
    """本地文件系统实现。"""

    def __init__(self, root: Path) -> None:
        # 必须绝对化：URI 无法表达相对路径，而 report_root 默认是 ./var/reports。
        self.root = Path(root).resolve()

    def _dir_for(self, content_hash: str, context_hash: str | None) -> Path:
        """报告目录由 (content_hash, context_hash) 决定，按前两位分桶避免单目录文件过多。

        注意路径里**没有 skill_id**：两个内容字节相同的 skill 会共用同一个
        报告目录。这不是假想——团队之间抄 skill 很常见，Tier 2 去重存在的
        理由就是这个。数据库里 (skill_id, content_hash) 是联合唯一，
        所以多行可以指向同一份文件。

        上下文进地址是必须的：同一份内容单独评和在一组耦合 skill 里评，报告
        内容不同。只按 content_hash 寻址的话后跑的会覆盖先跑的，而先跑那次
        的结果行仍指着这个地址，界面就会显示一份与结论对不上的报告。

        因此将来实现报告清理时**必须先做引用计数**：删一个目录前，确认没有
        别的 evaluation_result 行还引用它。写成"删掉某个 skill 的旧报告"
        会连带删掉别的 skill 正在用的报告。
        """
        digest = content_hash.split(":", 1)[-1]
        base = self.root / digest[:2] / digest
        if context_hash is None:
            return base
        return base / f"ctx-{context_hash.split(':', 1)[-1][:16]}"

    def put(
        self, content_hash: str, name: str, source: Path, *, context_hash: str | None = None
    ) -> str:
        target_dir = self._dir_for(content_hash, context_hash)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        shutil.copy2(source, target)
        return target.as_uri()

    def resolve(self, uri: str) -> Path | None:
        if not uri.startswith("file://"):
            return None
        path = Path(uri.removeprefix("file://"))
        return path if path.exists() else None
