"""一组耦合 skill 的端到端集成测试：真实调用 skillevaluator CLI。

为什么必须是端到端：要证明的那件事只有真跑才看得见。一套研发工作流拆成
几个 skill，彼此有跨目录引用；单独物化一个来评，指向兄弟 skill 的相对链接
全是死链，`Code Integrity & Hygiene` 直接判 fail——那是我们的物化方式造成的
误报，不是 skill 的问题。整套一起物化、交给 SkillEvaluator 的 catalog 模式，
同一份内容该项就通过。

这个对比（`test_alone_reports_dead_link` vs `test_bundle_resolves_cross_skill_link`）
是本文件的核心：只测后者的话，哪天物化改回单个也不会有人发现。

缺少 CLI 或扫描器时自动跳过，与 test_e2e_tier1.py 一致。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from skillprism.config import get_settings, reset_settings
from skillprism.content import LocalDirectorySource
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import EvaluationStatus
from skillprism.schemas import SubmitRequest
from skillprism.domain import ContentSource
from skillprism.service import get_evaluation, submit
from skillprism.storage import LocalReportStorage
from skillprism.worker import run_once

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

BUNDLE_ID = "dev-workflow"
MEMBERS = ("code-review", "test-gen")

#: 两个 skill 互相引用，外加一份共享约定——耦合的典型形态。
#: 链接必须是 markdown 链接：dead_links 检查看的就是它。
CODE_REVIEW_MD = """---
name: code-review
description: Review a merge request diff and report correctness issues before handing the findings to test generation.
metadata:
  author: SkillPrism E2E <e2e@example.com>
---

# Code Review

评审完成后交给 [test-gen](../test-gen/SKILL.md) 生成回归用例。
公共字段约定见 [接口约定](../shared/api.md)。
"""

TEST_GEN_MD = """---
name: test-gen
description: Generate regression tests from the findings produced by the code review skill in this workflow.
metadata:
  author: SkillPrism E2E <e2e@example.com>
---

# Test Gen

