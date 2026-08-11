from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import AlertEventRead
from shared.database import get_db
from shared.models import AlertEvent, AlertRule

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertEventRead])
async def list_alerts(
    product_id: int | None = None,
    status: Literal["open", "resolved"] | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AlertEvent).order_by(AlertEvent.triggered_at.desc())

    if product_id is not None:
        stmt = stmt.join(AlertRule, AlertRule.id == AlertEvent.alert_rule_id).where(
            AlertRule.product_id == product_id
        )
    if status == "open":
        stmt = stmt.where(AlertEvent.resolved_at.is_(None))
    elif status == "resolved":
        stmt = stmt.where(AlertEvent.resolved_at.isnot(None))

    return (await db.execute(stmt)).scalars().all()
