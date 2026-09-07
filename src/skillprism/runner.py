"""执行层：以子进程调用 skillevaluator CLI。

为什么是子进程而不是 in-process 调库：

1. 上游对外承诺稳定的是 CLI（退出码与 JSON schema 都有明确契约），
   Python 函数签名没有这层承诺——它的 __init__ 只导出 __version__。
2. Tier 1 要调 Semgrep / Gitleaks 等外部扫描器，遇到病态输入可能挂起或
   耗尽内存。子进程可以直接杀掉重来，in-process 故障会带走整个 worker。
3. 上游有 litellm<1.89、harbor==0.13.2 等硬 pin，独立安装才能避免依赖冲突，
   也才能同时保留新旧两个版本做升级灰度。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillprism.config import Settings
from skillprism.domain import (
    EXIT_CONFIG_ERROR,
    EXIT_RUNTIME_ERROR,
    REQUIRED_SCANNERS,
    RETRYABLE_EXIT_CODES,
)

logger = logging.getLogger(__name__)

#: 评测子进程的扫描器默认环境。
#:
#: semgrep 每次 scan 会向 semgrep.dev 查最新版本、并按 metrics 设置回传数据。
#: 对一个内网批量评测服务来说这两件事只有坏处：
#:
#: - 出网受限的机器上，这些请求会拖慢甚至挂住评测（DNS 解析不受 requests
#:   的 timeout 约束，丢包环境下能卡很久）；
#: - 把被扫代码的相关数据发到外部，本来就不该是默认行为。
#:
#: 所以在这里关掉，而不是让每个部署者自己去发现这个冷知识。
#: 部署者仍可用 SKILLPRISM_SCANNER_ENV 覆盖这两个值。
SCANNER_ENV_DEFAULTS = {
    "SEMGREP_ENABLE_VERSION_CHECK": "0",
    "SEMGREP_SEND_METRICS": "off",
}


class PreflightError(RuntimeError):
    """运行环境不满足评测前提。"""


def _subprocess_env(settings: Settings) -> dict[str, str]:
    """评测子进程的环境。

    刻意不继承调用方环境：评测不需要任何公司密钥，少一条泄漏路径。
    代价是 systemd 的 EnvironmentFile 也到不了扫描器那一层，所以扫描器
    需要的开关必须从这里显式给——SCANNER_ENV_DEFAULTS 管住已知的坑，
    SKILLPRISM_SCANNER_ENV 留给部署者补剩下的。
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(Path.home()),
        **SCANNER_ENV_DEFAULTS,
        # 放在最后：部署者显式写的值优先于我们的默认。
        **settings.scanner_env_pairs(),
    }


@dataclass
class RunOutcome:
    """一次 CLI 调用的原始产物。解释工作交给 adapter。"""

    exit_code: int
    report: dict[str, Any] | None
    report_json_path: Path | None
    report_html_path: Path | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    #: 进程层面的失败原因（超时、找不到报告等），与 skill 本身无关。
    failure: str | None = None

    @property
    def retryable(self) -> bool:
        if self.timed_out:
            return True
        return self.exit_code in RETRYABLE_EXIT_CODES


@dataclass
class PreflightReport:
    binary: str | None = None
    version: str | None = None
    missing_scanners: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.binary is not None and not self.missing_scanners


def preflight(settings: Settings) -> PreflightReport:
    """检查 CLI 与外部扫描器是否就位。

    扫描器缺失时上游会输出 overall_status=incomplete——安全扫描没跑全，
    却容易被下游当成通过。与其带病运行，不如让 worker 拒绝启动。
    """
    report = PreflightReport()

    binary = shutil.which(settings.skillevaluator_bin)
    if binary is None:
        return report
    report.binary = binary

    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            # 与真正跑评测时同一套环境，否则自检验的不是将要执行的那个环境。
            env=_subprocess_env(settings),
        )
        if proc.returncode == 0:
            report.version = proc.stdout.strip() or None
    except subprocess.TimeoutExpired:
        # 出网受限的机器上，扫描器的联网动作能把这里拖到超时。
        # 版本取不到不阻塞启动，但必须留下日志——否则 /healthz 里
        # 一个没来由的 version: null 无从查起。
        logger.warning("skillevaluator --version 30 秒未返回，版本未知；机器出网是否受限？")
    except OSError as exc:
        logger.warning("skillevaluator --version 执行失败：%s", exc)

    report.missing_scanners = [name for name in REQUIRED_SCANNERS if shutil.which(name) is None]
    return report


def require_ready(settings: Settings) -> PreflightReport:
    """启动自检。不通过就抛错，不要带病运行。"""
    report = preflight(settings)
    if report.binary is None:
        raise PreflightError(
            f"找不到 skillevaluator 可执行文件：{settings.skillevaluator_bin}。"
            "请独立安装（uv tool install），不要装进本服务的 venv。"
        )
    if report.missing_scanners and settings.require_scanners:
        raise PreflightError(
            f"缺少外部扫描器：{', '.join(report.missing_scanners)}。"
            "缺失会让 Tier 1 产出 incomplete 结果。"
            "装齐后再启动，或在明确接受不完整结论时设 SKILLPRISM_REQUIRE_SCANNERS=false。"
        )
    return report


def build_command(settings: Settings, skill_dir: Path, out_dir: Path) -> list[str]:
    """构造 Tier 1 的评测命令。

    用 --policy 而非 --profile：--profile 只能选 skillevaluator 包内自带的
    YAML，指不到外部文件；--policy 接受任意路径，overlay 在基础 profile 之上。
    """
    return [
        settings.skillevaluator_bin,
        "validate",
        str(skill_dir),
        "--policy",
        str(settings.policy_file),
        "--no-dedup",  # Tier 2 不在 M1 范围内
        "-r",
        "json,html",
        "-o",
        str(out_dir),
    ]


