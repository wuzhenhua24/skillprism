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
    resolve_source_kind,
)
from skillprism.db import SCHEMA_NOT_READY_HINT, schema_is_ready, session_scope
from skillprism.domain import ContentSource, EvaluationStatus
from skillprism.materialize import (
    MaterializeError,
    SkillFile,
    UnsafePathError,
    cleanup,
    compute_content_hash,
    materialize,
    materialize_bundle,
)
from skillprism.models import EvaluationTask
from skillprism.repository import clone_result, find_reusable_result, save_result
from skillprism.runner import (
    PreflightError,
    policy_file_hash,
    require_ready,
    run_catalog,
    run_validate,
)
from skillprism.storage import LocalReportStorage, ReportStorage

logger = logging.getLogger(__name__)


def process_task(
    session,
    task: EvaluationTask,
    *,
    settings: Settings,
    content_source: SkillContentSource,
    storage: ReportStorage,
    evaluator_version: str | None = None,
) -> EvaluationStatus:
    """处理一个任务。返回最终对外状态。"""
    source = resolve_source_kind(settings)
    if task.source != str(source):
        # 这条任务是在另一种接入下排的。本 worker 手上只有一个内容来源客户端，
        # 照跑就是去错的地方取内容——而两边的 skill_id 都可能"取得到"，
        # 于是评出一份看起来正常的错结论。宁可让它带着原因失败。
        task_queue.finish(
            session,
            task,
            error=(
                f"任务来源是 {task.source}，本 worker 配置的来源是 {source}："
                "内容来源在排队之后被改过，这条任务作废，请按原来源重新触发"
            ),
        )
        return EvaluationStatus.ERROR

    work_dir = settings.work_root / task.id
    skill_dir = work_dir / "skill"
    out_dir = work_dir / "reports"

    if task.bundle:
        try:
            return _process_bundle(
                session,
                task,
                settings=settings,
                source=source,
                content_source=content_source,
                storage=storage,
                evaluator_version=evaluator_version,
                work_dir=work_dir,
            )
        finally:
            cleanup(work_dir)

    try:
        files = content_source.fetch(task.skill_id, task.skill_version)
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
                    source=source,
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
            source=source,
            report_json_uri=json_uri,
            report_html_uri=html_uri,
            policy_file_hash=policy_hash,
        )
        task_queue.finish(session, task)
        return dto.status
    finally:
        cleanup(work_dir)


def _member_skill_id(bundle_skill_id: str, member: str) -> str:
    """一个成员对外的 skill_id。

    ``group/repo:skills`` + ``code-review`` → ``group/repo:skills/code-review``，
    正是这个 skill 单独提交时会用的那个 ID。管理系统因此不需要第二套查询
    方式：查一个成员的结论和查任何别的 skill 完全一样。
    """
    return f"{bundle_skill_id.rstrip('/')}/{member}"


