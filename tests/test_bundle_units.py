"""一组耦合 skill 的单元测试：物化布局、复用判据、报告寻址、成员 ID，
以及"这次任务产出了哪几条结论"的反查。

这几处的共同点是错了不报警——布局不对只是分数变差，复用判据漏了只是给出
一个看起来正常的过期结论，反查的条件漏一个则是任务接口多列或少列几个成员。
所以都要单独钉住。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from skillprism.config import get_settings, reset_settings
from skillprism.content import member_skill_id
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import ContentSource, EvaluationStatus, TaskState
from skillprism.materialize import (
    SKILL_MANIFEST,
    MaterializeError,
    SkillBundle,
    SkillFile,
    compute_content_hash,
    materialize_bundle,
)
from skillprism.models import Base, EvaluationResult, EvaluationTask
from skillprism.queue import enqueue
from skillprism.repository import bundle_member_results
from skillprism.runner import policy_file_hash
from skillprism.service import task_to_dto
from skillprism.storage import LocalReportStorage
from skillprism.worker import _worst_status, run_once

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
    assert member_skill_id("group/repo:skills", "code-review") == "group/repo:skills/code-review"
    assert member_skill_id("group/repo:skills/", "code-review") == "group/repo:skills/code-review"


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


# ---- 反查：一次 bundle 任务产出了哪几条结论 ----
#
# 结果表刻意不记 task_id（结论按内容复用，会被后来的任务共用，全命中的任务
# 一行都不写），所以关联靠 (来源, context_hash, `<bundle_id>/` 前缀)。这三条
# 缺一条就会多列或漏列成员，而两种错都不报警：任务接口只是把别人的结论说成
# 这次的，或者少说几条。


@pytest.fixture
def factory(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


BUNDLE_ID = "group/repo:skills"
CONTEXT = "sha256:whole-group"


def _member(session, skill_id, content_hash, *, context_hash, source=ContentSource.LOCAL):
    row = EvaluationResult(
        id=str(uuid.uuid4()),
        source=str(source),
        skill_id=skill_id,
        content_hash=content_hash,
        context_hash=context_hash,
        status="passed",
        severity_counts={},
        incomplete_scans=[],
    )
    session.add(row)
    session.flush()
    return row


def test_members_are_found_by_context_and_prefix(factory):
    """一次 bundle 评测的成员，按 (来源, 上下文, 前缀) 反查得回来。"""
    with factory() as session:
        for member, content_hash in (("code-review", "h-cr"), ("test-gen", "h-tg")):
            _member(
                session,
                member_skill_id(BUNDLE_ID, member),
                content_hash,
                context_hash=CONTEXT,
            )

        rows = bundle_member_results(session, ContentSource.LOCAL, BUNDLE_ID, CONTEXT)

        assert [(r.skill_id, r.content_hash) for r in rows] == [
            (f"{BUNDLE_ID}/code-review", "h-cr"),
            (f"{BUNDLE_ID}/test-gen", "h-tg"),
        ]


def test_another_group_with_the_same_content_is_not_this_group(factory):
    """两个 bundle 的内容一模一样时 context_hash 也一样，但它们是两批结论。

    只按 context_hash 反查的话，任务接口会把别人仓库的成员列成这次的产出。
    """
    other = "group/fork:skills"
    with factory() as session:
        _member(session, member_skill_id(BUNDLE_ID, "a"), "h-a", context_hash=CONTEXT)
        _member(session, member_skill_id(other, "a"), "h-a", context_hash=CONTEXT)

        rows = bundle_member_results(session, ContentSource.LOCAL, BUNDLE_ID, CONTEXT)

        assert [r.skill_id for r in rows] == [f"{BUNDLE_ID}/a"]


def test_a_standalone_verdict_is_not_a_member(factory):
    """同一个 skill 单独评过一次（context_hash 为 NULL），那条不属于这次 bundle。

    两条结论的 skill_id 可以完全一样，只有上下文不同——而它们的结论本来
    就不一样，混进来就是把单独评的分数说成整套评的。
    """
    with factory() as session:
        _member(session, member_skill_id(BUNDLE_ID, "a"), "h-a", context_hash=CONTEXT)
        _member(session, member_skill_id(BUNDLE_ID, "a"), "h-solo", context_hash=None)

        rows = bundle_member_results(session, ContentSource.LOCAL, BUNDLE_ID, CONTEXT)

        assert [r.content_hash for r in rows] == ["h-a"]


def test_members_do_not_cross_sources(factory):
    """skill_id 只在一个来源内部唯一，反查也得停在本次任务的来源里。"""
    with factory() as session:
        _member(
            session,
            member_skill_id(BUNDLE_ID, "a"),
            "h-a",
            context_hash=CONTEXT,
            source=ContentSource.GITLAB,
        )

        assert bundle_member_results(session, ContentSource.LOCAL, BUNDLE_ID, CONTEXT) == []
        assert bundle_member_results(session, ContentSource.GITLAB, BUNDLE_ID, CONTEXT)


def test_a_wildcard_in_the_bundle_id_stays_a_literal(factory):
    """skill_id 里的 ``%`` / ``_`` 是字面量，不能当成前缀通配。

    不转义的话 ``a_c:skills`` 会匹到 ``abc:skills`` 的成员——两个不同仓库的
    结论混进同一次任务，看起来完全正常。
    """
    with factory() as session:
        _member(session, member_skill_id("a_c:skills", "x"), "h-x", context_hash=CONTEXT)
        _member(session, member_skill_id("abc:skills", "x"), "h-y", context_hash=CONTEXT)

        rows = bundle_member_results(session, ContentSource.LOCAL, "a_c:skills", CONTEXT)

        assert [r.content_hash for r in rows] == ["h-x"]


# ---- worker 写进任务行的 hash，和成员结论上的 context_hash 是同一个 ----


@pytest.fixture
def worker_env(tmp_path, monkeypatch, db_url):
    """一个能跑 worker 的最小部署。"""
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_REQUIRE_SCANNERS", "false")
    monkeypatch.setenv(
        "SKILLPRISM_POLICY_FILE",
        str(Path(__file__).resolve().parent.parent / "profiles" / "internal.yaml"),
    )
    reset_settings()
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    yield settings
    reset_engine()
    reset_settings()


class BundleSource:
    """只供 bundle 的内容来源。"""

    def __init__(self, bundle: SkillBundle) -> None:
        self.bundle = bundle

    def fetch(self, skill_id, version=None):
        raise AssertionError("这条任务是 bundle")

    def fetch_bundle(self, skill_id, version=None):
        return self.bundle


def _member_files(bundle: SkillBundle, member: str) -> list[SkillFile]:
    """成员自己的那几个文件，路径相对成员目录——与 worker 算成员 hash 时一致。"""
    prefix = f"{member}/"
    return [
        SkillFile(path=f.path.removeprefix(prefix), data=f.data)
        for f in bundle.files
        if f.path.startswith(prefix)
    ]


def test_the_task_hash_is_the_context_the_members_carry(worker_env):
    """任务行上那个 hash 必须**就是**成员结论的 context_hash。

    这条等式是"任务 → 结论"整条链路的支点：任务接口按它反查成员（结果表
    没有 task_id 列）。哪天 worker 往任务行写了别的东西，反查会静默地什么
    都找不到——任务显示成功，results 却是空的，和当初只暴露组级 hash 时的
    症状一模一样。

    这里走的是**全部命中缓存**那条路：一行新记录都不写，任务照样要能说出
    自己对应哪几条结论。那正是结果表不该有 task_id 列的理由——有的话，
    这种情况下它只会指向更早的某个任务。
    """
    # 两个成员的内容必须不同。字节一样的话两者的 content_hash 也一样，
    # 复用会让两个成员命中同一行，clone 到对方 ID 上时撞唯一键——那是另一个
    # 单独的坑，不在这条用例的射程内。
    bundle = SkillBundle(
        files=[
            SkillFile(path=f"code-review/{SKILL_MANIFEST}", data=MANIFEST),
            SkillFile(path=f"test-gen/{SKILL_MANIFEST}", data=MANIFEST + b"\n# tg\n"),
        ],
        members=["code-review", "test-gen"],
    )
    context = compute_content_hash(bundle.files, name=None)
    policy = policy_file_hash(worker_env)
    assert policy, "策略指纹为空的话不会复用，这条用例就走不到全命中那条路"

    with session_scope() as db:
        for member in bundle.members:
            db.add(
                EvaluationResult(
                    id=str(uuid.uuid4()),
                    source=str(ContentSource.LOCAL),
                    skill_id=member_skill_id(BUNDLE_ID, member),
                    content_hash=compute_content_hash(
                        _member_files(bundle, member), name=member
                    ),
                    context_hash=context,
                    status="passed",
                    severity_counts={},
                    incomplete_scans=[],
                    policy_file_hash=policy,
                )
            )
        enqueue(
            db,
            source=ContentSource.LOCAL,
            skill_id=BUNDLE_ID,
            bundle=True,
        )

    assert run_once(
        settings=worker_env,
        content_sources={ContentSource.LOCAL: BundleSource(bundle)},
        storage=LocalReportStorage(worker_env.report_root),
    )

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        assert task.state == str(TaskState.DONE), task.error
        assert task.content_hash == context
        dto = task_to_dto(db, task)

    assert dto.content_hash is None, "组指纹不是任何一条结论的寻址键"
    assert dto.context_hash == context
    assert [r.skill_id for r in dto.results] == [
        member_skill_id(BUNDLE_ID, m) for m in bundle.members
    ]
    assert all(r.content_hash != context for r in dto.results)
