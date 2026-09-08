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
from sqlalchemy import create_engine, text

from skillprism.config import reset_settings
from skillprism.domain import ContentSource
from skillprism.models import Base

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return cfg


def test_migrations_produce_the_model_schema(db_url, monkeypatch):
    """跑完全部迁移后，库结构必须与模型定义一致。

    失败通常意味着改了 models.py 但没生成迁移：
        alembic revision --autogenerate -m "说明"
    """
    url = db_url
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", url)
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


def test_downgrade_to_base_is_reachable(db_url, monkeypatch):
    """迁移必须可回滚到空库。

    不可回滚的迁移在出问题时没有退路，而 autogenerate 生成的 downgrade
    偶尔需要手工补全，这里保证它至少是可执行的。
    """
    url = db_url
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", url)
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


#: 加 source 列的那次迁移之前的版本。
BEFORE_SOURCE = "37e6774f05d5"

_SEED = text(
    "INSERT INTO evaluation_result "
    "(id, skill_id, content_hash, status, severity_counts, incomplete_scans, "
    " evaluated_at, created_at) "
    "VALUES ('r1', 'group/repo', 'sha256:x', 'passed', '{}', '[]', "
    " '2026-09-01 00:00:00', '2026-09-01 00:00:00')"
)


def test_existing_rows_get_the_backfill_source(db_url, monkeypatch):
    """存量行必须都拿到一个来源——``source`` 是 NOT NULL，漏掉一行就升不上去。

    填成哪个来源是测试阶段的一次性选择（见迁移里的 BACKFILL_SOURCE），
    这里只钉住"填上了"。
    """
    monkeypatch.setenv("SKILLPRISM_DATABASE_URL", db_url)
    reset_settings()

    cfg = _alembic_config()
    engine = create_engine(db_url)
    try:
        command.upgrade(cfg, BEFORE_SOURCE)
        with engine.begin() as conn:
            conn.execute(_SEED)

        command.upgrade(cfg, "head")

        with engine.connect() as conn:
            sources = conn.execute(text("SELECT source FROM evaluation_result")).scalars().all()
    finally:
        engine.dispose()
        reset_settings()

    # 填成哪个来源是迁移里的一次性选择，这里不复述它，只要求是个合法取值。
    assert sources and all(s in set(ContentSource) for s in sources)
