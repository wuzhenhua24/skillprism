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

#: 一组耦合 skill（bundle）的物化上限。单文件上限不放宽——那个约束和一组
#: 里有几个 skill 无关。条目数与总量按成员数上限同比例放宽，不是"随便调大"：
#: 一个 bundle 就是若干个 skill 加少量共享文件。
#:
#: 成员数从 32 提到 64：实测的 plugin 仓已经有 47 个 skill，32 是拍的，挡住的
#: 是真实内容而不是滥用。同理它是这三条里**唯一开成部署可调**的
#: （``SKILLPRISM_MAX_BUNDLE_MEMBERS``，这里的值是默认值）：它挡的是对接的仓库
#: 有多大，那不是本服务能定死的数。另外两条不开——它们防的是归档本身的形状，
#: 与对接哪个仓库无关。
#:
#: 条目数与总量**不跟着涨**：它们才是真正的预算，64 个成员摊下来仍有 32 文件 /
#: 2 MB 每个，对"一个 SKILL.md 加几份参考"够用；先撞上哪条就说明是哪条不够，
#: 比两条一起放大更能看出问题在哪。
#:
#: 把成员数调很大意义不大：catalog 是**一个子进程**跑完全部成员
#: （见 runner.run_catalog），成员越多越贴近 SKILLPRISM_EVAL_TIMEOUT_SECONDS，
#: 而那个超时一到是整组没有报告，不是慢一点。真正的天花板在那儿，不在这里。
MAX_BUNDLE_MEMBERS = 64
MAX_BUNDLE_FILES = 2048
MAX_BUNDLE_TOTAL_BYTES = 128 * 1024 * 1024

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


@dataclass(frozen=True)
class SkillBundle:
    """一组互相耦合的 skill，作为一个整体取回、物化、评测。

    ``files`` 的路径相对 **bundle 根**（``code-review/SKILL.md``、
    ``shared/api.md``），不是相对某个 skill；``members`` 是根下直接含
    ``SKILL.md`` 的一级子目录名，也就是 SkillEvaluator 的 catalog 模式会
    识别成 skill 的那些目录。

    不属于任何成员的文件（共享参考文档、根上的 README）照样保留：耦合的
    典型形态就是几个 skill 共同引用一份约定，丢掉它们等于把跨 skill 链接
    重新变成死链——那正是要解决的问题。
    """

    files: list[SkillFile]
    members: list[str]


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


#: 目录名那一层摘要的域分隔符。带版本号是为了将来再往键里加东西时，
#: 老值不会和新值撞上。
_NAME_DOMAIN = b"skillprism/content-hash/v2\x00"


