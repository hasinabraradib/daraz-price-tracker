"""API tests. The scraper (Playwright/network) is never invoked here — API
endpoints only enqueue jobs onto (fake) Redis; actual scraping happens in
the worker process, which these tests don't exercise at all."""
from decimal import Decimal

import pytest

from shared.models import PriceSnapshot, Product, ScrapeAttempt
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
