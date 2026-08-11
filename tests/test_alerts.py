"""Tests for shared/alerts.py's rule evaluation and dedup strategy — see
that module's docstring for the full explanation of what "materially
changed" and "cooldown" mean and how they interact.

Notifications are always mocked here (`mock_notifications` fixture,
autouse) — no test in this suite makes a real SMTP or HTTP call.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from shared.alerts import evaluate_alerts
from shared.config import settings
from shared.models import AlertEvent, AlertRule, PriceSnapshot, Product, ProductCompetitor


@pytest.fixture(autouse=True)
def mock_notifications(monkeypatch):
    sent = []

    async def fake_send(channel, destination, subject, body, payload):
        sent.append(
            {"channel": channel, "destination": destination, "subject": subject, "payload": payload}
        )

    monkeypatch.setattr("shared.alerts.send_notification", fake_send)
    return sent


@pytest.fixture(autouse=True)
def default_alert_settings(monkeypatch):
    monkeypatch.setattr(settings, "alert_material_change_pct", 5.0)
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 60)


async def _make_product(db_session, name="Product") -> Product:
    product = Product(name=name, daraz_url=f"https://www.daraz.pk/{name.lower()}-i1-s1.html")
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def _add_snapshot(
    db_session, product_id: int, price: str, *, in_stock: bool = True
) -> PriceSnapshot:
    snapshot = PriceSnapshot(
        product_id=product_id,
        price=Decimal(price),
        currency="Rs",
        in_stock=in_stock,
        raw_title="x",
    )
    db_session.add(snapshot)
    await db_session.commit()
    await db_session.refresh(snapshot)
    return snapshot


async def _make_rule(db_session, product_id: int, rule_type: str, **kwargs) -> AlertRule:
    rule = AlertRule(
        product_id=product_id,
        rule_type=rule_type,
        channel="email",
        destination="buyer@example.com",
        **kwargs,
    )
    db_session.add(rule)
    await db_session.commit()
    await db_session.refresh(rule)
    return rule


# ---- each rule type fires correctly ----


@pytest.mark.asyncio
async def test_undercut_fires_when_competitor_cheaper(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()

    await _add_snapshot(db_session, competitor.id, "90.00")
    await _add_snapshot(db_session, product.id, "100.00")
    rule = await _make_rule(db_session, product.id, "undercut")

    events = await evaluate_alerts(db_session, product.id)

    assert len(events) == 1
    assert events[0].alert_rule_id == rule.id
    assert events[0].resolved_at is None
    assert events[0].trigger_price == Decimal("100.00")
    assert events[0].competitor_price == Decimal("90.00")
    assert events[0].competitor_product_id == competitor.id
    assert events[0].delivery_status == "sent"
    assert len(mock_notifications) == 1


@pytest.mark.asyncio
async def test_undercut_does_not_fire_when_no_competitor_is_cheaper(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()

    await _add_snapshot(db_session, competitor.id, "150.00")
    await _add_snapshot(db_session, product.id, "100.00")
    await _make_rule(db_session, product.id, "undercut")

    events = await evaluate_alerts(db_session, product.id)

    assert events == []
    assert mock_notifications == []


@pytest.mark.asyncio
async def test_price_below_fires(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "80.00")
    rule = await _make_rule(
        db_session, product.id, "price_below", threshold_price=Decimal("100.00")
    )

    events = await evaluate_alerts(db_session, product.id)

    assert len(events) == 1
    assert events[0].alert_rule_id == rule.id
    assert events[0].trigger_price == Decimal("80.00")
    assert len(mock_notifications) == 1


@pytest.mark.asyncio
async def test_price_below_does_not_fire_above_threshold(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "120.00")
    await _make_rule(db_session, product.id, "price_below", threshold_price=Decimal("100.00"))

    events = await evaluate_alerts(db_session, product.id)

    assert events == []
    assert mock_notifications == []


@pytest.mark.asyncio
async def test_back_in_stock_fires_on_flip_and_stays_open(db_session, mock_notifications):
    """Unlike the previous version of this rule (instant self-resolve),
    back_in_stock is now a persisting condition: the event stays open
    until the product goes out of stock again."""
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    rule = await _make_rule(db_session, product.id, "back_in_stock")

    events = await evaluate_alerts(db_session, product.id)

    assert len(events) == 1
    assert events[0].alert_rule_id == rule.id
    assert events[0].resolved_at is None
    assert len(mock_notifications) == 1


@pytest.mark.asyncio
async def test_back_in_stock_does_not_fire_without_a_prior_out_of_stock_snapshot(
    db_session, mock_notifications
):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    await _make_rule(db_session, product.id, "back_in_stock")

    events = await evaluate_alerts(db_session, product.id)

    assert events == []
    assert mock_notifications == []


@pytest.mark.asyncio
async def test_price_drop_pct_fires_on_qualifying_drop(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00")
    rule = await _make_rule(db_session, product.id, "price_drop_pct", threshold_pct=Decimal("10"))
    await evaluate_alerts(db_session, product.id)  # first-ever snapshot, no "previous" to drop from
    assert mock_notifications == []

    await _add_snapshot(db_session, product.id, "85.00")  # 15% drop, exceeds 10% threshold
    events = await evaluate_alerts(db_session, product.id)

    assert len(events) == 1
    assert events[0].alert_rule_id == rule.id
    assert events[0].trigger_price == Decimal("85.00")
    # edge-triggered: self-resolves immediately, no persisting open state
    assert events[0].resolved_at == events[0].triggered_at
    assert len(mock_notifications) == 1


@pytest.mark.asyncio
async def test_price_drop_pct_does_not_fire_on_small_drop(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00")
    await _make_rule(db_session, product.id, "price_drop_pct", threshold_pct=Decimal("10"))
    await evaluate_alerts(db_session, product.id)

    await _add_snapshot(db_session, product.id, "95.00")  # only a 5% drop
    events = await evaluate_alerts(db_session, product.id)

    assert events == []
    assert mock_notifications == []


@pytest.mark.asyncio
async def test_price_drop_pct_does_not_fire_on_price_increase(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00")
    await _make_rule(db_session, product.id, "price_drop_pct", threshold_pct=Decimal("10"))
    await evaluate_alerts(db_session, product.id)

    await _add_snapshot(db_session, product.id, "110.00")
    events = await evaluate_alerts(db_session, product.id)

    assert events == []
    assert mock_notifications == []


@pytest.mark.asyncio
async def test_price_drop_pct_price_staying_low_does_not_refire(
    db_session, mock_notifications, monkeypatch
):
    """"Not just the price staying low" (per the task spec): after a
    qualifying drop, holding steady at the new low price shouldn't refire
    — only a *new* qualifying drop (vs the immediately prior snapshot)
    should."""
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00")
    await _make_rule(db_session, product.id, "price_drop_pct", threshold_pct=Decimal("10"))
    await evaluate_alerts(db_session, product.id)

    await _add_snapshot(db_session, product.id, "85.00")  # 15% drop, fires
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1

    await _add_snapshot(db_session, product.id, "85.00")  # flat — no new drop
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1


@pytest.mark.asyncio
async def test_price_drop_pct_new_qualifying_drop_fires_again(
    db_session, mock_notifications, monkeypatch
):
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00")
    await _make_rule(db_session, product.id, "price_drop_pct", threshold_pct=Decimal("10"))
    await evaluate_alerts(db_session, product.id)

    await _add_snapshot(db_session, product.id, "85.00")  # 15% drop
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1

    await _add_snapshot(db_session, product.id, "70.00")  # another >10% drop
    second = await evaluate_alerts(db_session, product.id)

    assert len(second) == 1
    assert second[0].id != first[0].id  # a genuinely new event
    assert len(mock_notifications) == 2


@pytest.mark.asyncio
async def test_price_drop_pct_cooldown_respected(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00")
    await _make_rule(db_session, product.id, "price_drop_pct", threshold_pct=Decimal("10"))
    await evaluate_alerts(db_session, product.id)

    await _add_snapshot(db_session, product.id, "85.00")  # fires
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1

    # another qualifying drop, but within the default 60-minute cooldown
    await _add_snapshot(db_session, product.id, "70.00")
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1


# ---- back_in_stock (persisting condition: resolves on next out-of-stock) ----


@pytest.mark.asyncio
async def test_back_in_stock_does_not_refire_while_continuously_in_stock(
    db_session, mock_notifications
):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    await _make_rule(db_session, product.id, "back_in_stock")

    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1
    assert len(mock_notifications) == 1

    # still in stock on the next scrape — no new event, no new notification
    await _add_snapshot(db_session, product.id, "95.00", in_stock=True)
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1

    open_events = (
        await db_session.execute(select(AlertEvent).where(AlertEvent.resolved_at.is_(None)))
    ).scalars().all()
    assert len(open_events) == 1  # the original event, still open, untouched


@pytest.mark.asyncio
async def test_back_in_stock_resolves_when_it_goes_out_of_stock_again(
    db_session, mock_notifications
):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    await _make_rule(db_session, product.id, "back_in_stock")

    first = await evaluate_alerts(db_session, product.id)
    assert first[0].resolved_at is None
    assert len(mock_notifications) == 1

    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    second = await evaluate_alerts(db_session, product.id)

    assert len(second) == 1
    assert second[0].id == first[0].id
    assert second[0].resolved_at is not None
    assert len(mock_notifications) == 2  # original alert + resolution notice


@pytest.mark.asyncio
async def test_back_in_stock_retriggers_after_going_out_of_stock_and_back(
    db_session, mock_notifications, monkeypatch
):
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    await _make_rule(db_session, product.id, "back_in_stock")

    first = await evaluate_alerts(db_session, product.id)
    first_event_id = first[0].id

    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)  # resolves it
    resolved = await evaluate_alerts(db_session, product.id)
    assert resolved[0].id == first_event_id
    assert resolved[0].resolved_at is not None

    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)  # restocked again
    retriggered = await evaluate_alerts(db_session, product.id)

    assert len(retriggered) == 1
    assert retriggered[0].id != first_event_id  # a genuinely new row
    assert retriggered[0].resolved_at is None
    assert len(mock_notifications) == 3  # alert, resolution, fresh alert


@pytest.mark.asyncio
async def test_back_in_stock_cooldown_respected_on_retrigger(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    await _make_rule(db_session, product.id, "back_in_stock")

    await evaluate_alerts(db_session, product.id)
    assert len(mock_notifications) == 1

    await _add_snapshot(db_session, product.id, "100.00", in_stock=False)
    await evaluate_alerts(db_session, product.id)  # resolves, no cooldown gate on resolution

    # restocked again, but within the default 60-minute cooldown
    await _add_snapshot(db_session, product.id, "100.00", in_stock=True)
    events = await evaluate_alerts(db_session, product.id)

    assert events == []
    assert len(mock_notifications) == 2  # alert + resolution only, no third


# ---- dedup ----


@pytest.mark.asyncio
async def test_dedup_suppresses_repeat_when_nothing_materially_changed(
    db_session, mock_notifications
):
    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor.id, "90.00")
    await _add_snapshot(db_session, product.id, "100.00")
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1
    assert len(mock_notifications) == 1

    # same competitor, same price — nothing material changed
    await _add_snapshot(db_session, product.id, "100.00")
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1  # still just the one

    open_events = (
        await db_session.execute(select(AlertEvent).where(AlertEvent.resolved_at.is_(None)))
    ).scalars().all()
    assert len(open_events) == 1  # the original event, untouched


@pytest.mark.asyncio
async def test_dedup_different_competitor_becoming_cheapest_does_fire(
    db_session, mock_notifications, monkeypatch
):
    # Isolate the "different competitor" material-change path from
    # cooldown, which is covered on its own in
    # test_cooldown_suppresses_even_a_material_change below.
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    competitor_a = await _make_product(db_session, "CompetitorA")
    competitor_b = await _make_product(db_session, "CompetitorB")
    db_session.add_all(
        [
            ProductCompetitor(product_id=product.id, competitor_product_id=competitor_a.id),
            ProductCompetitor(product_id=product.id, competitor_product_id=competitor_b.id),
        ]
    )
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor_a.id, "90.00")
    await _add_snapshot(db_session, competitor_b.id, "95.00")
    await _add_snapshot(db_session, product.id, "100.00")
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1
    assert first[0].competitor_product_id == competitor_a.id

    # competitor B undercuts competitor A — a *different* cheapest competitor
    await _add_snapshot(db_session, competitor_b.id, "80.00")
    second = await evaluate_alerts(db_session, product.id)

    assert len(second) == 2  # old event resolved + new event opened
    resolved = [e for e in second if e.resolved_at is not None]
    opened = [e for e in second if e.resolved_at is None]
    assert len(resolved) == 1
    assert len(opened) == 1
    assert opened[0].competitor_product_id == competitor_b.id
    assert opened[0].competitor_price == Decimal("80.00")
    assert len(mock_notifications) == 2


@pytest.mark.asyncio
async def test_dedup_material_gap_widening_fires_again(db_session, mock_notifications, monkeypatch):
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor.id, "95.00")
    await _add_snapshot(db_session, product.id, "100.00")  # gap = 5
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1

    # same competitor, but the gap more than doubles (5 -> 20) — material
    await _add_snapshot(db_session, competitor.id, "80.00")
    second = await evaluate_alerts(db_session, product.id)

    assert len(second) == 2
    opened = [e for e in second if e.resolved_at is None][0]
    assert opened.competitor_price == Decimal("80.00")
    assert len(mock_notifications) == 2


@pytest.mark.asyncio
async def test_dedup_small_gap_change_does_not_fire(db_session, mock_notifications, monkeypatch):
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor.id, "95.00")
    await _add_snapshot(db_session, product.id, "100.00")  # gap = 5
    await evaluate_alerts(db_session, product.id)
    assert len(mock_notifications) == 1

    # gap moves from 5 to 5.10 — under the 5% material-change threshold
    await _add_snapshot(db_session, competitor.id, "94.90")
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1


@pytest.mark.asyncio
async def test_cooldown_suppresses_even_a_material_change(db_session, mock_notifications):
    """Default cooldown (60 min, unmodified) should block a re-fire even
    though the underlying change (a different, cheaper competitor) would
    otherwise be material — proving cooldown is an independent guard, not
    just a restatement of the material-change check."""
    product = await _make_product(db_session, "Mine")
    competitor_a = await _make_product(db_session, "CompetitorA")
    competitor_b = await _make_product(db_session, "CompetitorB")
    db_session.add_all(
        [
            ProductCompetitor(product_id=product.id, competitor_product_id=competitor_a.id),
            ProductCompetitor(product_id=product.id, competitor_product_id=competitor_b.id),
        ]
    )
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor_a.id, "90.00")
    await _add_snapshot(db_session, product.id, "100.00")
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1
    assert len(mock_notifications) == 1

    # a different, much cheaper competitor shows up — material, but within
    # the (default 60-minute) cooldown window
    await _add_snapshot(db_session, competitor_b.id, "50.00")
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1  # cooldown blocked it

    # the original event is left open and untouched
    open_events = (
        await db_session.execute(select(AlertEvent).where(AlertEvent.resolved_at.is_(None)))
    ).scalars().all()
    assert len(open_events) == 1
    assert open_events[0].competitor_product_id == competitor_a.id


@pytest.mark.asyncio
async def test_price_below_dedup_uses_gap_from_threshold_not_raw_price_change(
    db_session, mock_notifications, monkeypatch
):
    """A price move that's tiny in raw percent terms can still be material
    if it doubles how far under the threshold we are — this is what
    distinguishes price_below's dedup from a naive "did trigger_price
    change by more than 5%" check. threshold=1000, price 990 -> 980 is
    only ~1% raw movement, but the gap-below-threshold doubles (10 -> 20),
    a 100% change — material."""
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "990.00")
    await _make_rule(db_session, product.id, "price_below", threshold_price=Decimal("1000.00"))
    first = await evaluate_alerts(db_session, product.id)
    assert len(first) == 1

    await _add_snapshot(db_session, product.id, "980.00")
    second = await evaluate_alerts(db_session, product.id)

    assert len(second) == 2  # old resolved + new opened
    opened = [e for e in second if e.resolved_at is None][0]
    assert opened.trigger_price == Decimal("980.00")
    assert len(mock_notifications) == 2


@pytest.mark.asyncio
async def test_price_below_dedup_small_gap_change_does_not_fire(
    db_session, mock_notifications, monkeypatch
):
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    await _add_snapshot(db_session, product.id, "990.00")  # gap = 10
    await _make_rule(db_session, product.id, "price_below", threshold_price=Decimal("1000.00"))
    await evaluate_alerts(db_session, product.id)
    assert len(mock_notifications) == 1

    await _add_snapshot(db_session, product.id, "989.70")  # gap = 10.3, a 3% gap change
    second = await evaluate_alerts(db_session, product.id)

    assert second == []
    assert len(mock_notifications) == 1


# ---- resolution ----


@pytest.mark.asyncio
async def test_resolution_sets_resolved_at_and_sends_notice(db_session, mock_notifications):
    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor.id, "90.00")
    await _add_snapshot(db_session, product.id, "100.00")
    first = await evaluate_alerts(db_session, product.id)
    assert first[0].resolved_at is None
    assert len(mock_notifications) == 1

    # competitor is no longer cheaper — condition stops holding
    await _add_snapshot(db_session, competitor.id, "150.00")
    second = await evaluate_alerts(db_session, product.id)

    assert len(second) == 1
    assert second[0].id == first[0].id
    assert second[0].resolved_at is not None
    assert len(mock_notifications) == 2  # the original alert + a resolution notice


@pytest.mark.asyncio
async def test_resolved_then_retriggered_condition_fires_again_as_a_fresh_event(
    db_session, mock_notifications, monkeypatch
):
    monkeypatch.setattr(settings, "alert_cooldown_minutes", 0)

    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    await db_session.commit()
    await _make_rule(db_session, product.id, "undercut")

    await _add_snapshot(db_session, competitor.id, "90.00")
    await _add_snapshot(db_session, product.id, "100.00")
    first = await evaluate_alerts(db_session, product.id)
    first_event_id = first[0].id

    await _add_snapshot(db_session, competitor.id, "150.00")  # resolves it
    resolved = await evaluate_alerts(db_session, product.id)
    assert resolved[0].id == first_event_id
    assert resolved[0].resolved_at is not None

    await _add_snapshot(db_session, competitor.id, "90.00")  # undercut again
    retriggered = await evaluate_alerts(db_session, product.id)

    assert len(retriggered) == 1
    assert retriggered[0].id != first_event_id  # a genuinely new row
    assert retriggered[0].resolved_at is None
    assert len(mock_notifications) == 3  # alert, resolution, fresh alert
