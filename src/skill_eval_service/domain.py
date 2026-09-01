"""领域枚举与常量。

这里刻意不从 skillevaluator 导入任何东西：本服务对外的契约必须独立于上游，
上游 schema 漂移只应影响 adapter 层。上游枚举的一致性由
tests/test_upstream_contract.py 的契约测试保证，而非运行时耦合。
"""

from __future__ import annotations

from enum import StrEnum


class Tier(StrEnum):
    """评测层级。Tier 2/3 当前不执行，仅用于任务路由与结果占位。"""

    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"


class EvaluationStatus(StrEnum):
    """评测状态。

    五个值缺一不可，尤其是 INCOMPLETE 与 ERROR：

    - INCOMPLETE 表示外部扫描器缺失导致安全结论不完整。它不是通过。
      若把它并入 PASSED，界面会给一个实际没扫全的 skill 发合格徽章。
    - ERROR 表示评测本身失败（skill 未被判定），与 FAILED（skill 不合格）
      是两件事：前者应当重试，后者不应当。
    """

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    ERROR = "error"


class Severity(StrEnum):
    """严重级别。镜像上游取值，由契约测试锁定。"""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class TaskState(StrEnum):
    """任务生命周期状态（服务内部，不对外暴露）。"""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


#: SkillEvaluator CLI 退出码语义。
#: 0 通过 / 1 校验失败 / 2 配置错误 / 3 运行时错误。
EXIT_PASSED = 0
EXIT_VALIDATION_FAILED = 1
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 3

#: 退出码 3 是运行时故障，skill 从未被判定，重试有意义。
#: 退出码 2 是配置/参数问题，重试只会重复失败，需要人介入。
RETRYABLE_EXIT_CODES = frozenset({EXIT_RUNTIME_ERROR})

#: Tier 1 完整安全结论所需的外部扫描器。缺失会让上游产出 incomplete。
REQUIRED_SCANNERS = ("semgrep", "gitleaks", "skillspector")
