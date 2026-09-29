"""运行时评测的解释层：skill-up 的产物 → 结论。

fixture 是 POC 的真实输出（见 tests/fixtures/skillup/README.md）。前半截是契约
测试——锁定 runtime_adapter 读的那几个字段，skill-up 换版本改了格式时这里先红，
而不是线上解析出一份看起来正常的错结论；后半截钉住状态判法。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from skillprism.domain import EvaluationStatus
from skillprism.runtime_adapter import (
    RuntimeAdapterError,
    build_outcome,
    determine_status,
    read_events,
)

FIXTURES = Path(__file__).parent / "fixtures" / "skillup"


def _events(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _outcome(name: str, iterations: int = 1):
    events = read_events(FIXTURES / name / "events.jsonl")
    assert events is not None
    return build_outcome(events, FIXTURES / name / "out", iterations=iterations)


# ---- 契约：adapter 读的字段 ----


@pytest.mark.parametrize("name", ["passed", "iterations", "errored"])
def test_every_event_carries_the_protocol_version_we_understand(name):
    for event in _events(name):
        assert event["protocol_version"] == 1
        assert event["event_version"] == 1
        assert "event" in event and "payload" in event


@pytest.mark.parametrize("name", ["passed", "iterations", "errored"])
def test_case_completed_and_run_finished_carry_what_we_count(name):
    events = _events(name)
    cases = [e["payload"] for e in events if e["event"] == "case_completed"]
    finished = [e["payload"] for e in events if e["event"] == "run_finished"]
    assert cases and len(finished) == 1
    for payload in cases:
        for key in ("iteration", "case_id", "status", "configuration", "title"):
            assert key in payload, f"case_completed 缺 {key}"
        assert payload["status"] in {"PASS", "FAIL", "ERROR", "SKIP"}
    for key in ("passed", "failed", "errored", "skipped", "duration_ms"):
        assert key in finished[0], f"run_finished 缺 {key}"


def test_run_finished_status_is_not_a_verdict():
    """唯一的用例出错了，run_finished 仍说 COMPLETED。

    这就是判状态不看它（也不看退出码）的理由。哪天上游改成 FAILED 之类，
    这条会红——那时可以重新考虑，但计数判法依然成立。
    """
    finished = [e for e in _events("errored") if e["event"] == "run_finished"][0]["payload"]
    assert finished["errored"] == 1
    assert finished["status"] == "COMPLETED"


def test_result_json_carries_the_details_we_show():
    result = json.loads(
        (FIXTURES / "iterations" / "out" / "iteration-1" / "result.json").read_text()
    )
    assert isinstance(result["judge_tokens"], int)
    assert result["observed_configuration"]["version"]
    for case in result["case_results"]:
        for key in ("case_id", "title", "status", "configuration", "input_tokens", "output_tokens"):
            assert key in case, f"case_results 缺 {key}"
    failed = [c for c in result["case_results"] if c["status"] == "FAIL"]
    assert failed, "fixture 里应当有一个判错的用例"
    for assertion in failed[0]["grading"]["assertion_results"]:
        assert {"text", "passed", "evidence"} <= assertion.keys()


# ---- 解释 ----


def test_all_passing_run():
    outcome = _outcome("passed")
    assert outcome.status is EvaluationStatus.PASSED
    assert (outcome.passed, outcome.failed, outcome.errored, outcome.skipped) == (3, 0, 0, 0)
    assert [c.case_id for c in outcome.cases] == [
        "basic-format",
        "writes-file",
        "no-invented-steps",
    ]
    assert all(c.pass_rate == 1.0 and c.status is EvaluationStatus.PASSED for c in outcome.cases)
    assert outcome.cases[0].title == "Produces the fixed ticket layout"
    assert outcome.input_tokens > 0 and outcome.judge_tokens > 0
    assert outcome.engine_version == "2.1.284"


def test_served_model_comes_from_the_transcript():
    """请求的是别名，网关应答的是具体版本。skill-up 不报这个，只有会话记录里有。"""
    assert _outcome("passed").served_models == ["deepseek-v4-flash-ga-260731"]


def test_three_iterations_with_one_wrong_case():
    outcome = _outcome("iterations", iterations=3)
    assert outcome.status is EvaluationStatus.FAILED
    assert (outcome.passed, outcome.failed) == (9, 3)

    by_id = {c.case_id: c for c in outcome.cases}
    wrong = by_id["wrong-expectation"]
    assert wrong.status is EvaluationStatus.FAILED
    assert wrong.pass_rate == 0.0
    assert [r.iteration for r in wrong.runs] == [1, 2, 3]
    assert all("P0" in (r.reason or "") for r in wrong.runs), "失败原因要说出没满足哪条断言"

    for case_id in ("basic-format", "writes-file", "no-invented-steps"):
        assert by_id[case_id].status is EvaluationStatus.PASSED
        assert len(by_id[case_id].runs) == 3
        assert all(r.reason is None for r in by_id[case_id].runs)


def test_a_run_where_nothing_was_judged_is_an_error_with_a_reason():
    outcome = _outcome("errored")
    assert outcome.status is EvaluationStatus.ERROR
    assert "context deadline exceeded" in outcome.error
    assert outcome.cases[0].runs[0].status == "ERROR"


def test_missing_result_json_does_not_lose_the_verdict(tmp_path):
    """判定只认事件流；明细缺了，用例也不能凭空消失。"""
    shutil.copy(FIXTURES / "iterations" / "events.jsonl", tmp_path / "events.jsonl")
    events = read_events(tmp_path / "events.jsonl")
    outcome = build_outcome(events, tmp_path / "out", iterations=3)
    assert outcome.status is EvaluationStatus.FAILED
    assert {c.case_id for c in outcome.cases} == {
        "basic-format",
        "writes-file",
        "no-invented-steps",
        "wrong-expectation",
    }


def test_work_dir_paths_are_scrubbed_from_reasons(tmp_path):
    work = "/var/lib/skillprism/work/5f0c-task"
    out = tmp_path / "out" / "iteration-1"
    out.mkdir(parents=True)
    (out / "result.json").write_text(
        json.dumps(
            {
                "judge_tokens": 0,
                "case_results": [
                    {
                        "case_id": "c",
                        "status": "ERROR",
                        "configuration": "with_skill",
                        "error": f"script {work}/skill/skills/x/evals/check.sh exited 2",
                    }
                ],
            }
        )
    )
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            json.dumps({"protocol_version": 1, "event": e, "payload": p})
            for e, p in [
                ("case_completed", {"iteration": 1, "case_id": "c", "status": "ERROR"}),
                ("run_finished", {"passed": 0, "failed": 0, "errored": 1, "skipped": 0}),
            ]
        )
        + "\n"
    )
    outcome = build_outcome(
        read_events(events), tmp_path / "out", iterations=1, scrub_prefixes=[work]
    )
    reason = outcome.cases[0].runs[0].reason
    assert work not in reason
    assert "skill/skills/x/evals/check.sh" in reason


# ---- 事件流的边界 ----


def _write(path: Path, lines: list[dict], *, trailing: str = "") -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + trailing)
    return path


FINISHED = {
    "protocol_version": 1,
    "event": "run_finished",
    "payload": {"passed": 1, "failed": 0, "errored": 0, "skipped": 0},
}


def test_no_run_finished_means_no_verdict(tmp_path):
    """进程没正常收尾时手上的计数不完整，不能拿来判定。"""
    path = _write(
        tmp_path / "e.jsonl",
        [{"protocol_version": 1, "event": "run_started", "payload": {}}],
    )
    assert read_events(path) is None


def test_a_half_written_last_line_is_ignored(tmp_path):
    """进程被杀时最后一行可能写了一半。skill-up 的约定是只认以换行结尾的记录。"""
    path = _write(tmp_path / "e.jsonl", [FINISHED], trailing='{"protocol_version": 1, "ev')
    assert read_events(path).passed == 1


def test_an_unknown_protocol_version_is_refused(tmp_path):
    """猜着读一个改过的格式，给出的会是一份看起来正常的错结论。"""
    path = _write(tmp_path / "e.jsonl", [{**FINISHED, "protocol_version": 2}])
    with pytest.raises(RuntimeAdapterError):
        read_events(path)


def test_a_run_finished_without_counts_is_refused(tmp_path):
    path = _write(
        tmp_path / "e.jsonl", [{"protocol_version": 1, "event": "run_finished", "payload": {}}]
    )
    with pytest.raises(RuntimeAdapterError):
        read_events(path)


def test_baseline_runs_are_not_part_of_the_verdict(tmp_path):
    """without_skill 那次是参照，不是结论。"""
    path = _write(
        tmp_path / "e.jsonl",
        [
            {
                "protocol_version": 1,
                "event": "case_completed",
                "payload": {"iteration": 1, "case_id": "c", "status": "PASS", "configuration": "with_skill"},
            },
            {
                "protocol_version": 1,
                "event": "case_completed",
                "payload": {"iteration": 1, "case_id": "c", "status": "FAIL", "configuration": "without_skill"},
            },
            FINISHED,
        ],
    )
    assert read_events(path).runs == {(1, "c"): "PASS"}


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((3, 0, 0, 0), EvaluationStatus.PASSED),
        ((2, 1, 0, 0), EvaluationStatus.FAILED),
        # 有明确的判错就是 failed，出错的次数另外展示。
        ((1, 1, 1, 0), EvaluationStatus.FAILED),
        # 没被判定完不是通过。
        ((2, 0, 1, 0), EvaluationStatus.INCOMPLETE),
        ((2, 0, 0, 1), EvaluationStatus.INCOMPLETE),
        # 一次都没判定：skill 从未被评过。
        ((0, 0, 3, 0), EvaluationStatus.ERROR),
        ((0, 0, 0, 0), EvaluationStatus.ERROR),
    ],
)
def test_status_is_decided_by_counts(counts, expected):
    assert determine_status(*counts) is expected
