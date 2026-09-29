"""运行时评测的执行层：用例怎么收、怎么改写，子进程拿到什么环境，指纹管住什么。

几条都是"错了不报错"的那一类：用例写成 .yml 会被静默跳过、环境变量会静默
覆盖 eval.yaml 里的模型、指纹少一维复用就会把另一种结论当成同一种。
"""

from __future__ import annotations

import hashlib
import json
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
    environment_config,
    prepare_case,
    prepare_suite,
    preflight_runtime,
    run_skillup,
    run_timeout,
    sandbox_ttl,
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
    # 迭代逐轮串行：4 个用例、并发 3，每轮 2 波，3 轮就是 6 波，不是 ceil(12/3)=4。
    settings = _settings(tmp_path, runtime_iterations=3, runtime_parallelism=3)
    assert run_timeout(settings, 4) == 6 * 300 + 120


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


# ---- OpenSandbox ----


def _sandbox(tmp_path: Path, **overrides) -> Settings:
    values = {
        "runtime_environment": "opensandbox",
        "runtime_path": "",
        "opensandbox_base_url": "https://sandbox.internal/",
        "opensandbox_api_key": "osb-test",
        "opensandbox_image": "registry.internal/skill-up/claude:2.1.284",
        **overrides,
    }
    return _settings(tmp_path, **values)


def test_the_sandbox_section_is_generated_from_settings(tmp_path, skill):
    settings = _sandbox(
        tmp_path,
        opensandbox_extensions='{"profile":"ci"}',
        opensandbox_use_server_proxy=True,
        opensandbox_workspace="/home/agent/ws",
        opensandbox_entrypoint=["tail", "-f", "/dev/null"],
        opensandbox_max_sandboxes=3,
        opensandbox_request_timeout_seconds=900,
        opensandbox_env="NPM_CONFIG_REGISTRY=https://npm.internal",
        runtime_context_tokens=128000,
    )
    config_path, _ = prepare_suite(skill, settings, RuntimeProfile.from_settings(settings))
    environment = yaml.safe_load(config_path.read_text())["environment"]
    assert environment == {
        "type": "opensandbox",
        "image": "registry.internal/skill-up/claude:2.1.284",
        "ready_timeout_seconds": 120,
        "sandbox_timeout_seconds": 120 + 2 * 300 + 300,
        "entrypoint": ["tail", "-f", "/dev/null"],
        "workspace_mount": "/home/agent/ws",
        "use_server_proxy": True,
        # 上下文窗口只能从这里进沙箱：worker 子进程的环境过不去。
        "env": {
            "NPM_CONFIG_REGISTRY": "https://npm.internal",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "128000",
        },
        "kwargs": {"extensions": '{"profile":"ci"}', "request_timeout_seconds": "900"},
    }
    config = yaml.safe_load(config_path.read_text())
    assert config["cases"]["parallelism"] == 3, "沙箱模式下并发就是沙箱数"
    text = config_path.read_text()
    assert "osb-test" not in text and "sandbox.internal" not in text, "服务地址与 key 走环境变量"


def test_allow_declared_always_lets_the_agent_reach_the_gateway(tmp_path):
    """放行名单里没有网关，每个用例都会耗满超时才判 ERROR。"""
    environment = environment_config(
        _sandbox(
            tmp_path,
            opensandbox_network_policy="allow_declared",
            opensandbox_allowed_egress="npm.internal, gateway.internal",
        )
    )
    assert environment["network_policy"] == "allow_declared"
    assert environment["allowed_egress"] == ["gateway.internal", "npm.internal"]


def test_the_sandbox_subprocess_env_carries_the_connection_not_the_context(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSANDBOX_API_KEY", "someone-elses")
    env = subprocess_env(
        _sandbox(tmp_path, runtime_context_tokens=128000, runtime_env="HTTPS_PROXY=http://p:3128"),
        tmp_path / "home",
    )
    assert env == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "GATEWAY_BASE_URL": "https://gateway.internal/anthropic",
        "GATEWAY_API_KEY": "k-test",
        "OPENSANDBOX_BASE_URL": "https://sandbox.internal",
        "OPENSANDBOX_API_KEY": "osb-test",
        "HTTPS_PROXY": "http://p:3128",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_env", "OPENSANDBOX_API_KEY=x"),
        ("runtime_env", "opensandbox_base_url=x"),
        ("opensandbox_env", "ANTHROPIC_MODEL=x"),
        ("opensandbox_env", "GATEWAY_API_KEY=x"),
        ("opensandbox_env", "CLAUDE_CODE_MAX_CONTEXT_TOKENS=1"),
        ("opensandbox_extensions", "profile=ci"),
        ("opensandbox_extensions", '["ci"]'),
        ("opensandbox_extensions", '{"cpu": 2}'),
        ("opensandbox_base_url", "sandbox.internal"),
        ("opensandbox_network_policy", "allow_all"),
        ("opensandbox_sandbox_timeout_seconds", 300),
        ("opensandbox_max_sandboxes", 0),
        ("opensandbox_entrypoint", ["tail", " "]),
        ("runtime_environment", "docker"),
    ],
)
def test_bad_sandbox_settings_fail_at_startup(tmp_path, field, value):
    with pytest.raises(ValidationError):
        _sandbox(tmp_path, **{field: value})


