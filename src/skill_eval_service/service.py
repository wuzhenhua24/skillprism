"""编排：提交评测与查询结果的业务逻辑，供 API 与测试共用。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from skill_eval_service import queue as task_queue
from skill_eval_service.content import SkillContentSource
from skill_eval_service.domain import Tier
from skill_eval_service.materialize import compute_content_hash
from skill_eval_service.repository import find_result, latest_result, result_to_dto
from skill_eval_service.schemas import EvaluationDTO, SubmitRequest, SubmitResponse


def submit(
    session: Session,
    source: SkillContentSource,
    request: SubmitRequest,
) -> SubmitResponse:
    """提交一次评测。

    内容 hash 命中已有结果时直接复用，不产生新任务：既省成本，也避免
    同样的内容因为 LLM 或扫描器的抖动给出不同结论。
    """
    files = source.fetch(request.skill_id, request.skill_version)
    content_hash = compute_content_hash(files)

    if not request.force:
        cached = find_result(session, request.skill_id, content_hash)
        if cached is not None:
            return SubmitResponse(
                task_id="",
                skill_id=request.skill_id,
                content_hash=content_hash,
                state="done",
                cached=True,
            )

    task = task_queue.enqueue(
        session,
        skill_id=request.skill_id,
        content_hash=content_hash,
        skill_version=request.skill_version,
        tier=request.tier,
    )
    return SubmitResponse(
        task_id=task.id,
        skill_id=task.skill_id,
        content_hash=content_hash,
        state=task.state,
    )


def get_evaluation(
    session: Session,
    skill_id: str,
    *,
    content_hash: str | None = None,
) -> EvaluationDTO | None:
    row = (
        find_result(session, skill_id, content_hash)
        if content_hash
        else latest_result(session, skill_id)
    )
    if row is None:
        return None
    return result_to_dto(row)


def report_path(uri: str | None) -> Path | None:
    if not uri or not uri.startswith("file://"):
        return None
    path = Path(uri.removeprefix("file://"))
    return path if path.exists() else None


#: Tier 2/3 尚未实现。这里显式列出以便 API 返回明确的“未实现”，
#: 而不是静默当成 Tier 1 处理。
IMPLEMENTED_TIERS = {Tier.TIER1}
