"""结论复用的判据。

管理系统每次上传都会产生**新的资源 ID**，而版本号是用户在上传表单里
单独填的。于是"传同一个 zip、只改版本号"是一次很自然的操作，按
(skill_id, content_hash) 找缓存则永远不命中——同样的字节会被评第二次，
分数可能因为 LLM 或扫描器抖动而不同。用户看到的就是"我什么都没改，
分数怎么变了"。

所以复用按内容找，不看 skill_id。代价是判据必须严：评测器或策略变过、
或者上一次扫描根本没跑全，都不能复用。这组用例就是钉住这几条边界的
——放宽任何一条，都会让某个 skill 挂上一个不再成立的结论。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from skillprism.models import Base, EvaluationDetail, EvaluationResult
from skillprism.repository import clone_result, find_reusable_result

CONTENT = "sha256:abc"
EVALUATOR = "0.2.1"
POLICY = "sha256:policy-v1"


@pytest.fixture
def factory(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def _seed(session, **overrides) -> EvaluationResult:
    row = EvaluationResult(
        id=str(uuid.uuid4()),
        skill_id=overrides.pop("skill_id", "2000705"),
        skill_version=overrides.pop("skill_version", "1.0.0"),
        content_hash=overrides.pop("content_hash", CONTENT),
        status=overrides.pop("status", "passed"),
        gate_passed=True,
        score=91.5,
        grade="A",
        severity_counts={"low": 2},
        evaluator_version=overrides.pop("evaluator_version", EVALUATOR),
        profile="internal",
        policy_digest="upstream-digest",
        policy_file_hash=overrides.pop("policy_file_hash", POLICY),
        incomplete_scans=overrides.pop("incomplete_scans", []),
        report_json_uri="file:///reports/ab/abc/report.json",
        report_html_uri="file:///reports/ab/abc/report.html",
        evaluated_at=overrides.pop("evaluated_at", datetime(2026, 9, 1, tzinfo=UTC)),
    )
    row.details.append(
        EvaluationDetail(
            validator_name="schema",
            tier="tier1",
            passed=True,
            status="passed",
            findings=[{"check_name": "author_missing"}],
        )
    )
    session.add(row)
    session.flush()
    return row


def _lookup(session, **kwargs):
    return find_reusable_result(
        session,
        kwargs.pop("content_hash", CONTENT),
        evaluator_version=kwargs.pop("evaluator_version", EVALUATOR),
        policy_file_hash=kwargs.pop("policy_file_hash", POLICY),
    )


def test_same_content_from_another_upload_is_reusable(factory):
    """这是整件事的目的：换了资源 ID，同样的内容不该重评。"""
    with factory() as session:
        _seed(session, skill_id="2000705")
        assert _lookup(session) is not None


def test_different_content_is_not_reusable(factory):
    with factory() as session:
        _seed(session)
        assert _lookup(session, content_hash="sha256:other") is None


def test_a_different_evaluator_version_is_not_reusable(factory):
    """换了评测器还复用旧结论，正是“分数怎么变了”最难查的那种形态。"""
    with factory() as session:
        _seed(session, evaluator_version="0.2.1")
        assert _lookup(session, evaluator_version="0.3.0") is None


def test_a_changed_policy_is_not_reusable(factory):
    """策略是“起点不是定论”，会经常调。调完不重评，新策略对存量就不生效。"""
    with factory() as session:
        _seed(session, policy_file_hash="sha256:policy-v1")
        assert _lookup(session, policy_file_hash="sha256:policy-v2") is None


def test_an_incomplete_scan_is_never_reused(factory):
    """扫描没跑全的结论不是通过。环境修好之后要的正是重跑一遍，
    复用它等于把一次残缺的扫描永久固化下来。"""
    with factory() as session:
        _seed(session, incomplete_scans=["skillspector"])
        assert _lookup(session) is None


def test_unknown_policy_fingerprint_disables_reuse(factory):
    """读不到策略文件时指纹为空。宁可重跑，也不要拿含义不明的指纹去匹配。"""
    with factory() as session:
        _seed(session)
        assert _lookup(session, policy_file_hash="") is None


def test_legacy_rows_without_a_fingerprint_are_not_reused(factory):
    """迁移之前写下的结论没有指纹，判断不了策略变没变，一律重跑。"""
    with factory() as session:
        _seed(session, policy_file_hash=None)
        assert _lookup(session) is None


def test_clone_carries_the_verdict_but_takes_the_new_identity(factory):
    """复用要落成新资源 ID 名下的一行，否则按 ID 查结果的接口拿不到东西。"""
    with factory() as session:
        source = _seed(session, skill_id="2000705", skill_version="1.0.0")

        copy = clone_result(session, source, skill_id="2000706", skill_version="1.0.1")

        assert copy.skill_id == "2000706"
        assert copy.skill_version == "1.0.1"
        assert copy.score == source.score
        assert copy.status == source.status
        assert [d.validator_name for d in copy.details] == ["schema"]
        # 报告按 content_hash 寻址，两行共用同一份文件，不复制。
        assert copy.report_html_uri == source.report_html_uri
        # 评测确实是那时候跑的，改掉它等于谎报。
        assert copy.evaluated_at == source.evaluated_at


def test_clone_does_not_disturb_the_source(factory):
    with factory() as session:
        source = _seed(session, skill_id="2000705", skill_version="1.0.0")
        clone_result(session, source, skill_id="2000706", skill_version="1.0.1")

        assert source.skill_id == "2000705"
        assert source.skill_version == "1.0.0"
        assert len(source.details) == 1
