"""zip 解归档的安全测试。

管理系统按一个 skill 一个 zip 提供内容，解归档因此落在我们这边。
物化层防的是路径，不是归档格式——这些用例覆盖它挡不住的那一类。
"""

from __future__ import annotations

import io
import zipfile

import pytest

from skillprism.archive import ArchiveError, read_skill_bundle, read_skill_zip
from skillprism.materialize import MAX_FILE_BYTES, MAX_FILES

MANIFEST = b"---\nname: demo\ndescription: A demo skill.\n---\n\n# Demo\n"


def build_zip(entries, *, symlinks=(), mode_by_name=None) -> bytes:
    """构造一个 zip。entries 是 (路径, 字节) 列表。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name)
            # 传入 ZipInfo 时 compress_type 默认为 STORED，不会继承 ZipFile 的压缩设置
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in symlinks:
                info.external_attr = (0o120777 << 16)  # S_IFLNK
            elif mode_by_name and name in mode_by_name:
                info.external_attr = mode_by_name[name] << 16
            z.writestr(info, data)
    return buf.getvalue()


def test_reads_flat_archive():
    data = build_zip([("SKILL.md", MANIFEST), ("scripts/run.sh", b"echo hi\n")])
    files = read_skill_zip(data)
    assert {f.path for f in files} == {"SKILL.md", "scripts/run.sh"}
    assert next(f for f in files if f.path == "SKILL.md").data == MANIFEST


def test_strips_single_top_level_directory():
    """打包时带一层以 skill 名命名的目录是常见做法，要能剥掉。"""
    data = build_zip([("my-skill/SKILL.md", MANIFEST), ("my-skill/ref/a.md", b"a")])
    files = read_skill_zip(data)
    assert {f.path for f in files} == {"SKILL.md", "ref/a.md"}


def test_does_not_strip_when_manifest_at_root():
    """根上已有 SKILL.md 时不能误剥，否则会丢文件。"""
    data = build_zip([("SKILL.md", MANIFEST), ("docs/a.md", b"a")])
    files = read_skill_zip(data)
    assert {f.path for f in files} == {"SKILL.md", "docs/a.md"}


@pytest.mark.parametrize(
    "name",
    ["../escape.md", "a/../../escape.md", "/etc/passwd", "C:/win.md", "sub\\file.md"],
)
def test_rejects_zip_slip(name):
    data = build_zip([("SKILL.md", MANIFEST), (name, b"x")])
    with pytest.raises(ArchiveError, match="不安全路径"):
        read_skill_zip(data)


def test_rejects_symlink_entries():
    """zip 能存符号链接，解出来就是任意文件读写的入口。"""
    data = build_zip([("SKILL.md", MANIFEST), ("link", b"/etc/passwd")], symlinks={"link"})
    with pytest.raises(ArchiveError, match="符号链接"):
        read_skill_zip(data)


def test_rejects_non_regular_entries():
    fifo_mode = 0o010644  # S_IFIFO
    data = build_zip(
        [("SKILL.md", MANIFEST), ("pipe", b"")],
        mode_by_name={"pipe": fifo_mode, "SKILL.md": 0o100644},
    )
    with pytest.raises(ArchiveError, match="非普通文件"):
        read_skill_zip(data)


def test_rejects_duplicate_entries():
    """zip 允许同名条目，后写覆盖前写，可以把内容藏在被覆盖的那份里。"""
    data = build_zip([("SKILL.md", MANIFEST), ("a.md", b"first"), ("a.md", b"second")])
    with pytest.raises(ArchiveError, match="重复条目"):
        read_skill_zip(data)


def test_rejects_tampered_declared_size():
    """声明的 file_size 被篡改时必须干净拒绝，而不是抛未捕获异常。

    Python 的 zipfile 会先因 CRC 不符抛 BadZipFile；关键是我们把它收敛成
    ArchiveError，否则恶意归档能让 worker 崩掉。
    """
    payload = b"\0" * (MAX_FILE_BYTES + 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("SKILL.md", MANIFEST)
        info = zipfile.ZipInfo("bomb.bin")
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, payload)
        # 篡改中央目录里的声明值，模拟撒谎的归档
        z.filelist[-1].file_size = 10
    with pytest.raises(ArchiveError, match="损坏或被篡改|实际大小超限"):
        read_skill_zip(buf.getvalue())


def test_rejects_oversized_entry_by_declared_size():
    """诚实声明的超大条目由预筛拦下，读都不用读。"""
    data = build_zip([("SKILL.md", MANIFEST), ("big.bin", b"A" * (MAX_FILE_BYTES + 1))])
    with pytest.raises(ArchiveError, match="声明大小超限"):
        read_skill_zip(data)


def test_rejects_truncated_archive():
    """截断的归档同样要收敛成 ArchiveError。"""
    data = build_zip([("SKILL.md", MANIFEST), ("a.md", b"x" * 5000)])
    with pytest.raises(ArchiveError):
        read_skill_zip(data[: len(data) // 2])


def test_rejects_high_compression_ratio():
    """高度可压缩的大文件是解压炸弹的典型形态。"""
    payload = b"\0" * (4 * 1024 * 1024)
    data = build_zip([("SKILL.md", MANIFEST), ("bomb.bin", payload)])
    with pytest.raises(ArchiveError, match="压缩比异常"):
        read_skill_zip(data)


def test_rejects_too_many_entries():
    entries = [("SKILL.md", MANIFEST)]
    entries += [(f"f{i}.md", b"x") for i in range(MAX_FILES + 1)]
    with pytest.raises(ArchiveError, match="条目数超限"):
        read_skill_zip(build_zip(entries))


def test_requires_manifest_at_root():
    """没有 SKILL.md 的包要被明确指认为"不是 skill"，而不是"包坏了"。

    管理系统还托管 Commands / Agents / Hooks，那些包里本来就没有
    SKILL.md。两种情况的处理方式完全不同——一个找触发方查过滤，
    一个找上传的用户——所以文案必须分得开。
    """
    data = build_zip([("README.md", b"x"), ("docs/a.md", b"y")])
    with pytest.raises(ArchiveError, match="不是一个可评测的 skill"):
        read_skill_zip(data)


def test_rejects_manifest_nested_two_levels():
    """SKILL.md 埋在两层目录下不是我们能安全推断的布局。"""
    data = build_zip([("a/b/SKILL.md", MANIFEST)])
    with pytest.raises(ArchiveError, match="SKILL.md"):
        read_skill_zip(data)


def test_rejects_empty_archive():
    with pytest.raises(ArchiveError, match="为空"):
        read_skill_zip(build_zip([]))


def test_rejects_non_zip_bytes():
    with pytest.raises(ArchiveError, match="不是合法的 zip"):
        read_skill_zip(b"this is not a zip file")


def test_directory_entries_are_skipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("sub/"), b"")
        z.writestr("SKILL.md", MANIFEST)
        z.writestr("sub/a.md", b"a")
    files = read_skill_zip(buf.getvalue())
    assert {f.path for f in files} == {"SKILL.md", "sub/a.md"}


# ---- 调用方声明子目录（Git 归档接入）----


def test_subdir_strips_git_archive_prefix():
    """Git 服务端打的包形如 <repo>-<ref>-<sha>/<子目录>/…，前缀含 sha，猜不出来。"""
    data = build_zip(
        [
            ("repo-main-abc123/skills/foo/SKILL.md", MANIFEST),
            ("repo-main-abc123/skills/foo/ref/a.md", b"a"),
        ]
    )
    files = read_skill_zip(data, subdir="skills/foo")
    assert {f.path for f in files} == {"SKILL.md", "ref/a.md"}


def test_subdir_without_archive_root():
    """没有顶层目录时也要能剥——不同 Git 服务端的打包形态不一样。"""
    data = build_zip([("skills/foo/SKILL.md", MANIFEST), ("skills/foo/a.md", b"a")])
    files = read_skill_zip(data, subdir="skills/foo")
    assert {f.path for f in files} == {"SKILL.md", "a.md"}


def test_subdir_mismatch_is_rejected():
    """声明的子目录对不上就报错，不去试第二种解读。"""
    data = build_zip([("repo-main-abc123/skills/bar/SKILL.md", MANIFEST)])
    with pytest.raises(ArchiveError, match="没有声明的子目录"):
        read_skill_zip(data, subdir="skills/foo")


def test_subdir_does_not_bypass_manifest_check():
    """剥前缀之后仍然必须有 SKILL.md，声明子目录不是豁免。"""
    data = build_zip([("repo-main-abc123/skills/foo/README.md", b"x")])
    with pytest.raises(ArchiveError, match="不是一个可评测的 skill"):
        read_skill_zip(data, subdir="skills/foo")


def test_subdir_does_not_swallow_files_outside_it():
    """子目录之外还有文件时必须报错，不能悄悄丢掉——残缺的 skill 比失败更有害。"""
    data = build_zip(
        [
            ("repo-main-abc123/skills/foo/SKILL.md", MANIFEST),
            ("repo-main-abc123/README.md", b"x"),
        ]
    )
    with pytest.raises(ArchiveError, match="没有声明的子目录"):
        read_skill_zip(data, subdir="skills/foo")


def test_subdir_still_rejects_symlinks():
    """声明子目录不绕过任何一道归档防线。"""
    data = build_zip(
        [
            ("repo-main-abc123/skills/foo/SKILL.md", MANIFEST),
            ("repo-main-abc123/skills/foo/link", b"/etc/passwd"),
        ],
        symlinks=("repo-main-abc123/skills/foo/link",),
    )
    with pytest.raises(ArchiveError, match="符号链接"):
        read_skill_zip(data, subdir="skills/foo")


# ---- 一组耦合 skill（bundle）----


def test_reads_bundle_with_shared_files():
    """成员之外的共享文件必须保留——耦合的典型形态就是几个 skill 共引一份约定。"""
    data = build_zip(
        [
            ("code-review/SKILL.md", MANIFEST),
            ("test-gen/SKILL.md", MANIFEST),
            ("shared/api.md", b"# api"),
            ("README.md", b"# bundle"),
        ]
    )
    bundle = read_skill_bundle(data)
    assert bundle.members == ["code-review", "test-gen"]
    assert {f.path for f in bundle.files} == {
        "code-review/SKILL.md",
        "test-gen/SKILL.md",
        "shared/api.md",
        "README.md",
    }


def test_bundle_strips_git_archive_prefix():
    data = build_zip(
        [
            ("repo-main-abc123/skills/a/SKILL.md", MANIFEST),
            ("repo-main-abc123/skills/b/SKILL.md", MANIFEST),
        ]
    )
    bundle = read_skill_bundle(data, subdir="skills")
    assert bundle.members == ["a", "b"]
    assert {f.path for f in bundle.files} == {"a/SKILL.md", "b/SKILL.md"}


def test_single_member_bundle_is_not_flattened():
    """只有一个成员时顶层目录名与成员名重合，剥掉就把一组误读成单个。"""
    data = build_zip([("code-review/SKILL.md", MANIFEST), ("code-review/ref.md", b"r")])
    bundle = read_skill_bundle(data)
    assert bundle.members == ["code-review"]
    assert {f.path for f in bundle.files} == {"code-review/SKILL.md", "code-review/ref.md"}


def test_bundle_rejects_single_skill_archive():
    """声明了一组、内容却是一个，要当场说出来，不替调用方改判。"""
    data = build_zip([("SKILL.md", MANIFEST), ("ref.md", b"r")])
    with pytest.raises(ArchiveError, match="这是单个 skill"):
        read_skill_bundle(data)


def test_bundle_requires_at_least_one_member():
    data = build_zip([("docs/a.md", b"a"), ("README.md", b"b")])
    with pytest.raises(ArchiveError, match="不是一组可评测的 skill"):
        read_skill_bundle(data)


def test_bundle_ignores_manifests_nested_deeper():
    """catalog 模式 glob 的是 */SKILL.md，埋更深的不算成员，这里跟着它。"""
    data = build_zip([("a/SKILL.md", MANIFEST), ("b/nested/SKILL.md", MANIFEST)])
    bundle = read_skill_bundle(data)
    assert bundle.members == ["a"]


def test_bundle_keeps_every_archive_defence():
    """bundle 与单 skill 共用同一条安全流水线，不是另开一条。"""
    data = build_zip(
        [("a/SKILL.md", MANIFEST), ("a/link", b"/etc/passwd")],
        symlinks=("a/link",),
    )
    with pytest.raises(ArchiveError, match="符号链接"):
        read_skill_bundle(data)

    with pytest.raises(ArchiveError, match="不安全路径"):
        read_skill_bundle(build_zip([("a/SKILL.md", MANIFEST), ("../escape.md", b"x")]))


def test_bundle_rejects_too_many_members():
    from skillprism.materialize import MAX_BUNDLE_MEMBERS

    data = build_zip(
        [(f"skill-{i}/SKILL.md", MANIFEST) for i in range(MAX_BUNDLE_MEMBERS + 1)]
    )
    with pytest.raises(ArchiveError, match="成员数超限"):
        read_skill_bundle(data)
