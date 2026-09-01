"""Adapter 状态映射测试。

最重要的一条：incomplete 不能被当成 passed。扫描器缺失时上游会输出
incomplete，若下游并入 passed，界面会给一个没扫全的 skill 发合格徽章。
"""

from __future__ import annotations

from pathlib import Path

from skill_eval_service.adapter import determine_status, to_dto
from skill_eval_service.domain import EvaluationStatus, Severity
from skill_eval_service.runner import RunOutcome


def _outcome(exit_code: int = 0, report: dict | None = None, **kwargs) -> RunOutcome:
    return RunOutcome(
        exit_code=exit_code,
        report=report,
        report_json_path=None,
        report_html_path=None,
        **kwargs,
    )


def test_passed():
    status, error = determine_status(_outcome(0, {"overall_status": "passed"}))
    assert status is EvaluationStatus.PASSED
    assert error is None


def test_failed():
    status, _ = determine_status(_outcome(1, {"overall_status": "failed"}))
    assert status is EvaluationStatus.FAILED


def test_incomplete_status():
    status, _ = determine_status(_outcome(0, {"overall_status": "incomplete"}))
    assert status is EvaluationStatus.INCOMPLETE


def test_incomplete_scans_override_passed():
    """即便上游把 overall_status 写成 passed，只要有未完成的扫描就不算通过。"""
    status, _ = determine_status(
        _outcome(0, {"overall_status": "passed", "incomplete_scans": ["semgrep"]})
    )
    assert status is EvaluationStatus.INCOMPLETE


def test_config_error_is_error_not_failed():
    """退出码 2 是流水线配置问题，不是 skill 不合格。"""
    status, error = determine_status(_outcome(2, {"overall_status": "passed"}))
    assert status is EvaluationStatus.ERROR
    assert error


def test_runtime_error_is_error_and_retryable():
    outcome = _outcome(3, None, failure="boom")
    status, _ = determine_status(outcome)
    assert status is EvaluationStatus.ERROR
    assert outcome.retryable is True


def test_config_error_is_not_retryable():
    assert _outcome(2, None).retryable is False


def test_missing_report_is_error():
    """没抛异常不等于评测成功。"""
    status, error = determine_status(_outcome(0, None, failure="未找到 JSON 报告"))
    assert status is EvaluationStatus.ERROR
    assert "未找到" in (error or "")


def test_unknown_status_is_error():
    status, _ = determine_status(_outcome(0, {"overall_status": "weird"}))
    assert status is EvaluationStatus.ERROR


def test_timeout_is_retryable():
    outcome = _outcome(3, None, timed_out=True, failure="超时")
    assert outcome.retryable is True


def test_to_dto_from_real_report(upstream_report):
    """用真实上游报告验证字段映射。"""
    dto = to_dto(
        skill_id="demo/simple",
        content_hash="sha256:deadbeef",
        outcome=_outcome(0, upstream_report),
        evaluator_version="0.2.1",
    )

    assert dto.status is EvaluationStatus.PASSED
    assert dto.score == 76.5
    assert dto.grade == "C"
    assert dto.evaluator.profile == "external"
    assert dto.evaluator.policy_digest and dto.evaluator.policy_digest.startswith("sha256:")
    assert dto.severity_counts[Severity.MEDIUM] == 5
    assert dto.severity_counts[Severity.LOW] == 7

    # Tier 1 有结果，Tier 2/3 是留给将来的空位而非缺失字段
    assert dto.tiers.tier1 is not None
    assert dto.tiers.tier1.validators
    assert dto.tiers.tier2 is None
    assert dto.tiers.tier3 is None


def test_gate_passed_is_orthogonal_to_status(upstream_report):
    """incomplete 状态下仍要能看出阻断级检查是否通过。

    单一 status 会把“扫描没跑全”和“有 critical 问题”压成同一个值，
    界面就无法区分这两件性质完全不同的事。
    """
    report = dict(upstream_report)
    report["incomplete_scans"] = ["gitleaks"]
    report["overall_passed"] = False

    dto = to_dto(skill_id="d", content_hash="sha256:x", outcome=_outcome(1, report))
    assert dto.status is EvaluationStatus.INCOMPLETE
    assert dto.gate_passed is False


def test_file_path_is_relative_to_skill_root(tmp_path):
    """问题定位不能暴露物化临时目录。

    不同 validator 输出形式不一致：安全扫描给相对路径，schema 检查给绝对路径。
    后者含任务 UUID，原样显示给使用者毫无意义。
    """
    root = tmp_path / "work" / "task-uuid" / "skills" / "demo"
    root.mkdir(parents=True)
    report = {
        "overall_status": "failed",
        "results": [{
            "validator": "Schema", "passed": False, "status": "failed",
            "gating": {"tier": 1},
            "findings": [
                {"category": "SCHEMA", "severity": "high", "check_name": "c1",
                 "message": "m", "file_path": str(root / "SKILL.md")},
                {"category": "SECURITY", "severity": "high", "check_name": "c2",
                 "message": "m", "file_path": "SKILL.md"},
                {"category": "SCHEMA", "severity": "low", "check_name": "c3",
                 "message": "m", "file_path": str(root / "scripts" / "run.sh")},
            ],
        }],
    }
    dto = to_dto(skill_id="demo", content_hash="sha256:x",
                 outcome=_outcome(1, report), skill_root=root)
    paths = [f.file_path for v in dto.tiers.tier1.validators for f in v.findings]
    assert paths == ["SKILL.md", "SKILL.md", "scripts/run.sh"]
    assert not any("task-uuid" in p for p in paths)


def test_path_outside_skill_root_falls_back_to_filename(tmp_path):
    root = tmp_path / "skills" / "demo"; root.mkdir(parents=True)
    report = {"overall_status": "failed", "results": [{
        "validator": "V", "passed": False, "status": "failed", "gating": {"tier": 1},
        "findings": [{"category": "C", "severity": "low", "check_name": "c",
                      "message": "m", "file_path": "/etc/somewhere/other.md"}]}]}
    dto = to_dto(skill_id="d", content_hash="sha256:x",
                 outcome=_outcome(1, report), skill_root=root)
    assert dto.tiers.tier1.validators[0].findings[0].file_path == "other.md"


def test_to_dto_error_carries_reason():
    dto = to_dto(
        skill_id="demo",
        content_hash="sha256:x",
        outcome=_outcome(3, None, failure="评测超时（600s）"),
    )
    assert dto.status is EvaluationStatus.ERROR
    assert "超时" in (dto.error or "")
