"""对外 DTO。

这一层是管理系统看到的唯一契约，刻意不透传 SkillEvaluator 的原始 JSON：
上游改 schema 时只需改 adapter，管理系统不动。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from skillprism.domain import EvaluationStatus, Severity, Tier


def _check_skill_name(name: str | None, *, bundle: bool) -> None:
    """单 skill 必须给登记名；bundle 给了也没用，所以不强求。

    写成模型级校验而不是 ``Field(min_length=1)``，是因为这个"必填"是**有条件
    的**——条件在兄弟字段 ``bundle`` 上，字段级约束看不见它。
    """
    if not bundle and not name:
        raise ValueError(
            "skill_name 必填：物化目录用它命名，缺了只能退回拿 skill_id 当目录名，"
            "SCHEMA.name_consistency 会对每个 skill 报一条 HIGH"
        )


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
    #: 上游用两个通道报问题：结构化的 findings（带 severity 与位置），
    #: 以及只有一句话的 legacy errors。死链就走后者——``passed`` 为 false
    #: 而 ``findings`` 为空，光看结构化字段说不出"为什么没通过"。
    #: 这里如实透传，不给它们编一个 severity：上游没给，编了就是假的。
    errors: list[str] = Field(default_factory=list)


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
    #: 这条结论所在的一组耦合 skill 的整体指纹；单独评的为 null。
    #: 对外可见是有意的：同一个 skill 单独评和在一组里评结论可能不同，
    #: 界面要能说出这条是在哪种上下文下得到的。
    context_hash: str | None = None
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
    #: HTML 报告的公开地址，可直接给最终用户点开。链接钉住这条结论的
    #: content_hash，不会随后续评测漂走。服务端没配公开域名、或这条结论
    #: 没有报告时为 null——不会回落到内部存储地址。
    report_url: str | None = None
    #: status 为 ERROR 时说明原因。
    error: str | None = None


class TaskResultRef(BaseModel):
    """任务产出的一条结论的**寻址键**。

    这三个值（连同任务行上的 ``source``）直接就是两个查询端点的参数：

        GET /api/skills/{skill_id}/evaluation?source=…&content_hash=…
        GET /api/skills/{skill_id}/report?source=…&content_hash=…

    存在的理由是 bundle：一次提交产出几十条结论，它们各自的 ``content_hash``
    ——查结论和报告真正的钥匙——在任务侧原本没有任何出口，而任务行上那个
    hash 是**整组**的指纹，拿它去查一条都查不到。没有这个列表，调用方只能
    靠"知道内情"去拼 ``<bundle_id>/<成员>``，再挨个猜 hash。

    单任务也照样给（列表里就一条）：两种形态一个查法，调用方不需要为
    bundle 写第二条代码路径。
    """

    skill_id: str
    content_hash: str
    #: 这条结论所在的上下文，单独评的为 null。它也在寻址键里：同一个 skill
    #: 单独评过、又在一组里评过时，两条结论的 (skill_id, content_hash) 完全
    #: 相同，只有它不同。原样回传给查询端点（``?context_hash=``，null 传空）
    #: 就取到确定的那条；不传则取最近评完的一条。
    context_hash: str | None = None
    status: EvaluationStatus
    #: 同 :attr:`EvaluationDTO.report_url`：没配公开地址或这条结论没有报告
    #: 时为 null，不回落到内部存储地址。
    report_url: str | None = None


class TaskDTO(BaseModel):
    """任务状态。管理系统轮询这个接口等评测完成。

    两个 hash 字段分开，是因为它们**不是一个东西**，而 bundle 任务上只给
    前者会把调用方直接送进死路：

    - ``content_hash``：这条任务评的那份内容的指纹，也是结论的寻址键。
      bundle 任务为 null——整组的指纹不是任何一条结论的 ``content_hash``，
      给了就是给一个查什么都 404 的值。
    - ``context_hash``：整组内容的指纹，单任务为 null。它是每条成员结论的
      ``context_hash``（同名同义，见 :attr:`EvaluationDTO.context_hash`），
      拿来对账可以，拿来查结论不行。

    要查结论看 :attr:`results`，别自己拼。
    """

    task_id: str
    #: 内容来源，也是查询时的 ``?source=``。用 str 而不是 ContentSource：
    #: 库里出现无法识别的取值时（配置回滚之类），这个诊断接口更该照实回
    #: 显那个值，而不是 500——worker 那边也是照实报错再让任务作废。
    source: str
    skill_id: str
    #: 触发时声明的登记名。bundle 任务为 null——整组物化不用它命名
    #: （catalog 根固定叫 ``skills/``、成员目录用仓库里的名字），提交时给了
    #: 也不会落库。见 :attr:`SubmitRequest.skill_name`。
    skill_name: str | None = None
    skill_version: str | None = None
    #: 这次评的是不是一组耦合 skill。必须暴露：上面两个 hash 字段的含义由
    #: 它决定，而调用方不该被要求记着自己当初提交的是什么。
    bundle: bool = False
    content_hash: str | None = None
    context_hash: str | None = None
    tier: str
    queue: str
    state: str
    attempts: int = 0
    error: str | None = None
    #: 非空即"正在退避、还没到重试时间"。queued 的任务光看 ``state`` 分不出
    #: 是在排队还是在重试，这个字段和 ``error`` 一起才说得清。
    next_attempt_at: datetime | None = None
    #: 这条任务已经落库的结论。没跑完就是空的；bundle 部分成员失败时，
    #: 这里是**已经评出来的那几条**，失败的原因在 ``error`` 里。
    results: list[TaskResultRef] = Field(default_factory=list)


class SubmitRequest(BaseModel):
    """按 zip 下载接口触发评测（``POST /api/evaluations/zip``）。

    也是保留通道 ``POST /api/evaluations`` 的请求体：那个接口在只启用了一种
    接入时按那一种解释这些字段。GitLab 接入用 :class:`GitLabSubmitRequest`，
    它的字段是 ``project`` / ``subdir`` / ``ref``——两种接入对"哪个 skill"和
    "哪个版本"的解释不同，塞进同一组字段的话，服务端就只能靠配置猜是哪种，
    猜错不会报错、只会默默评错东西。

    只声明"评哪个 skill"，内容由 worker 按 skill_id 去管理系统下载。
    提交时不下载：这个调用挂在用户上传流程后面，不能被我们的网络耗时
    或服务可用性拖住——定位是"只展示不拦截"，那这种耦合就不该存在。
    """

    #: 管理系统里的资源 ID，也是拼下载地址用的那个 ID。
    skill_id: str
    #: 管理系统里登记的 skill 名。物化目录用它命名，SkillEvaluator 的
    #: SCHEMA.name_consistency 会拿它和 frontmatter 比对；用 skill_id
    #: （数字 ID）代替会让每个 skill 都平白多一条 HIGH。
    #:
    #: **单 skill 必填；``bundle`` 为 true 时不参与任何计算。**一次提交对应
    #: 多个 skill，登记名只有一个，给不出 N 个：整组物化时 catalog 根固定叫
    #: ``skills/``、不与任何 frontmatter 比对，成员目录名直接用仓库里的那个
    #: （见 ``materialize_bundle``），成员指纹也按各自的目录名算。所以 bundle
    #: 下它既不命名什么、也不进任何 hash——省略即可。
    #:
    #: 传了不报错（保留通道的老调用方还在传），但会被丢弃：不落库，任务状态
    #: 里回显为 null。不改成拒收是因为那会打断已经在传的调用方；不静默存下来
    #: 是因为回显一个没参与计算的名字，正是这个字段一直在误导人的地方。
    skill_name: str | None = Field(default=None, max_length=255)
    #: 用户在管理系统上传时手填的自由文本，与包内 frontmatter 的版本无关。
    #: 上限必须在这里卡住：超长时 PostgreSQL 会抛错而 SQLite 照单全收，
    #: 那是只在生产暴露的故障。
    skill_version: str | None = Field(default=None, max_length=128)
    tier: Tier = Tier.TIER1
    #: 内容未变时默认复用已有结果；置 true 强制重跑。
    force: bool = False
    #: 这次评的是一组耦合 skill：``skill_id`` 指向装着多个 skill 的父目录，
    #: 一次提交产出多条结果。
    #:
    #: 必须由触发方声明，我们不看内容形态推断——``skill_id`` 少写一层子目录
    #: 就会静默变成评另一批东西。声明与内容不符时任务直接失败并说明原因。
    bundle: bool = False

    @model_validator(mode="after")
    def _check_name(self) -> SubmitRequest:
        _check_skill_name(self.skill_name, bundle=self.bundle)
        return self


class GitLabSubmitRequest(BaseModel):
    """按 GitLab 仓库触发评测（``POST /api/evaluations/gitlab``）。

    位置与版本各占自己的字段，不复用 zip 那套 ``skill_id`` / ``skill_version``：
    这边的"哪个 skill"是 (项目, 仓库内子目录)，"哪个版本"是 git ref。内部仍
    编码成 ``项目[:子目录]`` 存进 ``skill_id``（见 ``content.join_skill_id``），
    但那是内部约定，不该要求调用方懂。

    好处不只是字段名好看：项目路径、子目录、ref 的合法性在**提交时**就校验，
    写错当场 422；塞在一个字符串里的时候，这些错要等 worker 去取内容才暴露，
    表现成一条十秒后失败的任务。
    """

    #: GitLab 项目路径 ``group/repo``，也接受数字项目 ID。
    project: str = Field(min_length=1, max_length=255)
    #: 仓库内子目录，例 ``skills/log-triage``。留空表示 SKILL.md 在仓库根。
    #: 带子目录时只取那一个子树——整仓取档容易撞上条目/体积上限，还会把同仓
    #: 其他 skill 的内容算进 content_hash。
    subdir: str | None = Field(default=None, max_length=255)
    #: git ref：分支、tag 或 commit sha。留空用 ``SKILLPRISM_GITLAB_DEFAULT_REF``。
    #: 它**决定取到哪份内容**，这一点和 zip 接入的自由文本版本号不同，
    #: 所以它进排队去重的键（见 ContentSource.version_selects_content）。
    ref: str | None = Field(default=None, max_length=128)
    #: 同 :attr:`SubmitRequest.skill_name`：物化目录用它命名，会和 frontmatter
    #: 比对；``bundle`` 为 true 时不参与任何计算，省略即可。
    skill_name: str | None = Field(default=None, max_length=255)
    tier: Tier = Tier.TIER1
    force: bool = False
    #: ``project`` + ``subdir`` 指向装着多个 skill 的父目录，例 ``skills``。
    #: Claude plugin 形态的仓库要指到 ``skills`` 那一层，不是仓库根。
    bundle: bool = False

    @model_validator(mode="after")
    def _check_name(self) -> GitLabSubmitRequest:
        _check_skill_name(self.skill_name, bundle=self.bundle)
        return self


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
