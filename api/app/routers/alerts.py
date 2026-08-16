from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_owner_email
from app.schemas import AlertEventRead, TestAlertRequest, TestAlertResponse
from shared.database import get_db
from shared.models import AlertEvent, AlertRule
from shared.notifiers import NotifyError, send_notification

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post("/test-webhook", response_model=TestAlertResponse)
async def test_webhook(payload: TestAlertRequest):
    """Fires a real notification at `webhook_url` right now, with no
    Product/AlertRule/AlertEvent behind it — used by the frontend's setup
    flow so someone can confirm their Discord webhook actually works
    *before* they've added any product to trigger a real alert off of.
    Goes through the exact same send_notification() (Discord
    auto-detection, retries, metrics) real alerts use, just with a
    canned message instead of one built from a real price change."""
    subject = "Test alert — Daraz Price Tracker"
    body = (
        "This is a test alert from Daraz Price Tracker.\n"
        "If you can see this, your webhook is set up correctly."
    )
    payload_dict = {
        "rule_type": "test",
        "message": body,
    }
    try:
        await send_notification("webhook", payload.webhook_url, subject, body, payload_dict)
    except NotifyError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not deliver to that webhook: {exc}",
        ) from exc
    return TestAlertResponse(sent=True)


@router.get("", response_model=list[AlertEventRead])
async def list_alerts(
    product_id: int | None = None,
    status: Literal["open", "resolved"] | None = None,
    db: AsyncSession = Depends(get_db),
    owner_email: str | None = Depends(get_owner_email),
):
    stmt = select(AlertEvent).order_by(AlertEvent.triggered_at.desc())
    needs_rule_join = product_id is not None or owner_email is not None

    if needs_rule_join:
        stmt = stmt.join(AlertRule, AlertRule.id == AlertEvent.alert_rule_id)
    if product_id is not None:
        stmt = stmt.where(AlertRule.product_id == product_id)
    if owner_email is not None:
        # Same "no header = no filtering, unowned rows always visible"
        # rule as everywhere else — see app/deps.py.
        stmt = stmt.where(
            (AlertRule.owner_email == owner_email) | (AlertRule.owner_email.is_(None))
        )
    if status == "open":
        stmt = stmt.where(AlertEvent.resolved_at.is_(None))
    elif status == "resolved":
        stmt = stmt.where(AlertEvent.resolved_at.isnot(None))

    return (await db.execute(stmt)).scalars().all()
