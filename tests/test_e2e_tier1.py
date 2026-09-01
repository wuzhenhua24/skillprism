"""Tier 1 端到端集成测试：真实调用 skillevaluator CLI 跑完整条流水线。

为什么需要它：契约测试（test_upstream_contract.py）只覆盖 JSON 的结构，
覆盖不到 CLI 的实际行为。已经踩到过的两类问题都只有真跑才会暴露——

1. **外部扫描器的版本漂移。** SkillSpector 2.10.0 改了
   ``risk_assessment.recommendation`` 的算法，SkillEvaluator 0.2.1 判定报告
   不可信，把安全扫描降级为 incomplete。失效方式很坑：干净的 skill 照常通过，
   有高危问题的 skill 反而扫不出结果。
2. **物化布局造成的误报。** 目录名与层级会被 SCHEMA.name_consistency 和
   SCHEMA.folder_hierarchy 读取，布局不对会让每个 skill 都平白多出
   一条 HIGH 加一条 MEDIUM。

缺少 CLI 或扫描器时自动跳过，因此本文件在裸环境下不会让 CI 变红；
要显式排除则用 ``pytest -m "not e2e"``。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from skill_eval_service import queue as task_queue
from skill_eval_service.config import get_settings, reset_settings
from skill_eval_service.content import LocalDirectorySource
from skill_eval_service.db import init_db, reset_engine, session_scope
from skill_eval_service.domain import EvaluationStatus
from skill_eval_service.runner import preflight
from skill_eval_service.schemas import SubmitRequest
from skill_eval_service.service import get_evaluation, submit
from skill_eval_service.storage import LocalReportStorage
from skill_eval_service.worker import run_once

pytestmark = pytest.mark.e2e

REQUIRED_SCANNERS = ("semgrep", "gitleaks", "skillspector")

_MISSING_SCANNERS = [name for name in REQUIRED_SCANNERS if shutil.which(name) is None]

needs_cli = pytest.mark.skipif(
    shutil.which("skillevaluator") is None,
    reason="skillevaluator 不在 PATH 上（见 README 的安装步骤）",
)
needs_scanners = pytest.mark.skipif(
    bool(_MISSING_SCANNERS),
    reason=f"缺少外部扫描器：{', '.join(_MISSING_SCANNERS)}",
)

SKILL_ID = "e2e-demo-skill"

#: 这份 fixture 有两处刻意设计：
#:
#: 1. 留了真实缺陷（无 author、缺推荐章节），好让结果不是空的。
#: 2. ``### Input/Output Separation`` 这个带斜杠的标题会被 SkillSpector 的
#:    引用解析当成无法解析的本地路径引用，使 ``analysis_completeness.is_complete``
#:    变为 false。SkillSpector ≥2.10 对覆盖不完整的扫描 fail closed，把
#:    recommendation 从 SAFE 升级为 CAUTION，而 SkillEvaluator 0.2.1 严格按
#:    severity 映射校验，于是判定报告不可信、整个安全扫描标为 incomplete。
#:
#: 换句话说：**没有第 2 点，test_security_scan_completes 抓不到版本回归**——
#: 覆盖完整的干净 skill 在新旧版本上表现一致。文档型 skill（标题含斜杠、
#: 表格里有路径样式的文本）在真实库里很常见，这个 fixture 就是照着它们建的。
SKILL_MD = """---
name: e2e-demo-skill
description: An end to end demonstration skill used by the integration test. Use when verifying that the evaluation pipeline runs to completion.
---

# E2E Demo Skill

## Overview

Prose only. No executable content, so the security scanners have nothing to flag.

### Input/Output Separation

