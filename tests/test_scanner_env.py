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
def test_env_var_names_match_installed_semgrep():
    """变量名拼错就是白设，让 semgrep 自己确认这两个名字。

    ``--enable-version-check/--disable-version-check`` 绑的是
    SEMGREP_ENABLE_VERSION_CHECK；名字一旦上游改了，这条会失败。
    """
    help_text = subprocess.run(
        ["semgrep", "scan", "--help"], capture_output=True, text=True, timeout=120, check=False
    ).stdout

    for name in SCANNER_ENV_DEFAULTS:
        assert name in help_text, f"semgrep 不认识 {name}，检查拼写或上游是否改名"
