"""失败任务的重试语义。

这组测试锁的是一次线上事故：``SKILLPRISM_CONTENT_URL_TEMPLATE`` 配错时，
一个任务在三分半里被重领了 105 次，而任务表上始终显示 ``queued``、
``attempts=0``、``error`` 为空——重试既不计次也不可见。

成因不在下载本身，而在异常的走向：``ContentFetchError`` 逃出 ``process_task``
后，``session_scope`` 把整个事务回滚掉了，连带 ``claim_next`` 写的
running/attempts 一起没了，任务原样退回队列等着被重领。任何一种预料之外的
异常都会踩到同一个坑，所以这里既测下载失败这条具体路径，也测兜底。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from skillprism.config import get_settings, reset_settings
from skillprism.content import ContentFetchError, SkillNotFoundError
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import TaskState
from skillprism.models import EvaluationTask
from skillprism.queue import claim_next
from skillprism.schemas import SubmitRequest
from skillprism.service import submit
from skillprism.storage import LocalReportStorage
from skillprism.worker import run_once

SKILL_ID = "demo"

#: 事故当天的原始异常：URL 模板多了一个 =，httpx 抛 UnsupportedProtocol，
#: 被 ZipArchiveSource 包成 ContentFetchError。
BROKEN_URL_ERROR = (
    "下载失败：UnsupportedProtocol: Request URL is missing an 'http://' or 'https://' protocol."
)


class FailingSource:
    """按脚本抛异常的内容来源；用完脚本后返回正常内容。"""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def fetch(self, skill_id, version=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return [type("F", (), {"path": "SKILL.md", "data": b"---\nname: demo\n---\n"})()]


@pytest.fixture
def env(tmp_path, monkeypatch, db_url):
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_REQUIRE_SCANNERS", "false")
    monkeypatch.setenv("SKILLPRISM_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("SKILLPRISM_RETRY_BACKOFF_SECONDS", "30")

    reset_settings()
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()

    with session_scope() as db:
        submit(db, SubmitRequest(skill_id=SKILL_ID, skill_name=SKILL_ID))

    yield settings

    reset_engine()
    reset_settings()


def _run(env, source) -> None:
    run_once(settings=env, source=source, storage=LocalReportStorage(env.report_root))


def _task() -> EvaluationTask:
    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        db.expunge(task)
        return task


def _clear_backoff() -> None:
    """把退避时间拨到过去，模拟"等到点了"，免得测试真的去 sleep。"""
    with session_scope() as db:
        db.query(EvaluationTask).one().next_attempt_at = datetime.now(tz=UTC) - timedelta(seconds=1)


def test_download_failure_is_counted_and_visible(env):
    """下载失败必须计次并写进任务，不能悄悄退回队列。

    这是事故的直接复现：修复前 fetch 会被调用无数次而 attempts 恒为 0。
    """
    source = FailingSource(*[ContentFetchError(BROKEN_URL_ERROR) for _ in range(10)])

    for _ in range(10):
        _run(env, source)
        _clear_backoff()

    task = _task()
    assert source.calls == 3, "重试次数没有被 max_attempts 卡住"
    assert task.state == str(TaskState.FAILED)
    assert task.attempts == 3
    assert "UnsupportedProtocol" in task.error
    assert "已重试 3 次" in task.error


def test_failure_reason_is_visible_while_still_queued(env):
    """重试期间任务停在 queued，但必须带着上一次的失败原因。

    事故里运维只看得到"排队中"，没有任何线索指向下载失败。
    """
    _run(env, FailingSource(ContentFetchError(BROKEN_URL_ERROR)))

    task = _task()
    assert task.state == str(TaskState.QUEUED)
    assert task.attempts == 1
    assert "UnsupportedProtocol" in task.error
    assert task.next_attempt_at is not None


def test_backoff_holds_the_task_until_it_is_due(env):
    """退避没到点，claim_next 不许领这条任务。

    没有退避的话 max_attempts 会在一个轮询周期的几秒内烧光，
    管理系统重启一次就足以让任务永久失败。
    """
    _run(env, FailingSource(ContentFetchError(BROKEN_URL_ERROR)))

    with session_scope() as db:
        assert claim_next(db) is None, "退避期内的任务不该被领走"

    _clear_backoff()
    with session_scope() as db:
        claimed = claim_next(db)
        assert claimed is not None
        assert claimed.attempts == 2
        assert claimed.next_attempt_at is None, "领取时应当清掉退避标记"


def test_backoff_grows_with_attempts(env):
    """退避按尝试次数指数增长，并封顶。"""
    assert env.backoff_for(1) == 30
    assert env.backoff_for(2) == 60
    assert env.backoff_for(3) == 120
    assert env.backoff_for(99) == env.retry_backoff_max_seconds


def test_transient_failure_recovers_on_retry(env):
    """故障恢复后下一次领取就该成功——事故里改完配置重启正是如此。"""
    source = FailingSource(ContentFetchError(BROKEN_URL_ERROR))

    _run(env, source)
    _clear_backoff()

    with session_scope() as db:
        claimed = claim_next(db)
        assert claimed is not None, "退避到点后任务必须还能被领"
    assert source.calls == 1


def test_missing_skill_is_not_retried(env):
    """内容不存在不是可重试故障，重试多少次结论都一样。"""
    source = FailingSource(*[SkillNotFoundError("找不到 skill：demo") for _ in range(5)])

    for _ in range(5):
        _run(env, source)

    task = _task()
    assert source.calls == 1
    assert task.state == str(TaskState.FAILED)
    assert "找不到 skill" in task.error


def test_unexpected_exception_lands_on_the_task(env):
    """预料之外的异常也不能让任务隐身。

    ContentFetchError 只是踩中这个坑的第一种异常。物化、存储、adapter
    任何一处抛出没被分类的异常，都会走同一条回滚路径。
    """
    source = FailingSource(*[RuntimeError("磁盘满了") for _ in range(10)])

    for _ in range(10):
        _run(env, source)
        _clear_backoff()

    task = _task()
    assert source.calls == 3, "兜底路径没有计次，任务会被无限重领"
    assert task.state == str(TaskState.FAILED)
    assert "RuntimeError" in task.error
    assert "磁盘满了" in task.error