def test_the_sandbox_ttl_covers_a_case_and_its_judge(tmp_path):
    assert sandbox_ttl(_sandbox(tmp_path, runtime_case_timeout_seconds=100)) == 120 + 200 + 300
    assert sandbox_ttl(_sandbox(tmp_path, opensandbox_sandbox_timeout_seconds=3600)) == 3600


def test_in_the_sandbox_a_wave_may_take_as_long_as_the_sandbox_lives(tmp_path):
    settings = _sandbox(
        tmp_path,
        runtime_parallelism=8,
        opensandbox_max_sandboxes=2,
        opensandbox_sandbox_timeout_seconds=1000,
    )
    assert run_timeout(settings, 3) == 2 * 1000 + 120, "波数按沙箱数算，不按本机并发"


def test_the_entrypoint_is_read_as_a_json_array(tmp_path, monkeypatch):
    """env 里只能写字符串；写成 JSON 数组才能原样还原那三项。"""
    monkeypatch.setenv("SKILLPRISM_OPENSANDBOX_ENTRYPOINT", '["tail","-f","/dev/null"]')
    monkeypatch.setenv("SKILLPRISM_OPENSANDBOX_MAX_SANDBOXES", "4")
    settings = Settings(_env_file=None)
    assert settings.opensandbox_entrypoint == ["tail", "-f", "/dev/null"]
    assert settings.opensandbox_max_sandboxes == 4


@pytest.mark.parametrize(
    "change",
    [
        {"runtime_environment": "opensandbox"},
        {"opensandbox_image": "registry.internal/other:1"},
        {"opensandbox_network_policy": "deny_all"},
        {"opensandbox_allowed_egress": "npm.internal"},
    ],
)
def test_where_the_agent_runs_changes_the_fingerprint(tmp_path, change):
    base = _sandbox(tmp_path) if "runtime_environment" not in change else _settings(tmp_path)
    changed = _sandbox(tmp_path, **change)
    assert RuntimeProfile.from_settings(base).fingerprint != RuntimeProfile.from_settings(changed).fingerprint


def test_sandbox_connection_details_do_not_enter_the_fingerprint(tmp_path):
    a = RuntimeProfile.from_settings(_sandbox(tmp_path))
    b = RuntimeProfile.from_settings(
        _sandbox(
            tmp_path,
            opensandbox_base_url="https://other.internal",
            opensandbox_api_key="osb-other",
            opensandbox_extensions='{"profile":"big"}',
            opensandbox_use_server_proxy=True,
            opensandbox_ready_timeout_seconds=300,
            opensandbox_max_sandboxes=5,
            opensandbox_entrypoint=["sleep", "infinity"],
        )
    )
    assert a.fingerprint == b.fingerprint


def test_running_locally_keeps_the_fingerprint_it_had_before_sandboxes(tmp_path):
    """加沙箱支持之前落的结论，在本机模式下要照常复用。"""
    material = {
        "template": 1,
        "skillup": "0.12.0",
        "engine": "claude_code",
        "engine_version": "2.1.284",
        "model": "deepseek-v4-flash",
        "judge_model": "judge-model",
        "iterations": 1,
        "case_timeout": 300,
        "max_turns": 12,
        "context_tokens": None,
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    assert _profile().fingerprint == f"sha256:{hashlib.sha256(blob).hexdigest()}"


def test_sandbox_preflight_needs_no_local_claude(tmp_path, ready):
    fake, _ = ready
    result = preflight_runtime(_sandbox(tmp_path, skillup_bin=str(fake.path)))
    assert result.claude_bin is None
    assert fake.calls[0]["env"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"


def test_in_the_sandbox_the_engine_version_may_follow_the_image(tmp_path, skill, ready):
    """不钉版本就不写 engine.version：写了 skill-up 会在沙箱里现装它。"""
    fake, _ = ready
    settings = _sandbox(tmp_path, skillup_bin=str(fake.path), runtime_engine_version="")
    assert preflight_runtime(settings).claude_bin is None

    profile = RuntimeProfile.from_settings(settings)
    config_path, _ = prepare_suite(skill, settings, profile)
    engine = yaml.safe_load(config_path.read_text())["engine"]
    assert "version" not in engine
    assert engine["model"] == {"provider": PROVIDER, "name": "deepseek-v4-flash"}

    other_image = RuntimeProfile.from_settings(
        _sandbox(tmp_path, runtime_engine_version="", opensandbox_image="registry.internal/claude:next")
    )
    assert profile.fingerprint != other_image.fingerprint, "版本跟着镜像走，换镜像就是另一种结论"


def test_locally_the_engine_version_must_be_pinned(tmp_path, ready):
    fake, _ = ready
    with pytest.raises(RuntimePreflightError, match="SKILLPRISM_RUNTIME_ENGINE_VERSION"):
        preflight_runtime(_settings(tmp_path, skillup_bin=str(fake.path), runtime_engine_version=""))


def test_sandbox_preflight_names_missing_sandbox_settings(tmp_path, ready):
    fake, _ = ready
    with pytest.raises(RuntimePreflightError, match="SKILLPRISM_OPENSANDBOX_IMAGE"):
        preflight_runtime(_sandbox(tmp_path, skillup_bin=str(fake.path), opensandbox_image=""))
