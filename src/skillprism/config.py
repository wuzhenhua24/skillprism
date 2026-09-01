"""服务配置。所有可调项集中在这里，通过 SKILLPRISM_ 前缀的环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    #: 启动时自检外部扫描器，缺失则拒绝启动。
    #: 关掉它意味着接受产出 incomplete 结果，仅供本地开发。
    require_scanners: bool = True

    #: worker 轮询间隔（秒）。当前定位是“只展示不拦截”，实时性要求低。
    poll_interval_seconds: float = 2.0

    max_attempts: int = 3

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
