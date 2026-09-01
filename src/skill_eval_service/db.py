"""数据库连接与会话。开发用 SQLite，生产替换 SES_DATABASE_URL 即可。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from skill_eval_service.config import get_settings
from skill_eval_service.models import Base

_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        if url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


def init_db() -> None:
    """直接按模型建表。**仅供测试与本地快速起库使用。**

    生产路径是 ``alembic upgrade head``。不要在服务启动时调用它——
    ``create_all`` 只建缺失的表、从不修改已存在的表，一旦模型新增了表
    而迁移没跟上，它会把表建出来、掩盖掉本该暴露的迁移缺失。
    """
    Base.metadata.create_all(get_engine())


def schema_is_ready() -> bool:
    """判断库是否已经迁移过。

    用主表是否存在来判断，而不是查 alembic_version——后者在
    ``create_all`` 建起来的测试库里并不存在。
    """
    from sqlalchemy import inspect

    return inspect(get_engine()).has_table("evaluation_result")


SCHEMA_NOT_READY_HINT = (
    "数据库结构尚未初始化。请先执行迁移：\n"
    "    alembic upgrade head\n"
    "（服务不会自动建表——自动建表会掩盖迁移缺失，见 init_db 的说明。）"
)


def reset_engine() -> None:
    """丢弃缓存的 engine 与 session 工厂，下次调用按当前配置重建。

    供测试在切换 SES_DATABASE_URL 后调用；生产代码不应使用。
    """
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
