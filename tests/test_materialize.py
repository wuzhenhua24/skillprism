"""物化层安全测试。

存储里的路径是不可信输入，这些用例锁定“不会写到目标目录之外”。
"""

from __future__ import annotations

import pytest

from skillprism.materialize import (
    MaterializeError,
    SkillFile,
    UnsafePathError,
    compute_content_hash,
    materialize,
    safe_relative_path,
)

MANIFEST = SkillFile(path="SKILL.md", data=b"---\nname: demo\n---\n")


@pytest.mark.parametrize(
    "raw",
    [
        "../escape.md",
        "a/../../escape.md",
        "/etc/passwd",
        "C:/Windows/system32",
        "dir\\file.md",
        "~/secrets",
        "",
        "   ",
        "bad\x00name.md",
        "ctrl\x07char.md",
        "dir/trailing /file.md",
        "trailing.",
        "con.md",
        "sub/../../out.md",
    ],
)
def test_rejects_unsafe_paths(raw: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_relative_path(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("SKILL.md", "SKILL.md"),
        ("scripts/run.sh", "scripts/run.sh"),
        ("a/b/c/deep.md", "a/b/c/deep.md"),
        # 规范化后仍在根目录内的形式是安全的，接受
        ("./SKILL.md", "SKILL.md"),
        ("  SKILL.md  ", "SKILL.md"),
    ],
)
def test_accepts_normal_paths(raw: str, expected: str) -> None:
    assert safe_relative_path(raw).as_posix() == expected


def test_materialize_writes_inside_dest(tmp_path):
    files = [MANIFEST, SkillFile(path="scripts/run.sh", data=b"echo hi\n")]
    root = materialize(files, tmp_path / "skill", name="demo")

    assert (root / "SKILL.md").read_bytes() == MANIFEST.data
    assert (root / "scripts" / "run.sh").read_bytes() == b"echo hi\n"
    assert sorted(p.name for p in (tmp_path / "skill").rglob("*") if p.is_file()) == [
        "SKILL.md",
        "run.sh",
    ]


def test_materialize_uses_skills_parent_and_real_name(tmp_path):
    """目录布局必须是 skills/<name>/。

    用固定目录名（如 "skill"）会让 SkillEvaluator 的 name_consistency 报
    HIGH，folder_hierarchy 报 MEDIUM——两条都是我们的布局造成的误报。
    """
    root = materialize([MANIFEST], tmp_path / "w", name="api-and-interface-design")
    assert root.name == "api-and-interface-design"
    assert root.parent.name == "skills"


def test_materialize_rejects_name_with_separator(tmp_path):
    with pytest.raises((MaterializeError, UnsafePathError)):
        materialize([MANIFEST], tmp_path / "w", name="team/skill")


def test_materialize_rejects_traversal(tmp_path):
    files = [MANIFEST, SkillFile(path="../escape.md", data=b"x")]
    with pytest.raises(UnsafePathError):
        materialize(files, tmp_path / "skill", name="demo")
    assert not (tmp_path / "escape.md").exists()


def test_materialize_requires_manifest(tmp_path):
    with pytest.raises(MaterializeError, match="SKILL.md"):
        materialize([SkillFile(path="README.md", data=b"x")], tmp_path / "skill", name="demo")


def test_materialize_rejects_duplicate_paths(tmp_path):
    files = [MANIFEST, SkillFile(path="a.md", data=b"1"), SkillFile(path="a.md", data=b"2")]
    with pytest.raises(MaterializeError, match="重复"):
        materialize(files, tmp_path / "skill", name="demo")


def test_materialize_rejects_nonempty_dest(tmp_path):
    dest = tmp_path / "skill"
    dest.mkdir()
    (dest / "leftover.md").write_text("old")
    with pytest.raises(MaterializeError, match="非空"):
        materialize([MANIFEST], dest, name="demo")


def test_content_hash_is_order_independent():
    a = [MANIFEST, SkillFile(path="b.md", data=b"B")]
    b = [SkillFile(path="b.md", data=b"B"), MANIFEST]
    assert compute_content_hash(a, name="demo") == compute_content_hash(b, name="demo")


def test_content_hash_changes_with_content():
    base = compute_content_hash([MANIFEST], name="demo")
    changed = compute_content_hash(
        [SkillFile(path="SKILL.md", data=b"different")], name="demo"
    )
    assert base != changed


def test_content_hash_changes_with_path():
    a = compute_content_hash([MANIFEST, SkillFile(path="x.md", data=b"same")], name="demo")
    b = compute_content_hash([MANIFEST, SkillFile(path="y.md", data=b"same")], name="demo")
    assert a != b


def test_content_hash_changes_with_the_directory_name():
    """同样的字节铺成不同的目录名，是两份内容——因为它们是两个结论。

    ``SCHEMA.name_consistency`` 拿目录名和 frontmatter 的 name 比对：实测同一份
    SKILL.md（``name: alpha``）铺进 alpha/ 是 82.0/B，铺进 beta/ 是 79.5/C 并多
    一条 HIGH。哈希不带目录名的话，这两个结论会互相复用——一组里两个同内容
    成员互相顶替，或者换个登记名重传就把那条 HIGH 复用没了。
    """
    files = [MANIFEST]
    assert compute_content_hash(files, name="alpha") != compute_content_hash(
        files, name="beta"
    )
    # 名字相同就仍然是同一份内容：换个资源 ID 重传照样命中缓存，
    # 那正是复用当初要解决的场景。
    assert compute_content_hash(files, name="alpha") == compute_content_hash(
        files, name="alpha"
    )


def test_content_hash_without_a_name_is_a_different_key():
    """``name=None``（整组指纹）和某个具体名字不能撞上。

    组指纹与成员哈希都存在 evaluation_result 的同一批列里被比较，
    两者混淆会让"整组"被当成"某个成员"。
    """
    files = [MANIFEST]
    assert compute_content_hash(files, name=None) != compute_content_hash(
        files, name=""
    )
    assert compute_content_hash(files, name=None) != compute_content_hash(
        files, name="demo"
    )
