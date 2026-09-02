"""物化层：把管理系统存储中的 skill 内容还原成 SkillEvaluator 能读的目录树。

因为 skill 内容存在数据库/对象存储而非 Git 仓库，这一层是本服务最主要的
自建组件，也是唯一直接把外部数据写到磁盘的地方。

安全前提：存储里记录的路径是**不可信输入**。上游自带的 path_security 只服务于
它自己的扫描逻辑，不会替我们把关，因此这里必须独立完成校验。
"""

from __future__ import annotations

import hashlib
import shutil
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: 单个 skill 的物化上限。超限即拒绝，不做截断——截断会产出一个
#: “看起来通过了”的残缺结果，比直接失败更危险。
MAX_FILES = 512
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024

#: SkillEvaluator 以根目录下的 SKILL.md 识别一个 skill。
SKILL_MANIFEST = "SKILL.md"

#: 物化时套在外面的目录层级。SkillEvaluator 的 SCHEMA.folder_hierarchy 检查
#: 要求 skill 位于 skills/ 或 team-skills/ 下，缺了它每个 skill 都会平白多一条
#: MEDIUM 问题——那是我们的布局造成的，不是 skill 的问题。
SKILLS_PARENT = "skills"

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class UnsafePathError(ValueError):
    """存储中的路径无法安全落盘。"""


class MaterializeError(ValueError):
    """内容不满足物化前提（超限、缺少 SKILL.md 等）。"""


@dataclass(frozen=True)
class SkillFile:
    """来自管理系统的一个文件条目。

    path 是仓库内相对路径，未经校验；data 是原始字节。
    """

    path: str
    data: bytes


