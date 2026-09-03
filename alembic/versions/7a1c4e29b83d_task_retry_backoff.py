"""任务重试退避：evaluation_task.next_attempt_at

内容下载失败此前会以轮询间隔无限重试：异常逃出 process_task，session_scope
回滚掉 claim_next 写的 running/attempts，任务原样退回 queued。既不计次也不
留错误信息，外部只看得到"一直排队中"。

改成走 requeue 之后重试有了次数上限，但没有退避的话 max_attempts 会在几秒
内烧光——管理系统重启一次就不止几秒，等于把上游的短暂故障变成任务的永久
失败。所以补这一列：退避到点之前 claim_next 不领这条任务。

必须落库而不是留在 worker 内存里，worker 随时会重启。

Revision ID: 7a1c4e29b83d
Revises: 3bf94a5f050e
Create Date: 2026-09-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7a1c4e29b83d'
down_revision: Union[str, Sequence[str], None] = '3bf94a5f050e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('evaluation_task', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('evaluation_task', schema=None) as batch_op:
        batch_op.drop_column('next_attempt_at')