Keep inputs and outputs distinct.
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把整个服务指向临时目录，跑完即弃。"""
    skills = tmp_path / "skills"
    (skills / SKILL_ID).mkdir(parents=True)
    (skills / SKILL_ID / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

    policy = Path(__file__).resolve().parent.parent / "profiles" / "internal.yaml"

    monkeypatch.setenv("SES_DATABASE_URL", f"sqlite:///{tmp_path / 'e2e.db'}")
    monkeypatch.setenv("SES_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SES_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SES_LOCAL_SKILLS_ROOT", str(skills))
    monkeypatch.setenv("SES_POLICY_FILE", str(policy))
    # 扫描器缺失时由 needs_scanners 跳过，这里不再重复拦截。
    monkeypatch.setenv("SES_REQUIRE_SCANNERS", "false")

    reset_settings()
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()

    yield settings

    reset_engine()
    reset_settings()


def _evaluate(settings, *, skill_id: str = SKILL_ID):
    """跑完整条流水线，返回最终 DTO。"""
    source = LocalDirectorySource(settings.local_skills_root)
    storage = LocalReportStorage(settings.report_root)

    with session_scope() as db:
        submit(db, source, SubmitRequest(skill_id=skill_id))

    assert run_once(settings=settings, source=source, storage=storage), "worker 没有取到任务"

    with session_scope() as db:
        return get_evaluation(db, skill_id)


@needs_cli
def test_pipeline_runs_to_completion(env):
    """流水线跑通，且结果不是 ERROR。

    ERROR 意味着 skill 从未被判定——CLI 没跑起来、报告没生成，
    或 adapter 解析失败。上游任何一处变动打断链路，这条先红。
    """
    dto = _evaluate(env)

    assert dto is not None, "评测没有产出结果"
    assert dto.status is not EvaluationStatus.ERROR, f"评测失败：{dto.error}"
    assert dto.evaluator.profile == "internal", "自定义策略没有生效"
    assert dto.evaluator.policy_digest, "缺少策略摘要，无法追溯用了哪套规则"
    assert dto.tiers.tier1 is not None and dto.tiers.tier1.validators
    # Tier 2/3 未实现，必须是显式的空位而不是被误填
    assert dto.tiers.tier2 is None
    assert dto.tiers.tier3 is None


@needs_cli
@needs_scanners
def test_security_scan_completes(env):
    """扫描器齐备时，安全扫描必须真正跑完。

    这是本文件最重要的一条：它会在外部扫描器与 SkillEvaluator 的契约
    发生漂移时立刻变红——正是 SkillSpector 2.10.0 那次的失效形式。
    incomplete_scans 非空代表结论不完整，绝不能当成通过。
    """
    dto = _evaluate(env)

    assert dto.evaluator.incomplete_scans == [], (
        f"安全扫描未跑全：{dto.evaluator.incomplete_scans}。"
        "常见原因是 SkillSpector 版本与 SkillEvaluator 的契约不匹配，"
        "见 README 的版本 pin 说明。"
    )
    assert dto.status is not EvaluationStatus.INCOMPLETE

    validators = {v.validator for v in dto.tiers.tier1.validators}
    assert any("Security" in name for name in validators), "结果里没有安全扫描"


@needs_cli
def test_no_false_positives_from_materialization_layout(env):
    """物化布局不得制造出 skill 本身没有的问题。

    目录名必须是 skill 标识名、且位于 skills/ 下，否则
    SCHEMA.name_consistency（HIGH）与 SCHEMA.folder_hierarchy（MEDIUM）
    会对每一个 skill 报错。
    """
    dto = _evaluate(env)

    checks = {f.check_name for v in dto.tiers.tier1.validators for f in v.findings}
    assert "name_consistency" not in checks, "物化目录名与 frontmatter 的 name 不一致"
    assert "folder_hierarchy" not in checks, "物化布局缺少 skills/ 父层级"


@needs_cli
def test_finding_paths_never_leak_internal_directories(env):
    """问题定位必须是 skill 内的相对路径。

    不同 validator 输出形式不一致，schema 检查会给物化目录的绝对路径，
    里面含任务 UUID——原样传给管理系统对使用者毫无意义。
    """
    dto = _evaluate(env)

    paths = [f.file_path for v in dto.tiers.tier1.validators for f in v.findings]
    assert paths, "这个 skill 应当至少报出一条问题（它没有 author）"

    work_root = str(env.work_root.resolve())
    for path in paths:
        assert not Path(path).is_absolute(), f"暴露了绝对路径：{path}"
        assert work_root not in path, f"暴露了物化目录：{path}"


@needs_cli
def test_reports_are_persisted_and_readable(env):
    """HTML 与 JSON 报告都要落到存储里，且能读回。"""
    dto = _evaluate(env)

    assert dto.report_url, "没有生成 HTML 报告链接"
    report = LocalReportStorage(env.report_root).resolve(dto.report_url)
    assert report is not None and report.exists()
    assert report.read_text(encoding="utf-8").lstrip().lower().startswith("<!doctype html")

    saved = sorted(p.name for p in env.report_root.rglob("*") if p.is_file())
    assert saved == ["report.html", "report.json"]


@needs_cli
def test_unchanged_content_hits_cache(env):
    """内容没变就不该重跑——既省成本，也避免结论无谓抖动。"""
    first = _evaluate(env)

    source = LocalDirectorySource(env.local_skills_root)
    with session_scope() as db:
        again = submit(db, source, SubmitRequest(skill_id=SKILL_ID))
    assert again.cached is True
    assert again.content_hash == first.content_hash

    # 命中缓存时不产生新任务，worker 应当无事可做
    assert run_once(settings=env, source=source, storage=LocalReportStorage(env.report_root)) is False


@needs_cli
def test_changed_content_reevaluates(env):
    """内容变了必须重新评测，并得到新的 content hash。"""
    first = _evaluate(env)

    manifest = env.local_skills_root / SKILL_ID / "SKILL.md"
    manifest.write_text(SKILL_MD + "\n## Instructions\n\nDo the thing.\n", encoding="utf-8")

    second = _evaluate(env)
    assert second.content_hash != first.content_hash


@needs_cli
def test_work_directory_is_cleaned_up(env):
    """物化目录必须清理，否则 work 目录会无限增长。"""
    _evaluate(env)
    leftovers = [p for p in env.work_root.iterdir()] if env.work_root.exists() else []
    assert leftovers == [], f"残留了物化目录：{leftovers}"


@needs_cli
def test_missing_skill_fails_task_without_result(env):
    """取不到内容时任务应当失败，且不写入任何结果。

    写一条不存在的结论，比什么都不写更糟。
    """
    source = LocalDirectorySource(env.local_skills_root)
    with session_scope() as db:
        task = task_queue.enqueue(db, skill_id="does-not-exist", content_hash="sha256:0")
        task_id = task.id

    run_once(settings=env, source=source, storage=LocalReportStorage(env.report_root))

    with session_scope() as db:
        from skill_eval_service.models import EvaluationTask

        refreshed = db.get(EvaluationTask, task_id)
        assert refreshed.state == "failed"
        assert refreshed.error
        assert get_evaluation(db, "does-not-exist") is None


def test_preflight_reports_scanner_state():
    """自检必须如实报告缺哪些扫描器——缺失会让结果变成 incomplete。"""
    report = preflight(get_settings())
    if shutil.which("skillevaluator") is None:
        pytest.skip("skillevaluator 不在 PATH 上")
    assert report.binary is not None
    assert set(report.missing_scanners) <= set(REQUIRED_SCANNERS)
