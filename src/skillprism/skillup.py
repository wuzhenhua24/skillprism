"""执行层（Tier 3）：以子进程调用 skill-up CLI 跑作者写的用例。

设计见 docs/runtime-evaluation.md。和 runner.py 调 SkillEvaluator 是同一套理由：
子进程可以直接杀掉重来，也不和上游的依赖搅在一起。这里只负责"把东西准备好、
把进程跑起来"；skill-up 的输出长什么样只有 runtime_adapter 知道。

几条在 POC 里踩出来的约束，决定了这个模块的形状：

- **环境变量会静默覆盖 eval.yaml。** skill-up 按 provider 名读
  ``<PROVIDER>_BASE_URL`` / ``_MODEL`` / ``_API_KEY``，优先于配置文件。所以
  provider 用一个自定义名（:data:`PROVIDER`），子进程环境逐项给出，不继承。
- **用例路径必须是相对路径**，绝对路径会被拼到根目录后面。平台的 eval.yaml
  因此放在物化后 skill 的 ``evals/`` 里，根目录就是 skill 目录，作者按
  "相对 skill 根"写的 fixture 路径原样可用。
- **``agent_judge`` 用例不写 ``judge.model`` 校验不过**，而 judge 模型本来就该
  由平台定，所以拷进来时统一改写。

agent 可以跑在 worker 本机（``environment: none``），也可以跑在 OpenSandbox 里
（每个用例一个沙箱）。两者只在生成的 ``environment`` 段、子进程环境与预检上
不同，见 docs/runtime-evaluation.md §14。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from skillprism.config import Settings

logger = logging.getLogger(__name__)

#: 改写规则或生成的 eval.yaml 变了就升这个数。它进运行时指纹：模板一变，
#: 同样的内容评出来就可能是另一种结论，旧结论不该再被复用。
TEMPLATE_VERSION = 1

#: 固定的 provider 名。**不能是 anthropic**：skill-up 会读 ``ANTHROPIC_BASE_URL``
#: 并让它覆盖 eval.yaml，而那个变量在运维环境和 Claude Code 自己的环境里都很
#: 常见。POC 里故意把它设成一个死地址，用例照常通过，说明走的是这个名字。
PROVIDER = "gateway"
_PROVIDER_ENV = PROVIDER.upper()

ENGINE = "claude_code"

#: 平台生成的 eval.yaml，放在物化后 skill 的 ``evals/`` 下（理由见模块文档）。
#: skill-up 装 skill 时排除 ``evals/``，agent 看不到它。
EVAL_CONFIG_NAME = ".skillprism-eval.yaml"

#: 作者用例的唯一来源。作者自己的 ``evals/eval.yaml`` 整个不读，见
#: docs/runtime-evaluation.md §4.1。
CASES_DIR = ("evals", "cases")

#: "没有用例"的错误文案前缀。管理系统靠它区分"提示作者补用例"与"评测失败"，
#: 是对外约定的一部分，别改措辞。
NO_CASES_PREFIX = "无运行时用例"

#: 整体超时在"用例数 × 迭代 × 单用例上限 ÷ 并发"之外的余量，给 skill 安装、
#: 报告生成这些不计入单用例时间的步骤。
RUN_TIMEOUT_MARGIN_SECONDS = 120

VALIDATE_TIMEOUT_SECONDS = 60

#: OpenSandbox 下子进程 PATH 的默认值。worker 上的 skill-up 不再需要找 claude，
#: 只要一个能跑的 PATH。
_SANDBOX_HOST_PATH = "/usr/local/bin:/usr/bin:/bin"

#: 自动算沙箱存活上限时，在"就绪等待 + 两个单用例上限"（agent 一次、
#: agent_judge 一次）之外的余量：装 Claude Code、传 fixture、下载产物。
SANDBOX_TTL_MARGIN_SECONDS = 300


class CaseError(ValueError):
    """作者的用例有问题。重试多少次都一样，任务直接结束，信息要能转给作者。"""


class NoCasesError(CaseError):
    """skill 里没有运行时用例。和别的用例错误分开：管理系统要据此提示作者补用例。"""


class RuntimePreflightError(RuntimeError):
    """sandbox worker 的运行前提不满足。"""


@dataclass(frozen=True)
class RuntimeProfile:
    """一次运行时评测的执行配置。它的指纹进结论身份，见 :attr:`fingerprint`。"""

    skillup_version: str
    #: 钉住的 Claude Code 版本。沙箱模式下可以为空：用镜像里装的那个，
    #: 由镜像进指纹来管住它（见 :attr:`fingerprint`）。
    engine_version: str
    model: str
    judge_model: str
    iterations: int
    case_timeout_seconds: int
    max_turns: int
    context_tokens: int | None

    engine: str = ENGINE
    #: agent 在哪儿跑。``none`` 之外的取值连同下面三项进指纹。
    environment: str = "none"
    sandbox_image: str = ""
    network_policy: str = ""
    #: 平台额外放行的出网目标（不含自动放行的网关主机，理由同 base_url）。
    extra_egress: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: Settings) -> RuntimeProfile:
        return cls(
            skillup_version=settings.skillup_version,
            engine_version=settings.runtime_engine_version,
            model=settings.runtime_model,
            judge_model=settings.effective_judge_model,
            iterations=settings.runtime_iterations,
            case_timeout_seconds=settings.runtime_case_timeout_seconds,
            max_turns=settings.runtime_max_turns,
            context_tokens=settings.runtime_context_tokens,
            environment=settings.runtime_environment,
            sandbox_image=settings.opensandbox_image,
            network_policy=settings.opensandbox_network_policy,
            extra_egress=tuple(sorted(settings.opensandbox_egress())),
        )

    @property
    def fingerprint(self) -> str:
        """执行配置的指纹。复用要求它精确相等。

        **不进来的：** 网关地址（换地址不该让所有结论失效，换了后端就该换模型名）、
        并发（只影响快慢）、key。网关背后的实际模型版本也不在里面——跑之前
        拿不到，只能事后记下来（``served_models``），漂移了靠 force 重跑。
        OpenSandbox 的服务地址、扩展参数、超时同理不进来：它们决定沙箱建在
        哪儿、多快，不决定 agent 手边有什么。

        ``environment`` 只在不是 ``none`` 时才进原料，这样本机运行的指纹与
        加入沙箱支持之前完全一样，已有的结论照常复用。

        沙箱模式下不钉 Claude Code 版本时，``engine_version`` 是空串，版本由
        镜像代表：镜像换了指纹就变。前提是镜像 tag 不可变——同一个 tag 重新
        推一个装了别的版本的镜像，这里看不出来，只能从结论里记下的实际版本
        发现。
        """
        material = {
            "template": TEMPLATE_VERSION,
            "skillup": self.skillup_version,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "model": self.model,
            "judge_model": self.judge_model,
            "iterations": self.iterations,
            "case_timeout": self.case_timeout_seconds,
            "max_turns": self.max_turns,
            "context_tokens": self.context_tokens,
        }
        if self.environment != "none":
            material["environment"] = {
                "type": self.environment,
                "image": self.sandbox_image,
                "network_policy": self.network_policy,
                "extra_egress": list(self.extra_egress),
            }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(blob).hexdigest()}"


# ---------------------------------------------------------------------------
# 预检


@dataclass(frozen=True)
class RuntimeReady:
    """预检通过后的可执行文件位置。"""

    skillup_bin: str
    #: 本机的 claude。agent 跑在沙箱里时为 None：那边的版本由 skill-up 按
    #: ``engine.version`` 核对或安装，跑完再对一次（runtime_worker）。
    claude_bin: str | None


_REQUIRED_SETTINGS = (
    ("runtime_base_url", "SKILLPRISM_RUNTIME_BASE_URL"),
    ("runtime_api_key", "SKILLPRISM_RUNTIME_API_KEY"),
    ("runtime_model", "SKILLPRISM_RUNTIME_MODEL"),
)

_REQUIRED_BY_ENVIRONMENT = {
    "none": (
        ("runtime_path", "SKILLPRISM_RUNTIME_PATH"),
        # 本机的 claude 是运维装的，版本不钉就没有东西代表它。
        ("runtime_engine_version", "SKILLPRISM_RUNTIME_ENGINE_VERSION"),
    ),
    "opensandbox": (
        ("opensandbox_base_url", "SKILLPRISM_OPENSANDBOX_BASE_URL"),
        ("opensandbox_api_key", "SKILLPRISM_OPENSANDBOX_API_KEY"),
        ("opensandbox_image", "SKILLPRISM_OPENSANDBOX_IMAGE"),
    ),
}


def host_path(settings: Settings) -> str:
    """评测子进程（worker 上的 skill-up）的 PATH。"""
    if settings.runtime_path:
        return settings.runtime_path
    return _SANDBOX_HOST_PATH


def _version_of(binary: str, env: dict[str, str]) -> str | None:
    """跑 ``<binary> --version``，取出其中的版本号。取不到返回 None。

    两个 CLI 的输出形状不同（``skill-up version 0.12.0`` 与
    ``2.1.284 (Claude Code)``），所以取"第一个以数字开头的词"而不是固定位置。
    """
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("%s --version 失败：%s", binary, exc)
        return None
    if proc.returncode != 0:
        return None
    for word in proc.stdout.split():
        if word[:1].isdigit():
            return word
    return None


def preflight_runtime(settings: Settings) -> RuntimeReady:
    """sandbox worker 的启动自检。不通过就抛错，不要带病运行。

    版本必须和配置**精确相等**，不是"能跑就行"：两者都进运行时指纹。机器上
    换了版本而配置没跟着改，复用就会把新版本评出来的东西当成旧版本的结论。
    """
    required = _REQUIRED_SETTINGS + _REQUIRED_BY_ENVIRONMENT[settings.runtime_environment]
    missing = [env for attr, env in required if not getattr(settings, attr)]
    if missing:
        raise RuntimePreflightError(f"运行时评测缺少配置：{'、'.join(missing)}")

    path = host_path(settings)
    skillup = shutil.which(settings.skillup_bin) or shutil.which(settings.skillup_bin, path=path)
    if skillup is None:
        raise RuntimePreflightError(f"找不到 skill-up 可执行文件：{settings.skillup_bin}")

    with tempfile.TemporaryDirectory(prefix="skillprism-preflight-") as home:
        skillup_version = _version_of(skillup, {"PATH": path, "HOME": home})
    if skillup_version != settings.skillup_version:
        raise RuntimePreflightError(
            f"skill-up 版本是 {skillup_version or '未知'}，配置钉的是 "
            f"{settings.skillup_version}（SKILLPRISM_SKILLUP_VERSION）"
        )

    if settings.runtime_environment != "none":
        # 沙箱在每个用例开始时才建，这里摸不到。本机有没有 claude 无关紧要。
        return RuntimeReady(skillup_bin=skillup, claude_bin=None)

    claude = shutil.which("claude", path=settings.runtime_path)
    if claude is None:
        raise RuntimePreflightError(
            f"SKILLPRISM_RUNTIME_PATH 里找不到 claude：{settings.runtime_path}"
        )
    with tempfile.TemporaryDirectory(prefix="skillprism-preflight-") as home:
        engine_version = _version_of(claude, {"PATH": settings.runtime_path, "HOME": home})
    if engine_version != settings.runtime_engine_version:
        raise RuntimePreflightError(
            f"claude 版本是 {engine_version or '未知'}，配置钉的是 "
            f"{settings.runtime_engine_version}（SKILLPRISM_RUNTIME_ENGINE_VERSION）"
        )
    return RuntimeReady(skillup_bin=skillup, claude_bin=claude)


# ---------------------------------------------------------------------------
# 用例


def collect_cases(skill_root: Path, *, max_cases: int) -> list[str]:
    """作者的用例，返回相对 skill 根的 posix 路径，按文件名排序。

    只认 ``evals/cases/*.yaml``。``.yml`` 不是静默忽略而是报错：写了用例却没被
    跑，是作者最难发现的一种错。超过上限也报错，不截断——只跑前 N 个会给出
    一个覆盖不全、看起来却完整的结论。
    """
    cases_dir = skill_root.joinpath(*CASES_DIR)
    if not cases_dir.is_dir():
        raise NoCasesError(f"{NO_CASES_PREFIX}：skill 里没有 {'/'.join(CASES_DIR)}/ 目录")

    entries = sorted(p for p in cases_dir.iterdir() if p.is_file())
    stray = [p.name for p in entries if p.suffix == ".yml"]
    if stray:
        raise CaseError(f"用例文件扩展名必须是 .yaml，这些不会被识别：{'、'.join(stray)}")

    cases = [p for p in entries if p.suffix == ".yaml"]
    if not cases:
        raise NoCasesError(f"{NO_CASES_PREFIX}：{'/'.join(CASES_DIR)}/ 下没有 *.yaml")
    if len(cases) > max_cases:
        raise CaseError(f"用例数 {len(cases)} 超过上限 {max_cases}（SKILLPRISM_RUNTIME_MAX_CASES）")
    return [p.relative_to(skill_root).as_posix() for p in cases]


def _cap(section: dict[str, Any], key: str, limit: int) -> None:
    value = section.get(key)
    # bool 是 int 的子类，``timeout_seconds: true`` 不该被当成 1。
    if isinstance(value, int) and not isinstance(value, bool) and value > limit:
        section[key] = limit


def prepare_case(path: Path, profile: RuntimeProfile) -> None:
    """就地改写一个用例（改的是我们物化出来的那份，不是作者的源文件）。

    平台接管的字段见 docs/runtime-evaluation.md §4.2：

    - ``agent_judge`` 的 ``judge.model`` 强制换成平台的 judge 模型；
    - ``constraints`` 里的超时与轮数压到平台上限以内；
    - 用例级 MCP 只收 ``mode: mocked``。
    """
    name = path.name
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CaseError(f"{name} 读不出来或不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise CaseError(f"{name} 的顶层必须是一个映射")

    judge = data.get("judge")
    if isinstance(judge, dict) and judge.get("type") == "agent_judge":
        judge["model"] = f"{PROVIDER}/{profile.judge_model}"

    constraints = data.get("constraints")
    if isinstance(constraints, dict):
        _cap(constraints, "timeout_seconds", profile.case_timeout_seconds)
        _cap(constraints, "max_turns", profile.max_turns)

    mcp = data.get("mcp")
    if isinstance(mcp, dict):
        for server in mcp.get("servers") or []:
            if not isinstance(server, dict) or server.get("mode") != "mocked":
                raise CaseError(f"{name}：用例级 MCP 只能用 mode: mocked")

    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def sandbox_ttl(settings: Settings) -> int:
    """沙箱的存活上限（秒）。配了就用配的，否则按单用例上限算。

    一个用例在沙箱里依次经历：就绪、装 Claude Code、传 skill 与 fixture、
    agent（≤ 单用例上限）、agent_judge（同样是一次 agent 运行）、下载产物。
    """
    if settings.opensandbox_sandbox_timeout_seconds:
        return settings.opensandbox_sandbox_timeout_seconds
    return (
        settings.opensandbox_ready_timeout_seconds
        + 2 * settings.runtime_case_timeout_seconds
        + SANDBOX_TTL_MARGIN_SECONDS
    )


def environment_config(settings: Settings) -> dict[str, Any]:
    """eval.yaml 的 ``environment`` 段。

    OpenSandbox 的服务地址与 key 不在这里，走环境变量（:func:`subprocess_env`）。
    沙箱里的命令只看得到这里的 ``env`` 与 skill-up 自己给的变量，worker 子进程
    的环境过不去，所以上下文窗口在沙箱模式下要写进这里。
    """
    if settings.runtime_environment == "none":
        return {"type": "none"}

    env = settings.opensandbox_env_pairs()
    if settings.runtime_context_tokens:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(settings.runtime_context_tokens)

    kwargs: dict[str, str] = {}
    if settings.opensandbox_extensions:
        kwargs["extensions"] = settings.opensandbox_extensions
    if settings.opensandbox_request_timeout_seconds:
        kwargs["request_timeout_seconds"] = str(settings.opensandbox_request_timeout_seconds)

    section: dict[str, Any] = {
        "type": "opensandbox",
        "image": settings.opensandbox_image,
        "ready_timeout_seconds": settings.opensandbox_ready_timeout_seconds,
        "sandbox_timeout_seconds": sandbox_ttl(settings),
    }
    if settings.opensandbox_entrypoint:
        section["entrypoint"] = list(settings.opensandbox_entrypoint)
    if settings.opensandbox_workspace:
        section["workspace_mount"] = settings.opensandbox_workspace
    if settings.opensandbox_use_server_proxy:
        section["use_server_proxy"] = True
    if env:
        section["env"] = env
    if kwargs:
        section["kwargs"] = kwargs
    if settings.opensandbox_network_policy:
        section["network_policy"] = settings.opensandbox_network_policy
    if settings.opensandbox_network_policy == "allow_declared":
        # 先决条件：agent 得连得上模型网关，否则每个用例都耗满超时才判 ERROR。
        gateway = urlsplit(settings.runtime_base_url).hostname
        egress = [gateway] if gateway else []
        section["allowed_egress"] = egress + [t for t in settings.opensandbox_egress() if t not in egress]
    return section


def eval_config(settings: Settings, cases: list[str], profile: RuntimeProfile) -> dict[str, Any]:
    """平台生成的 eval.yaml 内容。

    ``skills`` 显式写出，不靠 skill-up"找到 SKILL.md 就自动装"。网关地址和
    key 不在这里：它们走环境变量（:func:`subprocess_env`），不落盘。
    """
    engine: dict[str, Any] = {"name": profile.engine}
    if profile.engine_version:
        # 写了它，skill-up 在沙箱里发现版本不符就会现装；不写就用镜像里的。
        engine["version"] = profile.engine_version
    engine["model"] = {"provider": PROVIDER, "name": profile.model}
    return {
        "schema_version": "v1alpha1",
        "environment": environment_config(settings),
        "skills": [{"source": "local_path", "path": "."}],
        "engine": engine,
        "cases": {
            "files": list(cases),
            "defaults": {
                "timeout_seconds": profile.case_timeout_seconds,
                "max_turns": profile.max_turns,
            },
            "parallelism": settings.case_parallelism,
        },
    }


def prepare_suite(skill_root: Path, settings: Settings, profile: RuntimeProfile) -> tuple[Path, int]:
    """收集并改写用例、写出平台的 eval.yaml。返回 ``(eval.yaml 路径, 用例数)``。

    抛 :class:`CaseError`（含 :class:`NoCasesError`）。
    """
    cases = collect_cases(skill_root, max_cases=settings.runtime_max_cases)
    for rel in cases:
        prepare_case(skill_root / rel, profile)
    config_path = skill_root.joinpath("evals", EVAL_CONFIG_NAME)
    config_path.write_text(
        yaml.safe_dump(
            eval_config(settings, cases, profile),
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return config_path, len(cases)


# ---------------------------------------------------------------------------
# 执行


def subprocess_env(settings: Settings, home: Path) -> dict[str, str]:
    """评测子进程的环境：**逐项给出，完整列表就是这些**，不继承 worker 的环境。

    这不只是整洁。skill-up 读 ``<PROVIDER>_*`` 并让它覆盖 eval.yaml，继承下来的
    任何同名变量都会让调用静默改道或换模型。``<PROVIDER>_MODEL`` 刻意不给：
    模型名只从 eval.yaml 来，一处定义。

    HOME 是本次任务专用的：共用 HOME 下 ``~/.claude`` 里的用户级 skills、
    CLAUDE.md、settings 都会被 agent 加载；``~/.config/skill-up`` 也会被
    skill-up 读进去（它的 ``runtime_kwargs.opensandbox`` 能改沙箱参数）。

    沙箱模式下网关的地址与 key 照样给这里：skill-up 在 worker 上解析 provider，
    再以 ``ANTHROPIC_*`` 注入沙箱里的命令。上下文窗口则改走 eval.yaml
    （:func:`environment_config`），这里给了沙箱也看不到。
    """
    env = {
        "PATH": host_path(settings),
        "HOME": str(home),
        f"{_PROVIDER_ENV}_BASE_URL": settings.runtime_base_url,
        f"{_PROVIDER_ENV}_API_KEY": settings.runtime_api_key,
    }
    if settings.runtime_environment == "opensandbox":
        env["OPENSANDBOX_BASE_URL"] = settings.opensandbox_base_url
        env["OPENSANDBOX_API_KEY"] = settings.opensandbox_api_key
    elif settings.runtime_context_tokens:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(settings.runtime_context_tokens)
    # 与 SCANNER_ENV 同理放在最后；会碰上面几项的键在配置校验时就拒了。
    env.update(settings.runtime_env_pairs())
    return env


def probe_gateway(settings: Settings) -> str | None:
    """开跑前探一下网关，返回失败原因；通则返回 None。

    网关不通时 Claude Code 会一直重试，每个用例都耗满超时才判 ERROR——POC 里
    一个用例在 60s 超时上耗了整整 60s。先花几十个 token 确认它活着，比白等
    "用例数 × 超时"再重试几轮划算。
    """
    try:
        response = httpx.post(
            f"{settings.runtime_base_url}/v1/messages",
            headers={
                "x-api-key": settings.runtime_api_key,
                # 和 skill-up 一样两种头都给：有的网关只认 Bearer。
                "authorization": f"Bearer {settings.runtime_api_key}",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": settings.runtime_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=settings.runtime_probe_timeout_seconds,
        )
    except httpx.HTTPError as exc:
        return f"模型网关不可达：{type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        return f"模型网关返回 {response.status_code}：{response.text[:200]}"
    return None


@dataclass
class SkillupRun:
    """一次 skill-up 调用的进程层面结果。解释产物是 runtime_adapter 的事。"""

    out_dir: Path
    events_path: Path
    exit_code: int | None = None
    #: skill-up validate 没过，也就是作者的用例写错了。不重试。
    invalid_cases: str | None = None
    #: 进程层面的故障（超时、起不来）。值得重试。
    failure: str | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""


def run_timeout(settings: Settings, case_count: int) -> int:
    """整体超时：按并发分几波跑完，每波最长一个单用例上限，再加余量。

    迭代是**一轮跑完再跑下一轮**的（skill-up runner 逐轮调 EvaluatePlan），
    并发只在一轮之内生效，所以波数是"每轮的波数 × 轮数"，不能把各轮的用例
    摊在一起除。

    沙箱模式下每波的上限换成沙箱的存活上限：建沙箱、装 Claude Code 都算在
    用例头上，而一个用例不可能活得比它的沙箱久。
    """
    waves = settings.runtime_iterations * math.ceil(case_count / settings.case_parallelism)
    per_wave = settings.runtime_case_timeout_seconds
    if settings.runtime_environment == "opensandbox":
        per_wave = sandbox_ttl(settings)
    return waves * per_wave + RUN_TIMEOUT_MARGIN_SECONDS


def _tail(text: str, work_dir: Path, lines: int = 20) -> str:
    """错误输出的末尾几行，去掉工作目录前缀——里面有任务 UUID 与服务器路径，
    原样转给作者既没用也不该给。"""
    out = "\n".join(text.strip().splitlines()[-lines:])
    for prefix in {str(work_dir), str(work_dir.resolve())}:
        out = out.replace(prefix + "/", "").replace(prefix, "")
    return out


def run_skillup(
    settings: Settings,
    ready: RuntimeReady,
    config_path: Path,
    work_dir: Path,
    *,
    case_count: int,
) -> SkillupRun:
    """先 validate 再 run。任何进程层面的异常都收敛成 :class:`SkillupRun`，不外抛。

    validate 单独跑一次，是为了把"作者的用例写错了"（不重试）和"跑的过程中
    出了故障"（重试）分开——run 失败时两者都是退出码 1，分不出来。

    工作目录设成 ``work_dir`` 而不是 skill 目录：skill-up 会读
    ``$PWD/.skill-up.yaml``，它能注入环境变量与运行时参数，skill 里要是带了
    这个文件就会被读进去。
    """
    home = work_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_dir = work_dir / "out"
    events_path = work_dir / "events.jsonl"
    run = SkillupRun(out_dir=out_dir, events_path=events_path)
    env = subprocess_env(settings, home)

    try:
        proc = subprocess.run(
            [ready.skillup_bin, "validate", str(config_path)],
            capture_output=True,
            text=True,
            timeout=VALIDATE_TIMEOUT_SECONDS,
            check=False,
            cwd=work_dir,
            env=env,
        )
    except subprocess.TimeoutExpired:
        run.failure, run.timed_out = f"skill-up validate 超时（{VALIDATE_TIMEOUT_SECONDS}s）", True
        return run
    except OSError as exc:
        run.failure = f"无法启动 skill-up：{exc}"
        return run
    if proc.returncode != 0:
        run.exit_code = proc.returncode
        run.invalid_cases = _tail(proc.stderr or proc.stdout, work_dir)
        return run

    timeout = run_timeout(settings, case_count)
    command = [
        ready.skillup_bin,
        "run",
        str(config_path),
        "--output-dir",
        str(out_dir),
        "--event-log",
        str(events_path),
        "--format",
        "html",
        "--iteration",
        str(settings.runtime_iterations),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=work_dir,
            env=env,
        )
    except subprocess.TimeoutExpired:
        run.failure, run.timed_out = f"skill-up run 超时（{timeout}s）", True
        return run
    except OSError as exc:
        run.failure = f"无法启动 skill-up：{exc}"
        return run

    run.exit_code = proc.returncode
    run.stdout = proc.stdout or ""
    run.stderr = proc.stderr or ""
    return run
