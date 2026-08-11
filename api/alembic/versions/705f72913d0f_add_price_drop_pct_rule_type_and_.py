"""add price_drop_pct rule type and threshold_pct column

Revision ID: 705f72913d0f
Revises: 15ad79c1ef72
Create Date: 2026-08-11 08:20:15.577196+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '705f72913d0f'
down_revision: Union[str, None] = '15ad79c1ef72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'alert_rules', sa.Column('threshold_pct', sa.Numeric(precision=6, scale=2), nullable=True)
    )
    # Alembic's autogenerate doesn't detect CHECK constraint *body* changes
    # (only added/removed constraints), so this needs to be done by hand:
    # drop and recreate with 'price_drop_pct' added to the allowed set.
    op.drop_constraint('ck_alert_rules_rule_type', 'alert_rules', type_='check')
    op.create_check_constraint(
        'ck_alert_rules_rule_type',
        'alert_rules',
        "rule_type IN ('undercut', 'price_below', 'price_drop_pct', 'back_in_stock')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_alert_rules_rule_type', 'alert_rules', type_='check')
    op.create_check_constraint(
        'ck_alert_rules_rule_type',
        'alert_rules',
        "rule_type IN ('undercut', 'price_below', 'back_in_stock')",
    )
    op.drop_column('alert_rules', 'threshold_pct')
