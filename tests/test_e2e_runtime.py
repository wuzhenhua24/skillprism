"""运行时评测（Tier 3）端到端：真的 skill-up、真的 Claude Code、真的模型网关。

单测里 skill-up 是回放真实输出的替身，覆盖不到的是：skill-up 与 Claude Code
的实际行为（版本一漂，eval.yaml 的写法、用例改写、环境白名单都可能失效），
以及网关是不是真的按 Anthropic 协议应答。

需要的东西全部就位才跑，否则跳过——它要花真实的 token（约 5 万）和一两分钟：

    SKILLPRISM_SKILLUP_BIN              skill-up 可执行文件（或在 PATH 上）
    SKILLPRISM_RUNTIME_PATH             能找到 claude / bash / git 的 PATH
    SKILLPRISM_RUNTIME_ENGINE_VERSION   本机 claude 的版本
    SKILLPRISM_RUNTIME_BASE_URL         网关的 Anthropic 兼容地址
    SKILLPRISM_RUNTIME_API_KEY
    SKILLPRISM_RUNTIME_MODEL

被评的是 tests/fixtures/runtime_skill/ticket-formatter，POC 里它三个用例都稳定通过。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from skillprism.config import get_settings, reset_settings
from skillprism.content import LocalDirectorySource
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import ContentSource, EvaluationStatus, Tier
from skillprism.runtime_worker import make_process_runtime_task
from skillprism.schemas import SubmitRequest
from skillprism.service import get_runtime_evaluation, submit
from skillprism.skillup import RuntimePreflightError, preflight_runtime
from skillprism.storage import LocalReportStorage
from skillprism.worker import run_once

pytestmark = pytest.mark.e2e

MOCK_SKILL = Path(__file__).parent / "fixtures" / "runtime_skill" / "ticket-formatter"
SKILL_ID = "ticket-formatter"


@pytest.fixture
def env(tmp_path, monkeypatch, db_url):
    skills = tmp_path / "skills"
    shutil.copytree(MOCK_SKILL, skills / SKILL_ID)
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    monkeypatch.setenv("SKILLPRISM_REPORT_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("SKILLPRISM_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("SKILLPRISM_LOCAL_SKILLS_ROOT", str(skills))
    reset_settings()
    settings = get_settings()
    try:
        ready = preflight_runtime(settings)
    except RuntimePreflightError as exc:
        reset_settings()
        pytest.skip(f"运行时评测的环境不齐：{exc}")
    reset_engine()
    settings.ensure_dirs()
    init_db()
    yield settings, ready
    reset_engine()
    reset_settings()


def test_the_mock_skill_passes_end_to_end(env):
    settings, ready = env
    with session_scope() as db:
        submit(
            db,
            SubmitRequest(skill_id=SKILL_ID, skill_name=SKILL_ID, tier=Tier.TIER3),
            source=ContentSource.LOCAL,
        )

    assert run_once(
        settings=settings,
        content_sources={ContentSource.LOCAL: LocalDirectorySource(settings.local_skills_root)},
        storage=LocalReportStorage(settings.report_root),
        queue="sandbox",
        process=make_process_runtime_task(ready),
    )

    with session_scope() as db:
        dto = get_runtime_evaluation(db, ContentSource.LOCAL, SKILL_ID)
    assert dto is not None, "没有落下运行时结论，看任务的 error"
    assert dto.status is EvaluationStatus.PASSED, [
        (c.case_id, [r.reason for r in c.runs]) for c in dto.cases
    ]
    assert [c.case_id for c in dto.cases] == [
        "basic-format",
        "no-invented-steps",
        "writes-file",
    ]
    assert dto.runtime.served_models, "transcript 里取不到实际应答的模型，会话记录格式可能变了"
    assert dto.runtime.engine_version == settings.runtime_engine_version
    assert dto.judge_tokens > 0, "agent_judge 用例没有真的调 judge"
