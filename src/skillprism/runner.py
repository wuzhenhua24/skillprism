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
import os
import shutil
import subprocess
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


class PreflightError(RuntimeError):
    """运行环境不满足评测前提。"""


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
        )
        if proc.returncode == 0:
            report.version = proc.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass

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


def _locate_reports(out_dir: Path) -> tuple[Path | None, Path | None]:
    """在输出目录里找报告。文件名带时间戳，不能写死。"""
    json_candidates = sorted(p for p in out_dir.rglob("*.json") if not p.name.endswith(".sarif.json"))
    html_candidates = sorted(out_dir.rglob("*.html"))
    return (
        json_candidates[-1] if json_candidates else None,
        html_candidates[-1] if html_candidates else None,
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
    command = build_command(settings, skill_dir, out_dir)

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.eval_timeout_seconds,
            check=False,
            # 不继承调用方环境中的凭据；评测不需要任何公司密钥。
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
        )
    except subprocess.TimeoutExpired as exc:
        return RunOutcome(
            exit_code=EXIT_RUNTIME_ERROR,
            report=None,
            report_json_path=None,
            report_html_path=None,
            stderr=str(exc),
            timed_out=True,
            failure=f"评测超时（{settings.eval_timeout_seconds}s）",
        )
    except OSError as exc:
        return RunOutcome(
            exit_code=EXIT_CONFIG_ERROR,
            report=None,
            report_json_path=None,
            report_html_path=None,
            stderr=str(exc),
            failure=f"无法启动评测进程：{exc}",
        )

    json_path, html_path = _locate_reports(out_dir)
    report: dict[str, Any] | None = None
    failure: str | None = None

    if json_path is not None:
        try:
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            report = loaded if isinstance(loaded, dict) else None
            if report is None:
                failure = "JSON 报告不是对象"
        except (OSError, json.JSONDecodeError) as exc:
            failure = f"读取 JSON 报告失败：{exc}"
    else:
        failure = "未找到 JSON 报告"

    return RunOutcome(
        exit_code=proc.returncode,
        report=report,
        report_json_path=json_path,
        report_html_path=html_path,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        failure=failure,
    )
