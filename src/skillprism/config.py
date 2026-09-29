"""服务配置。所有可调项集中在这里，通过 SKILLPRISM_ 前缀的环境变量覆盖。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from skillprism.materialize import MAX_BUNDLE_FILES, MAX_BUNDLE_MEMBERS


def _parse_pairs(value: str, env_name: str) -> list[tuple[str, str]]:
    """把 ``K=V,K=V`` 解析成键值对，保留重复项。格式不对就抛，不做静默忽略。"""
    pairs: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, val = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"{env_name} 的每一项都要形如 K=V（逗号分隔），这一项不合法：{item!r}"
            )
        pairs.append((key, val.strip()))
    return pairs


def parse_scanner_env(value: str) -> dict[str, str]:
    return dict(_parse_pairs(value, "SKILLPRISM_SCANNER_ENV"))


def is_header_name(value: str) -> bool:
    return bool(value) and not any(ord(ch) < 0x21 or ord(ch) > 0x7E or ch == ":" for ch in value)


def parse_content_headers(value: str) -> dict[str, str]:
    """把 ``SKILLPRISM_CONTENT_HEADERS`` 解析成请求头。

    头名不合法、值为空或含控制字符 / 非 ASCII、同名（不分大小写）出现两次，
    都在这里抛。这些错误到了发请求时才暴露的话，表现是 httpx 抛异常、被收敛
    成可重试的 ContentFetchError，任务退避几轮之后才终结——而重试多少次都一样。
    """
    headers: dict[str, str] = {}
    seen: set[str] = set()
    for name, val in _parse_pairs(value, "SKILLPRISM_CONTENT_HEADERS"):
        if not is_header_name(name):
            raise ValueError(f"SKILLPRISM_CONTENT_HEADERS 里的请求头名不合法：{name!r}")
        if name.lower() in seen:
            raise ValueError(f"SKILLPRISM_CONTENT_HEADERS 里 {name!r} 出现了不止一次")
        seen.add(name.lower())
        if not val or any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in val):
            raise ValueError(
                f"SKILLPRISM_CONTENT_HEADERS 里 {name!r} 的值为空或含控制字符 / 非 ASCII：{val!r}"
            )
        headers[name] = val
    return headers


#: SKILLPRISM_RUNTIME_ENV 不许碰的键，见 Settings._runtime_env_is_parseable。
#: ``OPENSANDBOX_*`` 三个是 skill-up 读的连接参数，由专门的配置项给。
_RUNTIME_ENV_RESERVED = frozenset(
    {
        "PATH",
        "HOME",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "OPENSANDBOX_BASE_URL",
        "OPENSANDBOX_API_KEY",
        "OPENSANDBOX_EXTENSIONS",
    }
)

#: SKILLPRISM_OPENSANDBOX_ENV 不许碰的键（前缀），见
#: Settings._opensandbox_env_is_parseable。
_SANDBOX_ENV_RESERVED = frozenset({"HOME", "CLAUDE_CODE_MAX_CONTEXT_TOKENS"})
_SANDBOX_ENV_RESERVED_PREFIXES = ("ANTHROPIC_", "GATEWAY_", "CLAUDE_CODE_")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SKILLPRISM_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./var/skillprism.db"

    #: 报告原文落地位置。生产环境换成对象存储实现，见 storage.py。
    report_root: Path = Path("./var/reports")

    #: 物化临时目录的父目录。每个任务在其下建独立子目录，用完即删。
    work_root: Path = Path("./var/work")

    #: 开发用的本地 skill 目录（LocalDirectorySource 的根）。
    #: 接入管理系统后由 ZipArchiveSource 取代。
    local_skills_root: Path = Path("./var/skills")

    # ---- 内容来源：管理系统的 zip 下载接口 ----
    # 与下面的 GitLab 接入可以同时配，两个都配就两种接入都启用；触发时由
    # 各自的入口声明用哪个（见 content.enabled_sources）。
    #: 下载地址模板，{skill_id} 会被 URL 编码后替换。
    #: 例：https://skills.internal/api/skills/{skill_id}/download
    content_url_template: str = ""
    #: 调用管理系统用的令牌，作为 Bearer 发送。
    content_token: str = ""
    #: 调用管理系统时额外带上的请求头，格式 ``Name=Value``，逗号分隔。
    #:
    #: 用于经公司内部网关转发的场景：网关按头决定转给谁，例
    #: ``X-Ploto-Direct-Target=lingxi-manager/default``。只作用于上面这个
    #: 下载地址，GitLab 接入不带——那是另一个上游，要不要过网关是两回事。
    #:
    #: 值里不能有逗号（逗号是分隔符）。令牌仍然走 CONTENT_TOKEN，两处都给
    #: Authorization 会在启动时报错。
    content_headers: str = ""
    content_timeout_seconds: float = 60.0
    #: 下载体积上限。解压前先卡住，避免拉一个超大响应体进内存。
    max_download_bytes: int = 64 * 1024 * 1024

    #: 承载本服务的公开地址，例 https://skillprism.internal。配了之后结果
    #: DTO 的 report_url 才是一个能点开的链接；留空则该字段为 null。
    #: 这里只放"对外怎么访问到我们"，与内容来源无关——服务自己进程绑的是
    #: 127.0.0.1，公开地址由前置网关决定，进程无从得知。
    public_base_url: str = ""

    # ---- 内容来源：GitLab 归档接口 ----
    #: GitLab 实例地址，例 https://gitlab.internal。配了就启用 GitLab 接入。
    #: 可以和 CONTENT_URL_TEMPLATE 同时配，见 content.enabled_sources。
    gitlab_base_url: str = ""
    #: 只读令牌。权限给到 read_repository 即可，不要给 api。
    #: 不要放进 SCANNER_ENV——那是注给评测子进程的，公司凭据不进那一层。
    gitlab_token: str = ""
    #: 发令牌用的请求头名。PAT / group / project token 用 PRIVATE-TOKEN，
    #: CI 的 CI_JOB_TOKEN 只认 JOB-TOKEN，OAuth token 用 Authorization。
    gitlab_token_header: str = "PRIVATE-TOKEN"
    #: 提交时没给 skill_version 就用这个 ref。
    gitlab_default_ref: str = "main"

    #: SkillEvaluator CLI 的可执行文件。独立安装，不与本服务共用 venv——
    #: 上游有 litellm<1.89、harbor==0.13.2 等硬 pin，共用早晚会冲突。
    skillevaluator_bin: str = "skillevaluator"

    #: 自定义策略 YAML。走 --policy 而非 --profile：后者只认上游包内的文件。
    policy_file: Path = Path("./profiles/internal.yaml")

    eval_timeout_seconds: int = 600

    #: 一个 bundle 最多几个成员。默认见 materialize.MAX_BUNDLE_MEMBERS；这里
    #: 开成可调是因为它挡住的是**真实内容的规模**（一个 plugin 仓里有多少
    #: skill），而那取决于对接哪个仓库，不是本服务能定死的数。
    #:
    #: 调大之前先想清楚两件事：
    #:
    #: * 条目数与总量上限**不跟着涨**（MAX_BUNDLE_FILES / MAX_BUNDLE_TOTAL_BYTES
    #:   仍是常量）。成员多到摊不开时会先撞那两条，报错会直接说是哪条。
    #: * catalog 是一个子进程跑完全部成员（runner.run_catalog），成员越多越贴近
    #:   EVAL_TIMEOUT_SECONDS，而那个超时一到是**整组没有报告**，不是慢一点。
    #:   真正的天花板在那儿，调这个数时要同步看那个。
    max_bundle_members: int = MAX_BUNDLE_MEMBERS

    #: 额外传给评测子进程的环境变量，格式 ``K=V``，逗号分隔。
    #:
    #: 子进程默认只拿到 PATH/HOME（见 runner._subprocess_env），systemd 的
    #: EnvironmentFile 注入的东西到不了扫描器那一层。这个字段是唯一的注入口，
    #: 用于调扫描器的行为——例如出网受限的机器上给 semgrep 加超时上限。
    #: 值在这里写明而不是从父进程继承，"不把公司凭据带进评测进程"这条才守得住。
    scanner_env: str = ""

    #: 启动时自检外部扫描器，缺失则拒绝启动。
    #: 关掉它意味着接受产出 incomplete 结果，仅供本地开发。
    require_scanners: bool = True

    #: worker 轮询间隔（秒）。当前定位是“只展示不拦截”，实时性要求低。
    poll_interval_seconds: float = 2.0

    max_attempts: int = 3

    #: 重试退避的基数：第 n 次尝试失败后等 base * 2**(n-1) 秒再领。
    #: 不能是 0——没有退避的话 max_attempts 会在几秒内烧光，而管理系统
    #: 重启一次就不止几秒，等于把上游的短暂故障变成任务的永久失败。
    retry_backoff_seconds: float = 30.0
    #: 退避上限。指数增长很快就会超出"只展示不拦截"这个定位的容忍度。
    retry_backoff_max_seconds: float = 300.0

    # ---- Embedding shim（M2）----
    #: 火山方舟 OpenAI 兼容端点。shim 是唯一直接调它的组件。
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3"
    ark_api_key: str = ""
    #: 方舟 embeddings 接口的单请求输入上限。实测为 10，超出返回 400。
    ark_batch_size: int = 10
    #: 并发发起的分片请求数。方舟单请求 4.5~16s，串行会把重建窗口拉得很长。
    shim_concurrency: int = 4
    #: 传输层抖动重试次数。实测该端点偶发 TLS 握手失败。
    shim_retries: int = 3
    shim_timeout_seconds: float = 120.0

    # ---- 运行时评测（Tier 3），见 docs/runtime-evaluation.md ----
    # 只有 sandbox worker（skillprism-worker --queue sandbox）读这一组。
    #: skill-up CLI。从 tag 编译的单二进制，不用 install.sh——那个脚本从
    #: GitHub 下载，出网受限的机器上装不了。
    skillup_bin: str = "skill-up"
    #: 钉住的 skill-up 版本。预检时核对 ``skill-up --version``，对不上拒绝
    #: 启动：它进结论的运行时指纹，换了版本却没人知道，复用就会给出旧版本
    #: 评出来的结论。result.json 也没有稳定性承诺，换版本要先过契约测试。
    skillup_version: str = "0.12.0"
    #: 评测子进程的 PATH。必须能找到 claude、bash、git。子进程不继承 worker
    #: 的环境（见 skillup.subprocess_env），所以这里要写全。agent 跑在
    #: OpenSandbox 里时本机不需要 claude，可以留空。
    runtime_path: str = ""
    #: 钉住的 Claude Code 版本，例 ``2.1.284``。写进 eval.yaml 的
    #: ``engine.version``，skill-up 在 ``environment: none`` 下会核对本机的
    #: ``claude --version``，不一致直接报错；在沙箱里则是版本不对就按它安装。
    #: 沙箱模式下可以留空：用镜像里装的版本，镜像进指纹代表它。
    runtime_engine_version: str = ""
    #: 模型网关的 Anthropic 兼容地址（不含 ``/v1/messages``）。
    runtime_base_url: str = ""
    #: 网关的 key。用专门的一个并设额度：本期不隔离，agent 读得到它。
    runtime_api_key: str = ""
    runtime_model: str = ""
    #: ``agent_judge`` 用的模型，留空同 ``runtime_model``。
    runtime_judge_model: str = ""
    #: 模型的真实上下文窗口。Claude Code 不认识非 Claude 模型名时按 200k
    #: 自动压缩；配了就作为 CLAUDE_CODE_MAX_CONTEXT_TOKENS 传下去。
    runtime_context_tokens: int | None = None
    #: 每个用例跑几次。进运行时指纹：1 次和 3 次评出来的不是一种结论。
    runtime_iterations: int = 1
    #: skill-up 的用例并发（本机模式）。不进指纹——只影响快慢。沙箱模式下
    #: 改用 opensandbox_max_sandboxes。
    runtime_parallelism: int = 2
    #: 单用例上限（秒），作者在用例里写得更大也会被压到这里。
    runtime_case_timeout_seconds: int = 300
    runtime_max_turns: int = 12
    #: 用例数上限。超出直接报错、不截断：只跑前 N 个会给出一个覆盖不全、
    #: 看起来却完整的结论。
    runtime_max_cases: int = 20
    #: 额外传给评测子进程的环境变量，``K=V`` 逗号分隔，同 SCANNER_ENV。
    runtime_env: str = ""
    #: 开跑前探活网关的超时。网关不通时 Claude Code 会一直重试，每个用例都
    #: 耗满超时才判 ERROR，探活就是为了不白等这一轮。实测方舟上一个
    #: max_tokens=1 的请求要 2.6～7.9s（带 thinking 的模型也要先想一下），
    #: 10s 会误判，所以给 30s。
    runtime_probe_timeout_seconds: float = 30.0
    #: 开跑前要不要从 worker 探活网关。agent 跑在 OpenSandbox 里时，连网关的是
    #: 沙箱而不是 worker；worker 本身连不上网关时关掉它，否则每个任务都会被
    #: 探活判死、无限退避。
    runtime_probe_gateway: bool = True
    #: agent 在哪儿跑。``none`` 是 worker 本机（见 docs/runtime-evaluation.md §10
    #: 接受的风险）；``opensandbox`` 是公司的 OpenSandbox，每个用例一个沙箱。
    runtime_environment: Literal["none", "opensandbox"] = "none"

    # ---- OpenSandbox（runtime_environment=opensandbox 时才读） ----
    #: OpenSandbox 服务地址，以 ``OPENSANDBOX_BASE_URL`` 传给 skill-up。
    opensandbox_base_url: str = ""
    #: OpenSandbox 的 key，以 ``OPENSANDBOX_API_KEY`` 传给 skill-up，不落盘。
    opensandbox_api_key: str = ""
    #: 一个任务最多同时开几个沙箱，也就是沙箱模式下 skill-up 的用例并发。
    #: sandbox worker 一次只处理一个任务，所以整个部署同时存在的沙箱数是它
    #: 乘以 sandbox worker 的进程数。不进指纹。
    opensandbox_max_sandboxes: int = 2
    #: 沙箱的启动命令，JSON 数组，例 ``["tail","-f","/dev/null"]``。留空用
    #: skill-up 的默认（``tail -F /dev/null``）。不进指纹。
    opensandbox_entrypoint: list[str] = []
    #: 沙箱镜像。进运行时指纹：镜像决定了 agent 手边有哪些工具。
    #: 建议预装钉住版本的 Claude Code，否则每个用例都要在沙箱里 npm 装一遍。
    opensandbox_image: str = ""
    #: 沙箱里的工作目录，留空用 skill-up 的默认 ``/workspace``。
    opensandbox_workspace: str = ""
    #: OpenSandbox 的扩展参数，JSON 对象字符串，原样给 skill-up 的
    #: ``environment.kwargs.extensions``。公司部署特有的东西（资源档位、
    #: 租户之类）放这里。
    opensandbox_extensions: str = ""
    #: 文件与命令请求经 OpenSandbox 服务端转发，而不是直连沙箱。worker 连不到
    #: 沙箱本身的地址时要开。
    opensandbox_use_server_proxy: bool = False
    #: 等沙箱就绪的上限（秒）。SDK 默认只有 30s，冷启动的镜像不够。
    opensandbox_ready_timeout_seconds: int = 120
    #: 沙箱的存活上限（秒），到点由服务端回收。0 表示按单用例上限自动算，
    #: 见 skillup.sandbox_ttl。worker 被杀、skill-up 来不及收尾时，沙箱靠它回收。
    opensandbox_sandbox_timeout_seconds: int = 0
    #: SDK 单个请求的超时（秒），留空用 SDK 默认。
    opensandbox_request_timeout_seconds: int | None = None
    #: 沙箱出网策略：空（服务端默认）、``deny_all``、``allow_declared``。
    #: ``allow_declared`` 时网关的主机名自动放行，其余见 opensandbox_allowed_egress。
    #: 进运行时指纹：能不能出网会改变 skill 的表现。
    opensandbox_network_policy: Literal["", "deny_all", "allow_declared"] = ""
    #: ``allow_declared`` 时额外放行的目标（逗号分隔，FQDN 或通配），例如内部
    #: npm 源。
    opensandbox_allowed_egress: str = ""
    #: 注入沙箱内命令的环境变量，``K=V`` 逗号分隔。和 SKILLPRISM_RUNTIME_ENV
    #: 不同：那个给的是 worker 上的 skill-up 进程，这个给的是沙箱里的 agent。
    opensandbox_env: str = ""

    @field_validator("gitlab_base_url")
    @classmethod
    def _gitlab_base_url_is_http(cls, value: str) -> str:
        """启动时就挡住写错的地址，别等到第一个任务跑起来才发现。"""
        if not value:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"SKILLPRISM_GITLAB_BASE_URL 必须是 http(s) 地址：{value!r}")
        return value.rstrip("/")

    @field_validator("public_base_url")
    @classmethod
    def _public_base_url_is_http(cls, value: str) -> str:
        """同 gitlab_base_url：写错的地址要在启动时就挡住。

        这个尤其值得当场报错——拼错了不会有任何运行时异常，只会让每一条
        结论都带上一个点不开的链接，而链接会进管理系统的库长期存在。
        """
        if not value:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"SKILLPRISM_PUBLIC_BASE_URL 必须是 http(s) 地址：{value!r}")
        return value.rstrip("/")

    @field_validator("gitlab_token_header")
    @classmethod
    def _gitlab_token_header_is_a_header_name(cls, value: str) -> str:
        value = value.strip()
        if not is_header_name(value):
            raise ValueError(f"SKILLPRISM_GITLAB_TOKEN_HEADER 不是合法的请求头名：{value!r}")
        return value

    @field_validator("content_headers")
    @classmethod
    def _content_headers_are_parseable(cls, value: str) -> str:
        parse_content_headers(value)
        return value

    @model_validator(mode="after")
    def _content_token_and_headers_do_not_both_set_authorization(self) -> Settings:
        """两处都给 Authorization 时只有一个能生效，哪个赢都是在静默丢配置。"""
        if self.content_token and any(
            name.lower() == "authorization" for name in self.content_header_pairs()
        ):
            raise ValueError(
                "SKILLPRISM_CONTENT_TOKEN 与 SKILLPRISM_CONTENT_HEADERS 里的 Authorization "
                "只能配一个：前者会以 Bearer 发送 Authorization"
            )
        return self

    @field_validator("max_bundle_members")
    @classmethod
    def _max_bundle_members_is_sane(cls, value: int) -> int:
        """挡住两头：0 让所有 bundle 都提交不了，比上限还大则是个填错的数。

        上界取 ``MAX_BUNDLE_FILES``——一个成员至少占一个 ``SKILL.md``，成员数
        超过条目数上限时那个上限会先拦下来，这里配的数根本到不了。
        """
        if value < 1:
            raise ValueError(f"SKILLPRISM_MAX_BUNDLE_MEMBERS 至少为 1：{value!r}")
        if value > MAX_BUNDLE_FILES:
            raise ValueError(
                f"SKILLPRISM_MAX_BUNDLE_MEMBERS 不能超过归档条目数上限 "
                f"{MAX_BUNDLE_FILES}（一个成员至少占一个 SKILL.md）：{value!r}"
            )
        return value

    @field_validator("scanner_env")
    @classmethod
    def _scanner_env_is_parseable(cls, value: str) -> str:
        """启动时就校验格式。写错了要当场报错，不能到评测时才静默丢掉。"""
        parse_scanner_env(value)
        return value

    @field_validator("runtime_base_url")
    @classmethod
    def _runtime_base_url_is_http(cls, value: str) -> str:
        """同 gitlab_base_url。写错了的表现是每个用例都耗满超时才判 ERROR。"""
        if not value:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"SKILLPRISM_RUNTIME_BASE_URL 必须是 http(s) 地址：{value!r}")
        return value.rstrip("/")

    @field_validator("runtime_env")
    @classmethod
    def _runtime_env_is_parseable(cls, value: str) -> str:
        """格式之外还要挡住几个键：它们由专门的配置项给，而且进运行时指纹。

        从这里塞 ``GATEWAY_MODEL`` 会静默覆盖 eval.yaml 里的模型（skill-up 的
        ``<PROVIDER>_*`` 环境变量优先于配置文件），结论换了模型、指纹却没变，
        复用就会把它当成同一种结论。PATH / HOME 同理，由代码决定。
        """
        for key, _ in _parse_pairs(value, "SKILLPRISM_RUNTIME_ENV"):
            upper = key.upper()
            if upper in _RUNTIME_ENV_RESERVED or upper.startswith("GATEWAY_"):
                raise ValueError(
                    f"SKILLPRISM_RUNTIME_ENV 不能设置 {key}：它由专门的配置项给出"
                    "（SKILLPRISM_RUNTIME_PATH / _BASE_URL / _API_KEY / _CONTEXT_TOKENS）"
                )
        return value

    @field_validator("opensandbox_base_url")
    @classmethod
    def _opensandbox_base_url_is_http(cls, value: str) -> str:
        if not value:
            return value
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"SKILLPRISM_OPENSANDBOX_BASE_URL 必须是 http(s) 地址：{value!r}")
        return value.rstrip("/")

    @field_validator("opensandbox_extensions")
    @classmethod
    def _opensandbox_extensions_is_a_json_object(cls, value: str) -> str:
        """skill-up 遇到不合法的 JSON 只打一条警告就忽略，扩展参数静默丢掉，
        表现是沙箱建在了错误的档位上。所以这里当场报错。"""
        value = value.strip()
        if not value:
            return value
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"SKILLPRISM_OPENSANDBOX_EXTENSIONS 不是合法的 JSON：{exc}") from exc
        if not isinstance(parsed, dict) or not all(isinstance(v, str) for v in parsed.values()):
            raise ValueError("SKILLPRISM_OPENSANDBOX_EXTENSIONS 必须是值全为字符串的 JSON 对象")
        return value

    @field_validator("opensandbox_env")
    @classmethod
    def _opensandbox_env_is_parseable(cls, value: str) -> str:
        """沙箱里的命令环境排在 skill-up 自己给的之前，能盖掉的不多，但
        ``ANTHROPIC_MODEL`` 这类 skill-up 不设的键会直接生效——结论换了模型，
        指纹却没变。所以连同凭据一起挡掉。"""
        for key, _ in _parse_pairs(value, "SKILLPRISM_OPENSANDBOX_ENV"):
            upper = key.upper()
            if upper in _SANDBOX_ENV_RESERVED or upper.startswith(_SANDBOX_ENV_RESERVED_PREFIXES):
                raise ValueError(
                    f"SKILLPRISM_OPENSANDBOX_ENV 不能设置 {key}：模型、凭据与上下文窗口由专门的配置项给出"
                )
        return value

    @field_validator(
        "runtime_iterations",
        "runtime_case_timeout_seconds",
        "runtime_max_turns",
        "runtime_max_cases",
    )
    @classmethod
    def _runtime_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"运行时评测的次数、超时与上限都至少为 1：{value!r}")
        return value

    @field_validator("runtime_parallelism")
    @classmethod
    def _runtime_parallelism_in_range(cls, value: int) -> int:
        """skill-up 自己只接受 1～256，超出会让每个任务都在 run 那一步失败。"""
        if not 1 <= value <= 256:
            raise ValueError(f"SKILLPRISM_RUNTIME_PARALLELISM 必须在 1～256 之间：{value!r}")
        return value

    def scanner_env_pairs(self) -> dict[str, str]:
        return parse_scanner_env(self.scanner_env)

    @field_validator("opensandbox_max_sandboxes")
    @classmethod
    def _opensandbox_max_sandboxes_in_range(cls, value: int) -> int:
        """它就是 skill-up 的 parallelism，范围同 runtime_parallelism。"""
        if not 1 <= value <= 256:
            raise ValueError(f"SKILLPRISM_OPENSANDBOX_MAX_SANDBOXES 必须在 1～256 之间：{value!r}")
        return value

    @field_validator("opensandbox_entrypoint")
    @classmethod
    def _opensandbox_entrypoint_has_no_blanks(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("SKILLPRISM_OPENSANDBOX_ENTRYPOINT 里不能有空项")
        return value

    @field_validator("opensandbox_ready_timeout_seconds", "opensandbox_sandbox_timeout_seconds")
    @classmethod
    def _opensandbox_timeouts_not_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"OpenSandbox 的超时不能为负：{value!r}")
        return value

    @model_validator(mode="after")
    def _sandbox_outlives_a_case(self) -> Settings:
        """沙箱先于用例到期，用例就会在中途被回收，表现是一个莫名其妙的 ERROR。"""
        ttl = self.opensandbox_sandbox_timeout_seconds
        if ttl and ttl < self.runtime_case_timeout_seconds + self.opensandbox_ready_timeout_seconds:
            raise ValueError(
                "SKILLPRISM_OPENSANDBOX_SANDBOX_TIMEOUT_SECONDS 至少要是单用例上限加就绪等待："
                f"{ttl} < {self.runtime_case_timeout_seconds} + {self.opensandbox_ready_timeout_seconds}"
            )
        return self

    @property
    def case_parallelism(self) -> int:
        """交给 skill-up 的用例并发。沙箱模式下每个并发槽位就是一个沙箱。"""
        if self.runtime_environment == "opensandbox":
            return self.opensandbox_max_sandboxes
        return self.runtime_parallelism

    def runtime_env_pairs(self) -> dict[str, str]:
        return dict(_parse_pairs(self.runtime_env, "SKILLPRISM_RUNTIME_ENV"))

    def opensandbox_env_pairs(self) -> dict[str, str]:
        return dict(_parse_pairs(self.opensandbox_env, "SKILLPRISM_OPENSANDBOX_ENV"))

    def opensandbox_egress(self) -> list[str]:
        return [t.strip() for t in self.opensandbox_allowed_egress.split(",") if t.strip()]

    @property
    def effective_judge_model(self) -> str:
        return self.runtime_judge_model or self.runtime_model

    def content_header_pairs(self) -> dict[str, str]:
        return parse_content_headers(self.content_headers)

    def backoff_for(self, attempts: int) -> float:
        """第 ``attempts`` 次尝试失败后要等的秒数。"""
        if self.retry_backoff_seconds <= 0:
            return 0.0
        delay = self.retry_backoff_seconds * 2 ** max(0, attempts - 1)
        return min(delay, self.retry_backoff_max_seconds)

    def ensure_dirs(self) -> None:
        self.report_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """丢弃缓存的配置，下次 get_settings() 重新从环境读取。

    供测试在切换环境变量后调用；生产代码不应使用。
    """
    global _settings
    _settings = None
