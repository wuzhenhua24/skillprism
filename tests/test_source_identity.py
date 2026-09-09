"""内容来源是身份的一部分。

``skill_id`` 只在一个来源内部唯一：管理系统的资源 ID ``42`` 和 GitLab 的
数字项目 ID ``42`` 是同一个字符串。两种接入并存时，不带来源的键会让两边
互相覆盖、互相折叠——两种都没有异常、没有日志，只是结论悄悄换成了另一个
skill 的。这组测试就是钉住"带上了"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from skillprism.config import get_settings, reset_settings
from skillprism.content import SkillNotFoundError
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import ContentSource, EvaluationStatus, TaskState, Tier
from skillprism.materialize import SkillFile, compute_content_hash
from skillprism.models import Base, EvaluationTask
from skillprism.queue import enqueue, find_queued
from skillprism.runner import policy_file_hash
from skillprism.repository import clone_result, find_reusable_result, find_result, save_result
from skillprism.schemas import EvaluationDTO, SubmitRequest
from skillprism.service import lookup_result, submit
from skillprism.storage import LocalReportStorage
from skillprism.worker import run_once

#: 两个来源下恰好撞在一起的 ID：管理系统的资源 ID 与 GitLab 的数字项目 ID。
COLLIDING_ID = "42"
CONTENT = "sha256:same-bytes"
EVALUATOR = "0.2.1"
POLICY = "sha256:policy-v1"


@pytest.fixture
def factory(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _dto(**overrides) -> EvaluationDTO:
    return EvaluationDTO(
        skill_id=overrides.pop("skill_id", COLLIDING_ID),
        skill_version=overrides.pop("skill_version", "v1"),
        content_hash=overrides.pop("content_hash", CONTENT),
        status=overrides.pop("status", EvaluationStatus.PASSED),
        score=overrides.pop("score", 90.0),
        evaluated_at=datetime(2026, 9, 1, tzinfo=UTC),
        **overrides,
    )


def test_the_same_skill_id_under_two_sources_are_two_results(factory):
    """同一个 ID 在两个来源下是两条结论，不是一条。

    唯一键不带 source 的话，后写的那条会先把前一条 delete 掉
    （见 repository.save_result），管理系统那边的结论就这么没了。
    """
    with factory() as session:
        save_result(session, _dto(score=90.0), source=ContentSource.ZIP, policy_file_hash=POLICY)
        save_result(session, _dto(score=10.0), source=ContentSource.GITLAB, policy_file_hash=POLICY)

        from_zip = find_result(session, ContentSource.ZIP, COLLIDING_ID, CONTENT)
        from_gitlab = find_result(session, ContentSource.GITLAB, COLLIDING_ID, CONTENT)

        assert from_zip is not None and from_gitlab is not None
        assert from_zip.id != from_gitlab.id
        assert (from_zip.score, from_gitlab.score) == (90.0, 10.0)


def test_a_query_does_not_reach_into_another_source(factory):
    """查询必须停在自己的来源里，否则会取到一条同名但无关的结论。"""
    with factory() as session:
        save_result(session, _dto(), source=ContentSource.ZIP, policy_file_hash=POLICY)

        assert lookup_result(session, ContentSource.ZIP, COLLIDING_ID) is not None
        assert lookup_result(session, ContentSource.GITLAB, COLLIDING_ID) is None


def test_queue_does_not_fold_across_sources(factory):
    """两个来源的同名 ID 不能折叠成一条任务。

    折叠了的话，GitLab 那次触发会拿到一条按 zip 接口去下载的任务——
    而两边都"下得到东西"，跑出来是另一个 skill 的结论。
    """
    with factory() as session:
        enqueue(session, source=ContentSource.ZIP, skill_id=COLLIDING_ID, skill_name="a")

        assert find_queued(session, ContentSource.ZIP, COLLIDING_ID, Tier.TIER1) is not None
        assert find_queued(session, ContentSource.GITLAB, COLLIDING_ID, Tier.TIER1) is None


def test_submit_from_two_sources_creates_two_tasks(factory):
    """走到 service 这一层也是同一个结论：不去重，各排各的。"""
    request = SubmitRequest(skill_id=COLLIDING_ID, skill_name="a")
    with factory() as session:
        first = submit(session, request, source=ContentSource.ZIP)
        second = submit(session, request, source=ContentSource.GITLAB)

        assert second.deduplicated is False
        assert first.task_id != second.task_id


def test_a_verdict_is_reusable_across_sources_but_lands_in_the_new_one(factory):
    """结论跨来源可复用，身份不可。

    同一份字节从 zip 传上来还是从 GitLab 取下来，评出来就该是同一个结论，
    重跑一遍只是浪费。但克隆出来的那条必须挂在**本次触发**的来源下，
    否则它在自己的来源里查不到，界面上就是"评过了却查无此结论"。
    """
    with factory() as session:
        save_result(
            session,
            _dto(skill_id="2000705"),
            source=ContentSource.ZIP,
            policy_file_hash=POLICY,
        )
        session.commit()

        reusable = find_reusable_result(
            session, CONTENT, evaluator_version=None, policy_file_hash=POLICY
        )
        assert reusable is not None, "跨来源复用被卡住了：同样的字节不该重评"

        cloned = clone_result(
            session,
            reusable,
            source=ContentSource.GITLAB,
            skill_id="group/repo",
            skill_version="main",
        )
        assert cloned.source == str(ContentSource.GITLAB)
        assert cloned.score == reusable.score
        assert lookup_result(session, ContentSource.GITLAB, "group/repo") is not None


class NeverCalled:
    """一个只要被碰到就让测试失败的内容来源。"""

    def fetch(self, skill_id, version=None):
        raise AssertionError("来源不匹配的任务不该去取内容")

    def fetch_bundle(self, skill_id, version=None):
        raise AssertionError("来源不匹配的任务不该去取内容")


@pytest.fixture
def env(tmp_path, monkeypatch, db_url):
    """一个只配了本地目录来源（ContentSource.LOCAL）的部署。"""
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_REQUIRE_SCANNERS", "false")
    # 复用判据要拿策略文件的指纹，读不到就一律不复用（见 runner.policy_file_hash），
    # 那样下面那条缓存命中的用例根本走不到被测的分支。
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


class RecordingSource:
    """记下自己被问过哪些 skill_id 的内容来源。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.asked: list[str] = []

    def fetch(self, skill_id, version=None):
        self.asked.append(skill_id)
        raise SkillNotFoundError(f"{self.label} 上没有 {skill_id}")

    def fetch_bundle(self, skill_id, version=None):
        return self.fetch(skill_id)