def _run_cli(
    settings: Settings, target: Path, out_dir: Path
) -> tuple[subprocess.CompletedProcess | None, int, str | None, bool]:
    """跑一次 CLI。返回 ``(proc, exit_code, failure, timed_out)``。

    ``proc`` 为 None 表示进程层面就没跑起来（超时或起不来），此时没有任何
    报告可读，``failure`` 说明原因。
    """
    command = build_command(settings, target, out_dir)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.eval_timeout_seconds,
            check=False,
            env=_subprocess_env(settings),
        )
    except subprocess.TimeoutExpired as exc:
        return None, EXIT_RUNTIME_ERROR, f"评测超时（{settings.eval_timeout_seconds}s）：{exc}", True
    except OSError as exc:
        return None, EXIT_CONFIG_ERROR, f"无法启动评测进程：{exc}", False
    return proc, proc.returncode, None, False


def _load_report(json_path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """读一份 JSON 报告，返回 ``(报告, 失败原因)``。"""
    if json_path is None:
        return None, "未找到 JSON 报告"
    try:
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"读取 JSON 报告失败：{exc}"
    if not isinstance(loaded, dict):
        return None, "JSON 报告不是对象"
    return loaded, None


def _locate_reports(out_dir: Path) -> tuple[Path | None, Path | None]:
    """在输出目录里找报告。文件名带时间戳，不能写死。"""
    json_candidates = sorted(p for p in out_dir.rglob("*.json") if not p.name.endswith(".sarif.json"))
    html_candidates = sorted(out_dir.rglob("*.html"))
    return (
        json_candidates[-1] if json_candidates else None,
        html_candidates[-1] if html_candidates else None,
    )


@dataclass
class CatalogOutcome:
    """一次 catalog 调用的产物：整体进程状态 + 每个成员一份报告。

    退出码是整个 catalog 的（任一成员失败即非零），所以它**不能**用来判断
    单个成员的结论——那要看各自报告里的 overall_status。这里只用它区分
    "进程层面的故障"（超时、起不来）和"跑完了，有成员没通过"。
    """

    members: dict[str, RunOutcome]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    failure: str | None = None

    @property
    def retryable(self) -> bool:
        if self.timed_out:
            return True
        return self.exit_code in RETRYABLE_EXIT_CODES


def run_catalog(
    settings: Settings,
    catalog_root: Path,
    out_dir: Path,
    members: Sequence[str],
) -> CatalogOutcome:
    """对一组 skill 跑一次评测。

    命令和单 skill 完全一样，只是目标指向父目录：SkillEvaluator 见到一个
    "自身没有 SKILL.md、但含 ``*/SKILL.md``"的目录就按 catalog 逐个评，
    每个成员一份独立报告落在 ``<out_dir>/<成员>/``。不需要额外的开关。

    成员列表由调用方给，不去扫 ``out_dir``：少了谁必须能看出来。按输出目录
    反推的话，一个成员崩了只会表现成"结果里没有它"，而不是一条错误。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    proc, exit_code, failure, timed_out = _run_cli(settings, catalog_root, out_dir)
    if proc is None:
        return CatalogOutcome(
            members={},
            exit_code=exit_code,
            stderr=failure or "",
            timed_out=timed_out,
            failure=failure,
        )

    outcomes: dict[str, RunOutcome] = {}
    for member in members:
        member_dir = out_dir / member
        json_path, html_path = _locate_reports(member_dir) if member_dir.is_dir() else (None, None)
        report, failure = _load_report(json_path)
        outcomes[member] = RunOutcome(
            exit_code=exit_code,
            report=report,
            report_json_path=json_path,
            report_html_path=html_path,
            failure=failure,
        )

    return CatalogOutcome(
        members=outcomes,
        exit_code=exit_code,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def policy_file_hash(settings: Settings) -> str:
    """当前策略文件的指纹，用于判断既有结论还能不能复用。

    报告里的 ``policy.digest`` 是上游算的，只有跑完评测才拿得到，
    没法用来决定"要不要跑"。所以这里自己算一份。

    ``--policy`` 是 overlay 在评测器包内的基础 profile 之上的，
    基础 profile 随评测器版本走，所以 (evaluator_version, 本指纹)
    合起来才刻画了实际生效的策略。

    读不到文件时返回空串——宁可不复用去重跑一遍，也不要拿一个
    含义不明的指纹去匹配。
    """
    try:
        data = Path(settings.policy_file).read_bytes()
    except OSError:
        return ""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def run_validate(settings: Settings, skill_dir: Path, out_dir: Path) -> RunOutcome:
    """跑一次 Tier 1 评测。任何进程层面的异常都收敛成 RunOutcome，不外抛。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    proc, exit_code, failure, timed_out = _run_cli(settings, skill_dir, out_dir)
    if proc is None:
        return RunOutcome(
            exit_code=exit_code,
            report=None,
            report_json_path=None,
            report_html_path=None,
            stderr=failure or "",
            timed_out=timed_out,
            failure=failure,
        )

    json_path, html_path = _locate_reports(out_dir)
    report, failure = _load_report(json_path)

    return RunOutcome(
        exit_code=exit_code,
        report=report,
        report_json_path=json_path,
        report_html_path=html_path,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        failure=failure,
    )
