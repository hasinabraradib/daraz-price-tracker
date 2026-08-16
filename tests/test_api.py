"""API tests. The scraper (Playwright/network) is never invoked here — API
endpoints only enqueue jobs onto (fake) Redis; actual scraping happens in
the worker process, which these tests don't exercise at all."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from shared.models import (
    AlertEvent,
    AlertRule,
    PriceSnapshot,
    Product,
    ProductCompetitor,
    ScrapeAttempt,
)
from shared.queue import dead_letter


@pytest.mark.asyncio
async def test_health_check(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


@pytest.mark.asyncio
async def test_create_product(client):
    response = await client.post(
        "/products",
        json={
            "name": "Test Product",
            "daraz_url": "https://www.daraz.pk/products/x-i1-s1.html",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Test Product"
    assert body["daraz_url"] == "https://www.daraz.pk/products/x-i1-s1.html"
    assert body["is_active"] is True
    assert "id" in body


@pytest.mark.asyncio
async def test_create_product_strips_tracking_params(client):
    response = await client.post(
        "/products",
        json={
            "name": "Test Product",
            "daraz_url": "https://www.daraz.pk/products/x-i1-s1.html?spm=abc&search=1",
        },
    )
    assert response.status_code == 201
    assert response.json()["daraz_url"] == "https://www.daraz.pk/products/x-i1-s1.html"


@pytest.mark.asyncio
async def test_create_duplicate_product_returns_409(client):
    payload = {
        "name": "Test Product",
        "daraz_url": "https://www.daraz.pk/products/x-i1-s1.html",
    }
    first = await client.post("/products", json=payload)
    assert first.status_code == 201

    # different tracking params, same underlying product
    second = await client.post(
        "/products",
        json={
            "name": "Test Product Again",
            "daraz_url": "https://www.daraz.pk/products/x-i1-s1.html?spm=different",
        },
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_create_product_rejects_non_daraz_url(client):
    response = await client.post(
        "/products",
        json={"name": "Not Daraz", "daraz_url": "https://www.amazon.com/dp/B08N5WRWNW"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_product_not_found_returns_404(client):
    response = await client.get("/products/999/history")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_product_history_returns_snapshots_newest_first(client, db_session):
    product = Product(name="Widget", daraz_url="https://www.daraz.pk/w-i1-s1.html")
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    older = PriceSnapshot(
        product_id=product.id, price=Decimal("100.00"), currency="Rs", in_stock=True,
        raw_title="Widget",
    )
    newer = PriceSnapshot(
        product_id=product.id, price=Decimal("90.00"), currency="Rs", in_stock=True,
        raw_title="Widget",
    )
    db_session.add(older)
    await db_session.commit()
    db_session.add(newer)
    await db_session.commit()

    response = await client.get(f"/products/{product.id}/history")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    # newest first
    assert Decimal(body[0]["price"]) == Decimal("90.00")
    assert Decimal(body[1]["price"]) == Decimal("100.00")


@pytest.mark.asyncio
async def test_list_products_includes_latest_price(client, db_session):
    with_price = Product(name="Has Snapshot", daraz_url="https://www.daraz.pk/a-i1-s1.html")
    without_price = Product(name="No Snapshot Yet", daraz_url="https://www.daraz.pk/b-i2-s2.html")
    db_session.add_all([with_price, without_price])
    await db_session.commit()
    await db_session.refresh(with_price)
    await db_session.refresh(without_price)

    db_session.add(
        PriceSnapshot(
            product_id=with_price.id, price=Decimal("50.00"), currency="Rs",
            in_stock=True, raw_title="Has Snapshot",
        )
    )
    await db_session.commit()

    response = await client.get("/products")
    assert response.status_code == 200
    body = {p["id"]: p for p in response.json()}

    assert body[with_price.id]["latest_price"]["price"] == "50.00"
    assert body[without_price.id]["latest_price"] is None


@pytest.mark.asyncio
async def test_product_attempts_endpoint(client, db_session):
    product = Product(name="Widget", daraz_url="https://www.daraz.pk/w-i1-s1.html")
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    db_session.add(
        ScrapeAttempt(
            product_id=product.id, success=False, error_type="RetryableScrapeError",
            error_message="timeout", duration_ms=100, attempt_number=1,
        )
    )
    await db_session.commit()

    response = await client.get(f"/products/{product.id}/attempts")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["error_type"] == "RetryableScrapeError"


@pytest.mark.asyncio
async def test_product_attempts_404_for_missing_product(client):
    response = await client.get("/products/999/attempts")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_trigger_scrape_enqueues_job(client, db_session, fake_redis):
    product = Product(name="Widget", daraz_url="https://www.daraz.pk/w-i1-s1.html")
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    response = await client.post(f"/products/{product.id}/scrape")
    assert response.status_code == 202
    body = response.json()
    assert body["queued"] is True
    assert body["queue_depth"] == 1

    depth_response = await client.get("/queue/depth")
    assert depth_response.json() == {"depth": 1}


@pytest.mark.asyncio
async def test_scrape_health_aggregates_correctly(client, db_session):
    product = Product(name="Widget", daraz_url="https://www.daraz.pk/w-i1-s1.html")
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    db_session.add_all(
        [
            ScrapeAttempt(
                product_id=product.id, success=True, error_type=None, error_message=None,
                duration_ms=100, attempt_number=1,
            ),
            ScrapeAttempt(
                product_id=product.id, success=False, error_type="RetryableScrapeError",
                error_message="timeout", duration_ms=200, attempt_number=1,
            ),
            ScrapeAttempt(
                product_id=product.id, success=False, error_type="RetryableScrapeError",
                error_message="timeout", duration_ms=210, attempt_number=2,
            ),
            ScrapeAttempt(
                product_id=product.id, success=False, error_type="TerminalScrapeError",
                error_message="404", duration_ms=50, attempt_number=1,
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/stats/scrape-health")
    assert response.status_code == 200
    body = response.json()

    assert body["total_attempts"] == 4
    assert body["success_rate"] == pytest.approx(0.25)
    assert body["failures_by_error_type"] == {
        "RetryableScrapeError": 2,
        "TerminalScrapeError": 1,
    }
    assert body["queue_depth"] == 0
    assert body["delayed_queue_depth"] == 0
    assert body["dead_letter_depth"] == 0


@pytest.mark.asyncio
async def test_scrape_health_with_no_attempts_has_null_success_rate(client):
    response = await client.get("/stats/scrape-health")
    assert response.status_code == 200
    body = response.json()
    assert body["total_attempts"] == 0
    assert body["success_rate"] is None


@pytest.mark.asyncio
async def test_dead_letters_list_replay_and_discard_via_api(client, fake_redis):
    job = {
        "job_id": "api-dlq-job",
        "product_id": 1,
        "url": "https://www.daraz.pk/w-i1-s1.html",
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [
            {
                "attempt_number": 1,
                "attempted_at": "2026-01-01T00:00:05+00:00",
                "error_type": "TerminalScrapeError",
                "error_message": "404",
            }
        ],
    }
    await dead_letter(job, final_error_type="TerminalScrapeError", final_error_message="404")

    list_response = await client.get("/dead-letters")
    assert list_response.status_code == 200
    [record] = list_response.json()
    assert record["job_id"] == "api-dlq-job"
    assert record["final_error_type"] == "TerminalScrapeError"

    replay_response = await client.post("/dead-letters/api-dlq-job/replay")
    assert replay_response.status_code == 202
    assert replay_response.json() == {"replayed": True, "job_id": "api-dlq-job"}

    depth_response = await client.get("/queue/depth")
    assert depth_response.json() == {"depth": 1}

    # already replayed — gone from the DLQ now
    second_replay = await client.post("/dead-letters/api-dlq-job/replay")
    assert second_replay.status_code == 404


@pytest.mark.asyncio
async def test_discard_dead_letter_via_api(client, fake_redis):
    job = {
        "job_id": "api-dlq-discard",
        "product_id": 1,
        "url": "https://www.daraz.pk/w-i1-s1.html",
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [],
    }
    await dead_letter(job, final_error_type="TerminalScrapeError", final_error_message="404")

    delete_response = await client.delete("/dead-letters/api-dlq-discard")
    assert delete_response.status_code == 204

    list_response = await client.get("/dead-letters")
    assert list_response.json() == []

    missing_response = await client.delete("/dead-letters/api-dlq-discard")
    assert missing_response.status_code == 404


# ---- competitors ----


async def _seed_two_products(db_session, suffix: str = ""):
    mine = Product(name=f"Mine{suffix}", daraz_url=f"https://www.daraz.pk/mine{suffix}-i1-s1.html")
    theirs = Product(
        name=f"Theirs{suffix}", daraz_url=f"https://www.daraz.pk/theirs{suffix}-i2-s2.html"
    )
    db_session.add_all([mine, theirs])
    await db_session.commit()
    await db_session.refresh(mine)
    await db_session.refresh(theirs)
    return mine, theirs


@pytest.mark.asyncio
async def test_add_competitor(client, db_session):
    mine, theirs = await _seed_two_products(db_session)

    response = await client.post(
        f"/products/{mine.id}/competitors", json={"competitor_product_id": theirs.id}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["product_id"] == mine.id
    assert body["competitor_product_id"] == theirs.id


@pytest.mark.asyncio
async def test_add_competitor_self_rejected(client, db_session):
    mine, _ = await _seed_two_products(db_session)

    response = await client.post(
        f"/products/{mine.id}/competitors", json={"competitor_product_id": mine.id}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_add_competitor_duplicate_rejected(client, db_session):
    mine, theirs = await _seed_two_products(db_session)
    await client.post(f"/products/{mine.id}/competitors", json={"competitor_product_id": theirs.id})

    response = await client.post(
        f"/products/{mine.id}/competitors", json={"competitor_product_id": theirs.id}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_add_competitor_missing_product_returns_404(client, db_session):
    mine, _ = await _seed_two_products(db_session)

    response = await client.post(
        f"/products/{mine.id}/competitors", json={"competitor_product_id": 999999}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_competitors_includes_price_and_gap(client, db_session):
    mine, theirs = await _seed_two_products(db_session)
    db_session.add(ProductCompetitor(product_id=mine.id, competitor_product_id=theirs.id))
    db_session.add_all(
        [
            PriceSnapshot(
                product_id=mine.id, price=Decimal("100.00"), currency="Rs",
                in_stock=True, raw_title="Mine",
            ),
            PriceSnapshot(
                product_id=theirs.id, price=Decimal("80.00"), currency="Rs",
                in_stock=True, raw_title="Theirs",
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(f"/products/{mine.id}/competitors")
    assert response.status_code == 200
    [entry] = response.json()
    assert entry["competitor_product_id"] == theirs.id
    assert entry["latest_price"] == "80.00"
    assert Decimal(entry["gap"]) == Decimal("20.00")


@pytest.mark.asyncio
async def test_remove_competitor(client, db_session):
    mine, theirs = await _seed_two_products(db_session)
    await client.post(f"/products/{mine.id}/competitors", json={"competitor_product_id": theirs.id})

    delete_response = await client.delete(f"/products/{mine.id}/competitors/{theirs.id}")
    assert delete_response.status_code == 204

    list_response = await client.get(f"/products/{mine.id}/competitors")
    assert list_response.json() == []

    missing_response = await client.delete(f"/products/{mine.id}/competitors/{theirs.id}")
    assert missing_response.status_code == 404


@pytest.mark.asyncio
async def test_comparison_flags_cheapest(client, db_session):
    mine, theirs = await _seed_two_products(db_session)
    db_session.add(ProductCompetitor(product_id=mine.id, competitor_product_id=theirs.id))
    db_session.add_all(
        [
            PriceSnapshot(
                product_id=mine.id, price=Decimal("100.00"), currency="Rs",
                in_stock=True, raw_title="Mine",
            ),
            PriceSnapshot(
                product_id=theirs.id, price=Decimal("80.00"), currency="Rs",
                in_stock=True, raw_title="Theirs",
            ),
        ]
    )
    await db_session.commit()

    response = await client.get(f"/products/{mine.id}/comparison")
    assert response.status_code == 200
    body = response.json()
    by_id = {e["product_id"]: e for e in body["entries"]}
    assert by_id[mine.id]["is_cheapest"] is False
    assert by_id[mine.id]["is_self"] is True
    assert by_id[theirs.id]["is_cheapest"] is True
    assert by_id[theirs.id]["is_self"] is False


# ---- alert rules ----


@pytest.mark.asyncio
async def test_create_alert_rule(client, db_session):
    mine, _ = await _seed_two_products(db_session)

    response = await client.post(
        f"/products/{mine.id}/alert-rules",
        json={"rule_type": "undercut", "channel": "email", "destination": "buyer@example.com"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["rule_type"] == "undercut"
    assert body["is_active"] is True


@pytest.mark.asyncio
async def test_create_price_below_rule_requires_threshold(client, db_session):
    mine, _ = await _seed_two_products(db_session)

    response = await client.post(
        f"/products/{mine.id}/alert-rules",
        json={"rule_type": "price_below", "channel": "email", "destination": "buyer@example.com"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_webhook_rule_requires_http_url(client, db_session):
    mine, _ = await _seed_two_products(db_session)

    response = await client.post(
        f"/products/{mine.id}/alert-rules",
        json={"rule_type": "undercut", "channel": "webhook", "destination": "not-a-url"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_and_delete_alert_rule(client, db_session):
    mine, _ = await _seed_two_products(db_session)
    create_response = await client.post(
        f"/products/{mine.id}/alert-rules",
        json={"rule_type": "undercut", "channel": "email", "destination": "buyer@example.com"},
    )
    rule_id = create_response.json()["id"]

    list_response = await client.get(f"/products/{mine.id}/alert-rules")
    assert len(list_response.json()) == 1

    delete_response = await client.delete(f"/products/{mine.id}/alert-rules/{rule_id}")
    assert delete_response.status_code == 204

    missing_response = await client.delete(f"/products/{mine.id}/alert-rules/{rule_id}")
    assert missing_response.status_code == 404


# ---- alerts ----


@pytest.mark.asyncio
async def test_list_alerts_filters_by_product_and_status(client, db_session):
    mine, _ = await _seed_two_products(db_session)
    other, _ = await _seed_two_products(db_session, suffix="2")  # unrelated product's rule

    rule_for_mine = AlertRule(
        product_id=mine.id, rule_type="undercut", channel="email", destination="a@example.com"
    )
    rule_for_other = AlertRule(
        product_id=other.id, rule_type="undercut", channel="email", destination="b@example.com"
    )
    db_session.add_all([rule_for_mine, rule_for_other])
    await db_session.commit()
    await db_session.refresh(rule_for_mine)
    await db_session.refresh(rule_for_other)

    db_session.add_all(
        [
            AlertEvent(
                alert_rule_id=rule_for_mine.id, resolved_at=None, trigger_price=Decimal("90"),
                message="open for mine",
            ),
            AlertEvent(
                alert_rule_id=rule_for_mine.id,
                resolved_at=datetime.now(timezone.utc),
                trigger_price=Decimal("90"),
                message="resolved for mine",
            ),
            AlertEvent(
                alert_rule_id=rule_for_other.id, resolved_at=None, trigger_price=Decimal("90"),
                message="for other product",
            ),
        ]
    )
    await db_session.commit()

    all_response = await client.get("/alerts")
    assert len(all_response.json()) == 3

    mine_response = await client.get(f"/alerts?product_id={mine.id}")
    assert len(mine_response.json()) == 2
    assert all(e["message"] != "for other product" for e in mine_response.json())

    open_response = await client.get("/alerts?status=open")
    assert len(open_response.json()) == 2
    assert all(e["resolved_at"] is None for e in open_response.json())

    resolved_response = await client.get("/alerts?status=resolved")
    assert len(resolved_response.json()) == 1
    assert resolved_response.json()[0]["message"] == "resolved for mine"


# ---- owner_email ("Option B" ownership — see api/app/deps.py) ----
# THIS IS NOT AUTHENTICATION: X-Owner-Email is just a client-supplied
# string, never verified. These tests confirm the filtering behavior
# that string drives, not any kind of access control guarantee.


@pytest.mark.asyncio
async def test_create_product_sets_owner_email_from_header(client):
    response = await client.post(
        "/products",
        json={"name": "Owned", "daraz_url": "https://www.daraz.pk/products/owned-i1-s1.html"},
        headers={"X-Owner-Email": "alice@example.com"},
    )
    assert response.status_code == 201
    assert response.json()["owner_email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_create_product_without_header_leaves_owner_email_null(client):
    response = await client.post(
        "/products",
        json={"name": "Unowned", "daraz_url": "https://www.daraz.pk/products/unowned-i1-s1.html"},
    )
    assert response.status_code == 201
    assert response.json()["owner_email"] is None


@pytest.mark.asyncio
async def test_list_products_filters_by_owner_email_but_keeps_unowned_visible(client):
    await client.post(
        "/products",
        json={"name": "Alice's", "daraz_url": "https://www.daraz.pk/products/a-i1-s1.html"},
        headers={"X-Owner-Email": "alice@example.com"},
    )
    await client.post(
        "/products",
        json={"name": "Bob's", "daraz_url": "https://www.daraz.pk/products/b-i1-s1.html"},
        headers={"X-Owner-Email": "bob@example.com"},
    )
    await client.post(
        "/products",
        json={"name": "Nobody's", "daraz_url": "https://www.daraz.pk/products/c-i1-s1.html"},
    )

    no_header = await client.get("/products")
    assert {p["name"] for p in no_header.json()} == {"Alice's", "Bob's", "Nobody's"}

    alice_view = await client.get("/products", headers={"X-Owner-Email": "alice@example.com"})
    # Alice sees her own product and the unowned one, not Bob's.
    assert {p["name"] for p in alice_view.json()} == {"Alice's", "Nobody's"}


@pytest.mark.asyncio
async def test_owned_product_endpoint_404s_for_a_different_owner(client):
    create = await client.post(
        "/products",
        json={"name": "Alice's", "daraz_url": "https://www.daraz.pk/products/d-i1-s1.html"},
        headers={"X-Owner-Email": "alice@example.com"},
    )
    product_id = create.json()["id"]

    as_owner = await client.get(
        f"/products/{product_id}/history", headers={"X-Owner-Email": "alice@example.com"}
    )
    assert as_owner.status_code == 200

    as_someone_else = await client.get(
        f"/products/{product_id}/history", headers={"X-Owner-Email": "bob@example.com"}
    )
    assert as_someone_else.status_code == 404

    no_header = await client.get(f"/products/{product_id}/history")
    assert no_header.status_code == 200


@pytest.mark.asyncio
async def test_create_alert_rule_sets_owner_email_and_list_filters_by_it(client):
    create = await client.post(
        "/products",
        json={"name": "P", "daraz_url": "https://www.daraz.pk/products/e-i1-s1.html"},
        headers={"X-Owner-Email": "alice@example.com"},
    )
    product_id = create.json()["id"]

    rule = await client.post(
        f"/products/{product_id}/alert-rules",
        json={
            "rule_type": "price_below",
            "threshold_price": "1000",
            "channel": "email",
            "destination": "alice@example.com",
        },
        headers={"X-Owner-Email": "alice@example.com"},
    )
    assert rule.status_code == 201
    assert rule.json()["owner_email"] == "alice@example.com"

    as_someone_else = await client.post(
        f"/products/{product_id}/alert-rules",
        json={
            "rule_type": "price_below",
            "threshold_price": "1000",
            "channel": "email",
            "destination": "bob@example.com",
        },
        headers={"X-Owner-Email": "bob@example.com"},
    )
    assert as_someone_else.status_code == 404


@pytest.mark.asyncio
async def test_list_alerts_filters_by_owner_email(client, db_session):
    mine = Product(name="Mine", daraz_url="https://www.daraz.pk/mine-i1-s1.html")
    others = Product(name="Others", daraz_url="https://www.daraz.pk/others-i1-s1.html")
    db_session.add_all([mine, others])
    await db_session.commit()
    await db_session.refresh(mine)
    await db_session.refresh(others)

    my_rule = AlertRule(
        product_id=mine.id, rule_type="undercut", channel="email",
        destination="alice@example.com", owner_email="alice@example.com",
    )
    others_rule = AlertRule(
        product_id=others.id, rule_type="undercut", channel="email",
        destination="bob@example.com", owner_email="bob@example.com",
    )
    db_session.add_all([my_rule, others_rule])
    await db_session.commit()
    await db_session.refresh(my_rule)
    await db_session.refresh(others_rule)

    db_session.add_all(
        [
            AlertEvent(alert_rule_id=my_rule.id, resolved_at=None, message="mine"),
            AlertEvent(alert_rule_id=others_rule.id, resolved_at=None, message="others"),
        ]
    )
    await db_session.commit()

    alice_view = await client.get("/alerts", headers={"X-Owner-Email": "alice@example.com"})
    assert [e["message"] for e in alice_view.json()] == ["mine"]

    no_header = await client.get("/alerts")
    assert {e["message"] for e in no_header.json()} == {"mine", "others"}


# ---- test-webhook ----


@pytest.mark.asyncio
async def test_test_webhook_sends_and_reports_success(client, monkeypatch):
    calls = []

    async def fake_send_webhook(destination, body, payload):
        calls.append((destination, body, payload))

    monkeypatch.setattr("shared.notifiers._send_webhook", fake_send_webhook)

    response = await client.post(
        "/alerts/test-webhook", json={"webhook_url": "https://discord.com/api/webhooks/1/abc"}
    )
    assert response.status_code == 200
    assert response.json() == {"sent": True}
    assert len(calls) == 1
    assert calls[0][0] == "https://discord.com/api/webhooks/1/abc"


@pytest.mark.asyncio
async def test_test_webhook_rejects_non_http_url(client):
    response = await client.post("/alerts/test-webhook", json={"webhook_url": "not-a-url"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_test_webhook_reports_delivery_failure(client, monkeypatch):
    # Raises NotifyError directly (not a generic Exception) so
    # send_notification's `except NotifyError: raise` fast path applies —
    # a generic Exception would instead go through 3 real, slow
    # full-jitter-backoff retries before giving up (shared/notifiers.py's
    # NOTIFY_MAX_ATTEMPTS), which is what this test is deliberately
    # avoiding, not what it's testing.
    from shared.notifiers import NotifyError

    async def failing_send_webhook(destination, body, payload):
        raise NotifyError("connection refused")

    monkeypatch.setattr("shared.notifiers._send_webhook", failing_send_webhook)

    response = await client.post(
        "/alerts/test-webhook", json={"webhook_url": "https://example.com/hook"}
    )
    assert response.status_code == 502


# ---- GET /products?daraz_url= (product-by-url lookup) ----


@pytest.mark.asyncio
async def test_list_products_by_daraz_url_finds_existing_product(client):
    await client.post(
        "/products",
        json={"name": "Findable", "daraz_url": "https://www.daraz.pk/products/f-i1-s1.html"},
    )
    response = await client.get(
        "/products", params={"daraz_url": "https://www.daraz.pk/products/f-i1-s1.html"}
    )
    assert response.status_code == 200
    results = response.json()
    assert len(results) == 1
    assert results[0]["name"] == "Findable"


@pytest.mark.asyncio
async def test_list_products_by_daraz_url_returns_empty_for_unknown_url(client):
    response = await client.get(
        "/products", params={"daraz_url": "https://www.daraz.pk/products/nope-i1-s1.html"}
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_list_products_by_daraz_url_ignores_owner_filter(client):
    await client.post(
        "/products",
        json={"name": "Bob's", "daraz_url": "https://www.daraz.pk/products/g-i1-s1.html"},
        headers={"X-Owner-Email": "bob@example.com"},
    )
    # Alice looking up Bob's product by URL should still find it — this
    # is an existence check for the "link a competitor" flow, not a
    # listing operation, so it isn't owner-filtered.
    response = await client.get(
        "/products",
        params={"daraz_url": "https://www.daraz.pk/products/g-i1-s1.html"},
        headers={"X-Owner-Email": "alice@example.com"},
    )
    assert len(response.json()) == 1


@pytest.mark.asyncio
async def test_list_products_by_daraz_url_malformed_url_returns_empty(client):
    response = await client.get("/products", params={"daraz_url": "not-a-url-at-all"})
    assert response.status_code == 200
    assert response.json() == []