def _process_bundle(
    session,
    task: EvaluationTask,
    *,
    settings: Settings,
    source: ContentSource,
    content_source: SkillContentSource,
    storage: ReportStorage,
    evaluator_version: str | None,
    work_dir,
) -> EvaluationStatus:
    """评一组耦合 skill：整套一起物化，跑一次 catalog，产出多条结果。

    整套一起物化是这件事的全部意义。单独物化一个成员来评，它指向兄弟 skill
    的相对链接全部变成死链——那是我们的物化方式造成的误报，不是 skill 的
    问题，而且耦合越紧的 skill 分数越难看。

    返回的是**整体**状态：任一成员出错即 ERROR，全部通过才算通过。单个成员
    的结论各自落库，查询按成员的 skill_id 取。
    """
    skill_dir = work_dir / "skill"
    out_dir = work_dir / "reports"

    try:
        bundle = content_source.fetch_bundle(task.skill_id, task.skill_version)
    except SkillNotFoundError as exc:
        task_queue.finish(session, task, error=f"取不到内容：{exc}")
        return EvaluationStatus.ERROR
    except ContentFetchError as exc:
        _requeue(session, task, settings, f"取不到内容：{exc}")
        return EvaluationStatus.ERROR

    # 整套的指纹。它同时是任务的 content_hash（任务评的就是这一整套）和每个
    # 成员结论的 context_hash（成员的结论取决于它所在的这一套）。
    context_hash = compute_content_hash(bundle.files)
    task.content_hash = context_hash
    policy_hash = policy_file_hash(settings)

    # 成员自身的内容指纹。路径要相对成员目录，否则同样的 skill 换个位置
    # 就算成不同内容，缓存永远不命中。
    member_files: dict[str, list] = {member: [] for member in bundle.members}
    for item in bundle.files:
        head, sep, rest = item.path.partition("/")
        if sep and head in member_files:
            member_files[head].append(SkillFile(path=rest, data=item.data))
    member_hashes = {m: compute_content_hash(files) for m, files in member_files.items()}

    # 逐个成员查缓存。全中就不用跑评测器——一套里只改了一个 skill 是常态，
    # 其余成员的 context_hash 也变了（整套指纹变了），所以这里通常不会全中；
    # 真正省下的是"同一个 commit 被重复触发"的情况。
    reusable = {}
    if not task.force:
        for member, member_hash in member_hashes.items():
            row = find_reusable_result(
                session,
                member_hash,
                evaluator_version=evaluator_version,
                policy_file_hash=policy_hash,
                context_hash=context_hash,
            )
            if row is not None:
                reusable[member] = row

    if len(reusable) == len(bundle.members) and bundle.members:
        for member, row in reusable.items():
            member_id = _member_skill_id(task.skill_id, member)
            if row.skill_id != member_id or row.source != str(source):
                clone_result(
                    session,
                    row,
                    source=source,
                    skill_id=member_id,
                    skill_version=task.skill_version,
                )
        task_queue.finish(session, task)
        return _worst_status(EvaluationStatus(row.status) for row in reusable.values())

    try:
        catalog_root = materialize_bundle(bundle, skill_dir)
    except (UnsafePathError, MaterializeError) as exc:
        task_queue.finish(session, task, error=f"物化失败：{exc}")
        return EvaluationStatus.ERROR

    outcome = run_catalog(settings, catalog_root, out_dir, bundle.members)
    if outcome.failure is not None:
        # 进程层面就没跑起来，一个成员的结论都没有。
        if outcome.retryable:
            _requeue(session, task, settings, outcome.failure)
        else:
            task_queue.finish(session, task, error=outcome.failure)
        return EvaluationStatus.ERROR

    statuses: list[EvaluationStatus] = []
    errors: list[str] = []
    for member in bundle.members:
        member_outcome = outcome.members[member]
        member_id = _member_skill_id(task.skill_id, member)
        dto = to_dto(
            skill_id=member_id,
            skill_version=task.skill_version,
            content_hash=member_hashes[member],
            context_hash=context_hash,
            outcome=member_outcome,
            evaluator_version=evaluator_version,
            skill_root=catalog_root / member,
        )

        if dto.status is EvaluationStatus.ERROR:
            # 单个成员没被判定就不写结果——写进去界面会显示一个并不存在的
            # 结论。其余成员照常落库：一个成员的报告坏了，不该把另外几个
            # 已经评出来的结论一起丢掉。
            errors.append(f"{member}：{dto.error or '评测失败'}")
            statuses.append(EvaluationStatus.ERROR)
            continue

        json_uri = html_uri = None
        if member_outcome.report_json_path is not None:
            json_uri = storage.put(
                dto.content_hash,
                "report.json",
                member_outcome.report_json_path,
                context_hash=context_hash,
            )
        if member_outcome.report_html_path is not None:
            html_uri = storage.put(
                dto.content_hash,
                "report.html",
                member_outcome.report_html_path,
                context_hash=context_hash,
            )
        dto.report_url = html_uri

        save_result(
            session,
            dto,
            source=source,
            report_json_uri=json_uri,
            report_html_uri=html_uri,
            policy_file_hash=policy_hash,
        )
        statuses.append(dto.status)

    if errors:
        reason = "；".join(errors)
        if outcome.retryable:
            _requeue(session, task, settings, reason)
        else:
            task_queue.finish(session, task, error=reason)
        return EvaluationStatus.ERROR

    task_queue.finish(session, task)
    return _worst_status(statuses)


def _worst_status(statuses) -> EvaluationStatus:
    """一组成员的整体状态：只要有一个不通过，整套就不通过。

    刻意不做加权或多数决——"这套工作流能不能用"取决于最弱的那一环。
    """
    order = [
        EvaluationStatus.ERROR,
        EvaluationStatus.FAILED,
        EvaluationStatus.INCOMPLETE,
        EvaluationStatus.PASSED,
    ]
    seen = set(statuses)
    for status in order:
        if status in seen:
            return status
    return EvaluationStatus.ERROR


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
    content_source: SkillContentSource,
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
                content_source=content_source,
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

    content_source = build_content_source(settings)
    storage = LocalReportStorage(settings.report_root)

    logger.info("worker 已启动，轮询队列 fast")
    while True:
        try:
            worked = run_once(
                settings=settings,
                content_source=content_source,
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
