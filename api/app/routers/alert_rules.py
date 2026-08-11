from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import AlertRuleCreate, AlertRuleRead
from shared.database import get_db
from shared.models import AlertRule, Product

router = APIRouter(prefix="/products", tags=["alert-rules"])


async def _get_product_or_404(db: AsyncSession, product_id: int) -> Product:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="product not found")
    return product


@router.post(
    "/{product_id}/alert-rules",
    response_model=AlertRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_alert_rule(
    product_id: int, payload: AlertRuleCreate, db: AsyncSession = Depends(get_db)
):
    await _get_product_or_404(db, product_id)

    rule = AlertRule(
        product_id=product_id,
        rule_type=payload.rule_type,
        threshold_price=payload.threshold_price,
        channel=payload.channel,
        destination=payload.destination,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/{product_id}/alert-rules", response_model=list[AlertRuleRead])
async def list_alert_rules(product_id: int, db: AsyncSession = Depends(get_db)):
    await _get_product_or_404(db, product_id)

    stmt = (
        select(AlertRule)
        .where(AlertRule.product_id == product_id)
        .order_by(AlertRule.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.delete(
    "/{product_id}/alert-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_alert_rule(product_id: int, rule_id: int, db: AsyncSession = Depends(get_db)):
    stmt = select(AlertRule).where(AlertRule.id == rule_id, AlertRule.product_id == product_id)
    rule = (await db.execute(stmt)).scalars().first()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert rule not found")
    await db.delete(rule)
    await db.commit()
