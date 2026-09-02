"""触发接口带上登记名与版本；下载移出提交路径

三处改动同源，都来自"触发接口只声明评哪个 skill，内容由 worker 去下"：

- ``evaluation_task.skill_name``：物化目录名的来源。此前取 skill_id 的最后
  一段，而管理系统的 skill_id 是纯数字的资源 ID，会让
  SCHEMA.name_consistency 对**每个** skill 都报一条 HIGH。
- ``evaluation_task.content_hash`` 放开 NOT NULL：入队时还没下载内容，
  此刻给不出真实的 hash。
- ``evaluation_task.force``：缓存判定随下载一起挪到了 worker，
  "强制重跑"这个意图必须随任务落库才传得过去。
- ``evaluation_result.skill_version``：结果要能自述"这是 v2.0.0 的结论"，
  否则界面只拿得到一串 content_hash。
- ``evaluation_result.policy_file_hash``：资源 ID 每次上传都变，结论复用只能
  按内容找，而按内容找必须能判断策略有没有变过。报告里的 policy_digest 是
  上游算的、跑完才知道，判不了"要不要跑"。

Revision ID: 3bf94a5f050e
Revises: d3cb3bc90ca1
Create Date: 2026-09-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3bf94a5f050e'
down_revision: Union[str, Sequence[str], None] = 'd3cb3bc90ca1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.add_column(sa.Column('skill_version', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('policy_file_hash', sa.String(length=80), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_evaluation_result_policy_file_hash'), ['policy_file_hash'], unique=False
        )

    with op.batch_alter_table('evaluation_task', schema=None) as batch_op:
        batch_op.add_column(sa.Column('skill_name', sa.String(length=255), nullable=True))
        # autogenerate 给的是不带默认值的 NOT NULL，存量表上会直接失败。
        # server_default 只为这一次迁移存在，模型里不需要它。
        batch_op.add_column(
            sa.Column('force', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.alter_column('content_hash',
               existing_type=sa.VARCHAR(length=80),
               nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    # 回滚前先填掉 NULL，否则恢复 NOT NULL 会失败。
    # 这些是尚未下载内容的排队任务，旧代码在提交时就算好 hash、
    # worker 处理时又会以实际取到的内容覆盖，空串只会存在一瞬。
    # 用删除来清理会丢掉待评的任务，代价更大。
    op.execute("UPDATE evaluation_task SET content_hash = '' WHERE content_hash IS NULL")

    with op.batch_alter_table('evaluation_task', schema=None) as batch_op:
        batch_op.alter_column('content_hash',
               existing_type=sa.VARCHAR(length=80),
               nullable=False)
        batch_op.drop_column('force')
        batch_op.drop_column('skill_name')

    with op.batch_alter_table('evaluation_result', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_evaluation_result_policy_file_hash'))
        batch_op.drop_column('policy_file_hash')
        batch_op.drop_column('skill_version')
