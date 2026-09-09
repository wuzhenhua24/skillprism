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
from skillprism.models import EvaluationTask
from skillprism.schemas import SubmitRequest
from skillprism.domain import ContentSource
from skillprism.service import get_evaluation, lookup_result, submit, task_to_dto
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


@needs_cli
@needs_scanners
def test_the_task_hands_back_keys_that_actually_resolve(env):
    """真跑一遍之后，只靠任务接口就能走到每一条结论。

    调用方的流程是 提交 → 轮任务 → 拿任务上的 hash 查结论。bundle 上任务行
    记的是**整组**的指纹，它是成员结论的 context_hash、不是任何一条结论的
    寻址键——这条路于是在最后一步静默断掉：提交 202、任务 done、查询 404，
    中间没有一步报错。所以成员各自的寻址键必须由任务接口交出来，而且交出来
    的键得真的查得到。
    """
    _run(env, SubmitRequest(skill_id=BUNDLE_ID, skill_name=BUNDLE_ID, bundle=True))

    with session_scope() as db:
        task = db.query(EvaluationTask).one()
        dto = task_to_dto(db, task)

        assert dto.bundle is True
        # 组指纹只以 context_hash 的名义出现，不冒充 content_hash。
        assert dto.content_hash is None
        assert dto.context_hash == task.content_hash
        assert [r.skill_id for r in dto.results] == [f"{BUNDLE_ID}/{m}" for m in MEMBERS]

        for ref in dto.results:
            assert ref.content_hash != dto.context_hash
            got = get_evaluation(
                db, ContentSource.LOCAL, ref.skill_id, content_hash=ref.content_hash
            )
            assert got is not None, f"任务交回的键查不到结论：{ref.skill_id}"
            assert got.context_hash == dto.context_hash


#: 两个成员用**逐字节相同**的 SKILL.md，frontmatter 的 name 都写 alpha。
#: 于是 alpha/ 的目录名与它一致、beta/ 的不一致——同样的字节，两个结论。
TWIN_MD = """---
name: alpha
description: A duplicated skill used to check that two byte-identical members keep their own verdicts. Use when auditing the reuse key.
metadata:
  author: SkillPrism E2E <e2e@example.com>
---

# Alpha

Prose only. No executable content.
"""

TWINS_ID = "twins"


def _checks(dto) -> set[str]:
    return {f.check_name for v in dto.tiers.tier1.validators for f in v.findings}


@needs_cli
@needs_scanners
def test_two_identical_members_keep_their_own_verdicts(env):
    """一组里两个成员字节相同、目录名不同，结论必须各归各的。

    目录名参与判定（SCHEMA.name_consistency 拿它和 frontmatter 的 name 比），
    所以这两个成员的结论本来就不一样。哈希不带目录名的话它们会算出同一个
    content_hash，后果有三个，这条用例把三个一起钉住：

    1. 复用按 (content_hash, context_hash) 找，两个成员会命中同一行，于是
       **重新触发一次同样的内容，其中一个的分数就变了**——正是整个复用设计
       最想避免的那个症状。
    2. 报告按 (content_hash, context_hash) 寻址，两个成员会共用一个文件，
       后跑的覆盖先跑的。这一条第一次评测当场就发生，不需要复用参与。
    3. clone 往已经有行的身份上插，撞唯一键把任务搞崩。
    """
    root = env.local_skills_root / TWINS_ID
    for member in ("alpha", "beta"):
        (root / member).mkdir(parents=True)
        (root / member / "SKILL.md").write_text(TWIN_MD, encoding="utf-8")

    _run(env, SubmitRequest(skill_id=TWINS_ID, skill_name=TWINS_ID, bundle=True))

    with session_scope() as db:
        alpha = get_evaluation(db, ContentSource.LOCAL, f"{TWINS_ID}/alpha")
        beta = get_evaluation(db, ContentSource.LOCAL, f"{TWINS_ID}/beta")
        reports = {
            m: lookup_result(db, ContentSource.LOCAL, f"{TWINS_ID}/{m}").report_html_uri
            for m in ("alpha", "beta")
        }

    assert alpha is not None and beta is not None
    assert alpha.content_hash != beta.content_hash, (
        "字节相同的两个成员算出了同一个 content_hash——目录名没进哈希"
    )
    # 上下文是整组的，两个成员共享，这是对的。
    assert alpha.context_hash == beta.context_hash

    assert "name_consistency" not in _checks(alpha)
    assert "name_consistency" in _checks(beta), (
        "beta/ 的目录名与 frontmatter 的 alpha 不符，这条该报"
    )

    assert reports["alpha"] != reports["beta"], (
        "两个成员共用了一份报告文件，后跑的覆盖了先跑的"
    )

    # 再触发一次同样的内容：走全命中缓存那条路，结论必须原封不动。
    _run(env, SubmitRequest(skill_id=TWINS_ID, skill_name=TWINS_ID, bundle=True))

    with session_scope() as db:
        alpha_again = get_evaluation(db, ContentSource.LOCAL, f"{TWINS_ID}/alpha")
        beta_again = get_evaluation(db, ContentSource.LOCAL, f"{TWINS_ID}/beta")

    for before, after, member in (
        (alpha, alpha_again, "alpha"),
        (beta, beta_again, "beta"),
    ):
        assert after.score == before.score, f"{member} 的分数被另一个成员的结论顶掉了"
        assert _checks(after) == _checks(before), f"{member} 的问题清单变了"
        # 命中缓存就不会重跑评测器，评测时间因此不变。
        assert after.evaluated_at == before.evaluated_at, f"{member} 被重新评了一遍"
