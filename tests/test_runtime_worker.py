"""sandbox 队列的任务处理：从领任务到结论落库。

skill-up 换成一个回放真实输出的替身（tests/skillup_fake.py），网关探活换成
桩——其余（取内容、算 hash、物化、改写用例、解释产物、存报告、落库、复用）
都是真的。
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skillprism.config import get_settings, reset_settings
from skillprism.db import init_db, reset_engine, session_scope
from skillprism.domain import ContentSource, EvaluationStatus, TaskState, Tier
from skillprism.materialize import SkillFile, compute_content_hash
from skillprism.models import EvaluationResult, EvaluationTask, RuntimeResult
from skillprism.queue import enqueue
from skillprism.runtime_worker import make_process_runtime_task
from skillprism.schemas import EvaluationDTO, EvaluatorInfo
from skillprism.repository import save_result
from skillprism.service import get_evaluation, get_runtime_evaluation, task_results
from skillprism.skillup import NO_CASES_PREFIX, RuntimeProfile, RuntimeReady
from skillprism.storage import LocalReportStorage
from skillprism.worker import run_once
from tests.skillup_fake import FIXTURES, FakeSkillup, fake_claude

MOCK_SKILL = Path(__file__).parent / "fixtures" / "runtime_skill" / "ticket-formatter"
SKILL_ID = "group/repo:skills/ticket-formatter"


def _files(root: Path = MOCK_SKILL) -> list[SkillFile]:
    return [
        SkillFile(path=p.relative_to(root).as_posix(), data=p.read_bytes())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


class FixedSource:
    def __init__(self, files) -> None:
        self.files = files

    def fetch(self, skill_id, version=None):
        return list(self.files)

    def fetch_bundle(self, skill_id, version=None):
        raise AssertionError("运行时评测不该走 bundle 取内容")


@pytest.fixture
def env(tmp_path, monkeypatch, db_url):
    bin_dir = tmp_path / "bin"
    fake_claude(bin_dir)
    fake = FakeSkillup(tmp_path / "fake")
    for key, value in {
        "DATABASE_URL": db_url,
        "REPORT_ROOT": str(tmp_path / "reports"),
        "WORK_ROOT": str(tmp_path / "work"),
        "RUNTIME_PATH": f"{bin_dir}:/usr/bin:/bin",
        "RUNTIME_ENGINE_VERSION": "2.1.284",
        "RUNTIME_BASE_URL": "https://gateway.internal",
        "RUNTIME_API_KEY": "k-test",
        "RUNTIME_MODEL": "deepseek-v4-flash",
        "RETRY_BACKOFF_SECONDS": "0",
    }.items():
        monkeypatch.setenv(f"SKILLPRISM_{key}", value)
    reset_settings()
    reset_engine()
    settings = get_settings()
    settings.ensure_dirs()
    init_db()

    probe: dict = {"failure": None, "calls": 0}

    def fake_probe(_settings):
        probe["calls"] += 1
        return probe["failure"]

    monkeypatch.setattr("skillprism.runtime_worker.probe_gateway", fake_probe)
    ready = RuntimeReady(skillup_bin=str(fake.path), claude_bin=str(bin_dir / "claude"))
    yield settings, fake, ready, probe
    reset_engine()
    reset_settings()


def _enqueue(skill_id: str = SKILL_ID, **kwargs) -> str:
    with session_scope() as db:
        task = enqueue(
            db,
            source=ContentSource.LOCAL,
            skill_id=skill_id,
            skill_name="ticket-formatter",
            tier=Tier.TIER3,
            **kwargs,
        )
        return task.id


def _run(env, files=None) -> bool:
    settings, _, ready, _ = env
    return run_once(
        settings=settings,
        content_sources={ContentSource.LOCAL: FixedSource(files or _files())},
        storage=LocalReportStorage(settings.report_root),
        queue="sandbox",
        process=make_process_runtime_task(ready),
    )


def _task(task_id: str) -> EvaluationTask:
    with session_scope() as db:
        task = db.get(EvaluationTask, task_id)
        db.expunge(task)
        return task


def _rows(skill_id: str = SKILL_ID) -> list[RuntimeResult]:
    with session_scope() as db:
        rows = db.query(RuntimeResult).filter_by(skill_id=skill_id).all()
        for row in rows:
            db.expunge(row)
        return rows


def test_tier3_tasks_go_to_the_sandbox_queue_only(env):
    """fast worker 领不到 tier3 任务：它没有 skill-up，也没做运行时预检。"""
    settings, fake, _, _ = env
    _enqueue()
    assert not run_once(
        settings=settings,
        content_sources={ContentSource.LOCAL: FixedSource(_files())},
        storage=LocalReportStorage(settings.report_root),
        queue="fast",
    )
    assert fake.calls == []


def test_a_passing_run_lands_a_runtime_result(env):
    settings, fake, _, _ = env
    task_id = _enqueue()
    assert _run(env)

    task = _task(task_id)
    assert task.state == str(TaskState.DONE), task.error
    # 与 Tier 1 同一把钥匙：同样的文件、同样的登记名。
    assert task.content_hash == compute_content_hash(_files(), name="ticket-formatter")

    [row] = _rows()
    assert row.status == "passed"
    assert (row.passed, row.failed, row.errored, row.skipped) == (3, 0, 0, 0)
    assert row.case_count == 3
    assert row.served_models == ["deepseek-v4-flash-ga-260731"]
    assert row.engine_version == "2.1.284"
    assert row.model == "deepseek-v4-flash"
    assert row.runtime_fingerprint == RuntimeProfile.from_settings(settings).fingerprint
    assert fake.commands() == ["validate", "run"]

    storage = LocalReportStorage(settings.report_root)
    for uri in (row.report_html_uri, row.report_json_uri, row.events_uri):
        assert storage.resolve(uri) is not None
    assert "/runtime-" in row.report_html_uri, "和 Tier 1 的 report.html 分开放"

    assert not (settings.work_root / task_id).exists(), "工作目录（含专用 HOME）跑完就删"


def test_the_authors_cases_were_rewritten_before_the_run(env, monkeypatch):
    """judge.model 在交给 skill-up 之前就被改成了平台的。替身在 run 时读不到
    工作目录（跑完就删了），所以在 run_skillup 那一刻截住看。"""
    seen = {}
    import skillprism.runtime_worker as rw

    original = rw.run_skillup

    def spy(settings, ready, config_path, work_dir, *, case_count):
        seen["case"] = (config_path.parent / "cases" / "no-invented-steps.yaml").read_text()
        return original(settings, ready, config_path, work_dir, case_count=case_count)

    monkeypatch.setattr(rw, "run_skillup", spy)
    _enqueue()
    _run(env)
    assert "model: gateway/deepseek-v4-flash" in seen["case"]


def test_tier1_and_tier3_results_for_the_same_content_coexist(env):
    """放进 evaluation_result 的话，save_result 先删后插会删掉 Tier 1 那条。"""
    content_hash = compute_content_hash(_files(), name="ticket-formatter")
    with session_scope() as db:
        save_result(
            db,
            EvaluationDTO(
                skill_id=SKILL_ID,
                content_hash=content_hash,
                status=EvaluationStatus.PASSED,
                score=90.0,
                evaluator=EvaluatorInfo(version="0.3.0"),
            ),
            source=ContentSource.LOCAL,
        )
    _enqueue()
    _run(env)

    with session_scope() as db:
        assert db.query(EvaluationResult).count() == 1
        dto = get_evaluation(db, ContentSource.LOCAL, SKILL_ID, content_hash=content_hash)
    assert dto.score == 90.0, "Tier 1 的结论原样还在"
    assert dto.tiers.tier3 is not None
    assert dto.tiers.tier3.status is EvaluationStatus.PASSED
    assert [v.validator for v in dto.tiers.tier3.validators] == [
        "basic-format",
        "writes-file",
        "no-invented-steps",
    ]


def test_task_results_point_at_the_runtime_result(env):
    task_id = _enqueue()
    _run(env)
    with session_scope() as db:
        task = db.get(EvaluationTask, task_id)
        [ref] = task_results(db, task, public_base_url="https://prism.internal")
    [row] = _rows()
    assert ref.runtime_fingerprint == row.runtime_fingerprint
    assert ref.context_hash is None
    assert ref.report_url.startswith(
        f"https://prism.internal/api/skills/{SKILL_ID}/runtime-report?source=local&content_hash="
    )
    assert "fingerprint=sha256" in ref.report_url


def test_three_iterations_store_every_iterations_files(env, monkeypatch):
    monkeypatch.setenv("SKILLPRISM_RUNTIME_ITERATIONS", "3")
    reset_settings()
    settings, fake, ready, probe = env
    env = (get_settings(), fake, ready, probe)
    fake.set(scenario="iterations")
    _enqueue()
    _run(env)

    [row] = _rows()
    assert row.status == "failed"
    assert (row.passed, row.failed) == (9, 3)
    assert row.iterations == 3
    stored = Path(LocalReportStorage(get_settings().report_root).resolve(row.report_json_uri)).parent
    assert {"result.json", "result-2.json", "result-3.json", "events.jsonl"} <= {
        p.name for p in stored.iterdir()
    }


def test_every_iterations_report_is_reachable_over_http(env, monkeypatch, tmp_path):
    """库里只记第一轮的报告，其余几轮靠命名约定从同一目录推出来。入存储
    与取报告两边的约定要对得上，否则迭代 2、3 的报告存下了却没有出口。"""
    monkeypatch.setenv("SKILLPRISM_RUNTIME_ITERATIONS", "3")
    reset_settings()
    _, fake, ready, probe = env
    env = (get_settings(), fake, ready, probe)
    scenario = tmp_path / "scenario"
    shutil.copytree(FIXTURES / "iterations", scenario)
    for n in (1, 2, 3):
        (scenario / "out" / f"iteration-{n}" / "report.html").write_text(f"<html>iter {n}</html>")
    fake.set(scenario=str(scenario))
    _enqueue()
    _run(env)

    with session_scope() as db:
        dto = get_runtime_evaluation(
            db, ContentSource.LOCAL, SKILL_ID, public_base_url="https://prism.internal"
        )
    assert [r.iteration for r in dto.iteration_reports] == [1, 2, 3]
    assert dto.iteration_reports[0].report_url == dto.report_url
    assert dto.iteration_reports[2].report_url.endswith("&iteration=3")
    assert dto.events_url is not None

    [row] = _rows()
    storage_dir = Path(LocalReportStorage(get_settings().report_root).resolve(row.report_html_uri)).parent
    assert (storage_dir / "report-3.html").read_text() == "<html>iter 3</html>"


def test_the_stored_event_log_does_not_carry_the_work_dir(env, tmp_path):
    """事件流会原样交给对接方，工作目录要和 reason 一样去掉。"""
    _, fake, _, _ = env
    scenario = tmp_path / "scenario"
    shutil.copytree(FIXTURES / "passed", scenario)
    events = scenario / "events.jsonl"
    events.write_text(events.read_text().replace(
        '"title":"', '"title":"@WORK@/skill ', 1
    ))
    fake.set(scenario=str(scenario))
    _enqueue()
    _run(env)

    [row] = _rows()
    stored = LocalReportStorage(get_settings().report_root).resolve(row.events_uri).read_text()
    assert str(get_settings().work_root) not in stored
    assert '"title":"skill ' in stored


def test_a_run_where_nothing_was_judged_is_retried_without_a_result(env):
    """最常见的原因是网关限流或抖动，值得重试；界面上不该出现一个并不存在的判定。"""
    _, fake, _, _ = env
    fake.set(scenario="errored")
    task_id = _enqueue()
    _run(env)

    task = _task(task_id)
    assert task.state == str(TaskState.QUEUED)
    assert "context deadline exceeded" in task.error
    assert _rows() == []


def test_invalid_cases_end_the_task_without_retry(env):
    _, fake, _, _ = env
    fake.set(validate_exit=1, validate_stderr="Error: validation failed: case x: bad\n")
    task_id = _enqueue()
    _run(env)

    task = _task(task_id)
    assert task.state == str(TaskState.FAILED)
    assert task.error.startswith("用例无效") and "case x: bad" in task.error
    assert fake.commands() == ["validate"]


def test_a_skill_without_cases_says_so(env):
    """管理系统靠这个前缀提示作者补用例，而不是报"评测失败"。"""
    _, fake, _, probe = env
    task_id = _enqueue()
    _run(env, files=[f for f in _files() if not f.path.startswith("evals/")])

    task = _task(task_id)
    assert task.state == str(TaskState.FAILED)
    assert task.error.startswith(NO_CASES_PREFIX)
    assert fake.calls == [] and probe["calls"] == 0, "没用例就不该探活、更不该起 skill-up"


def test_an_unreachable_gateway_requeues_before_starting(env):
    """网关不通时起 skill-up 只会让每个用例都耗满超时。"""
    _, fake, _, probe = env
    probe["failure"] = "模型网关不可达：ConnectError"
    task_id = _enqueue()
    _run(env)

    task = _task(task_id)
    assert task.state == str(TaskState.QUEUED)
    assert task.error == "模型网关不可达：ConnectError"
    assert fake.calls == []


def test_a_run_that_did_not_finish_is_retried(env):
    _, fake, _, _ = env
    fake.set(drop_run_finished=True, run_exit=1)
    task_id = _enqueue()
    _run(env)

    task = _task(task_id)
    assert task.state == str(TaskState.QUEUED)
    assert "没有正常收尾" in task.error
    assert _rows() == []


def test_the_same_content_is_reused_across_skill_ids(env):
    """同样的字节、同样的执行配置就是同一种结论，重跑只是花 token。"""
    _, fake, _, _ = env
    _enqueue("zip-upload-1")
    _run(env)
    _enqueue("zip-upload-2")
    _run(env)

    assert fake.commands() == ["validate", "run"], "第二次不该再跑"
    [first], [second] = _rows("zip-upload-1"), _rows("zip-upload-2")
    assert second.status == first.status
    assert second.report_html_uri == first.report_html_uri
    assert second.evaluated_at == first.evaluated_at, "评测确实是那时候跑的"


def test_force_reruns(env):
    _, fake, _, _ = env
    _enqueue()
    _run(env)
    _enqueue(force=True)
    _run(env)
    assert fake.commands() == ["validate", "run", "validate", "run"]
    assert len(_rows()) == 1, "同一身份覆盖写，不堆两行"


def test_a_result_with_unjudged_runs_is_not_reused(env):
    """和 Tier 1 "扫描没跑全不复用"同一个意思：一次抖动不该被固化。"""
    settings, fake, _, _ = env
    content_hash = compute_content_hash(_files(), name="ticket-formatter")
    with session_scope() as db:
        db.add(
            RuntimeResult(
                id=str(uuid.uuid4()),
                source="local",
                skill_id="elsewhere",
                content_hash=content_hash,
                runtime_fingerprint=RuntimeProfile.from_settings(settings).fingerprint,
                status="incomplete",
                passed=2,
                errored=1,
                cases=[],
                served_models=[],
                evaluated_at=datetime.now(tz=UTC),
            )
        )
    _enqueue()
    _run(env)
    assert fake.commands() == ["validate", "run"]


def test_a_different_model_is_a_different_result(env, monkeypatch):
    """换了模型指纹就变，旧结论不复用、也不被覆盖——两条都留着。"""
    _, fake, ready, probe = env
    _enqueue()
    _run(env)

    monkeypatch.setenv("SKILLPRISM_RUNTIME_MODEL", "doubao-seed")
    reset_settings()
    _enqueue()
    _run((get_settings(), fake, ready, probe))

    assert fake.commands() == ["validate", "run", "validate", "run"]
    assert sorted(r.model for r in _rows()) == ["deepseek-v4-flash", "doubao-seed"]


def test_a_bundle_task_is_refused(env):
    _, fake, _, _ = env
    task_id = _enqueue(bundle=True)
    _run(env)
    task = _task(task_id)
    assert task.state == str(TaskState.FAILED)
    assert "bundle" in task.error
    assert fake.calls == []


def test_the_probe_can_be_switched_off(env, monkeypatch):
    """沙箱里的 agent 连得上网关、worker 自己连不上时，探活只会把每个任务判死。"""
    _, fake, ready, probe = env
    probe["failure"] = "模型网关不可达：ConnectError"
    monkeypatch.setenv("SKILLPRISM_RUNTIME_PROBE_GATEWAY", "false")
    reset_settings()
    task_id = _enqueue()
    _run((get_settings(), fake, ready, probe))

    assert _task(task_id).state == str(TaskState.DONE)
    assert probe["calls"] == 0


def test_an_engine_that_is_not_the_pinned_one_writes_no_result(env, monkeypatch):
    """沙箱里的版本只有跑完才看得到。对不上还写结论，复用就会把它当成钉住
    那个版本评出来的。"""
    _, fake, ready, probe = env
    monkeypatch.setenv("SKILLPRISM_RUNTIME_ENGINE_VERSION", "2.1.300")
    reset_settings()
    task_id = _enqueue()
    _run((get_settings(), fake, ready, probe))

    task = _task(task_id)
    assert task.state == str(TaskState.FAILED)
    assert "2.1.284" in task.error and "2.1.300" in task.error
    assert _rows() == []


def test_following_the_image_records_the_version_it_found(env, monkeypatch):
    _, fake, _, probe = env
    for key, value in {
        "RUNTIME_ENVIRONMENT": "opensandbox",
        "RUNTIME_ENGINE_VERSION": "",
        "OPENSANDBOX_BASE_URL": "https://sandbox.internal",
        "OPENSANDBOX_API_KEY": "osb-test",
        "OPENSANDBOX_IMAGE": "registry.internal/claude:20260819",
    }.items():
        monkeypatch.setenv(f"SKILLPRISM_{key}", value)
    reset_settings()
    task_id = _enqueue()
    _run((get_settings(), fake, RuntimeReady(skillup_bin=str(fake.path), claude_bin=None), probe))

    assert _task(task_id).state == str(TaskState.DONE)
    [row] = _rows()
    assert row.engine_version == "2.1.284", "没钉版本时，结论里记的是沙箱里实际用的"


def test_a_sandbox_run_lands_its_own_result(env, monkeypatch):
    _, fake, _, probe = env
    _enqueue()
    _run(env)
    for key, value in {
        "RUNTIME_ENVIRONMENT": "opensandbox",
        "OPENSANDBOX_BASE_URL": "https://sandbox.internal",
        "OPENSANDBOX_API_KEY": "osb-test",
        "OPENSANDBOX_IMAGE": "registry.internal/claude:2.1.284",
    }.items():
        monkeypatch.setenv(f"SKILLPRISM_{key}", value)
    reset_settings()
    task_id = _enqueue(force=False)
    _run((get_settings(), fake, RuntimeReady(skillup_bin=str(fake.path), claude_bin=None), probe))

    assert _task(task_id).state == str(TaskState.DONE)
    assert fake.commands() == ["validate", "run", "validate", "run"], "换了运行环境不复用本机的结论"
    assert len(_rows()) == 2
    assert fake.calls[-1]["env"]["OPENSANDBOX_API_KEY"] == "osb-test"
