"""数据访问：结果的读写与 DTO 重建。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from skill_eval_service.domain import EvaluationStatus, Severity, Tier
from skill_eval_service.models import EvaluationDetail, EvaluationResult
from skill_eval_service.schemas import (
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


def save_result(
    session: Session,
    dto: EvaluationDTO,
    *,
    report_json_uri: str | None = None,
    report_html_uri: str | None = None,
) -> EvaluationResult:
    """写入结果。同一 (skill_id, content_hash) 覆盖既有记录。"""
    existing = find_result(session, dto.skill_id, dto.content_hash)
    if existing is not None:
        session.delete(existing)
        session.flush()

    row = EvaluationResult(
        id=str(uuid.uuid4()),
        skill_id=dto.skill_id,
        content_hash=dto.content_hash,
        status=str(dto.status),
        gate_passed=dto.gate_passed,
        score=dto.score,
        grade=dto.grade,
        severity_counts={str(k): v for k, v in dto.severity_counts.items()},
        evaluator_version=dto.evaluator.version,
        profile=dto.evaluator.profile,
        policy_digest=dto.evaluator.policy_digest,
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
