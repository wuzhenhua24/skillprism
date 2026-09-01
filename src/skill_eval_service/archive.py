"""从 zip 归档解出 skill 文件。

管理系统按一个 skill 一个 zip 的方式提供内容，因此解归档这一步落在我们
这边，随之而来的一整类归档特有风险也归我们负责。物化层防的是**路径**，
不是**归档格式**——以下四类它一个都挡不住：

1. **Zip slip**：条目名带 ``..`` 或绝对路径。（路径校验能挡，但必须真的
   把每个条目名都送进去校验，而不是直接 ``extractall``。）
2. **解压炸弹**：几 KB 的包解出几 GB。``ZipInfo.file_size`` 是归档自己声明的，
   会撒谎，所以必须按**实际读出的字节数**硬截断，声明值只用于快速预筛。
3. **符号链接条目**：zip 能存 symlink，解出来就是任意文件读写的入口。
4. **重复条目名**：zip 允许同名条目，后写覆盖前写，可以把内容藏在被覆盖的
   那一份里。

解析与下载刻意分开：``read_skill_zip`` 是纯函数，可以直接用构造出来的
恶意归档做测试，不需要起 HTTP 服务。
"""

from __future__ import annotations

import io
import stat
import zipfile

from skill_eval_service.materialize import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    SKILL_MANIFEST,
    MaterializeError,
    SkillFile,
    UnsafePathError,
    safe_relative_path,
)


class ArchiveError(ValueError):
    """归档无法安全解出。"""


#: 单个条目的最大压缩比。正常文本约 3~10 倍，超过这个量级基本只有炸弹。
MAX_COMPRESSION_RATIO = 200

#: 小文件的压缩比不看——一个 10 字节的文件压出高比例是正常的。
_RATIO_CHECK_MIN_BYTES = 64 * 1024


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """判断条目是否为符号链接。

    Unix 归档把文件模式放在 external_attr 的高 16 位。
    """
    mode = info.external_attr >> 16
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _is_regular_file(info: zipfile.ZipInfo) -> bool:
    """只接受普通文件：目录、设备、FIFO 等一律拒收。

    注意不能直接对 mode 用 ``S_ISREG``：很多打包工具（包括 Python 自己在
    传入 ZipInfo 时）只写权限位、不写文件类型位，例如 ``0o600``。
    此时 ``S_ISREG`` 为假，但它其实就是个普通文件。
    只有在归档确实记录了类型位时才据此判定。
    """
    if info.is_dir():
        return False
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type == 0:
        return True
    return file_type == stat.S_IFREG


def _strip_common_root(paths: list[str]) -> str | None:
    """若所有条目都在同一个顶层目录下，返回该目录名。

    管理系统打包时可能带一层以 skill 名命名的顶层目录
    （``my-skill/SKILL.md``），也可能直接把文件放在根上（``SKILL.md``）。
    两种都得支持，且不能靠猜——以 SKILL.md 的实际位置为准。
    """
    if any(p == SKILL_MANIFEST for p in paths):
        return None  # 文件已在根上，不剥离

    roots = {p.split("/", 1)[0] for p in paths if "/" in p}
    if len(roots) != 1:
        return None
    root = roots.pop()
    if any(not p.startswith(f"{root}/") for p in paths):
        return None
    if f"{root}/{SKILL_MANIFEST}" not in paths:
        return None
    return root


def read_skill_zip(data: bytes) -> list[SkillFile]:
    """把一个 skill 的 zip 解成文件列表。

    任何一条防线被触发就整体拒绝，不做部分解出——一个残缺的 skill 评出来
    的结果比评测失败更有害，因为它看起来是有效的。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"不是合法的 zip 归档：{exc}") from exc

    with archive:
        infos = archive.infolist()

        entries: list[zipfile.ZipInfo] = []
        for info in infos:
            if _is_symlink(info):
                raise ArchiveError(f"归档含符号链接条目：{info.filename!r}")
            if info.is_dir():
                continue
            if not _is_regular_file(info):
                raise ArchiveError(f"归档含非普通文件条目：{info.filename!r}")
            entries.append(info)

        if not entries:
            raise ArchiveError("归档为空")
        if len(entries) > MAX_FILES:
            raise ArchiveError(f"归档条目数超限：{len(entries)} > {MAX_FILES}")

        # 先按声明值快速预筛。声明值不可信，但用来挡住明显过大的归档很便宜。
        declared_total = 0
        for info in entries:
            if info.file_size > MAX_FILE_BYTES:
                raise ArchiveError(f"条目声明大小超限：{info.filename!r} 为 {info.file_size} 字节")
            declared_total += info.file_size
        if declared_total > MAX_TOTAL_BYTES:
            raise ArchiveError(f"归档声明总大小超限：{declared_total} > {MAX_TOTAL_BYTES}")

        # 路径校验放在读取内容之前——不安全的归档不该被读一个字节。
        raw_paths: list[str] = []
        for info in entries:
            try:
                raw_paths.append(safe_relative_path(info.filename).as_posix())
            except UnsafePathError as exc:
                raise ArchiveError(f"归档含不安全路径：{exc}") from exc

        common_root = _strip_common_root(raw_paths)
        paths = [p[len(common_root) + 1 :] if common_root else p for p in raw_paths]

        seen: set[str] = set()
        for path in paths:
            if path in seen:
                raise ArchiveError(f"归档含重复条目：{path}")
            seen.add(path)

        if SKILL_MANIFEST not in seen:
            raise ArchiveError(f"归档根目录缺少 {SKILL_MANIFEST}")

        files: list[SkillFile] = []
        actual_total = 0
        for info, path in zip(entries, paths, strict=True):
            try:
                with archive.open(info) as handle:
                    # 多读一个字节：读满上限说明声明值撒了谎。
                    blob = handle.read(MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, EOFError, OSError) as exc:
                # 损坏或被篡改的条目（CRC 不符、数据截断等）。必须收敛成
                # ArchiveError，否则一个恶意归档就能让 worker 抛未捕获异常。
                raise ArchiveError(f"条目无法读取（归档损坏或被篡改）：{path}：{exc}") from exc
            if len(blob) > MAX_FILE_BYTES:
                raise ArchiveError(f"条目实际大小超限（声明值不可信）：{path}")

            if (
                len(blob) >= _RATIO_CHECK_MIN_BYTES
                and info.compress_size > 0
                and len(blob) / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                ratio = len(blob) // info.compress_size
                raise ArchiveError(f"条目压缩比异常（疑似解压炸弹）：{path} 约 {ratio}:1")

            actual_total += len(blob)
            if actual_total > MAX_TOTAL_BYTES:
                raise ArchiveError(f"归档实际总大小超限：> {MAX_TOTAL_BYTES} 字节")

            files.append(SkillFile(path=path, data=blob))

        return files
