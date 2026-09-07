"""一组耦合 skill 的单元测试：物化布局、复用判据、报告寻址、成员 ID。

这几处的共同点是错了不报警——布局不对只是分数变差，复用判据漏了只是给出
一个看起来正常的过期结论。所以都要单独钉住。
"""

from __future__ import annotations

import pytest

from skillprism.domain import EvaluationStatus
from skillprism.materialize import (
    SKILL_MANIFEST,
    MaterializeError,
    SkillBundle,
    SkillFile,
    materialize_bundle,
)
from skillprism.storage import LocalReportStorage
from skillprism.worker import _member_skill_id, _worst_status

MANIFEST = b"---\nname: demo\ndescription: A demo skill.\n---\n"


def make_bundle(*members: str, extra: dict[str, bytes] | None = None) -> SkillBundle:
    files = [SkillFile(path=f"{m}/{SKILL_MANIFEST}", data=MANIFEST) for m in members]
    for path, data in (extra or {}).items():
        files.append(SkillFile(path=path, data=data))
    return SkillBundle(files=files, members=sorted(members))


def test_materialize_bundle_produces_a_catalog_root(tmp_path):
    """返回父目录而不是某个 skill 目录：SkillEvaluator 靠这个进 catalog 模式。"""
    bundle = make_bundle("code-review", "test-gen", extra={"shared/api.md": b"# api"})
    root = materialize_bundle(bundle, tmp_path / "work")

    assert root.name == "skills"
    assert not (root / SKILL_MANIFEST).exists(), "catalog 根上不能有 SKILL.md"
    assert sorted(p.name for p in root.iterdir()) == ["code-review", "shared", "test-gen"]
    assert (root / "code-review" / SKILL_MANIFEST).read_bytes() == MANIFEST
    # 共享文件必须留着：跨 skill 链接指向的就是它们。
    assert (root / "shared" / "api.md").read_bytes() == b"# api"


def test_sibling_paths_resolve_from_a_member(tmp_path):
    """`../test-gen/SKILL.md` 这种引用要能在盘上解析——本次改动的全部意义。"""
    bundle = make_bundle("code-review", "test-gen", extra={"shared/api.md": b"# api"})
    root = materialize_bundle(bundle, tmp_path / "work")

    member = root / "code-review"
    assert (member / ".." / "test-gen" / SKILL_MANIFEST).resolve().is_file()
    assert (member / ".." / "shared" / "api.md").resolve().is_file()


def test_materialize_bundle_rejects_manifest_at_catalog_root(tmp_path):
    """根上有 SKILL.md 会让 SkillEvaluator 把整个目录当成一个 skill，
    静默退回单 skill 模式，成员一个都不会被单独评。"""
    bundle = SkillBundle(
        files=[
            SkillFile(path=SKILL_MANIFEST, data=MANIFEST),
            SkillFile(path=f"a/{SKILL_MANIFEST}", data=MANIFEST),
        ],
        members=["a"],
    )
    with pytest.raises(MaterializeError, match="不能有"):
        materialize_bundle(bundle, tmp_path / "work")


def test_materialize_bundle_rejects_member_without_manifest(tmp_path):
    bundle = SkillBundle(files=[SkillFile(path="a/ref.md", data=b"x")], members=["a"])
    with pytest.raises(MaterializeError, match="没有"):
        materialize_bundle(bundle, tmp_path / "work")


@pytest.mark.parametrize("member", ["../escape", "a/b", "", "."])
def test_materialize_bundle_rejects_unsafe_member_names(member, tmp_path):
    bundle = SkillBundle(
        files=[SkillFile(path=f"{member}/{SKILL_MANIFEST}", data=MANIFEST)],
        members=[member],
    )
    with pytest.raises((MaterializeError, ValueError)):
        materialize_bundle(bundle, tmp_path / "work")


def test_member_skill_id_matches_standalone_submission():
    """成员的 skill_id 就是它单独提交时会用的那个，管理系统不需要第二套查法。"""
    assert _member_skill_id("group/repo:skills", "code-review") == "group/repo:skills/code-review"
    assert _member_skill_id("group/repo:skills/", "code-review") == "group/repo:skills/code-review"


def test_worst_status_is_the_weakest_link():
    """"这套工作流能不能用"取决于最弱的一环，不做加权也不做多数决。"""
    assert _worst_status([EvaluationStatus.PASSED, EvaluationStatus.PASSED]) is EvaluationStatus.PASSED
    assert _worst_status([EvaluationStatus.PASSED, EvaluationStatus.FAILED]) is EvaluationStatus.FAILED
    assert (
        _worst_status([EvaluationStatus.PASSED, EvaluationStatus.INCOMPLETE])
        is EvaluationStatus.INCOMPLETE
    )
    assert (
        _worst_status([EvaluationStatus.FAILED, EvaluationStatus.ERROR]) is EvaluationStatus.ERROR
    )
    # 空集合没有"整体通过"可言，报 ERROR 而不是默默算通过。
    assert _worst_status([]) is EvaluationStatus.ERROR


def test_report_address_separates_contexts(tmp_path):
    """同一份内容的两种上下文不能共用报告地址，否则后跑的覆盖先跑的，
    而先跑那次的结果行仍指着这个地址。"""
    storage = LocalReportStorage(tmp_path / "reports")
    src = tmp_path / "report.html"

    src.write_text("solo", encoding="utf-8")
    solo_uri = storage.put("sha256:abcdef0123456789", "report.html", src)
    src.write_text("in-bundle", encoding="utf-8")
    bundle_uri = storage.put(
        "sha256:abcdef0123456789", "report.html", src, context_hash="sha256:99887766554433"
    )

    assert solo_uri != bundle_uri
    assert storage.resolve(solo_uri).read_text(encoding="utf-8") == "solo"
    assert storage.resolve(bundle_uri).read_text(encoding="utf-8") == "in-bundle"
