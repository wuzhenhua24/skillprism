"""测试夹具。

数据库地址由 ``db_url`` 夹具统一提供：默认 SQLite（每个测试一个临时文件，
天然隔离、跑得快），设置 ``SES_TEST_DATABASE_URL`` 后改用 PostgreSQL。

为什么要能跑两种库：生产用 PG、开发用 SQLite，两者的行为并不一致。最要紧的
是 ``queue.claim_next`` 里的 ``SKIP LOCKED`` 分支——在 SQLite 上它必然抛异常、
永远走退化路径，也就是说**那条生产真正执行的代码在本地一次都没跑过**。
只有把同一套测试跑在 PG 上才能覆盖它。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

FIXTURES = Path(__file__).parent / "fixtures"

#: 设置本环境变量即让整套测试跑在 PostgreSQL 上，例如
#: postgresql+psycopg://user:pass@testbox:5432/postgres
#: 指向的库只用来建/删临时测试库，本身不会被改动。
PG_ENV_VAR = "SES_TEST_DATABASE_URL"


@pytest.fixture
def upstream_report() -> dict:
    """一份真实的 SkillEvaluator JSON 报告，用于锁定 adapter 的解析契约。"""
    return json.loads((FIXTURES / "upstream_report_passed.json").read_text(encoding="utf-8"))


def _admin_engine(base_url: str):
    # 建库/删库不能在事务里执行，必须 AUTOCOMMIT。
    return create_engine(base_url, isolation_level="AUTOCOMMIT")


@pytest.fixture
def db_url(tmp_path):
    """本次测试可用的数据库地址。

    SQLite 下是 tmp_path 里的一个文件；PostgreSQL 下是一个随机命名的临时库，
    测试结束即删除——这样测试机上多人并跑也不会互相污染。
    """
    base = os.environ.get(PG_ENV_VAR)
    if not base:
        yield f"sqlite:///{tmp_path / 'test.db'}"
        return

    name = f"ses_test_{uuid.uuid4().hex[:12]}"
    engine = _admin_engine(base)
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()

    try:
        yield str(make_url(base).set(database=name))
    finally:
        engine = _admin_engine(base)
        try:
            with engine.connect() as conn:
                # FORCE 需要 PostgreSQL 13+，用于踢掉残留连接。
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        finally:
            engine.dispose()


@pytest.fixture
def is_postgres(db_url) -> bool:
    return db_url.startswith("postgresql")