def test_worker_routes_each_task_to_its_own_source(env):
    """两种接入同时在线时，每条任务按自己记的来源取内容。

    路由错了不会报错：两边的 skill_id 都可能"取得到东西"，评出来的是一份
    看起来完全正常的错结论。
    """
    with session_scope() as db:
        enqueue(db, source=ContentSource.ZIP, skill_id=COLLIDING_ID, skill_name="a")
        enqueue(db, source=ContentSource.GITLAB, skill_id=COLLIDING_ID, skill_name="a")

    zip_source = RecordingSource("zip")
    gitlab_source = RecordingSource("gitlab")
    sources = {ContentSource.ZIP: zip_source, ContentSource.GITLAB: gitlab_source}
    storage = LocalReportStorage(env.report_root)

    assert run_once(settings=env, content_sources=sources, storage=storage)
    assert run_once(settings=env, content_sources=sources, storage=storage)

    assert zip_source.asked == [COLLIDING_ID]
    assert gitlab_source.asked == [COLLIDING_ID]


def test_worker_refuses_a_task_whose_source_is_not_enabled(env):
    """任务声明的接入在本 worker 上没启用时，任务作废而不是拿别的客户端跑。

    这会在配置排队之后被改过时发生。拿另一个客户端去取就是去错的地方按错的
    解释取内容——而两边都可能"取得到"。
    """
    with session_scope() as db:
        enqueue(db, source=ContentSource.ZIP, skill_id="2000705", skill_name="demo")

    run_once(
        settings=env,
        content_sources={ContentSource.LOCAL: NeverCalled()},
        storage=LocalReportStorage(env.report_root),
    )

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        assert task.state == str(TaskState.FAILED), "不该重试：换配置前排的队再试多少次都一样"
        assert "zip" in task.error and "local" in task.error
        assert lookup_result(db, ContentSource.ZIP, "2000705") is None


