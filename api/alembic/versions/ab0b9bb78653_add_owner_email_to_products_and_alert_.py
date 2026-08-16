"""add owner_email to products and alert_rules

Revision ID: ab0b9bb78653
Revises: 705f72913d0f
Create Date: 2026-08-13 09:40:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ab0b9bb78653'
down_revision: Union[str, None] = '705f72913d0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no default: existing rows get NULL ("no owner recorded"),
    # which the API treats as visible regardless of which X-Owner-Email
    # header a request sends — see shared/models.py's Product.owner_email
    # docstring and api/app/deps.py for why this is a soft label, not
    # access control.
    op.add_column('products', sa.Column('owner_email', sa.String(length=320), nullable=True))
    op.add_column('alert_rules', sa.Column('owner_email', sa.String(length=320), nullable=True))


def downgrade() -> None:
    op.drop_column('alert_rules', 'owner_email')
    op.drop_column('products', 'owner_email')
