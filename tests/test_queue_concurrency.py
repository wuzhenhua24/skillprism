"""任务领取的并发语义。

这组测试的存在理由：``claim_next`` 在 PostgreSQL 上走 ``SKIP LOCKED``、
在 SQLite 上不加锁，**生产真正执行的是前者，而本地开发只会跑到后者**。
不把同一套测试跑在 PG 上，那条分支就一次都没被验证过。

跑 PG 版：

    SKILLPRISM_TEST_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/postgres' \\
      pytest tests/test_queue_concurrency.py
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from skillprism.models import Base, EvaluationTask
from skillprism.queue import claim_next, enqueue
from tests.conftest import PG_ENV_VAR


def test_configured_backend_actually_engages(db_url):
    """设了 SKILLPRISM_TEST_DATABASE_URL 就必须真的跑在 PostgreSQL 上。

    这条是防"以为验证过了、其实一直在跑 SQLite"——那是最坏的情况，
    因为它给出的是虚假的信心。
    """
    if os.environ.get(PG_ENV_VAR):
        assert db_url.startswith("postgresql"), (
            f"{PG_ENV_VAR} 已设置，但测试仍在用 {db_url.split(':')[0]}。"
            "PG 路径没有被真正验证。"
        )
    else:
        assert db_url.startswith("sqlite")


@pytest.fixture
def seeded(db_url):
    """建表并预置任务，返回 (session 工厂, engine)。"""
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed(factory, count: int) -> None:
    with factory() as session:
        for i in range(count):
            enqueue(session, skill_id=f"skill-{i}", content_hash=f"sha256:{i}")
        session.commit()


def test_claims_in_creation_order(seeded):
    """按入队顺序领取，避免先来的任务被无限期饿死。"""
    _seed(seeded, 3)
    got = []
    with seeded() as session:
        for _ in range(3):
            task = claim_next(session)
            got.append(task.skill_id)
            session.commit()
    assert got == ["skill-0", "skill-1", "skill-2"]


def test_empty_queue_returns_none(seeded):
    with seeded() as session:
        assert claim_next(session) is None


def test_other_queues_are_not_touched(seeded):
    """按队列隔离——将来 Tier 2/3 的 worker 不该抢 Tier 1 的任务。"""
    _seed(seeded, 1)
    with seeded() as session:
        assert claim_next(session, queue="sandbox") is None
        assert claim_next(session, queue="fast") is not None


def test_two_workers_never_claim_the_same_task(seeded, is_postgres):
    """两个并发的领取者必须拿到不同的任务。

    这是多 worker 部署的核心保证，只有 SKIP LOCKED 能提供。
    """
    if not is_postgres:
        pytest.skip(
            "SQLite 不支持 SKIP LOCKED，无法提供该保证——"
            "这正是 SQLite 下只能跑单个 worker 的原因，见部署文档第九节"
        )

    _seed(seeded, 2)
    first, second = seeded(), seeded()
    try:
        a = claim_next(first)      # 锁住第一条，尚未提交
        b = claim_next(second)     # 必须跳过被锁的那条
        assert a is not None and b is not None
        assert a.id != b.id, "两个 worker 抢到了同一个任务"
    finally:
        first.rollback(); second.rollback()
        first.close(); second.close()


def test_single_task_is_claimed_by_exactly_one(seeded, is_postgres):
    """只有一个任务时，第二个领取者必须拿到 None 而不是同一个任务。

    比上一条更尖锐：它直接证明没有重复分配，而不只是"拿到了不同的"。
    """
    if not is_postgres:
        pytest.skip("SQLite 不支持 SKIP LOCKED，见部署文档第九节")

    _seed(seeded, 1)
    first, second = seeded(), seeded()
    try:
        assert claim_next(first) is not None
        assert claim_next(second) is None, "同一个任务被领取了两次"
    finally:
        first.rollback(); second.rollback()
        first.close(); second.close()


def test_claimed_task_is_marked_running(seeded):
    _seed(seeded, 1)
    with seeded() as session:
        task = claim_next(session)
        task_id = task.id
        session.commit()
    with seeded() as session:
        refreshed = session.get(EvaluationTask, task_id)
        assert refreshed.state == "running"
        assert refreshed.attempts == 1
        assert refreshed.started_at is not None
