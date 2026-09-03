"""Worker：把队列里的任务跑完并落库。

一次任务的完整链路：
    取任务 → 拉内容 → 算 hash → 查缓存 → 物化 → 子进程评测 → adapter 翻译
    → 报告入存储 → 摘要入库 → 清理临时目录

下载放在这里而不是提交时：触发接口挂在管理系统的上传流程后面，
不能让它承担我们的网络耗时和可用性。代价是缓存判定也只能在这里做。
"""

from __future__ import annotations

import logging
import sys
import time

from skillprism import queue as task_queue
from skillprism.adapter import to_dto
from skillprism.config import Settings, get_settings
from skillprism.content import (
    ContentFetchError,
    SkillContentSource,
    SkillNotFoundError,
    build_content_source,
)
from skillprism.db import SCHEMA_NOT_READY_HINT, schema_is_ready, session_scope
from skillprism.domain import EvaluationStatus
from skillprism.materialize import (
    MaterializeError,
    UnsafePathError,
    cleanup,
    compute_content_hash,
    materialize,
)
from skillprism.models import EvaluationTask
from skillprism.repository import clone_result, find_reusable_result, save_result
from skillprism.runner import PreflightError, policy_file_hash, require_ready, run_validate
from skillprism.storage import LocalReportStorage, ReportStorage

logger = logging.getLogger(__name__)


def process_task(
    session,
    task: EvaluationTask,
    *,
    settings: Settings,
    source: SkillContentSource,
    storage: ReportStorage,
    evaluator_version: str | None = None,
) -> EvaluationStatus:
    """处理一个任务。返回最终对外状态。"""
    work_dir = settings.work_root / task.id
    skill_dir = work_dir / "skill"
    out_dir = work_dir / "reports"

    try:
        files = source.fetch(task.skill_id, task.skill_version)
    except SkillNotFoundError as exc:
        # 内容不存在或归档解不出，再试多少次都一样。
        task_queue.finish(session, task, error=f"取不到内容：{exc}")
        return EvaluationStatus.ERROR
    except ContentFetchError as exc:
        # 网络抖动或管理系统暂时不可用，值得重试——但必须走 requeue。
        # 让它抛出去的话 session_scope 会回滚掉 claim_next 写的
        # running/attempts，任务原样退回 queued：既不计次也不留错误信息，
        # worker 于是每个轮询间隔重领一次，外部看到的只是"一直排队中"。
        _requeue(session, task, settings, f"取不到内容：{exc}")
        return EvaluationStatus.ERROR

    # 内容可能在入队之后发生变化，以实际取到的内容为准。
    content_hash = compute_content_hash(files)
    task.content_hash = content_hash

    policy_hash = policy_file_hash(settings)

    # 缓存判定在这里，不在提交时——提交时还没下载，看不见内容。
    # 判据里没有 skill_id：管理系统每次上传都换一个资源 ID，按 ID 找永远
    # 不命中，而"同一个 zip 只改版本号"是他们上传表单下很自然的操作。
    # 复用的三个前提见 find_reusable_result。
    if not task.force:
        reusable = find_reusable_result(
            session,
            content_hash,
            evaluator_version=evaluator_version,
            policy_file_hash=policy_hash,
        )
        if reusable is not None:
            if reusable.skill_id != task.skill_id:
                # 别的资源 ID 评过同样的内容，挂一份到本次的 ID 上。
                clone_result(
                    session,
                    reusable,
                    skill_id=task.skill_id,
                    skill_version=task.skill_version,
                )
            task_queue.finish(session, task)
            return EvaluationStatus(reusable.status)

    # 目录名用管理系统里登记的 skill 名：SkillEvaluator 的
    # SCHEMA.name_consistency 会拿它和 frontmatter 的 name 比对。用固定名或
    # skill_id（可能是纯数字的资源 ID）都会让每个 skill 都平白多一条 HIGH。
    # 回落到 skill_id 末段只为兼容 skill_name 落库之前排下的存量任务。
    skill_name = task.skill_name or task.skill_id.rstrip("/").split("/")[-1]

    try:
        skill_root = materialize(files, skill_dir, name=skill_name)
    except (UnsafePathError, MaterializeError) as exc:
        # 物化失败是内容问题，不是评测故障，重试没有意义。
        task_queue.finish(session, task, error=f"物化失败：{exc}")
        return EvaluationStatus.ERROR

    try:
        outcome = run_validate(settings, skill_root, out_dir)

        dto = to_dto(
            skill_id=task.skill_id,
            skill_version=task.skill_version,
            content_hash=content_hash,
            outcome=outcome,
            evaluator_version=evaluator_version,
            skill_root=skill_root,
        )

        json_uri = html_uri = None
        if outcome.report_json_path is not None:
            json_uri = storage.put(content_hash, "report.json", outcome.report_json_path)
        if outcome.report_html_path is not None:
            html_uri = storage.put(content_hash, "report.html", outcome.report_html_path)
        dto.report_url = html_uri

        # ERROR 表示 skill 从未被判定，不写入结果——否则界面会显示一个
        # 并不存在的结论。可重试的故障放回队列。
        if dto.status is EvaluationStatus.ERROR:
            reason = dto.error or "评测失败"
            if outcome.retryable:
                _requeue(session, task, settings, reason)
            else:
                task_queue.finish(session, task, error=reason)
            return EvaluationStatus.ERROR

        save_result(
            session,
            dto,
            report_json_uri=json_uri,
            report_html_uri=html_uri,
            policy_file_hash=policy_hash,
        )
        task_queue.finish(session, task)
        return dto.status
    finally:
        cleanup(work_dir)


