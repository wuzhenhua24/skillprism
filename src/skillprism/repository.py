"""数据访问：结果的读写与 DTO 重建。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from skillprism.content import member_skill_id
from skillprism.domain import ContentSource, EvaluationStatus, Severity, Tier
from skillprism.models import EvaluationDetail, EvaluationResult
from skillprism.schemas import (
    EvaluationDTO,
    EvaluatorInfo,
    Finding,
    TierBundle,
    TierResult,
    ValidatorOutcome,
)


#: "调用方没有指定上下文"。必须和"指定了单独评"（``context_hash`` 为 ``None``）
#: 区分开——后者是一个有含义的取值，不是"随便哪条"。
ANY_CONTEXT: Any = object()


def find_result(
    session: Session,
    source: ContentSource,
    skill_id: str,
    content_hash: str,
    *,
    context_hash: str | None | Any = ANY_CONTEXT,
) -> EvaluationResult | None:
    """定位一条结论。身份是 **(来源, skill_id, content_hash, context_hash)**。

    上下文是身份的一部分：同一个 skill 单独评（兄弟目录不在盘上，跨 skill
    链接是死链）和在一组里评，字节相同、content_hash 相同，结论却不一样。
    两条可以同时存在，见 :class:`~skillprism.models.EvaluationResult` 的两条
    部分唯一索引。

    ``context_hash`` 给了就精确匹配，包括显式给 ``None``——那表示"要单独评
    的那条"，不是"不限"。**写路径必须显式给**（:func:`save_result`、
    :func:`clone_result`）：不给就等于按三元组找，会把另一个上下文的结论当成
    同一条删掉。

    不给时回落到"最近评完的那条"。查询侧的老调用方只有三元组（补上下文之前
    发出去的 report_url、管理系统库里存着的链接），对它们只能这样——但要
    回一条确定的，不能因为查出多行就 500。取到的是哪条上下文，DTO 的
    ``context_hash`` 里写着。
    """
    stmt = select(EvaluationResult).where(
        EvaluationResult.source == str(source),
        EvaluationResult.skill_id == skill_id,
        EvaluationResult.content_hash == content_hash,
    )
    if context_hash is ANY_CONTEXT:
        # 多条上下文并存时给最近的一条。order by 不能省：不排序的"随便一条"
        # 会随数据库的物理顺序变，同一个请求两次结果不同。
        return session.execute(
            stmt.order_by(EvaluationResult.evaluated_at.desc()).limit(1)
        ).scalars().first()

    stmt = stmt.where(
        EvaluationResult.context_hash.is_(None)
        if context_hash is None
        else EvaluationResult.context_hash == context_hash
    )
    return session.execute(stmt).scalar_one_or_none()


def latest_result(
    session: Session,
    source: ContentSource,
    skill_id: str,
    *,
    context_hash: str | None | Any = ANY_CONTEXT,
) -> EvaluationResult | None:
    """这个 skill 最近评完的一条结论。

    ``context_hash`` 是可选的过滤，语义同 :func:`find_result`：给了就只在那个
    上下文里找（``None`` 表示单独评的那批），不给就不限。它和有没有指定
    ``content_hash`` 是两件事——"最近一条单独评的结论"是个成立的问题，
    不该因为没钉内容就退化成"最近一条，什么上下文都行"。
    """
    stmt = select(EvaluationResult).where(
        EvaluationResult.source == str(source),
        EvaluationResult.skill_id == skill_id,
    )
    if context_hash is not ANY_CONTEXT:
        stmt = stmt.where(
            EvaluationResult.context_hash.is_(None)
            if context_hash is None
            else EvaluationResult.context_hash == context_hash
        )
    stmt = stmt.order_by(EvaluationResult.evaluated_at.desc()).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def bundle_member_results(
    session: Session,
    source: ContentSource,
    bundle_skill_id: str,
    context_hash: str,
) -> list[EvaluationResult]:
    """一次 bundle 评测产出的那几条成员结论。

    关联键是 ``(source, context_hash, skill_id 前缀)``，**不是一列 task_id**。
    结果表刻意不记任务：结论只取决于内容、评测器和策略，同一条结论会被后来
    的任务复用（:func:`find_reusable_result`），全命中的 bundle 任务一行新记录
    都不写。那种情况下 task_id 列只会指向更早的某个任务，比没有更误导。

    三个条件缺一不可：

    - ``context_hash``：整组的指纹。它把"这一组内容"评出来的结论和同一批
      skill 在别的组合下评出来的结论分开——后者的成员 ``skill_id`` 可能完全
      一样，只有上下文不同。
    - ``skill_id`` 前缀：两组内容恰好一模一样时 ``context_hash`` 会相同，
      但它们挂在各自的 bundle ID 下，是两次触发的两批结论。
    - ``source``：``skill_id`` 只在一个来源内部唯一，理由同
      :func:`find_result`。

    按 ``skill_id`` 排序，好让轮询接口每次返回的顺序稳定。
    """
    # 空成员名即成员 ID 的公共前缀，且和落库时走同一套归一化（末尾斜杠）。
    prefix = member_skill_id(bundle_skill_id, "")
    stmt = (
        select(EvaluationResult)
        .where(
            EvaluationResult.source == str(source),
            EvaluationResult.context_hash == context_hash,
            # autoescape：skill_id 里出现 ``%`` 或 ``_`` 时它们是字面量，
            # 不转义的话前缀会变成通配，匹到别的 bundle 的成员。
            EvaluationResult.skill_id.startswith(prefix, autoescape=True),
        )
        .order_by(EvaluationResult.skill_id)
    )
    return list(session.execute(stmt).scalars())


def skill_ids_with_context(
    session: Session, source: ContentSource, context_hash: str
) -> list[str]:
    """哪些结论把这个值当作 ``context_hash``。

    只服务于一处：查询侧拿到一个查不到结论的 ``content_hash`` 时，判断它
    是不是其实是某一组的整组指纹。是的话 404 就能说出"你拿的是组指纹"，
    而不是让人从"没触发"一路查起——这个误传参正是 bundle 任务只暴露组级
    hash 时最容易犯的错。
    """
    stmt = (
        select(EvaluationResult.skill_id)
        .where(
            EvaluationResult.source == str(source),
            EvaluationResult.context_hash == context_hash,
        )
        .order_by(EvaluationResult.skill_id)
    )
    return list(session.execute(stmt).scalars())


def find_reusable_result(
    session: Session,
    content_hash: str,
    *,
    evaluator_version: str | None,
    policy_file_hash: str,
    context_hash: str | None = None,
) -> EvaluationResult | None:
    """找一条可以直接复用的结论。**刻意不看 skill_id，也不看 source。**

    管理系统每次上传都会产生新的资源 ID，所以 (skill_id, content_hash) 的
    缓存跨上传永远不命中。而它们的上传表单里版本号是**单独填的**——传同一个
    zip、只改版本号是很自然的操作，那就是对同样的字节评第二次，分数可能因为
    LLM 或扫描器抖动而不一样。结论只取决于内容、评测器和策略，跟它来自哪次
    上传无关，所以这里按内容找。

    三个必须卡住的条件：

    - ``evaluator_version``：换了评测器还复用旧结论，正是"分数怎么变了"
      这个问题最难查的形态。
    - ``policy_file_hash``：策略当前是"起点不是定论"，会经常调；
      调完就得重评，否则新策略对存量 skill 不生效。
    - ``incomplete_scans`` 为空：扫描没跑全的结论不该被复用——
      环境修好之后要的正是重跑一遍。存量行的 policy_file_hash 为 NULL，
      匹配不上，会重跑，这是安全的方向。

    **没**卡住的是外部扫描器自身的版本（semgrep / skillspector / gitleaks）。
    它们漂移时这里会给出旧结论，逃生口是 force=true。原有的缓存也有同样的
    问题，这里没让它变严重，但也没有解决它。

    第四个条件是 ``context_hash``：一组耦合 skill 里，成员的结论不只取决于
    它自己的字节。A 改名或删了文件，引用它的 B 字节没变、结论却该变。所以
    上下文必须精确相等才复用——包括 NULL 与非 NULL 不互认：同一个 skill
    单独评（兄弟目录不在盘上，跨 skill 链接是死链）和在一组里评，结论本来
    就不一样，互相复用会给出一个在当前上下文下并不成立的结论。

    ``source`` 不进条件，和 ``skill_id`` 是同一个理由：结论只取决于内容、
    评测器和策略。同一份字节从 zip 传上来还是从 GitLab 取下来，评出来就该
    是同一个结论，跨来源复用是对的。**身份**要分来源（谁的 skill_id），
    **结论**不必分。
    """
    if not policy_file_hash:
        return None
    stmt = (
        select(EvaluationResult)
        .where(
            EvaluationResult.content_hash == content_hash,
            EvaluationResult.evaluator_version == evaluator_version,
            EvaluationResult.policy_file_hash == policy_file_hash,
            EvaluationResult.context_hash.is_(None)
            if context_hash is None
            else EvaluationResult.context_hash == context_hash,
        )
        .order_by(EvaluationResult.evaluated_at.desc())
    )
    for row in session.execute(stmt).scalars():
        if not row.incomplete_scans:
            return row
    return None


def clone_result(
    session: Session,
    origin: EvaluationResult,
    *,
    source: ContentSource,
    skill_id: str,
    skill_version: str | None,
) -> EvaluationResult:
    """把一条既有结论挂到另一个身份上。

    报告按 (content_hash, context_hash) 寻址（见 storage.py），两者都相同才
    共用 URI，所以这里直接复用不复制文件。
    ``evaluated_at`` 保持原值——评测确实是那时候跑的，改掉它等于谎报。

    ``source`` 取的是**本次任务**的来源，不是 ``origin`` 的：跨来源复用是
    允许的（见 :func:`find_reusable_result`），但克隆出来的这条要挂在本次
    触发的命名空间下，否则它在自己的来源里查不到。

    **目标身份上已经有结论时先删后插**，和 :func:`save_result` 同语义。两点
    理由：

    - 不这么做就是往唯一键上硬插。复用是按内容找"最新的一条"，它未必属于
      本次的身份——而本次身份**同时**已经有一条，是完全可能的（同样的字节
      挂在两个 skill_id 或两个来源下，另一个评得更晚）。撞键抛出去会被
      worker 的兜底捕获成"处理异常"，任务重试到失败，错误信息还看不出成因。
    - 跳过也不对。既有的那条可能是旧策略、旧评测器下评的——它没被
      :func:`find_reusable_result` 选中，正说明它按当前判据已经不成立了。
      留着它等于"调完策略对存量 skill 不生效"。origin 是按当前评测器与策略
      筛出来的，覆盖是对的方向。

    origin 自己就挂在目标身份上时直接返回它，不做删了再插的空转——那一下
    会把 origin 删掉。worker 的判据不会走到这里，但这个函数不该依赖调用方。
    """
    # 上下文要显式带上：克隆出来的这条继承 origin 的 context_hash（见下面
    # 的赋值），所以它可能撞上的只有同一上下文下的那条。不带的话会去删另一
    # 个上下文的结论，而那条和这次克隆毫无关系。
    existing = find_result(
        session, source, skill_id, origin.content_hash, context_hash=origin.context_hash
    )
    if existing is not None:
        if existing.id == origin.id:
            return origin
        session.delete(existing)
        session.flush()

    row = EvaluationResult(
        id=str(uuid.uuid4()),
        source=str(source),
        skill_id=skill_id,
        skill_version=skill_version,
        content_hash=origin.content_hash,
        # 上下文必须跟着走：丢掉的话这条克隆看起来就是"单独评出来的"，
        # 之后按上下文找复用会命中一条其实来自别的上下文的结论。
        context_hash=origin.context_hash,
        status=origin.status,
        gate_passed=origin.gate_passed,
        score=origin.score,
        grade=origin.grade,
        severity_counts=dict(origin.severity_counts or {}),
        evaluator_version=origin.evaluator_version,
        profile=origin.profile,
        policy_digest=origin.policy_digest,
        policy_file_hash=origin.policy_file_hash,
        incomplete_scans=list(origin.incomplete_scans or []),
        report_json_uri=origin.report_json_uri,
        report_html_uri=origin.report_html_uri,
        error=origin.error,
        evaluated_at=origin.evaluated_at,
    )
    for detail in origin.details:
        row.details.append(
            EvaluationDetail(
                validator_name=detail.validator_name,
                tier=detail.tier,
                passed=detail.passed,
                status=detail.status,
                findings=list(detail.findings or []),
                errors=list(detail.errors or []),
            )
        )
    session.add(row)
    session.flush()
    return row


def save_result(
    session: Session,
    dto: EvaluationDTO,
    *,
    source: ContentSource,
    report_json_uri: str | None = None,
    report_html_uri: str | None = None,
    policy_file_hash: str | None = None,
) -> EvaluationResult:
    """写入结果。**同一 (source, skill_id, content_hash, context_hash)** 覆盖既有记录。

    四个值缺一不可，缺了都是静悄悄地删掉一条不该删的结论：

    - ``source``：不带的话，GitLab 上 ``group/repo`` 的结论会删掉管理系统里
      恰好也叫 ``group/repo`` 的那条。
    - ``context_hash``：不带的话，整组评出来的成员结论会删掉这个 skill 单独
      评的那条（反向亦然）——字节相同所以 content_hash 相同，但它们是两条
      不同的结论。同一个 bundle 改一个成员重跑也走这条路：没改的成员
      content_hash 不变、上下文变了，旧结论全被删。
    """
    existing = find_result(
        session, source, dto.skill_id, dto.content_hash, context_hash=dto.context_hash
    )
    if existing is not None:
        session.delete(existing)
        session.flush()

    row = EvaluationResult(
        id=str(uuid.uuid4()),
        source=str(source),
        skill_id=dto.skill_id,
        skill_version=dto.skill_version,
        content_hash=dto.content_hash,
        context_hash=dto.context_hash,
        status=str(dto.status),
        gate_passed=dto.gate_passed,
        score=dto.score,
        grade=dto.grade,
        severity_counts={str(k): v for k, v in dto.severity_counts.items()},
        evaluator_version=dto.evaluator.version,
        profile=dto.evaluator.profile,
        policy_digest=dto.evaluator.policy_digest,
        policy_file_hash=policy_file_hash,
        incomplete_scans=list(dto.evaluator.incomplete_scans),
        report_json_uri=report_json_uri,
        report_html_uri=report_html_uri,
        error=dto.error,
        evaluated_at=dto.evaluated_at,
    )

    for tier_name in (Tier.TIER1, Tier.TIER2, Tier.TIER3):
        tier_result = getattr(dto.tiers, str(tier_name))
        if tier_result is None:
            continue
        for validator in tier_result.validators:
            row.details.append(
                EvaluationDetail(
                    validator_name=validator.validator,
                    tier=str(tier_name),
                    passed=validator.passed,
                    status=validator.status,
                    findings=[f.model_dump(mode="json") for f in validator.findings],
                    errors=list(validator.errors),
                )
            )

    session.add(row)
    session.flush()
    return row


def result_to_dto(row: EvaluationResult, *, report_url: str | None = None) -> EvaluationDTO:
    """从数据库行重建对外 DTO。

    ``report_url`` 由调用方给出（见 :func:`service.report_url_for`），**不回落到
    ``row.report_html_uri``**。那是存储地址，形如
    ``file:///opt/skillprism/var/reports/...``：管理系统拿到它什么也做不了，
    还把我们的服务器路径漏了出去。没有公开地址时这个字段就该是 null。
    """
    by_tier: dict[str, list[ValidatorOutcome]] = {}
    for detail in row.details:
        by_tier.setdefault(detail.tier, []).append(
            ValidatorOutcome(
                validator=detail.validator_name,
                passed=detail.passed,
                status=detail.status,
                findings=[Finding.model_validate(f) for f in (detail.findings or [])],
                errors=list(detail.errors or []),
            )
        )

    status = EvaluationStatus(row.status)
    tiers = TierBundle()
    for tier_name in (Tier.TIER1, Tier.TIER2, Tier.TIER3):
        validators = by_tier.get(str(tier_name))
        if validators:
            setattr(tiers, str(tier_name), TierResult(status=status, validators=validators))

    counts: dict[Severity, int] = {}
    for key, value in (row.severity_counts or {}).items():
        try:
            counts[Severity(key)] = int(value)
        except (ValueError, TypeError):
            continue

    return EvaluationDTO(
        skill_id=row.skill_id,
        skill_version=row.skill_version,
        content_hash=row.content_hash,
        context_hash=row.context_hash,
        status=status,
        gate_passed=row.gate_passed,
        evaluated_at=row.evaluated_at,
        score=row.score,
        grade=row.grade,
        severity_counts=counts,
        evaluator=EvaluatorInfo(
            version=row.evaluator_version,
            profile=row.profile,
            policy_digest=row.policy_digest,
            incomplete_scans=list(row.incomplete_scans or []),
        ),
        tiers=tiers,
        report_url=report_url,
        error=row.error,
    )
