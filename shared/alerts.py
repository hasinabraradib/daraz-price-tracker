"""Alert evaluation: runs once per successful scrape, right after the
worker writes a PriceSnapshot (see worker_app/main.py::_handle_success).
For that product, it looks at every active AlertRule, decides whether the
rule's condition currently holds, and opens/resolves AlertEvents and sends
notifications accordingly.

Dedup strategy — the part worth being precise about
=====================================================
An AlertEvent is "open" while `resolved_at IS NULL`. On every evaluation
run, for each rule:

  1. Compute whether the condition holds *right now*, from current data.
     Never inferred from AlertEvent history — always recomputed fresh.

  2. Condition does NOT hold:
     - If there's an open AlertEvent for this rule, resolve it
       (`resolved_at = now`) and send a best-effort resolution notice.
     - Otherwise, nothing to do.

  3. Condition holds, and there's NO open AlertEvent for this rule:
     - Fresh trigger. Subject to cooldown (below), open a new AlertEvent
       and notify.

  4. Condition holds, and there IS an open AlertEvent for this rule — the
     interesting case. We do NOT want to notify every single scrape just
     because "still true; nothing new to say". A new AlertEvent (closing
     the old one first) only opens if something *material* changed:
       - undercut: a DIFFERENT competitor is now the cheapest one, OR the
         undercut gap (our price − competitor price) moved by more than
         `ALERT_MATERIAL_CHANGE_PCT` since the open event's numbers.
       - price_below: the trigger price moved by more than
         `ALERT_MATERIAL_CHANGE_PCT` since the open event's trigger_price
         (a further price drop is news; sitting flat under the threshold
         isn't).
       - back_in_stock: doesn't reach this branch at all — see below.
     If nothing material changed, we touch nothing: no new row, no
     notification, the open event stays exactly as it was.

  Cooldown (`ALERT_COOLDOWN_MINUTES`) is a second, independent guard,
  checked before any mutation in cases 3 and 4: even a fresh or
  materially-changed trigger is suppressed if this rule already opened an
  AlertEvent within the cooldown window. This protects against a price
  that oscillates several times in a few minutes — each swing might be
  "material", but nobody wants a flood. Checking cooldown *before*
  resolving the old event (in case 4) matters: if we're in cooldown, we
  leave the existing open event untouched and just try again next run,
  rather than resolving it with nothing to replace it.

  back_in_stock is different in kind from the other two: it's an
  edge-triggered event (the instant in_stock flips False -> True), not a
  condition you can re-check "does it still hold" on later. Each firing
  creates an AlertEvent that is immediately self-resolved
  (`resolved_at == triggered_at`) — there's no persisting state to dedup
  against, just repeat *flips*, which cooldown alone guards against.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import settings
from shared.models import AlertEvent, AlertRule, PriceSnapshot, Product, ProductCompetitor
from shared.notifiers import NotifyError, send_notification

logger = logging.getLogger("alerts")


@dataclass
class _Condition:
    holds: bool
    trigger_price: Decimal | None = None
    competitor_price: Decimal | None = None
    competitor_product_id: int | None = None
    message: str = ""


async def _latest_snapshot(session: AsyncSession, product_id: int) -> PriceSnapshot | None:
    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.product_id == product_id)
        .order_by(PriceSnapshot.scraped_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _two_latest_snapshots(
    session: AsyncSession, product_id: int
) -> tuple[PriceSnapshot | None, PriceSnapshot | None]:
    stmt = (
        select(PriceSnapshot)
        .where(PriceSnapshot.product_id == product_id)
        .order_by(PriceSnapshot.scraped_at.desc())
        .limit(2)
    )
    rows = (await session.execute(stmt)).scalars().all()
    latest = rows[0] if len(rows) > 0 else None
    previous = rows[1] if len(rows) > 1 else None
    return latest, previous


async def _cheapest_competitor(
    session: AsyncSession, product_id: int
) -> tuple[int, Decimal] | None:
    """Among this product's linked competitors, the one with the lowest
    latest price. None if there are no linked competitors, or none of them
    has a snapshot yet."""
    competitor_ids = (
        await session.execute(
            select(ProductCompetitor.competitor_product_id).where(
                ProductCompetitor.product_id == product_id
            )
        )
    ).scalars().all()

    best: tuple[int, Decimal] | None = None
    for competitor_id in competitor_ids:
        snapshot = await _latest_snapshot(session, competitor_id)
        if snapshot is None:
            continue
        if best is None or snapshot.price < best[1]:
            best = (competitor_id, snapshot.price)
    return best


async def _open_event(session: AsyncSession, rule_id: int) -> AlertEvent | None:
    stmt = (
        select(AlertEvent)
        .where(AlertEvent.alert_rule_id == rule_id, AlertEvent.resolved_at.is_(None))
        .order_by(AlertEvent.triggered_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _most_recent_event(session: AsyncSession, rule_id: int) -> AlertEvent | None:
    stmt = (
        select(AlertEvent)
        .where(AlertEvent.alert_rule_id == rule_id)
        .order_by(AlertEvent.triggered_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


def _in_cooldown(most_recent: AlertEvent | None, now: datetime) -> bool:
    if most_recent is None:
        return False
    elapsed_minutes = (now - most_recent.triggered_at).total_seconds() / 60
    return elapsed_minutes < settings.alert_cooldown_minutes


def _pct_change(old_value: Decimal, new_value: Decimal) -> float:
    """Percent change of new relative to old. A move away from exactly
    zero is always treated as material."""
    old_f, new_f = float(old_value), float(new_value)
    if old_f == 0:
        return 0.0 if new_f == 0 else float("inf")
    return abs(new_f - old_f) / abs(old_f) * 100


def _materially_changed(rule_type: str, open_event: AlertEvent, condition: _Condition) -> bool:
    if rule_type == "undercut":
        if condition.competitor_product_id != open_event.competitor_product_id:
            return True
        old_gap = (open_event.trigger_price or Decimal(0)) - (
            open_event.competitor_price or Decimal(0)
        )
        new_gap = (condition.trigger_price or Decimal(0)) - (
            condition.competitor_price or Decimal(0)
        )
        return _pct_change(old_gap, new_gap) > settings.alert_material_change_pct
    if rule_type == "price_below":
        return (
            _pct_change(open_event.trigger_price, condition.trigger_price)
            > settings.alert_material_change_pct
        )
    return False  # back_in_stock never reaches this branch


async def _evaluate_undercut(
    session: AsyncSession, rule: AlertRule, latest: PriceSnapshot
) -> _Condition:
    cheapest = await _cheapest_competitor(session, rule.product_id)
    if cheapest is None:
        return _Condition(holds=False)
    competitor_id, competitor_price = cheapest
    if competitor_price >= latest.price:
        return _Condition(holds=False)
    return _Condition(
        holds=True,
        trigger_price=latest.price,
        competitor_price=competitor_price,
        competitor_product_id=competitor_id,
        message=(
            f"Undercut: your price is {latest.price} {latest.currency}, "
            f"competitor product {competitor_id} is {competitor_price} {latest.currency}"
        ),
    )


def _evaluate_price_below(rule: AlertRule, latest: PriceSnapshot) -> _Condition:
    if rule.threshold_price is None or latest.price >= rule.threshold_price:
        return _Condition(holds=False)
    return _Condition(
        holds=True,
        trigger_price=latest.price,
        message=(
            f"Price dropped below your threshold of {rule.threshold_price} "
            f"{latest.currency}: now {latest.price} {latest.currency}"
        ),
    )


def _evaluate_back_in_stock(
    latest: PriceSnapshot, previous: PriceSnapshot | None
) -> _Condition:
    if previous is None or previous.in_stock or not latest.in_stock:
        return _Condition(holds=False)
    return _Condition(
        holds=True,
        trigger_price=latest.price,
        message=f"Back in stock at {latest.price} {latest.currency}",
    )


async def _deliver(rule: AlertRule, event: AlertEvent, *, resolution: bool) -> None:
    kind = "RESOLVED" if resolution else "ALERT"
    subject = f"[{kind}] {rule.rule_type} — product {rule.product_id}"
    body = f"Resolved: {event.message}" if resolution else event.message
    payload = {
        "rule_id": rule.id,
        "rule_type": rule.rule_type,
        "product_id": rule.product_id,
        "resolved": resolution,
        "trigger_price": str(event.trigger_price) if event.trigger_price is not None else None,
        "competitor_price": (
            str(event.competitor_price) if event.competitor_price is not None else None
        ),
        "competitor_product_id": event.competitor_product_id,
        "message": event.message,
    }
    try:
        await send_notification(rule.channel, rule.destination, subject, body, payload)
        if not resolution:
            event.delivery_status = "sent"
            event.delivery_error = None
    except NotifyError as exc:
        logger.warning("notification failed for rule=%s: %s", rule.id, exc)
        if not resolution:
            event.delivery_status = "failed"
            event.delivery_error = str(exc)


async def evaluate_alerts(session: AsyncSession, product_id: int) -> list[AlertEvent]:
    """Evaluate every active AlertRule for `product_id`, open/resolve
    AlertEvents per the dedup strategy above, and send notifications.
    Returns the AlertEvents created or resolved this run (mainly useful
    for tests/inspection)."""
    product = await session.get(Product, product_id)
    if product is None:
        return []

    latest, previous = await _two_latest_snapshots(session, product_id)
    if latest is None:
        return []

    rules = (
        await session.execute(
            select(AlertRule).where(
                AlertRule.product_id == product_id, AlertRule.is_active.is_(True)
            )
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    touched: list[AlertEvent] = []

    for rule in rules:
        if rule.rule_type == "undercut":
            condition = await _evaluate_undercut(session, rule, latest)
        elif rule.rule_type == "price_below":
            condition = _evaluate_price_below(rule, latest)
        elif rule.rule_type == "back_in_stock":
            condition = _evaluate_back_in_stock(latest, previous)
        else:
            continue

        open_event = await _open_event(session, rule.id)

        if not condition.holds:
            if open_event is not None:
                open_event.resolved_at = now
                touched.append(open_event)
                await _deliver(rule, open_event, resolution=True)
            continue

        if rule.rule_type == "back_in_stock":
            most_recent = await _most_recent_event(session, rule.id)
            if _in_cooldown(most_recent, now):
                continue
            event = AlertEvent(
                alert_rule_id=rule.id,
                triggered_at=now,
                resolved_at=now,  # edge-triggered: self-resolves immediately
                trigger_price=condition.trigger_price,
                message=condition.message,
                delivery_status="pending",
            )
            session.add(event)
            await session.flush()
            touched.append(event)
            await _deliver(rule, event, resolution=False)
            continue

        if open_event is not None and not _materially_changed(
            rule.rule_type, open_event, condition
        ):
            continue  # still open, nothing new to say

        most_recent = await _most_recent_event(session, rule.id)
        if _in_cooldown(most_recent, now):
            continue  # leave any open event untouched; try again next run

        if open_event is not None:
            open_event.resolved_at = now
            touched.append(open_event)

        event = AlertEvent(
            alert_rule_id=rule.id,
            triggered_at=now,
            resolved_at=None,
            trigger_price=condition.trigger_price,
            competitor_price=condition.competitor_price,
            competitor_product_id=condition.competitor_product_id,
            message=condition.message,
            delivery_status="pending",
        )
        session.add(event)
        await session.flush()
        touched.append(event)
        await _deliver(rule, event, resolution=False)

    await session.commit()
    return touched
