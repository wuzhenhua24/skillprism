"""持久化模型。

报告原文（HTML/JSON）走对象存储，数据库只存列表与详情页需要的摘要字段。

两条刻意的设计：
- task 表从第一天就带 tier 与 queue，避免将来加 Tier 2/3 时改表。
- 明细按 validator 名 + JSON 存，不拍平成 schema_errors / pii_errors 这类
  固定列——上游新增一个 validator 就要改表结构。
- task 上记 skill_name/skill_version：内容由 worker 下载，触发方在提交时
  声明的身份信息必须先落库，worker 才拿得到。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    pass


class EvaluationTask(Base):
    __tablename__ = "evaluation_task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(255), index=True)
    #: 管理系统里登记的 skill 名。物化目录用它命名，SkillEvaluator 的
    #: SCHEMA.name_consistency 会拿它和 frontmatter 的 name 比对。
    #: 不能拿包里的目录名或 frontmatter 回填——那样这条检查恒真，等于废掉。
    skill_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 入队时为空：内容由 worker 下载，此刻还不知道 hash。
    content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    #: tier 与 queue 是 Tier 2/3 的扩展位，M1 恒为 tier1。
    tier: Mapped[str] = mapped_column(String(16), default="tier1")
    queue: Mapped[str] = mapped_column(String(32), default="fast")

    #: 强制重跑。缓存判定在 worker 里做，所以这个意图必须随任务落库。
    force: Mapped[bool] = mapped_column(Boolean, default=False)

    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvaluationResult(Base):
    __tablename__ = "evaluation_result"
    __table_args__ = (UniqueConstraint("skill_id", "content_hash", name="uq_skill_content"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(255), index=True)
    #: 与 content_hash 唯一，天然实现“内容未变不重跑”。
    content_hash: Mapped[str] = mapped_column(String(80), index=True)
    #: 触发方声明的版本号。结果自身必须带版本，否则界面只能显示一串 hash，
    #: 说不出“这是 v2.0.0 的结论”。仅作标签用，不参与去重。
    skill_version: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(String(16), index=True)
    #: 阻断级检查是否通过，与 status 正交，见 schemas.EvaluationDTO。
    gate_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    severity_counts: Mapped[dict] = mapped_column(JSON, default=dict)

    #: 评测器身份。分数变化时用户第一个要问的就是评测器是否变过。
    evaluator_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_digest: Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: 我们自己算的策略文件指纹，用来判断一条结论还能不能复用。
    #: 上面那个 policy_digest 来自上游报告，只有跑完才知道，判不了"要不要跑"。
    policy_file_hash: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    #: 非空即代表安全扫描没跑全。
    incomplete_scans: Mapped[list] = mapped_column(JSON, default=list)

    report_json_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_html_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    details: Mapped[list["EvaluationDetail"]] = relationship(
        back_populates="result", cascade="all, delete-orphan"
    )


class EvaluationDetail(Base):
    __tablename__ = "evaluation_detail"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    result_id: Mapped[str] = mapped_column(ForeignKey("evaluation_result.id"), index=True)

    validator_name: Mapped[str] = mapped_column(String(128))
    tier: Mapped[str] = mapped_column(String(16), default="tier1")
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="")
    #: 问题明细整体以 JSON 存，结构跟随上游而不绑定表结构。
    findings: Mapped[list] = mapped_column(JSON, default=list)

    result: Mapped[EvaluationResult] = relationship(back_populates="details")
