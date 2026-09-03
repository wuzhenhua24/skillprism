"""任务队列。

定位是“只展示不拦截”，服务中断只意味着徽章不更新，因此不需要引入
消息中间件——一张表加轮询足够，运维成本最低。

queue 字段从第一天就存在：Tier 1 是纯静态分析的快队列，将来 Tier 2
（需要 embedding）与 Tier 3（需要沙箱）会落到不同队列与不同 worker 部署。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from skillprism.domain import TaskState, Tier
from skillprism.models import EvaluationTask

#: tier 到队列的路由。Tier 2/3 尚未实现，先占位以免将来改表。
QUEUE_BY_TIER = {
    Tier.TIER1: "fast",
    Tier.TIER2: "index",
    Tier.TIER3: "sandbox",
}


def find_queued(session: Session, skill_id: str, tier: Tier) -> EvaluationTask | None:
    """找一条同 skill 同 tier、尚未开跑的任务。

    只看 queued，不看 running：running 的任务**已经下载过内容**，它跑的是
    更早的那一份。把新的触发折叠进去，就等于宣称评了新内容却给出旧结论。
    running 期间的重复触发交给 worker 的缓存判定收敛——内容确实没变时，
    第二条任务算出同一个 hash，命中已有结果，不会真的重跑评测器。
    """
    stmt = (
        select(EvaluationTask)
        .where(
            EvaluationTask.skill_id == skill_id,
            EvaluationTask.tier == str(tier),
            EvaluationTask.state == str(TaskState.QUEUED),
        )
        .order_by(EvaluationTask.created_at)
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def enqueue(
    session: Session,
    *,
    skill_id: str,
    skill_name: str | None = None,
    skill_version: str | None = None,
    content_hash: str | None = None,
    tier: Tier = Tier.TIER1,
    force: bool = False,
) -> EvaluationTask:
    """排一条任务。

    ``content_hash`` 通常为空：内容由 worker 下载，入队时还不知道 hash。
    """
    task = EvaluationTask(
        id=str(uuid.uuid4()),
        skill_id=skill_id,
        skill_name=skill_name,
        skill_version=skill_version,
        content_hash=content_hash,
        tier=str(tier),
        queue=QUEUE_BY_TIER[tier],
        state=str(TaskState.QUEUED),
        force=force,
    )
    session.add(task)
    session.flush()
    return task


def claim_next(session: Session, queue: str = "fast") -> EvaluationTask | None:
    """取一个待处理任务并置为 running。

    按方言决定是否加锁，而不是靠捕获异常来判断：SQLite 不支持
    ``SKIP LOCKED``，其余数据库都支持。

    这里刻意不用 try/except 兜底。捕获所有异常会让**任何**一次查询失败都
    悄悄退化成不加锁的查询——在 PostgreSQL 上那意味着两个 worker 可能抢到
    同一个任务，而且没有任何迹象。宁可让异常抛出去。

    SQLite 下没有锁，因此**只能跑单个 worker 实例**，这是部署上的硬约束。
    """
    now = datetime.now(tz=UTC)
    stmt = (
        select(EvaluationTask)
        .where(
            EvaluationTask.state == str(TaskState.QUEUED),
            EvaluationTask.queue == queue,
            # 退避中的任务还没到点，跳过。SQLite 存的是不带时区偏移的字符串，
            # 但写进去的一律是 _now() 的 UTC 值，两边格式一致，比较成立。
            or_(
                EvaluationTask.next_attempt_at.is_(None),
                EvaluationTask.next_attempt_at <= now,
            ),
        )
        .order_by(EvaluationTask.created_at)
        .limit(1)
    )
    if session.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update(skip_locked=True)

    task = session.execute(stmt).scalar_one_or_none()
    if task is None:
        return None

    task.state = str(TaskState.RUNNING)
    task.started_at = now
    task.attempts += 1
    task.next_attempt_at = None
    session.flush()
    return task


def finish(session: Session, task: EvaluationTask, *, error: str | None = None) -> None:
    task.state = str(TaskState.FAILED if error else TaskState.DONE)
    task.error = error
    task.finished_at = datetime.now(tz=UTC)
    session.flush()


def requeue(
    session: Session,
    task: EvaluationTask,
    *,
    error: str,
    max_attempts: int,
    backoff_seconds: float = 0.0,
) -> bool:
    """可重试的失败放回队列；超过次数上限则终结。

    走这里的是"值得再试一次"的故障：评测器退出码 3 或超时，以及内容下载
    的网络/5xx 失败。skill 不合格不是失败，内容不存在也不是——那两种重试
    多少次结论都一样。

    ``error`` 无论重不重试都写在任务上。排队中的任务带着上一次的失败原因，
    是运维唯一能看见"它在重试、为什么重试"的地方。
    """
    if task.attempts >= max_attempts:
        finish(session, task, error=f"{error}（已重试 {task.attempts} 次）")
        return False
    task.state = str(TaskState.QUEUED)
    task.error = error
    task.next_attempt_at = (
        datetime.now(tz=UTC) + timedelta(seconds=backoff_seconds) if backoff_seconds > 0 else None
    )
    session.flush()
    return True
