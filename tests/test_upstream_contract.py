"""上游契约测试。

本服务刻意不在运行时依赖 skillevaluator 的 Python API——它的 __init__ 只导出
__version__，函数签名没有稳定性承诺。稳定的是 CLI 的退出码与 JSON schema。

这些用例锁定 adapter 依赖的那部分 JSON 结构；上游一旦改动，这里先红，
而不是等到线上解析出错。
"""

from __future__ import annotations

import pytest

from skill_eval_service.domain import Severity


def test_report_has_fields_adapter_depends_on(upstream_report):
    for key in ("overall_status", "severity_counts", "results", "policy", "quality_summary"):
        assert key in upstream_report, f"上游报告缺少 adapter 依赖的字段：{key}"


def test_result_entries_carry_validator_and_gating_tier(upstream_report):
    for entry in upstream_report["results"]:
        assert "validator" in entry
        assert "passed" in entry
        assert isinstance(entry.get("gating", {}).get("tier"), int)


def test_policy_exposes_profile_and_digest(upstream_report):
    policy = upstream_report["policy"]
    assert isinstance(policy.get("profile"), str)
    assert isinstance(policy.get("digest"), str)


def test_overall_status_uses_known_vocabulary(upstream_report):
    assert upstream_report["overall_status"] in {"passed", "failed", "incomplete"}


def test_severity_enum_matches_upstream():
    """我们镜像了上游的 Severity 取值，这里确认没有漂移。

    skillevaluator 未安装时跳过——它只是 dev 依赖，不是运行时依赖。
    """
    upstream = pytest.importorskip(
        "skillevaluator.models.result",
        reason="skillevaluator 未安装（仅 dev 依赖）",
    )
    assert {s.value for s in upstream.Severity} == {s.value for s in Severity}
