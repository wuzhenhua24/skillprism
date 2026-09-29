"""运行时评测的执行层：用例怎么收、怎么改写，子进程拿到什么环境，指纹管住什么。

几条都是"错了不报错"的那一类：用例写成 .yml 会被静默跳过、环境变量会静默
覆盖 eval.yaml 里的模型、指纹少一维复用就会把另一种结论当成同一种。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from skillprism.config import Settings
from skillprism.skillup import (
    EVAL_CONFIG_NAME,
    NO_CASES_PREFIX,
    PROVIDER,
    CaseError,
    NoCasesError,
    RuntimePreflightError,
    RuntimeProfile,
    RuntimeReady,
    collect_cases,
    prepare_case,
    prepare_suite,
    preflight_runtime,
    run_skillup,
    run_timeout,
    subprocess_env,
)
from tests.skillup_fake import FakeSkillup, fake_claude

MOCK_SKILL = Path(__file__).parent / "fixtures" / "runtime_skill" / "ticket-formatter"


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "runtime_path": str(tmp_path / "bin") + ":/usr/bin:/bin",
        "runtime_engine_version": "2.1.284",
        "runtime_base_url": "https://gateway.internal/anthropic",
        "runtime_api_key": "k-test",
        "runtime_model": "deepseek-v4-flash",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.fixture
def skill(tmp_path) -> Path:
    root = tmp_path / "skill" / "skills" / "ticket-formatter"
    shutil.copytree(MOCK_SKILL, root)
    return root


# ---- 收用例 ----


def test_cases_are_collected_sorted_and_relative(skill):
    assert collect_cases(skill, max_cases=20) == [
        "evals/cases/basic-format.yaml",
        "evals/cases/no-invented-steps.yaml",
        "evals/cases/writes-file.yaml",
    ]


def test_no_cases_directory_is_its_own_error(skill):
    """管理系统靠这个前缀提示作者补用例，而不是报"评测失败"。"""
    shutil.rmtree(skill / "evals")
    with pytest.raises(NoCasesError) as exc:
        collect_cases(skill, max_cases=20)
    assert str(exc.value).startswith(NO_CASES_PREFIX)


def test_an_empty_cases_directory_has_no_cases(skill):
    for path in (skill / "evals" / "cases").iterdir():
        path.unlink()
    with pytest.raises(NoCasesError):
        collect_cases(skill, max_cases=20)


def test_yml_cases_are_an_error_not_silently_skipped(skill):
    """写了用例却没被跑，是作者最难发现的一种错。"""
    (skill / "evals" / "cases" / "extra.yml").write_text("input: {prompt: hi}\n")
    with pytest.raises(CaseError) as exc:
        collect_cases(skill, max_cases=20)
    assert "extra.yml" in str(exc.value)
    assert not isinstance(exc.value, NoCasesError)


def test_too_many_cases_is_refused_not_truncated(skill):
    """只跑前 N 个会给出一个覆盖不全、看起来却完整的结论。"""
    with pytest.raises(CaseError, match="超过上限"):
        collect_cases(skill, max_cases=2)


def test_the_authors_eval_yaml_is_not_a_case(skill):
    (skill / "evals" / "eval.yaml").write_text("engine: {name: codex}\n")
    assert len(collect_cases(skill, max_cases=20)) == 3


# ---- 改写用例 ----


def _profile(**overrides) -> RuntimeProfile:
    base = RuntimeProfile(
        skillup_version="0.12.0",
        engine_version="2.1.284",
        model="deepseek-v4-flash",
        judge_model="judge-model",
        iterations=1,
        case_timeout_seconds=300,
        max_turns=12,
        context_tokens=None,
    )
    return replace(base, **overrides)


def _write_case(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


def test_agent_judge_model_is_forced_to_the_platforms(tmp_path):
    """不写校验不过（POC 实测）；作者写了也不该生效——judge 模型由平台定。"""
    case = _write_case(
        tmp_path / "c.yaml",
        {"input": {"prompt": "x"}, "judge": {"type": "agent_judge", "model": "anthropic/opus", "criteria": ["a"]}},
    )
    prepare_case(case, _profile())
    assert yaml.safe_load(case.read_text())["judge"]["model"] == f"{PROVIDER}/judge-model"


def test_other_judges_are_left_alone(tmp_path):
    judge = {"type": "rule_based", "success": [{"exit_code": 0}]}
    case = _write_case(tmp_path / "c.yaml", {"input": {"prompt": "x"}, "judge": judge})
    prepare_case(case, _profile())
    assert yaml.safe_load(case.read_text())["judge"] == judge


def test_case_limits_are_capped_not_raised(tmp_path):
    case = _write_case(
        tmp_path / "c.yaml",
        {"input": {"prompt": "x"}, "constraints": {"timeout_seconds": 3600, "max_turns": 5}},
    )
    prepare_case(case, _profile())
    constraints = yaml.safe_load(case.read_text())["constraints"]
    assert constraints == {"timeout_seconds": 300, "max_turns": 5}


def test_real_mcp_in_a_case_is_refused(tmp_path):
    case = _write_case(
        tmp_path / "c.yaml",
        {"input": {"prompt": "x"}, "mcp": {"servers": [{"name": "gh", "mode": "real"}]}},
    )
    with pytest.raises(CaseError, match="mocked"):
        prepare_case(case, _profile())


@pytest.mark.parametrize("text", ["input: [unclosed\n", "- just\n- a list\n"])
def test_a_case_that_is_not_a_mapping_is_the_authors_error(tmp_path, text):
    case = tmp_path / "c.yaml"
    case.write_text(text)
    with pytest.raises(CaseError):
        prepare_case(case, _profile())


def test_the_suite_config_lives_under_evals_and_names_every_case(tmp_path, skill):
    """放在物化后 skill 的 evals/ 下：skill-up 向上找 SKILL.md 定根目录，作者
    按相对 skill 根写的 fixture 路径才原样可用（POC 实测，放在别处要改写路径）。"""
    config_path, count = prepare_suite(skill, _settings(tmp_path), _profile())
    assert config_path == skill / "evals" / EVAL_CONFIG_NAME
    assert count == 3

    config = yaml.safe_load(config_path.read_text())
    assert config["environment"] == {"type": "none"}
    assert config["skills"] == [{"source": "local_path", "path": "."}]
    assert config["engine"]["model"] == {"provider": PROVIDER, "name": "deepseek-v4-flash"}
    assert config["engine"]["version"] == "2.1.284"
    assert config["cases"]["files"] == collect_cases(skill, max_cases=20)
    assert "base_url" not in config_path.read_text(), "网关地址走环境变量，不落盘"

    judged = yaml.safe_load((skill / "evals/cases/no-invented-steps.yaml").read_text())
    assert judged["judge"]["model"] == f"{PROVIDER}/judge-model"


# ---- 子进程环境 ----


def test_the_subprocess_env_is_exactly_the_whitelist(tmp_path, monkeypatch):
    """继承下来的 ANTHROPIC_BASE_URL 会让调用静默改道（POC 所在的会话里就有一个）；
    GATEWAY_MODEL 会静默换模型。子进程一个都不该看到。"""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("GATEWAY_MODEL", "something-else")
    monkeypatch.setenv("SKILLPRISM_GITLAB_TOKEN", "company-secret")

    env = subprocess_env(_settings(tmp_path), tmp_path / "home")
    assert env == {
        "PATH": str(tmp_path / "bin") + ":/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "GATEWAY_BASE_URL": "https://gateway.internal/anthropic",
        "GATEWAY_API_KEY": "k-test",
    }


def test_context_window_and_extra_env_are_passed_when_configured(tmp_path):
    env = subprocess_env(
        _settings(tmp_path, runtime_context_tokens=128000, runtime_env="NO_PROXY=.internal"),
        tmp_path / "home",
    )
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "128000"
    assert env["NO_PROXY"] == ".internal"


@pytest.mark.parametrize(
    "value", ["GATEWAY_MODEL=x", "gateway_base_url=x", "PATH=/tmp", "HOME=/tmp", "CLAUDE_CODE_MAX_CONTEXT_TOKENS=1"]
)
def test_runtime_env_cannot_override_what_enters_the_fingerprint(tmp_path, value):
    with pytest.raises(ValidationError):
        _settings(tmp_path, runtime_env=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_base_url", "gateway.internal"),
        ("runtime_parallelism", 0),
        ("runtime_parallelism", 257),
        ("runtime_iterations", 0),
        ("runtime_max_cases", 0),
    ],
)
def test_bad_runtime_settings_fail_at_startup(tmp_path, field, value):
    with pytest.raises(ValidationError):
        _settings(tmp_path, **{field: value})


def test_judge_model_defaults_to_the_run_model(tmp_path):
    assert _settings(tmp_path).effective_judge_model == "deepseek-v4-flash"
    assert _settings(tmp_path, runtime_judge_model="j").effective_judge_model == "j"


# ---- 指纹 ----


@pytest.mark.parametrize(
    "change",
    [
        {"skillup_version": "0.13.0"},
        {"engine_version": "2.1.300"},
        {"model": "other"},
        {"judge_model": "other"},
        {"iterations": 3},
        {"case_timeout_seconds": 600},
        {"max_turns": 20},
        {"context_tokens": 128000},
    ],
)
def test_every_part_of_the_profile_changes_the_fingerprint(change):
    assert _profile().fingerprint != _profile(**change).fingerprint


def test_parallelism_address_and_key_do_not_enter_the_fingerprint(tmp_path):
    """只影响快慢或连到哪儿，不影响判定。换网关地址不该让所有结论失效。"""
    a = RuntimeProfile.from_settings(_settings(tmp_path))
    b = RuntimeProfile.from_settings(
        _settings(
            tmp_path,
            runtime_parallelism=8,
            runtime_base_url="https://other.internal",
            runtime_api_key="k-other",
        )
    )
    assert a.fingerprint == b.fingerprint


def test_run_timeout_scales_with_waves(tmp_path):
    settings = _settings(tmp_path, runtime_parallelism=2, runtime_case_timeout_seconds=100)
    assert run_timeout(settings, 3) == 2 * 100 + 120
    settings = _settings(tmp_path, runtime_iterations=3, runtime_parallelism=3)
    assert run_timeout(settings, 4) == 4 * 300 + 120


# ---- 调用 ----


@pytest.fixture
def ready(tmp_path) -> tuple[FakeSkillup, RuntimeReady]:
    fake = FakeSkillup(tmp_path / "fake")
    claude = fake_claude(tmp_path / "bin")
    return fake, RuntimeReady(skillup_bin=str(fake.path), claude_bin=str(claude))


def test_run_passes_the_whitelist_env_and_runs_outside_the_skill(tmp_path, skill, ready, monkeypatch):
    """cwd 不能是 skill 目录：skill-up 会读 $PWD/.skill-up.yaml，skill 里带一个
    就能注入环境变量。"""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    fake, runtime = ready
    settings = _settings(tmp_path)
    config_path, count = prepare_suite(skill, settings, _profile())
    work = tmp_path / "work"
    work.mkdir()

    run = run_skillup(settings, runtime, config_path, work, case_count=count)

    assert run.failure is None and run.invalid_cases is None
    assert fake.commands() == ["validate", "run"]
    for call in fake.calls:
        assert Path(call["cwd"]).resolve() == work.resolve()
        # 解释器自己会往环境里补几个变量（macOS 上的 __CF_USER_TEXT_ENCODING
        # 之类），只比我们给的那部分，并确认没有漏进来的东西。
        assert "ANTHROPIC_BASE_URL" not in call["env"]
        assert call["env"]["HOME"] == str(work / "home")
        assert call["env"]["GATEWAY_API_KEY"] == "k-test"
    run_argv = fake.calls[1]["argv"]
    assert run_argv[run_argv.index("--iteration") + 1] == "1"
    assert run.events_path.is_file()


def test_invalid_cases_are_reported_without_our_paths(tmp_path, skill, ready):
    fake, runtime = ready
    work = tmp_path / "work"
    work.mkdir()
    fake.set(
        validate_exit=1,
        validate_stderr=f"Error: validation failed: case x: {work}/skill/skills/t/evals/cases/x.yaml: bad\n",
    )
    settings = _settings(tmp_path)
    config_path, count = prepare_suite(skill, settings, _profile())

    run = run_skillup(settings, runtime, config_path, work, case_count=count)

    assert run.invalid_cases is not None
    assert str(work) not in run.invalid_cases
    assert "skill/skills/t/evals/cases/x.yaml: bad" in run.invalid_cases
    assert fake.commands() == ["validate"], "用例都不合法就不该开跑"


def test_a_hung_run_is_killed_and_marked_retryable(tmp_path, skill, ready, monkeypatch):
    fake, runtime = ready
    fake.set(run_sleep=5)
    monkeypatch.setattr("skillprism.skillup.run_timeout", lambda settings, count: 1)
    settings = _settings(tmp_path)
    config_path, count = prepare_suite(skill, settings, _profile())
    work = tmp_path / "work"
    work.mkdir()

    run = run_skillup(settings, runtime, config_path, work, case_count=count)

    assert run.timed_out and run.failure


# ---- 预检 ----


def test_preflight_passes_with_pinned_versions(tmp_path, ready):
    fake, _ = ready
    result = preflight_runtime(_settings(tmp_path, skillup_bin=str(fake.path)))
    assert result.claude_bin == str(tmp_path / "bin" / "claude")


def test_preflight_names_missing_settings(tmp_path):
    with pytest.raises(RuntimePreflightError, match="SKILLPRISM_RUNTIME_MODEL"):
        preflight_runtime(_settings(tmp_path, runtime_model=""))


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [({"skillup_version": "0.11.0"}, "skill-up 版本"), ({"runtime_engine_version": "2.1.1"}, "claude 版本")],
)
def test_preflight_refuses_version_drift(tmp_path, ready, overrides, needle):
    """版本进运行时指纹。机器上换了版本而配置没跟着改，复用会把新版本评出来
    的东西当成旧版本的结论。"""
    fake, _ = ready
    with pytest.raises(RuntimePreflightError, match=needle):
        preflight_runtime(_settings(tmp_path, skillup_bin=str(fake.path), **overrides))


def test_preflight_looks_for_claude_on_the_runtime_path_only(tmp_path, ready):
    """子进程只拿到 RUNTIME_PATH，worker 自己 PATH 上的 claude 用不上。"""
    fake, _ = ready
    with pytest.raises(RuntimePreflightError, match="找不到 claude"):
        preflight_runtime(
            _settings(tmp_path, skillup_bin=str(fake.path), runtime_path="/nonexistent")
        )


def test_fake_really_is_isolated_from_our_environment(tmp_path, ready, monkeypatch):
    """自检一下测试替身：它记下的环境确实是子进程的，而不是测试进程的。"""
    monkeypatch.setenv("SKILLPRISM_CANARY", "1")
    fake, runtime = ready
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "evals.yaml").write_text("x")
    run_skillup(_settings(tmp_path), runtime, tmp_path / "evals.yaml", work, case_count=1)
    assert "SKILLPRISM_CANARY" not in fake.calls[0]["env"]
    assert os.environ["SKILLPRISM_CANARY"] == "1"
