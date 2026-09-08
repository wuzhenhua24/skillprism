"""从 zip 归档解出 skill 文件。

管理系统按一个 skill 一个 zip 的方式提供内容，GitLab 接入取的也是归档
（见 ``content.GitLabArchiveSource`` 为什么不 clone），因此解归档这一步
落在我们这边，随之而来的一整类归档特有风险也归我们负责。物化层防的是
**路径**，不是**归档格式**——以下四类它一个都挡不住：

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
from collections.abc import Callable

from skillprism.materialize import (
    MAX_BUNDLE_FILES,
    MAX_BUNDLE_MEMBERS,
    MAX_BUNDLE_TOTAL_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    SKILL_MANIFEST,
    MaterializeError,
    SkillBundle,
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


def _single_top_level(paths: list[str]) -> str | None:
    """所有条目共有的唯一顶层目录名；没有则返回 None。"""
    roots = {p.split("/", 1)[0] for p in paths if "/" in p}
    if len(roots) != 1:
        return None
    root = roots.pop()
    if any(not p.startswith(f"{root}/") for p in paths):
        return None
    return root


def _strip_common_root(paths: list[str]) -> str | None:
    """若所有条目都在同一个顶层目录下，返回该目录名。

    管理系统打包时可能带一层以 skill 名命名的顶层目录
    （``my-skill/SKILL.md``），也可能直接把文件放在根上（``SKILL.md``）。
    两种都得支持，且不能靠猜——以 SKILL.md 的实际位置为准。

    只认一层。埋得更深的布局有多种解读，这里不猜——需要更深的前缀时由
    调用方通过 ``subdir`` 明确声明，见 :func:`_prefix_for_subdir`。
    """
    if any(p == SKILL_MANIFEST for p in paths):
        return None  # 文件已在根上，不剥离

    root = _single_top_level(paths)
    if root is None:
        return None
    if f"{root}/{SKILL_MANIFEST}" not in paths:
        return None
    return root


def _prefix_for_subdir(paths: list[str], subdir: str) -> str:
    """调用方声明了 skill 在归档里的子目录时，算出要剥掉的前缀。

    与 :func:`_strip_common_root` 的区别在于这里**不推断**：前缀里的子目录
    是调用方给出的（GitLab 源知道自己按哪个 path 取的归档），归档里没有这个
    子目录就报错，不去试第二种解读。

    只接受两种前缀：``<subdir>/``，以及带一层归档顶层目录的
    ``<root>/<subdir>/``——后者是 Git 服务端打包的固定形态，顶层目录名
    形如 ``<repo>-<ref>-<sha>``，含 sha，事先猜不出来。有顶层目录时两个候选
    互斥（``_single_top_level`` 要求**所有**条目都在该目录下），不存在选错的
    情况。

    判据是"**有**条目落在前缀下"，不是"**所有**条目都落在前缀下"：取档 URL
    上的 ``path`` 只是一个优化，不是保证——老版 GitLab（早于 archive.zip
    支持 ``path`` 的版本）会忽略它、返回整仓归档。要求整包都在子目录下的话，
    这类实例上每一次带子目录的取档都必然失败。改成筛选之后，服务端过没过滤
    得到的文件集完全一样，``content_hash`` 也就不会因为 GitLab 版本而变。

    子目录之外的条目一律丢弃，这不会让 skill 残缺：按定义它们不属于
    ``<subdir>/`` 这棵子树。
    """
    candidates = []
    root = _single_top_level(paths)
    if root is not None:
        candidates.append(f"{root}/{subdir}/")
    candidates.append(f"{subdir}/")

    for prefix in candidates:
        if any(p.startswith(prefix) for p in paths):
            return prefix

    raise ArchiveError(
        f"归档里没有声明的子目录 {subdir!r}（条目形如 {paths[0]!r}）"
    )


def read_skill_zip(data: bytes, *, subdir: str | None = None) -> list[SkillFile]:
    """把一个 skill 的 zip 解成文件列表。

    ``subdir`` 声明 skill 在归档里的子目录。给了就只取这棵子树、归档里没有
    这个子目录才报错；不给则沿用"根上或单层顶层目录"的推断。这个口子是给
    Git 归档用的：那边的条目形如 ``<repo>-<ref>-<sha>/skills/foo/SKILL.md``，
    层级由调用方的取档参数决定，不该由这里去猜。

    任何一条防线被触发就整体拒绝，不做部分解出——一个残缺的 skill 评出来
    的结果比评测失败更有害，因为它看起来是有效的。
    """
    subdir = subdir.strip("/") if subdir else None

    def resolve(paths: list[str]) -> str:
        if subdir:
            return _prefix_for_subdir(paths, subdir)
        root = _strip_common_root(paths)
        return f"{root}/" if root else ""

    def require_manifest(paths: list[str]) -> None:
        if SKILL_MANIFEST not in paths:
            # 措辞要和"包损坏"区分开：内容下下来了、也解开了，只是它不是
            # 一个 skill。管理系统还托管 Commands / Agents / Hooks 等分类，
            # 那些包里本来就没有 SKILL.md，报"归档无法解出"会把人带偏。
            where = f"{subdir}/ 下" if subdir else "根目录"
            raise ArchiveError(f"归档{where}缺少 {SKILL_MANIFEST}，不是一个可评测的 skill")

    return _extract(
        data,
        max_files=MAX_FILES,
        max_total_bytes=MAX_TOTAL_BYTES,
        resolve_prefix=resolve,
        validate_layout=require_manifest,
    )


def read_skill_bundle(
    data: bytes, *, subdir: str | None = None, max_members: int = MAX_BUNDLE_MEMBERS
) -> SkillBundle:
    """把一组耦合 skill 的 zip 解成一个 :class:`SkillBundle`。

    与 :func:`read_skill_zip` 走同一条安全流水线（符号链接、解压炸弹、
    zip slip、重复条目、体积上限），只是布局判据不同：这里要求根上**没有**
    ``SKILL.md``、而有若干个直接含 ``SKILL.md`` 的一级子目录——正是
    SkillEvaluator 进 catalog 模式的条件。

    根上有 ``SKILL.md`` 就明确报错而不是降级成单 skill：调用方声明了这是
    一组，内容却是一个，这种不一致要当场说出来，不能替它改判。

    ``max_members`` 由调用方给（部署可调，见 ``Settings.max_bundle_members``），
    默认取模块常量。上限本身来自配置，但读配置的事不进这里——这个函数保持
    纯函数，才能直接拿构造出来的恶意归档做测试，不需要起服务、也不需要环境
    变量。其余几条上限（条目数、总量、单文件）仍是常量：它们防的是归档本身的
    形状，与"这个部署对接的仓库有多大"无关。
    """
    subdir = subdir.strip("/") if subdir else None

    def resolve(paths: list[str]) -> str:
        if subdir:
            return _prefix_for_subdir(paths, subdir)
        # 先试不剥。条目已经形如 ``<成员>/SKILL.md`` 时就不该动——只有一个
        # 成员时，顶层目录名和成员名是同一个，剥掉就把 bundle 误读成单 skill。
        if _bundle_members(paths):
            return ""
        root = _single_top_level(paths)
        stripped = paths
        if root is not None:
            stripped = [p[len(root) + 1 :] for p in paths]
            if _bundle_members(stripped):
                return f"{root}/"
            _reject_if_single_skill(stripped)
        _reject_if_single_skill(paths)
        # 提示按剥掉归档顶层目录后的路径给：调用方声明的子目录是仓库内相对
        # 路径，带上 <repo>-<ref>-<sha> 那层前缀的建议它根本填不进去。
        raise _no_members_error(stripped)

    def require_members(paths: list[str]) -> None:
        # subdir 路径上前缀是调用方给的，剥完才知道底下是一个还是一组。
        _reject_if_single_skill(paths)
        members = _bundle_members(paths)
        if not members:
            raise _no_members_error(paths, base=subdir)
        if len(members) > max_members:
            raise ArchiveError(
                f"成员数超限：{len(members)} > {max_members}"
                "（部署可调：SKILLPRISM_MAX_BUNDLE_MEMBERS）"
            )

    files = _extract(
        data,
        max_files=MAX_BUNDLE_FILES,
        max_total_bytes=MAX_BUNDLE_TOTAL_BYTES,
        resolve_prefix=resolve,
        validate_layout=require_members,
    )
    return SkillBundle(files=files, members=_bundle_members([f.path for f in files]))


#: 内容不是一组 skill 时的两条文案。分开写是因为处理方式不同：一个要调用方
#: 改提交方式，一个要它去查仓库布局。
_NO_MEMBERS = f"归档里没有任何直接含 {SKILL_MANIFEST} 的一级子目录，不是一组可评测的 skill"
_IS_SINGLE_SKILL = (
    f"归档根目录就有 {SKILL_MANIFEST}：这是单个 skill，不是一组。"
    "按单 skill 提交，或把 skill_id 指向包含多个 skill 的父目录"
)


#: Claude plugin 仓的标志目录。它自己不含 skill，但它在场足以说明这个仓是
#: 按 plugin 打包的——那种布局里 skill 固定在 ``skills/`` 下。
_PLUGIN_MARKER = ".claude-plugin"

#: 提示里最多列几个候选目录。一个 marketplace 仓能装几十个 plugin，
#: 全列出来的报错没人读。
_MAX_HINTED_SUBDIRS = 3


def _catalog_candidates(paths: list[str]) -> list[str]:
    """能当 catalog 根的子目录：其下**直接**含 ``<成员>/SKILL.md``。

    根目录本身不算——根下真有成员就走不到这里。
    """
    return sorted(
        {
            path.rsplit("/", 2)[0]
            for path in paths
            if path.endswith(f"/{SKILL_MANIFEST}") and path.count("/") >= 2
        }
    )


def _no_members_error(paths: list[str], *, base: str | None = None) -> ArchiveError:
    """"找不到成员"的报错，尽量指出该把 ``skill_id`` 指到哪一层。

    Claude plugin 仓（``.claude-plugin/`` 加 ``skills/<名>/SKILL.md``）会稳定
    撞上这条：仓库里确实有一堆 skill，只是埋在 ``skills/`` 下，而这里只认
    一级子目录。笼统地说"没有成员"会把人支去查仓库布局，实际要改的是
    ``skill_id`` 多带一段子目录。

    **只改文案，不自动认 ``skills/``。** 布局由调用方声明、对不上就报错是这个
    模块的前提（见 :func:`_prefix_for_subdir`）；自动推断会让"skill_id 少写
    一层"重新变成静默评错一批东西。

    ``base`` 是调用方已经声明的子目录，候选要接在它后面才是能直接填回
    ``skill_id`` 的那一段。
    """
    candidates = _catalog_candidates(paths)
    if not candidates:
        return ArchiveError(_NO_MEMBERS)

    prefix = f"{base.strip('/')}/" if base else ""
    shown = [f"{prefix}{name}" for name in candidates[:_MAX_HINTED_SUBDIRS]]
    hint = "、".join(shown)
    if len(candidates) > len(shown):
        hint += f"（另有 {len(candidates) - len(shown)} 个）"

    what = (
        f"这是一个 Claude plugin 仓（含 {_PLUGIN_MARKER}/）"
        if any(_PLUGIN_MARKER in path.split("/") for path in paths)
        else "skill 埋在更深的层级"
    )
    return ArchiveError(f"{_NO_MEMBERS}：{what}。把 skill_id 的子目录指到 {hint}")


def _reject_if_single_skill(paths: list[str]) -> None:
    """根上有 SKILL.md 就是单个 skill，报专门的文案而不是笼统的"找不到成员"。"""
    if SKILL_MANIFEST in paths:
        raise ArchiveError(_IS_SINGLE_SKILL)


def _bundle_members(paths: list[str]) -> list[str]:
    """根下直接含 ``SKILL.md`` 的一级子目录名，排序后返回。

    只认一级：SkillEvaluator 的 catalog 模式 glob 的就是 ``*/SKILL.md``，
    埋得更深的目录它不会当成 skill，这里跟着它，不自作主张多认一层。
    """
    return sorted(
        {
            path[: -(len(SKILL_MANIFEST) + 1)]
            for path in paths
            if path.endswith(f"/{SKILL_MANIFEST}") and path.count("/") == 1
        }
    )


def _extract(
    data: bytes,
    *,
    max_files: int,
    max_total_bytes: int,
    resolve_prefix: Callable[[list[str]], str],
    validate_layout: Callable[[list[str]], None],
) -> list[SkillFile]:
    """解归档的共享核心：所有防线都在这里，布局判据由调用方给。

    单 skill 与 bundle 只在"什么样的布局算合法"上不同，安全部分必须共用
    一份实现——各写一遍的话，将来加一道防线只会加到其中一边。

    顺序是**先定前缀、筛掉子目录之外的条目，再上防线**。因为归档不一定只装
    着我们要的那棵子树：老版 GitLab 会忽略取档 URL 上的 ``path``、返回整仓
    （见 :func:`_prefix_for_subdir`）。防线跟着"会被物化的条目"走，而不是跟着
    "归档里恰好有什么"走——否则同仓另一个目录里的一个符号链接、一个
    ``aux.c``（Windows 保留名）、或者仅仅是仓库文件多，就能让一个本身干净的
    skill 评不了。被丢弃的条目一个字节都不会读，也不会落盘，对它们设防没有
    意义；反过来，落在前缀内的条目一道防线都不少。
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"不是合法的 zip 归档：{exc}") from exc

    with archive:
        # 条目名先规范化，但校验不过的**先记下来不报错**：前缀是按规范化后的
        # 名字比的（NFC、``./`` 这类差异必须在比较前抹平），而子目录之外的一个
        # 坏名字不该牵连另一棵子树里的 skill。落在前缀内的下面立刻报。
        ok: list[tuple[zipfile.ZipInfo, str]] = []
        unsafe: list[tuple[str, UnsafePathError]] = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            try:
                ok.append((info, safe_relative_path(info.filename).as_posix()))
            except UnsafePathError as exc:
                unsafe.append((info.filename, exc))

        if not ok:
            if unsafe:
                raise ArchiveError(f"归档含不安全路径：{unsafe[0][1]}")
            raise ArchiveError("归档为空")

        prefix = resolve_prefix([path for _, path in ok])

        # 不安全条目按原始名比前缀：规范化失败就没有规范化后的名字可比，宁可
        # 用原始名多报一个，也不放过一个真的落在子树里的。
        for name, exc in unsafe:
            if name.startswith(prefix):
                raise ArchiveError(f"归档含不安全路径：{exc}") from exc

        entries = [(info, path) for info, path in ok if path.startswith(prefix)]

        for info, _ in entries:
            if _is_symlink(info):
                raise ArchiveError(f"归档含符号链接条目：{info.filename!r}")
            if not _is_regular_file(info):
                raise ArchiveError(f"归档含非普通文件条目：{info.filename!r}")

        if len(entries) > max_files:
            raise ArchiveError(f"归档条目数超限：{len(entries)} > {max_files}")

        # 先按声明值快速预筛。声明值不可信，但用来挡住明显过大的归档很便宜。
        declared_total = 0
        for info, _ in entries:
            if info.file_size > MAX_FILE_BYTES:
                raise ArchiveError(f"条目声明大小超限：{info.filename!r} 为 {info.file_size} 字节")
            declared_total += info.file_size
        if declared_total > max_total_bytes:
            raise ArchiveError(f"归档声明总大小超限：{declared_total} > {max_total_bytes}")

        paths = [path[len(prefix) :] for _, path in entries]

        seen: set[str] = set()
        for path in paths:
            if path in seen:
                raise ArchiveError(f"归档含重复条目：{path}")
            seen.add(path)

        validate_layout(paths)

        files: list[SkillFile] = []
        actual_total = 0
        for (info, _), path in zip(entries, paths, strict=True):
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
            if actual_total > max_total_bytes:
                raise ArchiveError(f"归档实际总大小超限：> {max_total_bytes} 字节")

            files.append(SkillFile(path=path, data=blob))

        return files
