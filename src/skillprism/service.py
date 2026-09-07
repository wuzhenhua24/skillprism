"""编排：受理触发与查询结果的业务逻辑，供 API 与测试共用。

提交路径上没有任何网络调用——API 进程不需要能访问管理系统，
只有 worker 需要。这是刻意的隔离，见 :func:`submit`。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from skillprism import queue as task_queue
from skillprism.domain import Tier
from skillprism.materialize import MaterializeError, safe_relative_path
from skillprism.models import EvaluationResult
from skillprism.repository import find_result, latest_result, result_to_dto
from skillprism.schemas import EvaluationDTO, SubmitRequest, SubmitResponse


def validate_skill_name(name: str) -> None:
    """校验登记名可以直接当物化目录名用。

    materialize 里有同样的校验，那里才是安全边界，这里不取代它。
    提前做一次是因为 skill_name 是调用方直接给的字段：给一个当场可改的
    422，比让任务在十秒后失败、再让人去翻任务状态要好。内容不能这样处理
    ——提交时还没下载，看不见。
    """
    if len(safe_relative_path(name).parts) != 1:
        raise MaterializeError(f"skill_name 必须是单段名字，不能含路径分隔符：{name!r}")


def submit(
    session: Session,
    request: SubmitRequest,
    *,
    version_selects_content: bool = False,
) -> SubmitResponse:
    """受理一次触发，立刻返回。

    ``version_selects_content`` 由内容来源决定（见
    :attr:`Settings.version_selects_content`），只影响排队去重的键。
    这里收一个布尔而不是直接读配置：提交路径要能在测试里两种语义都跑到，
    不该依赖进程级的全局配置。

    刻意不在这里下载内容。这个调用挂在用户上传流程后面，同步下载意味着
    对方要承担我们的网络耗时（超时上限 60s、包上限 64MB）和可用性——
    定位是"只展示不拦截"，我们挂了不该反映到他们的上传体验上。
    下载、算 hash、查缓存全部由 worker 承担。
    """
    validate_skill_name(request.skill_name)

    if not request.force:
        # 触发接口天然会被重试（对方超时重发、用户连点保存），排队中的
        # 同一个 skill 折叠成一条。
        #
        # 这里是先查后插，不是原子的：多个 API 进程并发提交仍可能各插一条。
        # 不上唯一索引是因为兜底已经存在——worker 会先算 hash 再查缓存，
        # 内容没变的重复任务不会真的跑评测器。这里收敛的是常见情况，
        # 不声称是强保证。
        existing = task_queue.find_queued(
            session,
            request.skill_id,
            request.tier,
            skill_version=request.skill_version,
            match_version=version_selects_content,
        )
        if existing is not None:
            # 那条任务还没下载内容，跑起来取到的是最新的一份，所以身份
            # 信息要跟着更新到本次触发——否则结果会挂着旧版本号，
            # 描述的却是新内容。
            #
            # version_selects_content 为真时版本已经在去重键里，这里的赋值
            # 是个恒等操作；留着是为了两条路径只有一份身份更新逻辑。
            existing.skill_name = request.skill_name
            existing.skill_version = request.skill_version
            # bundle 意图也要跟上：折叠进去的那条还没下载内容，用旧意图跑
            # 会按错误的形态解归档，报一个和本次触发无关的错。
            existing.bundle = request.bundle
            session.flush()
            return SubmitResponse(
                task_id=existing.id,
                skill_id=existing.skill_id,
                state=existing.state,
                deduplicated=True,
            )

    task = task_queue.enqueue(
        session,
        skill_id=request.skill_id,
        skill_name=request.skill_name,
        skill_version=request.skill_version,
        tier=request.tier,
        force=request.force,
        bundle=request.bundle,
    )
    return SubmitResponse(task_id=task.id, skill_id=task.skill_id, state=task.state)


def lookup_result(
    session: Session,
    skill_id: str,
    *,
    content_hash: str | None = None,
) -> EvaluationResult | None:
    """定位一条结论。给了 ``content_hash`` 就精确取，否则退回最近一条。

    结论与结果页必须走**同一条**查找逻辑。两边各写一遍的后果是"结论查准了、
    点开报告却是另一份"——补 content_hash 之前的 ``/report`` 就是这样。

    没给 hash 时的"最近一条"在两种接入下含义不同：zip 接入每次上传换一个
    资源 ID，一个 skill_id 基本只有一条结论，取最近的就是取那条；GitLab
    接入下 skill_id 是仓库路径、长期不变，多个 ref 的结论堆在同一个 ID 下，
    取到的是最近评完的那个 ref。要指定版本必须带 content_hash——
    ``skill_version`` 只是标签，同一份内容被两个 ref 评过时会被后写的覆盖
    （见 :func:`repository.save_result`），按它查会漏。
    """
    if content_hash:
        return find_result(session, skill_id, content_hash)
    return latest_result(session, skill_id)


def get_evaluation(
    session: Session,
    skill_id: str,
    *,
    content_hash: str | None = None,
) -> EvaluationDTO | None:
    row = lookup_result(session, skill_id, content_hash=content_hash)
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
