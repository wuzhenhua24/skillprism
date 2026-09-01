"""任务队列。

定位是“只展示不拦截”，服务中断只意味着徽章不更新，因此不需要引入
消息中间件——一张表加轮询足够，运维成本最低。

queue 字段从第一天就存在：Tier 1 是纯静态分析的快队列，将来 Tier 2
（需要 embedding）与 Tier 3（需要沙箱）会落到不同队列与不同 worker 部署。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_eval_service.domain import TaskState, Tier
from skill_eval_service.models import EvaluationTask

#: tier 到队列的路由。Tier 2/3 尚未实现，先占位以免将来改表。
QUEUE_BY_TIER = {
    Tier.TIER1: "fast",
    Tier.TIER2: "index",
    Tier.TIER3: "sandbox",
}


def enqueue(
    session: Session,
    *,
    skill_id: str,
    content_hash: str,
    skill_version: str | None = None,
    tier: Tier = Tier.TIER1,
) -> EvaluationTask:
    task = EvaluationTask(
        id=str(uuid.uuid4()),
        skill_id=skill_id,
        skill_version=skill_version,
        content_hash=content_hash,
        tier=str(tier),
        queue=QUEUE_BY_TIER[tier],
        state=str(TaskState.QUEUED),
    )
    session.add(task)
    session.flush()
    return task


def claim_next(session: Session, queue: str = "fast") -> EvaluationTask | None:
    """取一个待处理任务并置为 running。

    骨架用行锁 + 状态判断；多 worker 部署时 SQLite 会串行化，
    换 PostgreSQL 后可改成 SELECT ... FOR UPDATE SKIP LOCKED。
    """
    stmt = (
        select(EvaluationTask)
        .where(EvaluationTask.state == str(TaskState.QUEUED), EvaluationTask.queue == queue)
        .order_by(EvaluationTask.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    try:
        task = session.execute(stmt).scalar_one_or_none()
    except Exception:
        # SQLite 不支持 SKIP LOCKED，退回不加锁的查询。
        task = session.execute(
            select(EvaluationTask)
            .where(EvaluationTask.state == str(TaskState.QUEUED), EvaluationTask.queue == queue)
            .order_by(EvaluationTask.created_at)
            .limit(1)
        ).scalar_one_or_none()

    if task is None:
        return None

    task.state = str(TaskState.RUNNING)
    task.started_at = datetime.now(tz=UTC)
    task.attempts += 1
    session.flush()
    return task


def finish(session: Session, task: EvaluationTask, *, error: str | None = None) -> None:
    task.state = str(TaskState.FAILED if error else TaskState.DONE)
    task.error = error
    task.finished_at = datetime.now(tz=UTC)
    session.flush()


def requeue(session: Session, task: EvaluationTask, *, error: str, max_attempts: int) -> bool:
    """可重试的失败放回队列；超过次数上限则终结。

    只有评测本身故障（退出码 3、超时）才走这里；skill 不合格不是失败。
    """
    if task.attempts >= max_attempts:
        finish(session, task, error=f"{error}（已重试 {task.attempts} 次）")
        return False
    task.state = str(TaskState.QUEUED)
    task.error = error
    session.flush()
    return True
