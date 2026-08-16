from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_owned_product_or_404, get_owner_email
from app.schemas import AlertRuleCreate, AlertRuleRead
from shared.database import get_db
from shared.models import AlertRule, Product

router = APIRouter(prefix="/products", tags=["alert-rules"])


@router.post(
    "/{product_id}/alert-rules",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_alert_rule(
    product_id: int,
    payload: AlertRuleCreate,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
    owner_email: str | None = Depends(get_owner_email),
):
    rule = AlertRule(
        product_id=product_id,
        rule_type=payload.rule_type,
        threshold_price=payload.threshold_price,
        threshold_pct=payload.threshold_pct,
        channel=payload.channel,
        destination=payload.destination,
        owner_email=owner_email,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/{product_id}/alert-rules", response_model=list[AlertRuleRead])
async def list_alert_rules(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
):
    stmt = (
        select(AlertRule)
        .where(AlertRule.product_id == product_id)
        .order_by(AlertRule.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.delete(
    "/{product_id}/alert-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_alert_rule(
    product_id: int,
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    _owned: Product = Depends(get_owned_product_or_404),
):
    stmt = select(AlertRule).where(AlertRule.id == rule_id, AlertRule.product_id == product_id)
    rule = (await db.execute(stmt)).scalars().first()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert rule not found")
    await db.delete(rule)
    await db.commit()