def _requeue(session, task: EvaluationTask, settings: Settings, error: str) -> None:
    """按退避策略把任务放回队列，超过上限则终结。"""
    retrying = task_queue.requeue(
        session,
        task,
        error=error,
        max_attempts=settings.max_attempts,
        backoff_seconds=settings.backoff_for(task.attempts),
    )
    if retrying:
        logger.warning(
            "任务 %s 第 %d 次失败，%.0f 秒后重试：%s",
            task.id,
            task.attempts,
            settings.backoff_for(task.attempts),
            error,
        )
    else:
        logger.error("任务 %s 重试 %d 次后放弃：%s", task.id, task.attempts, error)


def run_once(
    *,
    settings: Settings,
    source: SkillContentSource,
    storage: ReportStorage,
    evaluator_version: str | None = None,
    queue: str = "fast",
) -> bool:
    """处理至多一个任务。返回是否真的处理了任务。"""
    task_id: str | None = None
    try:
        with session_scope() as session:
            task = task_queue.claim_next(session, queue=queue)
            if task is None:
                return False
            task_id = task.id
            process_task(
                session,
                task,
                settings=settings,
                source=source,
                storage=storage,
                evaluator_version=evaluator_version,
            )
            return True
    except Exception as exc:
        # 预料之外的异常。事务已经被 session_scope 回滚，claim_next 写的
        # running/attempts 一起没了，任务原样退回 queued——不补记的话它会被
        # 每个轮询间隔重领一次，而任务表上看不出任何异常。
        # 这是个兜底：具体的失败类型应当在 process_task 里分类处理。
        if task_id is None:
            raise
        _record_crash(task_id, settings, exc)
        return True


def _record_crash(task_id: str, settings: Settings, exc: BaseException) -> None:
    """把预料之外的异常补记到任务上。另开事务——原来那个已经回滚了。"""
    logger.exception("任务 %s 处理异常", task_id)
    try:
        with session_scope() as session:
            task = session.get(EvaluationTask, task_id)
            if task is None:
                return
            # attempts 跟着回滚一起丢了，补回来，否则重试上限永远够不着。
            task.attempts += 1
            _requeue(session, task, settings, f"处理异常：{type(exc).__name__}: {exc}")
    except Exception:
        logger.exception("任务 %s 的失败状态没能写进去", task_id)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    settings.ensure_dirs()

    # 启动自检：扫描器缺失会让 Tier 1 产出 incomplete 结论。
    # 与其带病运行、让界面显示一个没扫全的“合格”，不如直接拒绝启动。
    try:
        report = require_ready(settings)
    except PreflightError as exc:
        logger.error("启动自检未通过：%s", exc)
        return 1

    logger.info("skillevaluator: %s (%s)", report.binary, report.version or "版本未知")

    if not schema_is_ready():
        logger.error(SCHEMA_NOT_READY_HINT)
        return 1

    source = build_content_source(settings)
    storage = LocalReportStorage(settings.report_root)

    logger.info("worker 已启动，轮询队列 fast")
    while True:
        try:
            worked = run_once(
                settings=settings,
                source=source,
                storage=storage,
                evaluator_version=report.version,
            )
        except Exception:
            logger.exception("任务处理异常")
            worked = False
        if not worked:
            time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