def compute_content_hash(files: Iterable[SkillFile], *, name: str | None) -> str:
    """计算与文件顺序无关的规范化内容哈希。

    内容不在 Git 里，没有天然的 commit 标识，因此这个哈希承担两个职责：
    判断“内容是否变过”（缓存命中）与关联结果版本。

    做法：对每个文件取 (规范化路径, 内容摘要)，按路径排序后再整体摘要，
    最后把 ``name`` 罩在外面。

    **``name`` 是物化后最外层那个目录的名字，它必须进来。** 这个哈希是结论
    的寻址键，而结论并不只取决于文件字节：目录名会参与判定——SkillEvaluator
    的 ``SCHEMA.name_consistency`` 拿它和 frontmatter 的 ``name`` 比对，不一致
    报一条 HIGH，还会连带影响 discoverability 与总分。实测同一份 SKILL.md
    （frontmatter ``name: alpha``）铺进 ``alpha/`` 是 82.0/B，铺进 ``beta/``
    是 79.5/C。

    不带它的后果是"同样的字节就是同一个结论"这个前提不成立，而复用正是按
    这个前提做的：一组里两个内容相同的成员会互相顶替（重新触发一次，分数
    就变了）；同一个包换个资源 ID、登记名填得不一样，会复用前一个的干净
    结论，那条 HIGH 就此消失——而它本是一枚契约探针，管理系统的上传校验
    被绕过时全靠它报警。报告也按这个哈希寻址，撞上就是两个 skill 共用一份
    报告、后写的覆盖先写的。

    传什么名字：**实际会铺到磁盘上的那个**。单个 skill 是登记名
    （``materialize`` 的 ``name=``），一组里的成员是仓库里的目录名。

    ``name=None`` 表示这份内容不以某个名字被铺开评——只有一组 skill 的整组
    指纹是这种情况：它的根目录名固定是 ``skills/``、不与任何 frontmatter 比
    对，而成员名本来就在各自的路径里（``alpha/SKILL.md``），已经进了摘要。
    必须显式传，不给默认值：漏传不会报错，只会让缓存悄悄退回旧语义。
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
    inner = outer.hexdigest()

    if name is None:
        return f"sha256:{inner}"

    # 分两层而不是把名字混进同一遍摘要：名字和路径都是任意字符串，同一遍里
    # 拼接会有歧义（一个叫 name 的文件能构造出和某个名字一样的字节序列）。
    named = hashlib.sha256()
    named.update(_NAME_DOMAIN)
    named.update(name.encode("utf-8"))
    named.update(b"\x00")
    named.update(inner.encode("ascii"))
    return f"sha256:{named.hexdigest()}"


def _validate_budget(
    files: Sequence[SkillFile],
    *,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> None:
    if len(files) > max_files:
        raise MaterializeError(f"文件数超限：{len(files)} > {max_files}")

    total = 0
    for item in files:
        size = len(item.data)
        # 单文件上限不随 bundle 放宽：一个文件多大，和一组里有几个 skill 无关。
        if size > MAX_FILE_BYTES:
            raise MaterializeError(f"单文件超限：{item.path} 为 {size} 字节 > {MAX_FILE_BYTES}")
        total += size
    if total > max_total_bytes:
        raise MaterializeError(f"总字节数超限：{total} > {max_total_bytes}")


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

    return _write_tree(files, safe_paths, skill_root.resolve(strict=True))


def _write_tree(
    files: Sequence[SkillFile],
    safe_paths: Sequence[PurePosixPath],
    root: Path,
) -> Path:
    """把校验过的路径写进 ``root``。落盘前还有两道兜底，不依赖上游校验。"""
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


def materialize_bundle(bundle: SkillBundle, dest: Path) -> Path:
    """把一组耦合 skill 写进 ``dest/skills/``，返回该目录（catalog 根）。

    返回的是**父目录**而不是某个 skill 目录，这是与 :func:`materialize` 的
    关键区别：SkillEvaluator 对着一个"自身没有 SKILL.md、但含 ``*/SKILL.md``"
    的目录会自动按 catalog 逐个评，每个成员一份独立报告。成员的兄弟目录因此
    留在盘上，跨 skill 的相对链接能解析——单独物化一个 skill 时它们全是死链，
    那是我们的物化方式造成的误报，不是 skill 的问题。

    成员目录名直接用仓库里的名字，不像单 skill 那样用管理系统的登记名：
    一次提交对应多个 skill，登记名只有一个，给不出 N 个；而仓库里的目录名
    本来就是作者写在 frontmatter 里的那个，name_consistency 该怎么判就怎么判。
    """
    files = list(bundle.files)
    _validate_budget(files, max_files=MAX_BUNDLE_FILES, max_total_bytes=MAX_BUNDLE_TOTAL_BYTES)

    if not bundle.members:
        raise MaterializeError("bundle 没有任何成员")
    for member in bundle.members:
        if len(safe_relative_path(member).parts) != 1:
            raise MaterializeError(f"成员名必须是单段目录名：{member!r}")

    safe_paths = [safe_relative_path(item.path) for item in files]

    seen: set[str] = set()
    for path in safe_paths:
        key = path.as_posix()
        if key in seen:
            raise MaterializeError(f"路径重复：{key}")
        seen.add(key)

    for member in bundle.members:
        if f"{member}/{SKILL_MANIFEST}" not in seen:
            raise MaterializeError(f"成员 {member!r} 下没有 {SKILL_MANIFEST}")

    # catalog 根上不能有 SKILL.md：有的话 SkillEvaluator 会把整个目录当成
    # 一个 skill，静默退回单 skill 模式，成员一个都不会被单独评。
    if SKILL_MANIFEST in seen:
        raise MaterializeError(f"bundle 根目录不能有 {SKILL_MANIFEST}，那样会被当成单个 skill")

    dest = dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise MaterializeError(f"目标目录非空：{dest}")

    catalog_root = dest / SKILLS_PARENT
    catalog_root.mkdir(parents=True, exist_ok=True)

    return _write_tree(files, safe_paths, catalog_root.resolve(strict=True))


def cleanup(path: Path) -> None:
    """删除物化目录。评测结束后必须调用，避免 work 目录无限增长。"""
    shutil.rmtree(path, ignore_errors=True)
