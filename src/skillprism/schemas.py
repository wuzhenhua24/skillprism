"""对外 DTO。

这一层是管理系统看到的唯一契约，刻意不透传 SkillEvaluator 的原始 JSON：
上游改 schema 时只需改 adapter，管理系统不动。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from skillprism.domain import EvaluationStatus, Severity, Tier


class Finding(BaseModel):
    """一条问题。字段取自上游 finding，但由 adapter 显式映射。"""

    category: str
    severity: Severity
    check_name: str
    message: str
    file_path: str
    line_number: int | None = None
    suggestion: str | None = None


class ValidatorOutcome(BaseModel):
    """单个 validator 的结果。按 validator 名存放，不拍平成固定列——
    上游新增 validator 或将来加 Tier 3 都不需要改结构。"""

    validator: str
    description: str = ""
    passed: bool
    status: str
    findings: list[Finding] = Field(default_factory=list)


class TierResult(BaseModel):
    status: EvaluationStatus
    validators: list[ValidatorOutcome] = Field(default_factory=list)


class EvaluatorInfo(BaseModel):
    """评测器自身的版本与策略信息。

    必须随结果一起展示：分数变化时，用户第一个要问的就是“是不是评测器变了”。
    """

    version: str | None = None
    profile: str | None = None
    policy_digest: str | None = None
    #: 非空即代表安全扫描没跑全，结论不完整。
    incomplete_scans: list[str] = Field(default_factory=list)


class TierBundle(BaseModel):
    """三个层级固定存在，未实现的返回 null。

    这样管理系统前端按分区渲染，将来补上 Tier 2/3 时接口契约不变。
    """

    tier1: TierResult | None = None
    tier2: TierResult | None = None
    tier3: TierResult | None = None


class EvaluationDTO(BaseModel):
    skill_id: str
    #: 触发方声明的版本号。content_hash 标识内容，这个标识人看得懂的版本。
    skill_version: str | None = None
    content_hash: str
    status: EvaluationStatus
    #: 阻断级检查是否全部通过。与 status 正交：status 为 incomplete 时
    #: 仍可能存在 critical/high 问题，单看 status 会漏掉这一点。
    gate_passed: bool | None = None
    evaluated_at: datetime | None = None
    score: float | None = None
    grade: str | None = None
    severity_counts: dict[Severity, int] = Field(default_factory=dict)
    evaluator: EvaluatorInfo = Field(default_factory=EvaluatorInfo)
    tiers: TierBundle = Field(default_factory=TierBundle)
    report_url: str | None = None
    #: status 为 ERROR 时说明原因。
    error: str | None = None


class SubmitRequest(BaseModel):
    """管理系统触发评测。

    只声明"评哪个 skill"，内容由 worker 按 skill_id 去管理系统下载。
    提交时不下载：这个调用挂在用户上传流程后面，不能被我们的网络耗时
    或服务可用性拖住——定位是"只展示不拦截"，那这种耦合就不该存在。
    """

    #: 管理系统里的资源 ID，也是拼下载地址用的那个 ID。
    skill_id: str
    #: 管理系统里登记的 skill 名，必填。物化目录用它命名，
    #: SkillEvaluator 的 SCHEMA.name_consistency 会拿它和 frontmatter 比对。
    #: 用 skill_id（数字 ID）代替会让每个 skill 都平白多一条 HIGH。
    skill_name: str = Field(min_length=1, max_length=255)
    #: 用户在管理系统上传时手填的自由文本，与包内 frontmatter 的版本无关。
    #: 上限必须在这里卡住：超长时 PostgreSQL 会抛错而 SQLite 照单全收，
    #: 那是只在生产暴露的故障。
    skill_version: str | None = Field(default=None, max_length=128)
    tier: Tier = Tier.TIER1
    #: 内容未变时默认复用已有结果；置 true 强制重跑。
    force: bool = False


class SubmitResponse(BaseModel):
    """受理回执，不是评测结果。

    刻意不含 content_hash：提交时还没下载内容，此刻给不出真实的 hash，
    给一个占位值只会让调用方以为它有意义。hash 在任务状态与结果里给。
    """

    task_id: str
    skill_id: str
    state: str
    #: 折叠到了一条已排队的同 skill 任务上，未新建任务。
    #: 触发接口天然会被重试，这里如实告知而不是静默合并。
    deduplicated: bool = False
