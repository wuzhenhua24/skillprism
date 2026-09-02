"""数据访问：结果的读写与 DTO 重建。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from skillprism.domain import EvaluationStatus, Severity, Tier
from skillprism.models import EvaluationDetail, EvaluationResult
from skillprism.schemas import (
    EvaluationDTO,
    EvaluatorInfo,
    Finding,
    TierBundle,
    TierResult,
    ValidatorOutcome,
)


def find_result(session: Session, skill_id: str, content_hash: str) -> EvaluationResult | None:
    stmt = select(EvaluationResult).where(
        EvaluationResult.skill_id == skill_id,
        EvaluationResult.content_hash == content_hash,
    )
    return session.execute(stmt).scalar_one_or_none()


def latest_result(session: Session, skill_id: str) -> EvaluationResult | None:
    stmt = (
        select(EvaluationResult)
        .where(EvaluationResult.skill_id == skill_id)
        .order_by(EvaluationResult.evaluated_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def find_reusable_result(
    session: Session,
    content_hash: str,
    *,
    evaluator_version: str | None,
    policy_file_hash: str,
) -> EvaluationResult | None:
    """找一条可以直接复用的结论。**刻意不看 skill_id。**

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
    """
    if not policy_file_hash:
        return None
    stmt = (
        select(EvaluationResult)
        .where(
            EvaluationResult.content_hash == content_hash,
            EvaluationResult.evaluator_version == evaluator_version,
            EvaluationResult.policy_file_hash == policy_file_hash,
        )
        .order_by(EvaluationResult.evaluated_at.desc())
    )
    for row in session.execute(stmt).scalars():
        if not row.incomplete_scans:
            return row
    return None


def clone_result(
    session: Session,
    source: EvaluationResult,
    *,
    skill_id: str,
    skill_version: str | None,
) -> EvaluationResult:
    """把一条既有结论挂到另一个资源 ID 上。

    报告按 content_hash 寻址（见 storage.py），URI 直接共用，不复制文件。
    ``evaluated_at`` 保持原值——评测确实是那时候跑的，改掉它等于谎报。
    """
    row = EvaluationResult(
        id=str(uuid.uuid4()),
        skill_id=skill_id,
        skill_version=skill_version,
        content_hash=source.content_hash,
        status=source.status,
        gate_passed=source.gate_passed,
        score=source.score,
        grade=source.grade,
        severity_counts=dict(source.severity_counts or {}),
        evaluator_version=source.evaluator_version,
        profile=source.profile,
        policy_digest=source.policy_digest,
        policy_file_hash=source.policy_file_hash,
        incomplete_scans=list(source.incomplete_scans or []),
        report_json_uri=source.report_json_uri,
        report_html_uri=source.report_html_uri,
        error=source.error,
        evaluated_at=source.evaluated_at,
    )
    for detail in source.details:
        row.details.append(
            EvaluationDetail(
                validator_name=detail.validator_name,
                tier=detail.tier,
                passed=detail.passed,
                status=detail.status,
                findings=list(detail.findings or []),
            )
        )
    session.add(row)
    session.flush()
    return row


def save_result(
    session: Session,
    dto: EvaluationDTO,
    *,
    report_json_uri: str | None = None,
    report_html_uri: str | None = None,
    policy_file_hash: str | None = None,
) -> EvaluationResult:
    """写入结果。同一 (skill_id, content_hash) 覆盖既有记录。"""
    existing = find_result(session, dto.skill_id, dto.content_hash)
    if existing is not None:
        session.delete(existing)
        session.flush()

    row = EvaluationResult(
        id=str(uuid.uuid4()),
        skill_id=dto.skill_id,
        skill_version=dto.skill_version,
        content_hash=dto.content_hash,
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
                )
            )

    session.add(row)
    session.flush()
    return row


def result_to_dto(row: EvaluationResult, *, report_url: str | None = None) -> EvaluationDTO:
    """从数据库行重建对外 DTO。"""
    by_tier: dict[str, list[ValidatorOutcome]] = {}
    for detail in row.details:
        by_tier.setdefault(detail.tier, []).append(
            ValidatorOutcome(
                validator=detail.validator_name,
                passed=detail.passed,
                status=detail.status,
                findings=[Finding.model_validate(f) for f in (detail.findings or [])],
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
        report_url=report_url or row.report_html_uri,
        error=row.error,
    )
