"""sandbox 队列的任务处理：运行时评测（Tier 3）。设计见 docs/runtime-evaluation.md。

一次任务的完整链路：
    取任务 → 拉内容 → 算 hash → 查复用（内容 + 运行时指纹）→ 物化
    → 收集并改写用例、生成 eval.yaml → 网关探活 → skill-up validate / run
    → 解释产物 → 报告入存储 → 结论入 runtime_result → 清理工作目录

取内容、算 hash、物化与 Tier 1 完全相同（同一份 content_hash），这样两层的
结论能按同一把钥匙对上（``EvaluationDTO.tiers.tier3``）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from skillprism import queue as task_queue
from skillprism.config import Settings
from skillprism.content import SkillContentSource
from skillprism.domain import ContentSource, EvaluationStatus
from skillprism.materialize import (
    MaterializeError,
    UnsafePathError,
    cleanup,
    compute_content_hash,
    materialize,
)
from skillprism.models import EvaluationTask, RuntimeResult
from skillprism.repository import (
    clone_runtime_result,
    find_reusable_runtime_result,
    save_runtime_result,
)
from skillprism.runtime_adapter import (
    RuntimeAdapterError,
    RuntimeOutcome,
    build_outcome,
    iteration_dirs,
    read_events,
    scrub_text,
)
from skillprism.skillup import (
    CaseError,
    RuntimeProfile,
    RuntimeReady,
    prepare_suite,
    probe_gateway,
    run_skillup,
)
from skillprism.storage import ReportStorage, iteration_file_name
from skillprism.worker import (
    _requeue,
    discard_leftovers,
    fetch_task_files,
    resolve_task_source,
    task_skill_name,
)

logger = logging.getLogger(__name__)


def storage_variant(fingerprint: str) -> str:
    """运行时报告在存储里的子目录。

    Tier 1 与 Tier 3 的报告都叫 ``report.html``，同一份内容下不分开放就会
    互相覆盖；同一份内容在两套执行配置下评过，也要分开放。
    """
    return f"runtime-{fingerprint.split(':', 1)[-1][:16]}"


def make_process_runtime_task(ready: RuntimeReady):
    """把预检结果绑进处理函数，交给 ``worker.run_once(process=...)``。"""

    def process(session, task, *, settings, content_sources, storage, **_):
        return process_runtime_task(
            session,
            task,
            settings=settings,
            content_sources=content_sources,
            storage=storage,
            ready=ready,
        )

    return process


def process_runtime_task(
    session,
    task: EvaluationTask,
    *,
    settings: Settings,
    content_sources: dict[ContentSource, SkillContentSource],
    storage: ReportStorage,
    ready: RuntimeReady,
) -> EvaluationStatus:
    """处理一个 tier3 任务。返回最终对外状态。"""
    work_dir = settings.work_root / task.id
    # 沙箱 worker 一次尝试动辄几十分钟，改完配置就重启，最容易留下残留。
    discard_leftovers(work_dir)

    resolved = resolve_task_source(session, task, content_sources)
    if resolved is None:
        return EvaluationStatus.ERROR
    source, content_source = resolved

    if task.bundle:
        # 提交侧已经拒了（422），这里兜底：一组 skill 的用例怎么组织、结论挂给谁
        # 还没设计，照单个 skill 跑会按错的形态解归档。
        task_queue.finish(session, task, error="运行时评测暂不支持 bundle")
        return EvaluationStatus.ERROR

    files = fetch_task_files(session, task, settings, content_source)
    if files is None:
        return EvaluationStatus.ERROR

    skill_name = task_skill_name(task)
    content_hash = compute_content_hash(files, name=skill_name)
    task.content_hash = content_hash

    profile = RuntimeProfile.from_settings(settings)
    fingerprint = profile.fingerprint

    if not task.force:
        reusable = find_reusable_runtime_result(session, content_hash, fingerprint)
        if reusable is not None:
            # 别的身份评过同样的内容就挂一份到本次的身份上，理由同 Tier 1
            # （worker.process_task）：复用刻意跨 skill_id 与来源。
            clone_runtime_result(
                session,
                reusable,
                source=source,
                skill_id=task.skill_id,
                skill_version=task.skill_version,
            )
            task_queue.finish(session, task)
            return EvaluationStatus(reusable.status)

    try:
        return _evaluate(
            session,
            task,
            settings=settings,
            source=source,
            storage=storage,
            ready=ready,
            profile=profile,
            files=files,
            skill_name=skill_name,
            content_hash=content_hash,
            work_dir=work_dir,
        )
    finally:
        # 含本次任务专用的 HOME：Claude Code 会在里面攒会话记录，
        # POC 里 12 次运行攒了 4.6MB。
        cleanup(work_dir)


def _evaluate(
    session,
    task: EvaluationTask,
    *,
    settings: Settings,
    source: ContentSource,
    storage: ReportStorage,
    ready: RuntimeReady,
    profile: RuntimeProfile,
    files,
    skill_name: str,
    content_hash: str,
    work_dir,
) -> EvaluationStatus:
    try:
        skill_root = materialize(files, work_dir / "skill", name=skill_name)
    except (UnsafePathError, MaterializeError) as exc:
        task_queue.finish(session, task, error=f"物化失败：{exc}")
        return EvaluationStatus.ERROR

    try:
        config_path, case_count = prepare_suite(skill_root, settings, profile)
    except CaseError as exc:
        # 作者的问题（含"没有用例"），重试多少次都一样。文案原样给出去：
        # 管理系统要能直接转给作者，"无运行时用例"的前缀还用来提示补用例。
        task_queue.finish(session, task, error=str(exc))
        return EvaluationStatus.ERROR

    probe_failure = probe_gateway(settings) if settings.runtime_probe_gateway else None
    if probe_failure is not None:
        _requeue(session, task, settings, probe_failure)
        return EvaluationStatus.ERROR

    run = run_skillup(settings, ready, config_path, work_dir, case_count=case_count)
    if run.invalid_cases is not None:
        task_queue.finish(session, task, error=f"用例无效：{run.invalid_cases}")
        return EvaluationStatus.ERROR
    if run.failure is not None:
        _requeue(session, task, settings, run.failure)
        return EvaluationStatus.ERROR

    scrub = [str(work_dir), str(work_dir.resolve())]
    try:
        events = read_events(run.events_path)
    except RuntimeAdapterError as exc:
        # 事件流不是我们认识的格式：多半是 skill-up 换了版本而契约测试没拦住。
        # 重试只会重复同样的结果。
        task_queue.finish(session, task, error=f"无法解释 skill-up 的输出：{exc}")
        return EvaluationStatus.ERROR
    if events is None:
        tail = (run.stderr or run.stdout).strip().splitlines()[-5:]
        _requeue(
            session,
            task,
            settings,
            f"skill-up 没有正常收尾（退出码 {run.exit_code}）：{' | '.join(tail)}",
        )
        return EvaluationStatus.ERROR

    outcome = build_outcome(
        events, run.out_dir, iterations=settings.runtime_iterations, scrub_prefixes=scrub
    )
    if outcome.status is EvaluationStatus.ERROR:
        # 一个用例都没被判定。最常见的原因是网关限流或抖动（探活之后才出的
        # 问题），值得重试；不写结论——界面上不该出现一个并不存在的判定。
        _requeue(session, task, settings, outcome.error or "全部用例都没被判定")
        return EvaluationStatus.ERROR

    if profile.engine_version and outcome.engine_version and outcome.engine_version != profile.engine_version:
        # 本机运行时预检已经核对过；沙箱里的版本只有跑完才看得到（skill-up 会按
        # engine.version 安装，但镜像里预装的另一个版本也可能被直接用上）。
        # 版本进指纹，对不上的结论不能写：复用会把它当成钉住那个版本的结论。
        # 重试也是同一个镜像，所以直接结束。
        task_queue.finish(
            session,
            task,
            error=(
                f"agent 实际的 Claude Code 版本是 {outcome.engine_version}，"
                f"配置钉的是 {profile.engine_version}（SKILLPRISM_RUNTIME_ENGINE_VERSION）"
            ),
        )
        return EvaluationStatus.ERROR

    uris = _store_reports(
        storage, run, content_hash, profile.fingerprint, settings.runtime_iterations, scrub
    )
    row = _to_row(
        task,
        source=source,
        content_hash=content_hash,
        profile=profile,
        outcome=outcome,
        uris=uris,
        # skill-up 探测到的版本。钉了版本时上面已经核对过；没钉（沙箱模式跟着
        # 镜像走）时它是唯一能说明用了哪个版本的记录。
        engine_version=outcome.engine_version or profile.engine_version or None,
    )
    save_runtime_result(session, row)
    task_queue.finish(session, task)
    return outcome.status


def _store_reports(
    storage: ReportStorage,
    run,
    content_hash: str,
    fingerprint: str,
    iterations: int,
    scrub_prefixes: list[str],
) -> dict[str, str | None]:
    """报告入存储。第一个迭代的 report.html / result.json 是结论指向的那份；
    迭代多于一次时，其余的按 :func:`storage.iteration_file_name` 带上序号放在
    同一目录，取的时候照同一规则推出来。事件流覆盖全部迭代。"""
    variant = storage_variant(fingerprint)
    uris: dict[str, str | None] = {"html": None, "json": None, "events": None}
    if run.events_path.is_file():
        # 事件流会经 /runtime-events 原样交给对接方。v1 的字段里没有路径，
        # 但去掉工作目录的成本很低，免得哪天 skill-up 往事件里加了出错信息
        # 就把服务器目录结构带出去——reason 也是这么处理的。
        scrubbed = run.events_path.with_name("events.scrubbed.jsonl")
        text = run.events_path.read_text(encoding="utf-8")
        scrubbed.write_text(scrub_text(text, scrub_prefixes), encoding="utf-8")
        uris["events"] = storage.put(content_hash, "events.jsonl", scrubbed, variant=variant)
    for number, directory in enumerate(iteration_dirs(run.out_dir, iterations), start=1):
        for name, key in (("report.html", "html"), ("result.json", "json")):
            path = directory / name
            if not path.is_file():
                continue
            uri = storage.put(
                content_hash, iteration_file_name(name, number), path, variant=variant
            )
            if number == 1:
                uris[key] = uri
    return uris


def _to_row(
    task: EvaluationTask,
    *,
    source: ContentSource,
    content_hash: str,
    profile: RuntimeProfile,
    outcome: RuntimeOutcome,
    uris: dict[str, str | None],
    engine_version: str | None,
) -> RuntimeResult:
    return RuntimeResult(
        id=str(uuid.uuid4()),
        source=str(source),
        skill_id=task.skill_id,
        skill_version=task.skill_version,
        content_hash=content_hash,
        runtime_fingerprint=profile.fingerprint,
        status=str(outcome.status),
        passed=outcome.passed,
        failed=outcome.failed,
        errored=outcome.errored,
        skipped=outcome.skipped,
        case_count=outcome.case_count,
        iterations=profile.iterations,
        skillup_version=profile.skillup_version,
        engine=profile.engine,
        engine_version=engine_version,
        model=profile.model,
        judge_model=profile.judge_model,
        served_models=outcome.served_models,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        judge_tokens=outcome.judge_tokens,
        duration_ms=outcome.duration_ms,
        cases=[case.model_dump(mode="json") for case in outcome.cases],
        report_json_uri=uris["json"],
        report_html_uri=uris["html"],
        events_uri=uris["events"],
        error=outcome.error,
        evaluated_at=datetime.now(tz=UTC),
    )
