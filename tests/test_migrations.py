"""迁移与模型一致性测试。

测试里建表走 ``Base.metadata.create_all``（快），生产走 ``alembic upgrade head``。
两条路径分叉就意味着：改了模型但忘了生成迁移时，**测试全绿而生产炸**——
这正是 Alembic 要解决的问题本身，不能让它在自己的工程里复现。

所以这里把迁移真的跑一遍，再拿结果和模型比对。有差异就说明该补迁移了。
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from skill_eval_service.config import reset_settings
from skill_eval_service.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


def test_migrations_produce_the_model_schema(tmp_path, monkeypatch):
    """跑完全部迁移后，库结构必须与模型定义一致。

    失败通常意味着改了 models.py 但没生成迁移：
        alembic revision --autogenerate -m "说明"
    """
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("SES_DATABASE_URL", url)
    reset_settings()

    command.upgrade(_alembic_config(), "head")

    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
        reset_settings()

    assert diff == [], (
        "迁移产出的库结构与模型不一致。改了 models.py 之后需要生成迁移：\n"
        "  alembic revision --autogenerate -m \"说明\"\n"
        f"差异：{diff}"
    )


def test_downgrade_to_base_is_reachable(tmp_path, monkeypatch):
    """迁移必须可回滚到空库。

    不可回滚的迁移在出问题时没有退路，而 autogenerate 生成的 downgrade
    偶尔需要手工补全，这里保证它至少是可执行的。
    """
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    monkeypatch.setenv("SES_DATABASE_URL", url)
    reset_settings()

    cfg = _alembic_config()
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                names = set(MigrationContext.configure(connection).connection.dialect.get_table_names(connection))
        finally:
            engine.dispose()
    finally:
        reset_settings()

    # 只剩 alembic 自己的版本表
    assert names <= {"alembic_version"}, f"回滚后仍有残留表：{names - {'alembic_version'}}"
