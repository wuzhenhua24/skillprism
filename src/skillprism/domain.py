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


class ContentSource(StrEnum):
    """skill 内容从哪来。这是**身份的一部分**，不只是一项部署配置。

    两种接入对 ``skill_id`` / ``skill_version`` 的解释不一样：zip 接入下
    ``skill_id`` 是管理系统的资源 ID、``skill_version`` 是用户手填的标签；
    GitLab 接入下 ``skill_id`` 是 ``项目[:子目录]``、``skill_version`` 是
    git ref。所以同一个 ``skill_id`` 字符串在两边可能指向完全不同的东西——
    管理系统的资源 ID ``42`` 和 GitLab 的数字项目 ID ``42`` 就是一例。

    结论与排队都必须带上这一维。不带的话两个来源会在
    ``uq_skill_content`` 上互相覆盖、在排队去重时互相折叠，而且全程不报错
    ——又回到"不报错，只是评错"。
    """

    #: 开发用的本地目录（LocalDirectorySource）。它同样自成一个命名空间：
    #: 本地调试留下的结论不该被生产接入的查询取到。
    LOCAL = "local"
    #: 管理系统的 zip 下载接口（ZipArchiveSource）。
    ZIP = "zip"
    #: GitLab 归档接口（GitLabArchiveSource）。
    GITLAB = "gitlab"

    @property
    def version_selects_content(self) -> bool:
        """``skill_version`` 是否决定取到的是哪份内容。

        zip 接入下它是用户上传时手填的标签，内容由 ``skill_id`` 决定，
        同一个 skill 换个版本号仍然取到同一份内容；GitLab 接入下它是 ref，
        直接决定取到哪个 commit。

        排队去重的键因此不同：前者可以把新触发折叠进旧任务并刷新版本标签，
        后者这么做等于宣称评了 v1、给出的却是 v2 的结论。见
        :func:`skillprism.queue.find_queued`。

        挂在枚举上而不是 :class:`~skillprism.config.Settings` 上：它是**来源
        的性质**，不是部署的性质。放在配置里的写法只在"一个进程一种来源"
        时成立，两种接入并存后就没有正确取值了。
        """
        return self is ContentSource.GITLAB


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
