"""上下文进入结论的唯一键：单评与成组评不再互相覆盖

``context_hash`` 是在 37e6774f05d5 随 bundle 一起加上的，当时给了普通索引，
**唯一键没动**——它还是 (source, skill_id, content_hash)。于是身份多了一维、
键没跟上，``save_result`` 的覆盖判定把两条**不同上下文**的结论当成同一条：

- 单评一个成员、再整组评一次：成员的字节没变，content_hash 相同，
  上下文不同所以复用不命中、真的重评，落库时按三元组找到单评那条**删掉**。
  之后按同一个 skill_id 查到的是"47 个兄弟在场时"评出来的结论——分数可能
  一模一样，只是死链没了。反向同样成立。
- 更常见的是根本不涉及单评：一个 bundle 改了一个成员重跑，**没改的那些
  成员** content_hash 不变、context_hash 变了，旧行全被删。字节变了的成员
  两条都在，没变的只剩一条——历史留不留取决于内容变没变，正好是反的。
  上一次那条任务的 ``results[]`` 于是静默少几条（按 context_hash 反查）。

改成两条**部分唯一索引**而不是一条四列唯一约束：``context_hash`` 可空，而
PostgreSQL 与 SQLite 的唯一约束里 NULL 互不相等，四列约束对单评那批行等于
没有约束。按上下文在不在切成两半，各自在自己那半边真的唯一。

存量数据不需要清理：新索引严格弱于老约束（老约束下不重复的行，在两条新
索引下也不会重复），升级不会撞。**降级则可能撞**——升级之后新写进来的行
可以合法地共用一个三元组，那时 downgrade 会因重复而失败。这是有意的：
回滚就是要丢掉一半结论，让它当场报错，比静默删掉强。

Revision ID: 9c2d5a71e4b8
Revises: b91d7c4a5e02
Create Date: 2026-09-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c2d5a71e4b8'
down_revision: Union[str, Sequence[str], None] = 'b91d7c4a5e02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SOLO_WHERE = sa.text('context_hash IS NULL')
BUNDLE_WHERE = sa.text('context_hash IS NOT NULL')


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.drop_constraint('uq_skill_content', type_='unique')

    op.create_index(
        'uq_skill_content_solo',
        'evaluation_result',
        ['source', 'skill_id', 'content_hash'],
        unique=True,
        sqlite_where=SOLO_WHERE,
        postgresql_where=SOLO_WHERE,
    )
    op.create_index(
        'uq_skill_content_bundle',
        'evaluation_result',
        ['source', 'skill_id', 'content_hash', 'context_hash'],
        unique=True,
        sqlite_where=BUNDLE_WHERE,
        postgresql_where=BUNDLE_WHERE,
    )


def downgrade() -> None:
    """Downgrade schema.

    升级之后写进来的行可能共用一个 (source, skill_id, content_hash)——同一个
    skill 单独评过、又在一组里评过就是这种情况。那时重建老约束会因重复而
    失败，需要人工决定丢哪条。不在这里替人删：删掉的是一条真实的结论。
    """
    op.drop_index('uq_skill_content_bundle', table_name='evaluation_result')
    op.drop_index('uq_skill_content_solo', table_name='evaluation_result')

    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.create_unique_constraint(
            'uq_skill_content', ['source', 'skill_id', 'content_hash']
        )
