"""内容来源是身份的一部分。

``skill_id`` 只在一个来源内部唯一：管理系统的资源 ID ``42`` 和 GitLab 的
数字项目 ID ``42`` 是同一个字符串。两种接入并存时，不带来源的键会让两边
互相覆盖、互相折叠——两种都没有异常、没有日志，只是结论悄悄换成了另一个
skill 的。这组测试就是钉住"带上了"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from skillprism.config import get_settings, reset_settings
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import ContentSource, EvaluationStatus, TaskState, Tier
from skillprism.models import Base, EvaluationTask
from skillprism.queue import enqueue, find_queued
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
    reset_settings()
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()
    yield settings
    reset_engine()
    reset_settings()


def test_worker_refuses_a_task_from_another_source(env):
    """配置在排队之后被改过时，任务作废而不是照跑。

    worker 手上只有一个内容来源客户端。拿它去跑一条属于别的来源的任务，
    就是去错的地方按错的解释取内容——而两边都可能"取得到"。
    """
    with session_scope() as db:
        enqueue(db, source=ContentSource.ZIP, skill_id="2000705", skill_name="demo")

    run_once(
        settings=env,
        content_source=NeverCalled(),
        storage=LocalReportStorage(env.report_root),
    )

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        assert task.state == str(TaskState.FAILED), "不该重试：换配置前排的队再试多少次都一样"
        assert "zip" in task.error and "local" in task.error
        assert lookup_result(db, ContentSource.ZIP, "2000705") is None
