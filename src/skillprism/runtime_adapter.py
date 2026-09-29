"""skill-up 的产物 → 本服务的运行时结论。**唯一了解 skill-up 输出格式的模块。**

读三样东西，稳定性各不相同，所以各自只拿该拿的：

- ``--event-log`` 的事件流：带版本号（``schemas/evalevent/v1``，有 JSON Schema），
  **判状态只用它**——运行次数与每次运行的 PASS/FAIL/ERROR/SKIP。
- 每个迭代目录下的 ``result.json``：没有稳定性承诺（它是 skill-up
  ``internal/report.Input`` 的序列化，项目在 0.x），只取标题、失败原因、
  token 这类展示用的明细。缺了不影响判定。
- transcript：Claude Code 自己的格式，只用来尽力而为地取实际应答的模型名。

退出码与 ``run_finished.status`` 都**不是**结论：退出码只有 0/1，失败和出错
不分；用例全部出错时 ``run_finished.status`` 仍是 ``COMPLETED``（POC 实测）。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillprism.domain import EvaluationStatus
from skillprism.schemas import RuntimeCaseResult, RuntimeCaseRun

logger = logging.getLogger(__name__)

#: 我们认识的事件协议版本。不认识就不解释——猜着读一个改过的格式，
#: 给出的会是一份看起来正常的错结论。
EVENT_PROTOCOL_VERSION = 1

#: 只看有 skill 的那一次运行。本期不开 --baseline，但万一开了，
#: without_skill 那次是参照，不是结论（skill-up 自己算通过率时也这么排除）。
_WITH_SKILL = "with_skill"

#: 读 transcript 取模型名时每个文件最多读多少字节。它只是展示信息，
#: 不值得为一个几十 MB 的会话记录把内存吃满。
_TRANSCRIPT_READ_LIMIT = 8 * 1024 * 1024


class RuntimeAdapterError(ValueError):
    """产物无法按已知契约解释。"""


@dataclass
class EventSummary:
    """事件流里判定需要的全部信息。"""

    passed: int
    failed: int
    errored: int
    skipped: int
    duration_ms: int
    #: (iteration, case_id) → PASS / FAIL / ERROR / SKIP
    runs: dict[tuple[int, str], str]
    titles: dict[str, str]


def read_events(path: Path) -> EventSummary | None:
    """解析事件流。没有 ``run_finished`` 时返回 None——进程没正常收尾，
    手上的计数不完整，不能拿来判定。

    只读以换行结尾的记录：skill-up 的约定是半行不算数（进程被杀时最后一行
    可能写了一半）。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None

    runs: dict[tuple[int, str], str] = {}
    titles: dict[str, str] = {}
    finished: dict[str, int] | None = None
    for line in raw.split("\n")[:-1]:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeAdapterError(f"事件流里有一行不是 JSON：{exc}") from exc
        if event.get("protocol_version") != EVENT_PROTOCOL_VERSION:
            raise RuntimeAdapterError(
                f"事件协议版本是 {event.get('protocol_version')!r}，"
                f"本服务只认 {EVENT_PROTOCOL_VERSION}"
            )
        payload = event.get("payload") or {}
        kind = event.get("event")
        try:
            if kind == "case_completed":
                if payload.get("configuration", _WITH_SKILL) != _WITH_SKILL:
                    continue
                key = (int(payload["iteration"]), str(payload["case_id"]))
                runs[key] = str(payload["status"])
                titles.setdefault(key[1], str(payload.get("title") or ""))
            elif kind == "run_finished":
                finished = {
                    name: int(payload[name])
                    for name in ("passed", "failed", "errored", "skipped")
                }
                finished["duration_ms"] = int(payload.get("duration_ms") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeAdapterError(f"{kind} 事件缺字段或字段类型不对：{exc!r}") from exc

    if finished is None:
        return None
    return EventSummary(**finished, runs=runs, titles=titles)


def determine_status(passed: int, failed: int, errored: int, skipped: int) -> EvaluationStatus:
    """按运行次数定状态。整体与单个用例用同一个判法。

    - 有 FAIL：failed。出错的次数照样展示，但一个明确的"没做对"已经是结论。
    - 没有 FAIL、有运行没被判定：有通过的是 incomplete——**不是通过**，和
      Tier 1 "扫描没跑全不发徽章"同一条立场；一次都没通过就是 error，
      skill 从未被判定。
    - 全部 PASS：passed。一次都没跑（不该发生）也是 error。
    """
    if failed:
        return EvaluationStatus.FAILED
    if errored or skipped:
        return EvaluationStatus.INCOMPLETE if passed else EvaluationStatus.ERROR
    if passed:
        return EvaluationStatus.PASSED
    return EvaluationStatus.ERROR


@dataclass
class RuntimeOutcome:
    status: EvaluationStatus
    passed: int
    failed: int
    errored: int
    skipped: int
    cases: list[RuntimeCaseResult]
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    judge_tokens: int = 0
    served_models: list[str] = field(default_factory=list)
    #: skill-up 探测到的 engine 版本（result.json 的 observed_configuration）。
    engine_version: str | None = None
    #: status 为 ERROR 时的原因摘要。
    error: str | None = None

    @property
    def case_count(self) -> int:
        return len(self.cases)


def iteration_dirs(out_dir: Path, iterations: int) -> list[Path]:
    """``--iteration N`` 写出 ``iteration-1`` … ``iteration-N``。"""
    return [out_dir / f"iteration-{n}" for n in range(1, iterations + 1)]


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def scrub_text(text: str, prefixes: list[str]) -> str:
    for prefix in prefixes:
        text = text.replace(prefix + "/", "").replace(prefix, "")
    return text


def _failure_reason(case: dict[str, Any]) -> str | None:
    """一次没通过的运行，为什么没通过。

    FAIL 走 grading 里没满足的断言（``text`` 是断言、``evidence`` 是依据）；
    ERROR / SKIP 走 skill-up 给的原因。两边都没有就是 None，不编。
    """
    grading = case.get("grading") or {}
    status = case.get("status")
    if status == "FAIL":
        parts = []
        for item in grading.get("assertion_results") or []:
            if item.get("passed"):
                continue
            text, evidence = item.get("text") or "", item.get("evidence") or ""
            parts.append(f"{text}：{evidence}" if evidence and evidence != text else text)
        return "；".join(p for p in parts if p) or None
    return (
        case.get("error")
        or grading.get("error_reason")
        or grading.get("skip_reason")
        or None
    )


def served_models(out_dir: Path) -> list[str]:
    """从 transcript 里取实际应答的模型名。尽力而为：取不到就是空列表。

    请求的是别名（``deepseek-v4-flash``），网关应答的是具体版本
    （``deepseek-v4-flash-ga-260731``）。skill-up 的 ``observed_model`` 不报这个，
    只有 Claude Code 的会话记录里有（``type: assistant`` 行的 ``message.model``）。
    只看被测 agent 的会话（``outputs/agent/run``），judge 的不算。
    """
    found: set[str] = set()
    for path in out_dir.glob(f"iteration-*/*/{_WITH_SKILL}/outputs/agent/run/*.jsonl"):
        try:
            with path.open("rb") as handle:
                chunk = handle.read(_TRANSCRIPT_READ_LIMIT)
        except OSError:
            continue
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if '"model"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") if isinstance(record, dict) else None
            if record.get("type") == "assistant" and isinstance(message, dict):
                model = message.get("model")
                if isinstance(model, str) and model:
                    found.add(model)
    return sorted(found)


def build_outcome(
    events: EventSummary,
    out_dir: Path,
    *,
    iterations: int,
    scrub_prefixes: list[str] | None = None,
) -> RuntimeOutcome:
    """把一次跑完的产物翻成结论。

    ``scrub_prefixes`` 是要从失败原因里抹掉的路径前缀（工作目录）：里面有任务
    UUID 与服务器路径，对看结果的人没有意义，也不该给出去。
    """
    prefixes = scrub_prefixes or []
    details: dict[tuple[int, str], dict[str, Any]] = {}
    order: list[str] = []
    input_tokens = output_tokens = judge_tokens = 0
    engine_version: str | None = None

    for number, directory in enumerate(iteration_dirs(out_dir, iterations), start=1):
        result = _load_json(directory / "result.json")
        if result is None:
            continue
        judge_tokens += int(result.get("judge_tokens") or 0)
        observed = (result.get("observed_configuration") or {}).get("version")
        if engine_version is None and isinstance(observed, str) and observed:
            engine_version = observed
        for case in result.get("case_results") or []:
            if case.get("configuration", _WITH_SKILL) != _WITH_SKILL:
                continue
            case_id = str(case.get("case_id"))
            details[(number, case_id)] = case
            if case_id not in order:
                order.append(case_id)
            input_tokens += int(case.get("input_tokens") or 0)
            output_tokens += int(case.get("output_tokens") or 0)

    # 用例顺序以 result.json 为准（它按用例文件顺序写），事件里有而 result.json
    # 缺的用例补在后面——判定只认事件，明细缺了不能让用例凭空消失。
    for _, case_id in sorted(events.runs):
        if case_id not in order:
            order.append(case_id)

    per_case: dict[str, list[RuntimeCaseRun]] = defaultdict(list)
    titles: dict[str, str] = dict(events.titles)
    for (number, case_id), status in sorted(events.runs.items()):
        detail = details.get((number, case_id), {})
        if detail.get("title"):
            titles[case_id] = str(detail["title"])
        reason = None if status == "PASS" else _failure_reason({**detail, "status": status})
        per_case[case_id].append(
            RuntimeCaseRun(
                iteration=number,
                status=status,
                reason=scrub_text(reason, prefixes) if reason else None,
            )
        )

    cases: list[RuntimeCaseResult] = []
    for case_id in order:
        runs = per_case.get(case_id, [])
        counts = {s: sum(1 for r in runs if r.status == s) for s in ("PASS", "FAIL", "ERROR", "SKIP")}
        cases.append(
            RuntimeCaseResult(
                case_id=case_id,
                title=titles.get(case_id, ""),
                status=determine_status(
                    counts["PASS"], counts["FAIL"], counts["ERROR"], counts["SKIP"]
                ),
                pass_rate=counts["PASS"] / len(runs) if runs else 0.0,
                runs=runs,
            )
        )

    status = determine_status(events.passed, events.failed, events.errored, events.skipped)
    error = None
    if status is EvaluationStatus.ERROR:
        reasons = [r.reason for c in cases for r in c.runs if r.reason]
        error = "全部用例都没被判定" + (f"：{reasons[0]}" if reasons else "")

    return RuntimeOutcome(
        status=status,
        passed=events.passed,
        failed=events.failed,
        errored=events.errored,
        skipped=events.skipped,
        cases=cases,
        duration_ms=events.duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        judge_tokens=judge_tokens,
        served_models=served_models(out_dir),
        engine_version=engine_version,
        error=error,
    )
