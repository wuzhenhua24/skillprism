"""``db_url`` 夹具自身的正确性。

这组测试刻意**不需要 PostgreSQL**。夹具的 PG 分支曾经从未跑通过：
``str(URL)`` 把密码掩码成 ``***``，所有用例都以
"password authentication failed" 报错，而密码是对的。开发机默认跑 SQLite，
那条分支没有密码，于是这个 bug 在本地永远不会显形。

所以回归测试必须在没有 PG 的机器上也能跑——否则它和当初的盲区一模一样。
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

from tests.conftest import temp_db_url

BASE = "postgresql+psycopg://skillprism:pa55:w0rd@db.internal:5432/postgres"


def test_password_survives_url_rewriting():
    """密码不能在改库名的过程中被掩码掉。"""
    url = temp_db_url(BASE, "ses_test_abc123")

    assert "***" not in url, "密码被掩码了，拿这个串去连必然认证失败"
    assert make_url(url).password == "pa55:w0rd"


def test_only_the_database_is_changed():
    """除了库名，连接串的其他部分都要原样保留。"""
    before, after = make_url(BASE), make_url(temp_db_url(BASE, "ses_test_abc123"))

    assert after.database == "ses_test_abc123"
    assert (after.drivername, after.username, after.host, after.port) == (
        before.drivername,
        before.username,
        before.host,
        before.port,
    )


def test_password_free_url_is_untouched():
    """没有密码的连接串（本地 socket + peer 认证）不该被加工出个 None 来。"""
    url = temp_db_url("postgresql+psycopg://skillprism@/postgres", "ses_test_x")

    assert make_url(url).password is None
    assert "None" not in url