def test_worker_refuses_a_task_with_an_unreadable_source(env):
    """来源列上是个认不出的值时也当场终结，而不是当成某个默认来源跑。"""
    with session_scope() as db:
        task = enqueue(db, source=ContentSource.LOCAL, skill_id="2000705", skill_name="demo")
        task.source = "svn"

    run_once(
        settings=env,
        content_sources={ContentSource.LOCAL: NeverCalled()},
        storage=LocalReportStorage(env.report_root),
    )

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        assert task.state == str(TaskState.FAILED)
        assert "svn" in task.error


class FixedSource:
    """总是给出同一份内容的来源。"""

    def __init__(self, files) -> None:
        self.files = files

    def fetch(self, skill_id, version=None):
        return list(self.files)

    def fetch_bundle(self, skill_id, version=None):
        raise AssertionError("这条任务不是 bundle")


def test_a_cross_source_cache_hit_still_lands_a_verdict_in_this_source(env):
    """跨来源命中缓存时，必须往**本次触发的来源**下挂一条，否则等于没评。

    复用刻意不看 source（同样的字节评出同样的结论），但身份分来源。撞名的
    ID——zip 的资源 ID 与 GitLab 的数字项目 ID——命中对方那条结论时，只比
    skill_id 就不会克隆：任务显示成功，按本次的 source 查却是 404，任务接口
    的 results 也是空的。症状和 bundle 那个坑一模一样，成因不同。
    """
    files = [SkillFile(path="SKILL.md", data=b"---\nname: demo\n---\n")]
    content_hash = compute_content_hash(files, name="demo")
    policy = policy_file_hash(env)
    assert policy, "策略指纹为空的话复用根本不会发生，这条用例就没测到东西"

    with session_scope() as db:
        save_result(
            db,
            _dto(content_hash=content_hash),
            source=ContentSource.ZIP,
            policy_file_hash=policy,
        )
        enqueue(db, source=ContentSource.GITLAB, skill_id=COLLIDING_ID, skill_name="demo")

    assert run_once(
        settings=env,
        content_sources={ContentSource.GITLAB: FixedSource(files)},
        storage=LocalReportStorage(env.report_root),
    )

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        assert task.state == str(TaskState.DONE)
        assert task.content_hash == content_hash
        assert (
            lookup_result(db, ContentSource.GITLAB, COLLIDING_ID) is not None
        ), "命中的是 zip 那条结论，本次 GitLab 触发一条都没落库"


def test_a_cross_source_cache_hit_when_this_source_already_has_one(env):
    """本次身份上已经有结论、而命中的是另一个来源那条时，要覆盖而不是崩。

    撞名的 ID 两边都评过、GitLab 那条更新，再触发一次 zip 就是这个局面：
    复用按内容找最新的一条，找到的是 GitLab 那条，而 zip 自己也有一条。往
    唯一键上硬插会被 worker 的兜底吃成"处理异常"，任务重试到失败——而它本该
    是一次最普通的缓存命中。
    """
    files = [SkillFile(path="SKILL.md", data=b"---\nname: demo\n---\n")]
    content_hash = compute_content_hash(files, name="demo")
    policy = policy_file_hash(env)

    with session_scope() as db:
        older = save_result(
            db,
            _dto(content_hash=content_hash, score=10.0),
            source=ContentSource.ZIP,
            policy_file_hash=policy,
        )
        older.evaluated_at = datetime(2026, 9, 1, tzinfo=UTC)
        newer = save_result(
            db,
            _dto(content_hash=content_hash, score=90.0),
            source=ContentSource.GITLAB,
            policy_file_hash=policy,
        )
        newer.evaluated_at = datetime(2026, 9, 5, tzinfo=UTC)
        enqueue(db, source=ContentSource.ZIP, skill_id=COLLIDING_ID, skill_name="demo")

    assert run_once(
        settings=env,
        content_sources={ContentSource.ZIP: FixedSource(files)},
        storage=LocalReportStorage(env.report_root),
    )

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        assert task.state == str(TaskState.DONE), task.error
        row = lookup_result(db, ContentSource.ZIP, COLLIDING_ID)
        assert row is not None
        # 覆盖成当前判据下有效的那条，而不是留着旧的。
        assert row.score == 90.0
