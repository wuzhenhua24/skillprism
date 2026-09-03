"""评测子进程拿到的环境。

背景是一次部署事故：出网受限的机器上 semgrep 的联网动作会拖住评测，而
``run_validate`` 只给子进程 PATH 和 HOME 两个键——systemd 的 EnvironmentFile
注入的任何开关都到不了扫描器那一层，部署者没有任何自救手段。

这组测试锁三件事：默认把 semgrep 的联网关掉、部署者能覆盖、以及凭据不会
被顺手带进子进程。
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
from pydantic import ValidationError

from skillprism.config import Settings, parse_scanner_env
from skillprism.runner import SCANNER_ENV_DEFAULTS, _subprocess_env


def test_semgrep_phone_home_is_off_by_default():
    """默认就关掉版本检查与 metrics，不指望部署者知道这个冷知识。"""
    env = _subprocess_env(Settings())
    assert env["SEMGREP_ENABLE_VERSION_CHECK"] == "0"
    assert env["SEMGREP_SEND_METRICS"] == "off"


def test_deployer_can_inject_scanner_env():
    """SKILLPRISM_SCANNER_ENV 是部署者唯一的注入口，必须真的透传。"""
    env = _subprocess_env(Settings(scanner_env="SEMGREP_VERSION_CHECK_TIMEOUT=1,FOO=bar"))
    assert env["SEMGREP_VERSION_CHECK_TIMEOUT"] == "1"
    assert env["FOO"] == "bar"


def test_deployer_overrides_win_over_defaults():
    env = _subprocess_env(Settings(scanner_env="SEMGREP_SEND_METRICS=on"))
    assert env["SEMGREP_SEND_METRICS"] == "on"


def test_credentials_are_not_inherited(monkeypatch):
    """不继承调用方环境，这是子进程只给两个键的初衷，不能因为加了注入口就丢掉。"""
    monkeypatch.setenv("COMPANY_API_KEY", "s3cret")
    monkeypatch.setenv("SKILLPRISM_ARK_API_KEY", "s3cret")

    env = _subprocess_env(Settings())

    assert "COMPANY_API_KEY" not in env
    assert "SKILLPRISM_ARK_API_KEY" not in env
    assert set(env) == {"PATH", "HOME", *SCANNER_ENV_DEFAULTS}


def test_malformed_scanner_env_fails_at_startup():
    """格式写错要当场报错。静默忽略等于把开关调没了还以为设上了。"""
    with pytest.raises(ValidationError, match="K=V"):
        Settings(scanner_env="SEMGREP_SEND_METRICS")


def test_scanner_env_parsing():
    assert parse_scanner_env("") == {}
    assert parse_scanner_env("  A=1 , B=2  ") == {"A": "1", "B": "2"}
    # 值里可以有 =，只按第一个分割
    assert parse_scanner_env("URL=https://x/?a=b") == {"URL": "https://x/?a=b"}


@pytest.mark.skipif(shutil.which("semgrep") is None, reason="本机没装 semgrep")
def test_semgrep_still_reads_these_env_vars(tmp_path):
    """让 semgrep 自己证明它认识这两个变量名。

    判据是"给非法值会不会被拒绝"：semgrep 会校验自己认识的环境变量，
    不认识的直接忽略。所以真变量 + 非法值 => 非零退出，编造的变量名 +
    同样的非法值 => 正常跑完。末尾那个控制组不是装饰，它保证这条断言
    不会因为"随便设个变量 semgrep 都报错"而空过。

    **不要改回去比对 --help 的输出。** 上一版就是那么写的，在开发机的
    semgrep 1.175（OCaml/cmdliner 前端，会列出环境变量）上通过，到部署机的
    1.176 上直接失败——那个前端根本不列环境变量。变量本身一直是好的，
    测试盯错了东西：CLI 的排版不是上游承诺过的契约，行为才是。

    变量名一旦被上游改掉，我们在 SCANNER_ENV_DEFAULTS 里设的开关会静默失效，
    出网受限的机器上评测重新开始挂起。这条测试就是为了别让它静默。
    """
    target = tmp_path / "target"
    target.mkdir()
    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - id: never-matches\n"
        "    pattern: $NEVER_MATCHES_ANYTHING\n"
        "    message: placeholder\n"
        "    languages: [python]\n"
        "    severity: INFO\n",
        encoding="utf-8",
    )

    def run_with(var: str) -> int:
        # 基础环境用我们自己那套，扫描期间不会有任何联网动作——
        # 这条测试在出网受限的机器上也必须跑得动。
        env = {**_subprocess_env(Settings()), var: "bogus~value"}
        return subprocess.run(
            ["semgrep", "scan", "--config", str(rules), str(target)],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        ).returncode

    for name in SCANNER_ENV_DEFAULTS:
        assert run_with(name) != 0, f"semgrep 不再校验 {name}，八成是上游改名了"

    assert run_with("SEMGREP_NOT_A_REAL_SETTING") == 0, (
        "编造的变量名也让 semgrep 失败，说明上面那几条断言证明不了任何事"
    )
