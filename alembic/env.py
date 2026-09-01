"""Alembic 环境配置。

两处刻意的设置：

- **数据库地址从服务配置读**，不写在 alembic.ini 里。生产的连接串带凭据，
  不该进仓库；而且这样迁移和服务永远指向同一个库，不会跑错。
- **``render_as_batch=True``**。SQLite 的 ALTER TABLE 能力很弱，改列类型、
  加约束都不支持。batch 模式会建新表、拷数据、换名来绕过。不开这个选项，
  很多迁移在 SQLite 上会直接失败。切到 PostgreSQL 后它是无害的。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from skill_eval_service.config import get_settings
from skill_eval_service.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的对比基准
target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """生成 SQL 而不连库（alembic upgrade --sql）。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
