"""编排：受理触发与查询结果的业务逻辑，供 API 与测试共用。

提交路径上没有任何网络调用——API 进程不需要能访问管理系统，
只有 worker 需要。这是刻意的隔离，见 :func:`submit`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

from skillprism import queue as task_queue
from skillprism.content import join_skill_id, validate_ref
from skillprism.domain import ContentSource, EvaluationStatus, Tier
from skillprism.materialize import MaterializeError, safe_relative_path
from skillprism.models import EvaluationResult, EvaluationTask
from skillprism.repository import (
    ANY_CONTEXT,
    bundle_member_results,
    find_result,
    latest_result,
    result_to_dto,
)
from skillprism.schemas import (
    EvaluationDTO,
    GitLabSubmitRequest,
    SubmitRequest,
    SubmitResponse,
    TaskDTO,
    TaskResultRef,
)


def validate_skill_name(name: str) -> None:
    """校验登记名可以直接当物化目录名用。

    materialize 里有同样的校验，那里才是安全边界，这里不取代它。
    提前做一次是因为 skill_name 是调用方直接给的字段：给一个当场可改的
    422，比让任务在十秒后失败、再让人去翻任务状态要好。内容不能这样处理
    ——提交时还没下载，看不见。

    只对单 skill 调用。bundle 的物化目录名不来自这个字段（见
    :func:`~skillprism.materialize.materialize_bundle`），那里校验它等于用一条
    与结果无关的规则去拦触发。
    """
    if len(safe_relative_path(name).parts) != 1:
        raise MaterializeError(f"skill_name 必须是单段名字，不能含路径分隔符：{name!r}")


def to_submit_request(request: GitLabSubmitRequest) -> SubmitRequest:
    """把 GitLab 那组字段折进内部的 ``skill_id`` / ``skill_version``。

    编码规则（``项目[:子目录]``、ref 当版本）只活在这一层以内：对外是三个
    各自命名的字段，对内是队列和结果表里那两列。落库的形态不变，是有意的
    ——已有的结论、查询与报告地址都按那两列寻址。

    校验放在这里而不是留给 worker：项目路径、子目录、ref 写错了重试多少次
    都一样，当场 422 比十秒后一条失败任务好查得多。

    抛 :class:`~skillprism.content.SkillNotFoundError`（内容层用它表示"这个
    标识不成立"），由 API 层翻成 422。
    """
    return SubmitRequest(
        skill_id=join_skill_id(request.project, request.subdir),
        skill_name=request.skill_name,
        # 留空是合法的：worker 那边会用 GITLAB_DEFAULT_REF。这里不把默认值
        # 提前填进去——填了的话去重键上"没指定 ref"和"显式写了 main"就成了
        # 两个不同的值，而它们其实指同一份内容。
        skill_version=validate_ref(request.ref) if request.ref else None,
        tier=request.tier,
        force=request.force,
        bundle=request.bundle,
    )


def submit(
    session: Session,
    request: SubmitRequest,
    *,
    source: ContentSource,
) -> SubmitResponse:
    """受理一次触发，立刻返回。

    ``source`` 是本次触发的内容来源。它会随任务落库，因为它是身份的一部分
    （见 :class:`~skillprism.domain.ContentSource`），也决定去重的键：
    来源不同就是两个 ``skill_id`` 命名空间，不能互相折叠；来源还决定
    ``skill_version`` 进不进键（:attr:`ContentSource.version_selects_content`）。

    来源由调用方传进来而不是在这里读配置：提交路径要能在测试里两种语义都
    跑到，也为将来两个入口各自声明来源留好位置——那时进程级的"当前来源"
    这个概念就不存在了。

    刻意不在这里下载内容。这个调用挂在用户上传流程后面，同步下载意味着
    对方要承担我们的网络耗时（超时上限 60s、包上限 64MB）和可用性——
    定位是"只展示不拦截"，我们挂了不该反映到他们的上传体验上。
    下载、算 hash、查缓存全部由 worker 承担。
    """
    # bundle 时登记名不参与任何计算（见 SubmitRequest.skill_name），在这里就
    # 归一成 None，后面两条路径都用它、不再碰 request.skill_name：
    #   - 不校验：为一个随后被丢弃的值返回 422，会拿"名字里有斜杠"拦住一次
    #     本来完全合法的整组触发；
    #   - 不落库：存下来就会从 TaskDTO 回显出去，任务状态里于是有一个看起来
    #     权威、实际没参与任何计算的名字。
    skill_name = None if request.bundle else request.skill_name
    if skill_name is not None:
        validate_skill_name(skill_name)

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
            source,
            request.skill_id,
            request.tier,
            skill_version=request.skill_version,
            match_version=source.version_selects_content,
        )
        if existing is not None:
            # 那条任务还没下载内容，跑起来取到的是最新的一份，所以身份
            # 信息要跟着更新到本次触发——否则结果会挂着旧版本号，
            # 描述的却是新内容。
            #
            # source.version_selects_content 为真时版本已经在去重键里，这里的赋值
            # 是个恒等操作；留着是为了两条路径只有一份身份更新逻辑。
            # 形态也可能在这一次翻转（见下面 existing.bundle），所以这里赋的
            # 是按本次形态归一过的值：单 skill 折进 bundle 时名字要跟着清掉，
            # 反过来则要补上。
            existing.skill_name = skill_name
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
        source=source,
        skill_id=request.skill_id,
        skill_name=skill_name,
        skill_version=request.skill_version,
        tier=request.tier,
        force=request.force,
        bundle=request.bundle,
    )
    return SubmitResponse(task_id=task.id, skill_id=task.skill_id, state=task.state)


def lookup_result(
    session: Session,
    source: ContentSource,
    skill_id: str,
    *,
    content_hash: str | None = None,
    context_hash: str | None | Any = ANY_CONTEXT,
) -> EvaluationResult | None:
    """定位一条结论。给了 ``content_hash`` 就精确取，否则退回最近一条。

    ``skill_id`` 只在一个来源内部唯一，所以查找必须带上 ``source``——
    否则查询会跨到另一个接入的命名空间里去，取到一条同名但无关的结论。

    结论与结果页必须走**同一条**查找逻辑。两边各写一遍的后果是"结论查准了、
    点开报告却是另一份"——补 content_hash 之前的 ``/report`` 就是这样。

    没给 hash 时的"最近一条"在两种接入下含义不同：zip 接入每次上传换一个
    资源 ID，一个 skill_id 基本只有一条结论，取最近的就是取那条；GitLab
    接入下 skill_id 是仓库路径、长期不变，多个 ref 的结论堆在同一个 ID 下，
    取到的是最近评完的那个 ref。要指定版本必须带 content_hash——
    ``skill_version`` 只是标签，同一份内容被两个 ref 评过时会被后写的覆盖
    （见 :func:`repository.save_result`），按它查会漏。

    ``content_hash`` 还不足以唯一确定一条结论：同一个 skill 单独评过、又在
    一组里评过时，两条结论的 (skill_id, content_hash) 完全相同，只有上下文
    不同（见 :func:`repository.find_result`）。所以 ``context_hash`` 可以一起
    给——``None`` 表示"要单独评的那条"，不给表示不限、取最近评完的一条。
    """
    if content_hash:
        return find_result(
            session, source, skill_id, content_hash, context_hash=context_hash
        )
    return latest_result(session, source, skill_id, context_hash=context_hash)


def report_url_for(row: EvaluationResult, public_base_url: str) -> str | None:
    """这条结论的 HTML 报告的公开地址。

    **一定带上 source 与 content_hash。** 不带的话链接的含义是"这个 skill 最近评完的
    那条"，会随后续评测漂走——GitLab 接入下 skill_id 是长期不变的仓库路径，
    多个 ref 的结论堆在同一个 ID 下（同一条理由见 :func:`lookup_result`）。
    链接一旦回给管理系统就会进它们的库、长期存在，那时"指错版本"比现在
    难查得多。

    没配公开地址、或这条结论压根没有报告时返回 None。宁可没有链接，也不
    给一个点开是 404 的链接——后者会被当成服务坏了。
    """
    if not public_base_url or not row.report_html_uri:
        return None
    # 上下文也要钉住，理由和 content_hash 一样、只是更隐蔽：同一个 skill
    # 单独评过、又在一组里评过时，两条结论的 (skill_id, content_hash) 一模
    # 一样。只带 content_hash 的链接于是指向"这两条里最近的那条"，会随后续
    # 评测在两条之间跳。单独评的那条上下文为空，链接里就是个空值——那不是
    # "没写"，正是"要单独评的那条"（见 lookup_result）。
    # skill_id 可能含 ``/``（GitLab 接入下它就是仓库路径），而路由是
    # ``{skill_id:path}``，斜杠必须保留字面量；``:`` 在路径段里合法，一并
    # 放行。其余照常编码——一个没编码的 ``?`` 或 ``#`` 会把后面的查询串截掉。
    path = quote(row.skill_id, safe="/:")
    # source 必须进链接：两种接入的 skill_id 是两个命名空间，只带
    # content_hash 的话，同名 ID 在另一个来源下也存在时会取到那一条。
    query = urlencode(
        {
            "source": row.source,
            "content_hash": row.content_hash,
            "context_hash": row.context_hash or "",
        }
    )
    return f"{public_base_url.rstrip('/')}/api/skills/{path}/report?{query}"


def get_evaluation(
    session: Session,
    source: ContentSource,
    skill_id: str,
    *,
    content_hash: str | None = None,
    context_hash: str | None | Any = ANY_CONTEXT,
    public_base_url: str = "",
) -> EvaluationDTO | None:
    """取一条结论。``public_base_url`` 决定 ``report_url`` 拼不拼得出来。

    公开地址由调用方传进来而不是在这里读全局配置，和 :func:`submit` 收
    ``source`` 是同一个理由：查询路径要能在测试里两种配置都跑到，不该依赖
    进程级的全局状态。
    """
    row = lookup_result(
        session, source, skill_id, content_hash=content_hash, context_hash=context_hash
    )
    if row is None:
        return None
    return result_to_dto(row, report_url=report_url_for(row, public_base_url))


def task_results(
    session: Session, task: EvaluationTask, *, public_base_url: str = ""
) -> list[TaskResultRef]:
    """这条任务已经落库的结论，连同各自的寻址键。

    两种形态查法不同，因为任务行上那个 hash 的含义不同：

    - 单任务：``task.content_hash`` 就是结论的寻址键，直接精确取那一条。
    - bundle：它是**整组**的指纹，即每条成员结论的 ``context_hash``。成员
      按 (来源, 上下文, ``<bundle_id>/`` 前缀) 反查，见
      :func:`repository.bundle_member_results`。

    结果表不记 task_id，所以这里是**按内容反查**而不是按外键取。这不是将就：
    一条结论会被后来的任务复用，全命中的 bundle 任务一行新记录都不写，那时
    外键只会指向更早的某个任务。反查给出的是"这次任务的内容当前对应哪些
    结论"，那正是调用方要问的。

    没跑完（``content_hash`` 还没填）就是空列表。bundle 里个别成员评失败时
    这里少几条——失败原因在 ``task.error`` 里，不在这里编一条占位结论。
    """
    if not task.content_hash:
        return []
    try:
        source = ContentSource(task.source)
    except ValueError:
        # 库里存了个无法识别的来源。诊断接口不该因此 500：任务本身照常回显，
        # 结论查不了就是查不了（worker 也会让这种任务带着原因作废）。
        return []

    if task.bundle:
        rows = bundle_member_results(session, source, task.skill_id, task.content_hash)
    else:
        # 显式 context_hash=None：单任务评的就是"单独评"那个上下文。不指明
        # 的话会退回"最近一条"，而同一份内容在一组里也评过时，最近的那条是
        # 成员结论——任务于是交回一把指向别人结论的钥匙。
        row = find_result(
            session, source, task.skill_id, task.content_hash, context_hash=None
        )
        rows = [row] if row is not None else []

    return [
        TaskResultRef(
            skill_id=row.skill_id,
            content_hash=row.content_hash,
            context_hash=row.context_hash,
            status=EvaluationStatus(row.status),
            report_url=report_url_for(row, public_base_url),
        )
        for row in rows
    ]


def task_to_dto(
    session: Session, task: EvaluationTask, *, public_base_url: str = ""
) -> TaskDTO:
    """把任务行翻成对外的任务状态。

    这里做的关键一件事是**把任务行上那一列 hash 按形态放到对的字段上**：
    bundle 任务存的是整组指纹，它是成员结论的 ``context_hash``，不是任何一
    条结论的 ``content_hash``。落库时两者共用一列（任务确实只有一个"我取到
    的内容的指纹"），但对外必须分开——一词之差，调用方拿着它查一辈子都是
    404，而且中间没有任何一步报错。
    """
    is_bundle = bool(task.bundle)
    return TaskDTO(
        task_id=task.id,
        # 任务自己记着来源，不是现读配置：排队期间配置可能已经改了，
        # 而这条任务属于它入队时的那个来源。
        source=task.source,
        skill_id=task.skill_id,
        skill_name=task.skill_name,
        skill_version=task.skill_version,
        bundle=is_bundle,
        # 入队时为空，worker 下载完内容才填上。
        content_hash=None if is_bundle else task.content_hash,
        context_hash=task.content_hash if is_bundle else None,
        tier=task.tier,
        queue=task.queue,
        state=task.state,
        attempts=task.attempts,
        error=task.error,
        next_attempt_at=task.next_attempt_at,
        results=task_results(session, task, public_base_url=public_base_url),
    )


def report_path(uri: str | None) -> Path | None:
    if not uri or not uri.startswith("file://"):
        return None
    path = Path(uri.removeprefix("file://"))
    return path if path.exists() else None


#: Tier 2/3 尚未实现。这里显式列出以便 API 返回明确的“未实现”，
#: 而不是静默当成 Tier 1 处理。
IMPLEMENTED_TIERS = {Tier.TIER1}
