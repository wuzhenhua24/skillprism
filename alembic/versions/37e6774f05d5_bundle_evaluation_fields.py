"""一组耦合 skill：bundle 标记、上下文指纹，以及 validator 的 legacy errors

一套研发工作流会拆成几个 skill，彼此之间有跨目录引用。单独物化一个来评，
指向兄弟 skill 的相对链接全是死链——那是我们的物化方式造成的误报，不是
skill 的问题（实测：单独评报 `Dead link in SKILL.md: ../test-gen/SKILL.md`
并因此判 fail，整套一起评则该项通过）。

``evaluation_task.bundle`` 标记"这个任务评的是一组"，由触发方声明。不看内容
形态推断：skill_id 少写一层子目录就会静默变成评另一批东西。

``evaluation_result.context_hash`` 是评这条结论时所在 bundle 的整体指纹，
单独评的为 NULL，进复用判据。一组里改了 A，B 的字节没变但结论可能变——B
引用 A 的文件，A 改名或删了 B 就多出死链。只按 content_hash 复用会把过期
结论当成有效结论发出去，而且看起来完全正常。

``evaluation_detail.errors`` 顺带补上：上游用两个通道报问题，结构化的
findings 和只有一句话的 legacy.errors。死链走的是后者，此前完全没读——一个
因为死链失败的 validator 在对外结果里是 passed=false 且 findings 为空，界面
只能显示"某项没通过"，说不出为什么。跨 skill 引用正是靠死链体现的，这条不补
的话本次改动的效果在产品上根本看不见。

两个 JSON 列都带 server_default：存量表非空时，加一个 NOT NULL 又没有默认值
的列会直接失败。``bundle`` 同理。

Revision ID: 37e6774f05d5
Revises: 7a1c4e29b83d
Create Date: 2026-09-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '37e6774f05d5'
down_revision: Union[str, Sequence[str], None] = '7a1c4e29b83d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.add_column(sa.Column('context_hash', sa.String(length=80), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_evaluation_result_context_hash'), ['context_hash'], unique=False
        )

    with op.batch_alter_table('evaluation_task', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('bundle', sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table('evaluation_detail', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('errors', sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('evaluation_detail', schema=None) as batch_op:
        batch_op.drop_column('errors')

    with op.batch_alter_table('evaluation_task', schema=None) as batch_op:
        batch_op.drop_column('bundle')

    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_evaluation_result_context_hash'))
        batch_op.drop_column('context_hash')
