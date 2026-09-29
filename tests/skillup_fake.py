"""一个假的 skill-up 可执行文件，给执行层与 worker 的测试用。

它不跑任何 agent：``validate`` 按控制文件决定退出码，``run`` 把一份 fixture
（tests/fixtures/skillup/<场景>）原样拷到 ``--output-dir`` 与 ``--event-log``。
每次调用都把 argv、cwd 和**完整环境**记下来——子进程环境只给白名单是被测
行为之一，得能看见它到底拿到了什么。

控制与记录都走脚本旁边的文件，而不是环境变量：被测代码不让子进程继承环境，
测试想塞也塞不进去。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "skillup"

_SCRIPT = r'''#!{python}
import json, os, shutil, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
control = json.loads((here / "control.json").read_text())
with (here / "calls.jsonl").open("a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ)}}) + "\n")

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("skill-up version " + control.get("version", "0.12.0"))
    sys.exit(0)
if args[:1] == ["validate"]:
    sys.stderr.write(control.get("validate_stderr", ""))
    sys.exit(control.get("validate_exit", 0))
if args[:1] == ["run"]:
    time.sleep(control.get("run_sleep", 0))
    out = Path(args[args.index("--output-dir") + 1])
    events = Path(args[args.index("--event-log") + 1])
    scenario = Path(control["scenario"])
    shutil.copytree(scenario / "out", out, dirs_exist_ok=True)
    lines = (scenario / "events.jsonl").read_text().splitlines(keepends=True)
    if control.get("drop_run_finished"):
        lines = [l for l in lines if '"run_finished"' not in l]
    events.write_text("".join(lines))
    sys.exit(control.get("run_exit", 0))
sys.exit(2)
'''


class FakeSkillup:
    def __init__(self, directory: Path, **control) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.dir = directory
        self.path = directory / "skill-up"
        self.path.write_text(_SCRIPT.format(python=sys.executable))
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.control = {"scenario": str(FIXTURES / "passed")}
        self.set(**control)

    def set(self, **control) -> None:
        if "scenario" in control and not os.path.isabs(control["scenario"]):
            control["scenario"] = str(FIXTURES / control["scenario"])
        self.control.update(control)
        (self.dir / "control.json").write_text(json.dumps(self.control))

    @property
    def calls(self) -> list[dict]:
        log = self.dir / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def commands(self) -> list[str]:
        return [call["argv"][0] for call in self.calls]


def fake_claude(directory: Path, version: str = "2.1.284") -> Path:
    """一个只会回答 ``--version`` 的 claude，放在 ``directory`` 里当 PATH 用。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "claude"
    path.write_text(f"#!/bin/sh\necho '{version} (Claude Code)'\n")
    path.chmod(0o755)
    return path