输入来自 [code-review](../code-review/SKILL.md) 的结论。
"""


@pytest.fixture
def env(tmp_path, monkeypatch, db_url):
    """把整个服务指向临时目录，并铺一套耦合 skill。"""
    root = tmp_path / "skills"
    bundle = root / BUNDLE_ID
    (bundle / "code-review").mkdir(parents=True)
    (bundle / "test-gen").mkdir(parents=True)
    (bundle / "shared").mkdir(parents=True)
    (bundle / "code-review" / "SKILL.md").write_text(CODE_REVIEW_MD, encoding="utf-8")
    (bundle / "test-gen" / "SKILL.md").write_text(TEST_GEN_MD, encoding="utf-8")
    (bundle / "shared" / "api.md").write_text("# 接口约定\n", encoding="utf-8")

    # 同一个 skill 再单独放一份，用于对照单独评的结果。
    solo = root / "code-review"
    solo.mkdir(parents=True)
    (solo / "SKILL.md").write_text(CODE_REVIEW_MD, encoding="utf-8")

    policy = Path(__file__).resolve().parent.parent / "profiles" / "internal.yaml"

    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_LOCAL_SKILLS_ROOT", str(root))
    monkeypatch.setenv("SKILLPRISM_POLICY_FILE", str(policy))
    monkeypatch.setenv("SKILLPRISM_REQUIRE_SCANNERS", "false")

    reset_settings()
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()

    yield settings

    reset_engine()
    reset_settings()


def _run(settings, request: SubmitRequest) -> None:
    source = LocalDirectorySource(settings.local_skills_root)
    storage = LocalReportStorage(settings.report_root)
    with session_scope() as db:
        submit(db, request, source=ContentSource.LOCAL)
    assert run_once(
        settings=settings,
        content_sources={ContentSource.LOCAL: source},
        storage=storage,
    ), "worker 没有取到任务"


def _problems(dto, validator_fragment: str) -> list[str]:
    """某个 validator 报出的全部问题文本。

    findings 与 errors 都要看：死链走的是 legacy errors 通道，只看结构化
    findings 会看到一个"失败但没有任何问题"的 validator。
    """
    tier1 = dto.tiers.tier1
    assert tier1 is not None
    return [
        text
        for v in tier1.validators
        if validator_fragment.lower() in v.validator.lower()
        for text in [f.message for f in v.findings] + list(v.errors)
    ]


@needs_cli
@needs_scanners
def test_alone_reports_dead_link(env):
    """对照组：单独评一个耦合 skill，跨 skill 链接被判成死链。

    这不是 skill 的缺陷，是物化方式的产物。留着这条用例，是为了让
    "整套一起评"哪天被改回去时立刻能看出差别。
    """
    _run(env, SubmitRequest(skill_id="code-review", skill_name="code-review"))
    with session_scope() as db:
        dto = get_evaluation(db, ContentSource.LOCAL, "code-review")

    assert dto is not None
    messages = _problems(dto, "integrity")
    assert any("../test-gen/SKILL.md" in m for m in messages), messages


@needs_cli
@needs_scanners
def test_bundle_resolves_cross_skill_links(env):
    """整套一起评：同样的内容，跨 skill 链接不再是死链。"""
    _run(env, SubmitRequest(skill_id=BUNDLE_ID, skill_name=BUNDLE_ID, bundle=True))

    with session_scope() as db:
        for member in MEMBERS:
            dto = get_evaluation(db, ContentSource.LOCAL, f"{BUNDLE_ID}/{member}")
            assert dto is not None, f"成员 {member} 没有结果"
            assert dto.status is not EvaluationStatus.ERROR, dto.error
            dead = [m for m in _problems(dto, "integrity") if "SKILL.md" in m or "api.md" in m]
            assert not dead, f"{member} 仍有跨 skill 死链：{dead}"


@needs_cli
@needs_scanners
def test_bundle_stores_one_result_per_member(env):
    """一次提交产出多条结果，成员的 skill_id 与单独提交时一致。

    这样管理系统不需要第二套查询方式：查一个成员和查任何别的 skill 一样。
    """
    _run(env, SubmitRequest(skill_id=BUNDLE_ID, skill_name=BUNDLE_ID, bundle=True))

    with session_scope() as db:
        dtos = {m: get_evaluation(db, ContentSource.LOCAL, f"{BUNDLE_ID}/{m}") for m in MEMBERS}
        # bundle 的 id 本身不挂结论——它不是一个 skill。
        assert get_evaluation(db, ContentSource.LOCAL, BUNDLE_ID) is None

    assert all(dto is not None for dto in dtos.values())
    # 成员各自的内容指纹不同，但共享同一个上下文指纹。
    hashes = {m: dto.content_hash for m, dto in dtos.items()}
    contexts = {dto.context_hash for dto in dtos.values()}
    assert len(set(hashes.values())) == len(MEMBERS), hashes
    assert len(contexts) == 1 and None not in contexts, contexts


@needs_cli
@needs_scanners
def test_bundle_context_is_not_reused_across_shapes(env):
    """单独评的结论不能被 bundle 复用，反之亦然。

    同一份 code-review 内容在两种上下文下的结论本来就不同（跨 skill 链接
    一边是死链一边不是）。互相复用会给出一个在当前上下文下并不成立的结论，
    而且看起来完全正常。
    """
    _run(env, SubmitRequest(skill_id="code-review", skill_name="code-review"))
    _run(env, SubmitRequest(skill_id=BUNDLE_ID, skill_name=BUNDLE_ID, bundle=True))

    with session_scope() as db:
        solo = get_evaluation(db, ContentSource.LOCAL, "code-review")
        in_bundle = get_evaluation(db, ContentSource.LOCAL, f"{BUNDLE_ID}/code-review")

    assert solo is not None and in_bundle is not None
    # 同样的字节 → 同样的 content_hash；但上下文不同，结论各自独立。
    assert solo.content_hash == in_bundle.content_hash
    assert solo.context_hash is None
    assert in_bundle.context_hash is not None
    assert any("../test-gen/SKILL.md" in m for m in _problems(solo, "integrity"))
    assert not any("../test-gen/SKILL.md" in m for m in _problems(in_bundle, "integrity"))
