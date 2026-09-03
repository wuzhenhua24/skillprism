"""服务配置。所有可调项集中在这里，通过 SKILLPRISM_ 前缀的环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_scanner_env(value: str) -> dict[str, str]:
    """把 ``K=V,K=V`` 解析成字典。格式不对就抛，不做静默忽略。"""
    pairs: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        key, sep, val = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(
                f"SKILLPRISM_SCANNER_ENV 的每一项都要形如 K=V（逗号分隔），这一项不合法：{item!r}"
            )
        pairs[key] = val.strip()
    return pairs


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
    #: 下载地址模板，{skill_id} 会被 URL 编码后替换。
    #: 例：https://skills.internal/api/skills/{skill_id}/download
    content_url_template: str = ""
    #: 调用管理系统用的令牌，作为 Bearer 发送。
    content_token: str = ""
    content_timeout_seconds: float = 60.0
    #: 下载体积上限。解压前先卡住，避免拉一个超大响应体进内存。
    max_download_bytes: int = 64 * 1024 * 1024

    #: SkillEvaluator CLI 的可执行文件。独立安装，不与本服务共用 venv——
    #: 上游有 litellm<1.89、harbor==0.13.2 等硬 pin，共用早晚会冲突。
    skillevaluator_bin: str = "skillevaluator"

    #: 自定义策略 YAML。走 --policy 而非 --profile：后者只认上游包内的文件。
    policy_file: Path = Path("./profiles/internal.yaml")

    eval_timeout_seconds: int = 600

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

    @field_validator("scanner_env")
    @classmethod
    def _scanner_env_is_parseable(cls, value: str) -> str:
        """启动时就校验格式。写错了要当场报错，不能到评测时才静默丢掉。"""
        parse_scanner_env(value)
        return value

    def scanner_env_pairs(self) -> dict[str, str]:
        return parse_scanner_env(self.scanner_env)

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
