"""Adapter：把 SkillEvaluator 的 JSON 报告翻译成本服务的 DTO。

这是唯一了解上游 schema 的模块。上游改字段时只改这里，管理系统不受影响。
解析全程防御式：缺字段、类型不符都不应当让 worker 崩溃，而应当降级成
一个语义明确的结果。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skill_eval_service.domain import (
    EXIT_CONFIG_ERROR,
    EXIT_RUNTIME_ERROR,
    EvaluationStatus,
    Severity,
    Tier,
)
from skill_eval_service.runner import RunOutcome
from skill_eval_service.schemas import (
    EvaluationDTO,
    EvaluatorInfo,
    Finding,
    TierBundle,
    TierResult,
    ValidatorOutcome,
)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_str_list(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value)]


def determine_status(outcome: RunOutcome) -> tuple[EvaluationStatus, str | None]:
    """判定对外状态，并在 ERROR 时给出原因。

    优先级：
    1. 退出码 2/3 —— 评测本身失败，skill 从未被判定，一律 ERROR。
    2. 报告缺失或不可解析 —— 同样是 ERROR，不能因为“没抛异常”就当成通过。
    3. incomplete_scans 非空 —— INCOMPLETE。安全扫描没跑全不是通过，
       即便上游把 overall_status 写成 passed 也以此为准。
    4. overall_status 的 passed / failed / incomplete。
    5. 其它未知取值 —— 不猜，按 ERROR 处理。
    """
    if outcome.exit_code in (EXIT_CONFIG_ERROR, EXIT_RUNTIME_ERROR):
        return EvaluationStatus.ERROR, outcome.failure or f"评测退出码 {outcome.exit_code}"

    if outcome.report is None:
        return EvaluationStatus.ERROR, outcome.failure or "评测未产出可解析的报告"

    if _as_str_list(outcome.report.get("incomplete_scans")):
        return EvaluationStatus.INCOMPLETE, None

    raw = outcome.report.get("overall_status")
    if raw == "passed":
        return EvaluationStatus.PASSED, None
    if raw == "failed":
        return EvaluationStatus.FAILED, None
    if raw == "incomplete":
        return EvaluationStatus.INCOMPLETE, None

    return EvaluationStatus.ERROR, f"未知的 overall_status：{raw!r}"


def _parse_severity(raw: Any) -> Severity:
    try:
        return Severity(str(raw).lower())
    except ValueError:
        return Severity.INFO


def _relative_file_path(raw: Any, skill_root: Path | None) -> str:
    """把问题定位归一化成 skill 内部的相对路径。

    不同 validator 输出的形式不一致：安全扫描给的是 ``SKILL.md``，
    schema 检查给的是物化目录的绝对路径。后者会把我们的临时目录
    （含任务 UUID）原样暴露到管理系统界面上，对使用者毫无意义。
    """
    text = str(raw or "")
    if not text or skill_root is None:
        return text
    try:
        candidate = Path(text)
        if candidate.is_absolute():
            return candidate.relative_to(skill_root).as_posix() or "."
    except ValueError:
        # 不在 skill 根目录下：退回文件名，仍然不暴露内部目录结构。
        return Path(text).name
    return text


def _parse_finding(raw: Any, skill_root: Path | None = None) -> Finding | None:
    data = _as_dict(raw)
    if not data:
        return None
    line = data.get("line_number")
    return Finding(
        category=str(data.get("category", "")),
        severity=_parse_severity(data.get("severity")),
        check_name=str(data.get("check_name", "")),
        message=str(data.get("message", "")),
        file_path=_relative_file_path(data.get("file_path"), skill_root),
        line_number=line if isinstance(line, int) else None,
        suggestion=data.get("suggestion") if isinstance(data.get("suggestion"), str) else None,
    )


def _parse_validator(raw: Any, skill_root: Path | None = None) -> tuple[int, ValidatorOutcome] | None:
    data = _as_dict(raw)
    name = data.get("validator")
    if not name:
        return None

    findings = [
        f for f in (_parse_finding(item, skill_root) for item in _as_list(data.get("findings"))) if f
    ]
    tier = _as_dict(data.get("gating")).get("tier")

    return (
        tier if isinstance(tier, int) else 1,
        ValidatorOutcome(
            validator=str(name),
            description=str(data.get("description", "")),
            passed=bool(data.get("passed", False)),
            status=str(data.get("status", "")),
            findings=findings,
        ),
    )


def _parse_severity_counts(raw: Any) -> dict[Severity, int]:
    counts: dict[Severity, int] = {}
    for key, value in _as_dict(raw).items():
        try:
            counts[Severity(str(key).lower())] = int(value)
        except (ValueError, TypeError):
            continue
    return counts


def _parse_quality(report: dict[str, Any]) -> tuple[float | None, str | None]:
    """质量评分放在 quality_summary 数组里，取第一条。"""
    entries = _as_list(report.get("quality_summary"))
    if not entries:
        return None, None
    first = _as_dict(entries[0])
    score = first.get("overall_score")
    grade = first.get("grade")
    return (
        float(score) if isinstance(score, (int, float)) else None,
        str(grade) if isinstance(grade, str) else None,
    )


def to_dto(
    *,
    skill_id: str,
    content_hash: str,
    outcome: RunOutcome,
    report_url: str | None = None,
    evaluator_version: str | None = None,
    skill_root: Path | None = None,
) -> EvaluationDTO:
    """把一次运行的产物翻译成对外 DTO。"""
    status, error = determine_status(outcome)
    report = outcome.report or {}

    policy = _as_dict(report.get("policy"))
    incomplete = _as_str_list(report.get("incomplete_scans"))

    evaluator = EvaluatorInfo(
        version=evaluator_version,
        profile=policy.get("profile") if isinstance(policy.get("profile"), str) else None,
        policy_digest=policy.get("digest") if isinstance(policy.get("digest"), str) else None,
        incomplete_scans=incomplete,
    )

    by_tier: dict[int, list[ValidatorOutcome]] = {}
    for parsed in (_parse_validator(item, skill_root) for item in _as_list(report.get("results"))):
        if parsed is None:
            continue
        tier_no, validator = parsed
        by_tier.setdefault(tier_no, []).append(validator)

    tiers = TierBundle()
    if by_tier.get(1) or status is not EvaluationStatus.ERROR:
        tiers.tier1 = TierResult(status=status, validators=by_tier.get(1, []))
    # Tier 2 / Tier 3 当前不执行，保持 None——字段存在但为空，
    # 使管理系统前端的分区渲染在将来补齐时无需改契约。

    score, grade = _parse_quality(report)
    generated = report.get("generated_at")
    evaluated_at: datetime | None = None
    if isinstance(generated, str):
        try:
            evaluated_at = datetime.fromisoformat(generated)
        except ValueError:
            evaluated_at = None
    if evaluated_at is None:
        evaluated_at = datetime.now(tz=UTC)

    raw_passed = report.get("overall_passed")

    return EvaluationDTO(
        skill_id=skill_id,
        content_hash=content_hash,
        status=status,
        gate_passed=raw_passed if isinstance(raw_passed, bool) else None,
        evaluated_at=evaluated_at,
        score=score,
        grade=grade,
        severity_counts=_parse_severity_counts(report.get("severity_counts")),
        evaluator=evaluator,
        tiers=tiers,
        report_url=report_url,
        error=error,
    )


#: 供 worker 记录用：本 adapter 已知的上游 tier 编号到领域枚举的映射。
TIER_BY_NUMBER = {1: Tier.TIER1, 2: Tier.TIER2, 3: Tier.TIER3}
