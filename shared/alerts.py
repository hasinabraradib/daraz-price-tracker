"""Alert evaluation: runs once per successful scrape, right after the
worker writes a PriceSnapshot (see worker_app/main.py::_handle_success).
For that product, it looks at every active AlertRule, decides whether the
rule's condition currently holds, and opens/resolves AlertEvents and sends
notifications accordingly.

Four rule types, two different shapes of "condition":

- **Persisting conditions** (`undercut`, `price_below`, `back_in_stock`) —
  true or false right now, and can stay true across many scrapes in a row.
  These use the open/resolve dance below.
- **Edge-triggered conditions** (`price_drop_pct`) — only ever "true" on
  the exact scrape where a qualifying drop just happened, computed against
  the immediately prior snapshot. It can't meaningfully be "still true"
  next scrape unless a brand new qualifying drop occurs then too. These
  self-resolve immediately (`resolved_at == triggered_at`) and skip the
  open/resolve dance entirely — cooldown is the only repeat-guard they
  need.

Dedup strategy for persisting conditions — the part worth being precise about
===============================================================================
An AlertEvent is "open" while `resolved_at IS NULL`. On every evaluation
run, for each persisting-condition rule:

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
       - `undercut`: a DIFFERENT competitor is now the cheapest one, OR
         the undercut gap (our price − competitor price) moved by more
         than `ALERT_MATERIAL_CHANGE_PCT` since the open event's numbers.
       - `price_below`: the price moved more than `ALERT_MATERIAL_CHANGE_PCT`
         *further below the threshold* since the open event's trigger —
         measured as percent change in (threshold − price), not in the raw
         price, so it's "how much further under" that has to move, not
         just "how much the price moved" (a rich, above-threshold-anyway
         price wobbling doesn't apply here since the rule only fires when
         under threshold to begin with, but this framing matters once
         already under: a 1500 threshold with price going 1490 -> 1480 is
         a tiny raw move but doubles the gap).
       - `back_in_stock`: never reaches this branch with a material change
         — see below, it always returns "not material" here, which is
         exactly what keeps a continuous in-stock streak from re-notifying.
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

  `back_in_stock` fits the same four-case shape as `undercut`/`price_below`
  (unlike in the previous version of this module, which special-cased it
  as instantly self-resolving): "holds" tracks whether the product is
  *currently* in stock. The open event represents "there is a live
  in-stock streak the user has been told about." It resolves the moment
  the product goes out of stock again (case 2) — so the *next* restock
  after that is a genuinely fresh AlertEvent (case 3), not a reopen of the
  old one. The one wrinkle: a *fresh* trigger (case 3, no open event) only
  fires on a real observed flip (previous snapshot recorded out-of-stock,
  latest in-stock) — otherwise the very first snapshot ever taken while
  in stock would look like a "restock" with nothing to compare against.

  `price_drop_pct` doesn't participate in any of this — see the
  edge-triggered description above. "A NEW drop occurred that is itself
  larger than the threshold" falls out for free: the condition is defined
  entirely in terms of the two most recent snapshots, so it can only ever
  be true again if another qualifying drop actually happens; the price
  merely *staying* low changes nothing because "previous" has moved on to
  a newer snapshot each time.
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


def _materially_changed(rule: AlertRule, open_event: AlertEvent, condition: _Condition) -> bool:
    if rule.rule_type == "undercut":
        if condition.competitor_product_id != open_event.competitor_product_id:
            return True
        old_gap = (open_event.trigger_price or Decimal(0)) - (
            open_event.competitor_price or Decimal(0)
        )
        new_gap = (condition.trigger_price or Decimal(0)) - (
            condition.competitor_price or Decimal(0)
        )
        return _pct_change(old_gap, new_gap) > settings.alert_material_change_pct
    if rule.rule_type == "price_below":
        # How much *further under* the threshold we are now vs at the
        # open event's trigger — not the raw price's own percent change.
        threshold = rule.threshold_price or Decimal(0)
        old_gap = threshold - (open_event.trigger_price or Decimal(0))
        new_gap = threshold - (condition.trigger_price or Decimal(0))
        return _pct_change(old_gap, new_gap) > settings.alert_material_change_pct
    # back_in_stock: never material while continuously in stock (that's
    # what keeps it from re-notifying every scrape).
    # price_drop_pct: self-resolving, never reaches this function at all.
    return False


async def _evaluate_undercut(
    session: AsyncSession, rule: AlertRule, latest: PriceSnapshot
) -> _Condition:
    cheapest = await _cheapest_competitor(session, rule.product_id)
    if cheapest is None:
        return _Condition(holds=False)
    competitor_id, competitor_price = cheapest
    if competitor_price >= latest.price:
        return _Condition(holds=False)

    competitor = await session.get(Product, competitor_id)
    competitor_name = competitor.name if competitor is not None else f"product {competitor_id}"
    return _Condition(
        holds=True,
        trigger_price=latest.price,
        competitor_price=competitor_price,
        competitor_product_id=competitor_id,
        message=(
            f"Undercut: your price is {latest.price} {latest.currency}, "
            f"{competitor_name} is {competitor_price} {latest.currency}"
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


def _evaluate_price_drop_pct(
    rule: AlertRule, latest: PriceSnapshot, previous: PriceSnapshot | None
) -> _Condition:
    """Edge-triggered: only true on the exact scrape where the drop from
    the immediately prior snapshot exceeds threshold_pct. See module
    docstring for why this self-resolves instead of using open/resolve."""
    if rule.threshold_pct is None or previous is None or previous.price <= 0:
        return _Condition(holds=False)
    if latest.price >= previous.price:
        return _Condition(holds=False)

    drop_pct = float((previous.price - latest.price) / previous.price * 100)
    if drop_pct <= float(rule.threshold_pct):
        return _Condition(holds=False)

    return _Condition(
        holds=True,
        trigger_price=latest.price,
        message=(
            f"Price dropped {drop_pct:.1f}% (more than your {rule.threshold_pct}% "
            f"threshold): {previous.price} -> {latest.price} {latest.currency}"
        ),
    )


def _evaluate_back_in_stock(
    latest: PriceSnapshot, previous: PriceSnapshot | None, has_open_event: bool
) -> _Condition:
    if has_open_event:
        # Already alerting for the current in-stock streak. "holds" just
        # tracks whether to keep it open (True) or resolve it (False —
        # i.e. it went out of stock again).
        return _Condition(
            holds=latest.in_stock,
            trigger_price=latest.price,
            message=f"Back in stock at {latest.price} {latest.currency}",
        )
    # No open event: only a *fresh* trigger on a genuinely observed flip.
    # Without this gate, the very first snapshot ever recorded while
    # in_stock=True would look like a trigger with nothing to compare
    # against — but nothing actually flipped.
    if previous is not None and not previous.in_stock and latest.in_stock:
        return _Condition(
            holds=True,
            trigger_price=latest.price,
            message=f"Back in stock at {latest.price} {latest.currency}",
        )
    return _Condition(holds=False)


async def _deliver(
    product: Product, rule: AlertRule, event: AlertEvent, *, resolution: bool
) -> None:
    kind = "RESOLVED" if resolution else "ALERT"
    subject = f"[{kind}] {rule.rule_type} — {product.name}"
    prefix = "Resolved: " if resolution else ""
    body = f"{prefix}{product.name}\n{event.message}\n{product.daraz_url}"
    payload = {
        "rule_id": rule.id,
        "rule_type": rule.rule_type,
        "product_id": rule.product_id,
        "product_name": product.name,
        "product_url": product.daraz_url,
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
        open_event = await _open_event(session, rule.id)

        if rule.rule_type == "undercut":
            condition = await _evaluate_undercut(session, rule, latest)
        elif rule.rule_type == "price_below":
            condition = _evaluate_price_below(rule, latest)
        elif rule.rule_type == "back_in_stock":
            condition = _evaluate_back_in_stock(latest, previous, open_event is not None)
        elif rule.rule_type == "price_drop_pct":
            condition = _evaluate_price_drop_pct(rule, latest, previous)
            if condition.holds:
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
                await _deliver(product, rule, event, resolution=False)
            continue
        else:
            continue

        if not condition.holds:
            if open_event is not None:
                open_event.resolved_at = now
                touched.append(open_event)
                await _deliver(product, rule, open_event, resolution=True)
            continue

        if open_event is not None and not _materially_changed(rule, open_event, condition):
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
        await _deliver(product, rule, event, resolution=False)

    await session.commit()
    return touched