def safe_relative_path(raw: str) -> PurePosixPath:
    """把不可信的相对路径规范化，拒绝一切可能逸出根目录的形式。

    拒绝：空路径、绝对路径、盘符、反斜杠、``.``/``..`` 分段、控制字符与
    NUL、Windows 保留名。返回规范化后的 POSIX 相对路径。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise UnsafePathError("路径为空")

    # NFC 归一，避免用等价字形绕过后续比较。
    candidate = unicodedata.normalize("NFC", raw).strip()

    if "\x00" in candidate:
        raise UnsafePathError(f"路径含 NUL 字节: {raw!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        raise UnsafePathError(f"路径含控制字符: {raw!r}")
    if "\\" in candidate:
        raise UnsafePathError(f"路径含反斜杠: {raw!r}")
    if candidate.startswith("/"):
        raise UnsafePathError(f"路径是绝对路径: {raw!r}")
    # C:/... 或 C:\... 形式的盘符
    if len(candidate) >= 2 and candidate[1] == ":":
        raise UnsafePathError(f"路径含盘符: {raw!r}")
    if candidate.startswith("~"):
        raise UnsafePathError(f"路径含家目录展开: {raw!r}")

    parts = [p for p in PurePosixPath(candidate).parts]
    if not parts:
        raise UnsafePathError(f"路径规范化后为空: {raw!r}")

    for part in parts:
        if part in (".", ".."):
            raise UnsafePathError(f"路径含相对分段: {raw!r}")
        if part.endswith((" ", ".")):
            # Windows 会静默剥掉结尾空格与点，导致落盘名与校验名不一致。
            raise UnsafePathError(f"路径分段以空格或点结尾: {raw!r}")
        if part.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise UnsafePathError(f"路径含保留名: {raw!r}")

    return PurePosixPath(*parts)


def compute_content_hash(files: Iterable[SkillFile]) -> str:
    """计算与文件顺序无关的规范化内容哈希。

    内容不在 Git 里，没有天然的 commit 标识，因此这个哈希承担两个职责：
    判断“内容是否变过”（缓存命中）与关联结果版本。

    做法：对每个文件取 (规范化路径, 内容摘要)，按路径排序后再整体摘要。
    """
    digests: list[tuple[str, str]] = []
    for item in files:
        path = safe_relative_path(item.path).as_posix()
        digests.append((path, hashlib.sha256(item.data).hexdigest()))

    if not digests:
        raise MaterializeError("内容为空，无法计算 content hash")

    outer = hashlib.sha256()
    for path, digest in sorted(digests):
        outer.update(path.encode("utf-8"))
        outer.update(b"\x00")
        outer.update(digest.encode("ascii"))
        outer.update(b"\n")
    return f"sha256:{outer.hexdigest()}"


def _validate_budget(files: Sequence[SkillFile]) -> None:
    if len(files) > MAX_FILES:
        raise MaterializeError(f"文件数超限：{len(files)} > {MAX_FILES}")

    total = 0
    for item in files:
        size = len(item.data)
        if size > MAX_FILE_BYTES:
            raise MaterializeError(f"单文件超限：{item.path} 为 {size} 字节 > {MAX_FILE_BYTES}")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise MaterializeError(f"总字节数超限：{total} > {MAX_TOTAL_BYTES}")


def materialize(files: Sequence[SkillFile], dest: Path, *, name: str) -> Path:
    """把内容写进 ``dest/skills/<name>/``，返回该 skill 目录。

    *name* 必须是 skill 在管理系统里**登记的名字**（由触发方随请求给出）。
    目录名会被 SkillEvaluator 的 SCHEMA.name_consistency 检查拿去和
    frontmatter 的 name 比对，所以不能用 "skill" 这类固定名，也不能用纯数字
    的资源 ID——否则每个 skill 都会平白多一条 HIGH 问题。同样不能拿包里的
    目录名或 frontmatter 自己回填：那样这条检查恒真，等于把它废掉。
    用登记名，这条检查才回归它本来的语义：
    “登记的 skill 标识与 frontmatter 声明不一致”。

    *dest* 必须不存在或为空目录——复用已有目录会让上一次评测的残留混进本次结果。
    """
    safe_name = safe_relative_path(name)
    if len(safe_name.parts) != 1:
        raise MaterializeError(f"skill 名不能包含路径分隔符：{name!r}")
    files = list(files)
    _validate_budget(files)

    safe_paths = [safe_relative_path(item.path) for item in files]
    if not any(p.as_posix() == SKILL_MANIFEST for p in safe_paths):
        raise MaterializeError(f"根目录缺少 {SKILL_MANIFEST}，不是一个可评测的 skill")

    seen: set[str] = set()
    for path in safe_paths:
        key = path.as_posix()
        if key in seen:
            raise MaterializeError(f"路径重复：{key}")
        seen.add(key)

    dest = dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise MaterializeError(f"目标目录非空：{dest}")

    skill_root = dest / SKILLS_PARENT / safe_name.as_posix()
    skill_root.mkdir(parents=True, exist_ok=True)

    root = skill_root.resolve(strict=True)
    for item, rel in zip(files, safe_paths, strict=True):
        target = root / Path(*rel.parts)

        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)

        # 兜底：即便前面的校验被绕过，也不允许写到根目录之外，
        # 且路径链上不得出现符号链接。
        resolved_parent = parent.resolve(strict=True)
        if resolved_parent != root and root not in resolved_parent.parents:
            raise UnsafePathError(f"路径逸出根目录：{item.path!r}")
        for ancestor in [resolved_parent, *resolved_parent.parents]:
            if ancestor == root:
                break
            if ancestor.is_symlink():
                raise UnsafePathError(f"路径链上存在符号链接：{ancestor}")

        if target.exists() or target.is_symlink():
            raise UnsafePathError(f"目标已存在：{item.path!r}")

        target.write_bytes(item.data)

    return root


def cleanup(path: Path) -> None:
    """删除物化目录。评测结束后必须调用，避免 work 目录无限增长。"""
    shutil.rmtree(path, ignore_errors=True)
