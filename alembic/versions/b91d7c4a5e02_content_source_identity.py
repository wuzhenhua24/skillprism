"""内容来源进入身份：task 与 result 都带 source

``skill_id`` 只在**一个来源内部**唯一。管理系统的资源 ID ``42`` 和 GitLab 的
数字项目 ID ``42`` 是同一个字符串，两种接入并存时会在 ``uq_skill_content``
上互相覆盖、在排队去重时互相折叠——两种都没有任何症状，只是结论悄悄换成了
另一个 skill 的。所以来源必须落库，并进结果的唯一键。

存量行统一回填成 ``BACKFILL_SOURCE``（见下）。现在还在测试阶段，库里没有
要保的结论，所以不做"按部署配置判断"或"要求人工指定"那一套——真需要区分
的时候重建库最快，或者升级后一条 ``UPDATE`` 改掉。

Revision ID: b91d7c4a5e02
Revises: 37e6774f05d5
Create Date: 2026-09-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b91d7c4a5e02'
down_revision: Union[str, Sequence[str], None] = '37e6774f05d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('evaluation_task', 'evaluation_result')

#: 存量行填哪个来源。测试阶段一律标成主要接入方式；不对的话改这一行，
#: 或者升级后 ``UPDATE evaluation_result SET source = '...'``。
BACKFILL_SOURCE = 'gitlab'


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            # server_default 只为回填存量行而存在，下面立刻去掉：来源是调用方
            # 声明的东西，漏传时该报错，不该悄悄变成某一个默认值。
            batch_op.add_column(
                sa.Column('source', sa.String(length=16), nullable=False,
                          server_default=BACKFILL_SOURCE)
            )
            batch_op.create_index(batch_op.f(f'ix_{table}_source'), ['source'], unique=False)

    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column('source', existing_type=sa.String(length=16),
                                  existing_nullable=False, server_default=None)

    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.drop_constraint('uq_skill_content', type_='unique')
        batch_op.create_unique_constraint(
            'uq_skill_content', ['source', 'skill_id', 'content_hash']
        )


def downgrade() -> None:
    """Downgrade schema。

    两个来源下的同名 skill_id 评过同一份内容时，收窄唯一键会撞。测试阶段
    不为这种情况写迁移逻辑：真撞上了数据库会直接报唯一键冲突，届时人工
    决定留哪条。
    """
    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.drop_constraint('uq_skill_content', type_='unique')
        batch_op.create_unique_constraint('uq_skill_content', ['skill_id', 'content_hash'])

    for table in TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f'ix_{table}_source'))
            batch_op.drop_column('source')
