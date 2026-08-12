"""Tests for shared/metrics.py and its instrumentation of the real code
paths (worker scrape handling, queue enqueue, alert firing, notification
delivery), plus the /metrics endpoint's format.

Counters live on prometheus_client's process-wide default registry —
shared across the whole test session, same as production. Tests assert
*deltas* around the action under test rather than absolute values, so
they're correct regardless of what else in the suite touches the same
metric. Gauges are the opposite: `.set()` is idempotent/absolute (not
accumulated), so those tests just assert the value directly after a
refresh.
"""
from decimal import Decimal

import pytest
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
from worker_app.main import process_job
from worker_app.scraper import (
    RetryableScrapeError,
    ScrapedProduct,
    TerminalScrapeError,
)

from shared.alerts import evaluate_alerts
from shared.metrics import (
    ALERT_DELIVERIES_TOTAL,
    ALERT_DELIVERY_DURATION_SECONDS,
    ALERTS_FIRED_TOTAL,
    JOBS_DEAD_LETTERED_TOTAL,
    JOBS_ENQUEUED_TOTAL,
    SCRAPE_ATTEMPTS_TOTAL,
    SCRAPE_DURATION_SECONDS,
    refresh_gauges,
)
from shared.models import AlertRule, PriceSnapshot, Product, ProductCompetitor
from shared.notifiers import NotifyError, send_notification
from shared.queue import enqueue_job, schedule_retry


def _metric_value(name: str, labels: dict[str, str] | None = None) -> float:
    """Read a single sample's current value straight off the same
    default registry /metrics serves — the closest thing prometheus_client
    has to a public "give me this counter's value" API. Scans every
    sample directly rather than filtering by family name first: for a
    Counter, prometheus_client strips the "_total" suffix from the
    *family* name (e.g. "scrape_attempts") while the actual exposed
    *sample* keeps the full "scrape_attempts_total" — matching on
    sample.name sidesteps that quirk entirely."""
    text = generate_latest().decode()
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and (labels is None or sample.labels == labels):
                return sample.value
    return 0.0


async def _make_product(db_session, name="Product") -> Product:
    product = Product(name=name, daraz_url=f"https://www.daraz.pk/{name.lower()}-i1-s1.html")
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


# ---- /metrics format ----


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_valid_prometheus_format(client):
    # A labeled Counter/Histogram only produces samples for label
    # combinations that have actually been used at least once — touch
    # each metric directly so this test doesn't depend on some *other*
    # test having already exercised the real code paths first.
    SCRAPE_ATTEMPTS_TOTAL.labels(outcome="success", error_type="").inc()
    ALERTS_FIRED_TOTAL.labels(rule_type="undercut", channel="email").inc()
    ALERT_DELIVERIES_TOTAL.labels(channel="email", status="sent").inc()
    JOBS_ENQUEUED_TOTAL.inc()
    JOBS_DEAD_LETTERED_TOTAL.labels(reason="terminal").inc()
    SCRAPE_DURATION_SECONDS.observe(1.0)
    ALERT_DELIVERY_DURATION_SECONDS.observe(1.0)

    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    # Parses without raising -> it's valid Prometheus text exposition format.
    families = list(text_string_to_metric_families(response.text))
    sample_names = {sample.name for family in families for sample in family.samples}

    assert "queue_depth" in sample_names
    assert "delayed_queue_depth" in sample_names
    assert "dead_letter_depth" in sample_names
    assert "products_tracked_total" in sample_names
    assert "scrape_attempts_total" in sample_names
    assert "alerts_fired_total" in sample_names
    assert "alert_deliveries_total" in sample_names
    assert "jobs_enqueued_total" in sample_names
    assert "jobs_dead_lettered_total" in sample_names
    assert "scrape_duration_seconds_count" in sample_names
    assert "alert_delivery_duration_seconds_count" in sample_names


# ---- worker scrape instrumentation (real dispatch, mocked Playwright layer) ----


@pytest.mark.asyncio
async def test_scrape_attempts_total_and_duration_increment_on_success(
    db_session, monkeypatch
):
    product = await _make_product(db_session, "Success")

    async def fake_scrape(url):
        return ScrapedProduct(
            title="Widget", price=Decimal("100.00"), currency="Rs", in_stock=True
        )

    monkeypatch.setattr("worker_app.main.scrape_product", fake_scrape)

    before_count = _metric_value(
        "scrape_attempts_total", {"outcome": "success", "error_type": ""}
    )
    before_hist = _metric_value("scrape_duration_seconds_count")

    job = {
        "job_id": "metrics-test-success",
        "product_id": product.id,
        "url": product.daraz_url,
        "attempt": 1,
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [],
    }
    await process_job(job)

    assert _metric_value(
        "scrape_attempts_total", {"outcome": "success", "error_type": ""}
    ) == before_count + 1
    assert _metric_value("scrape_duration_seconds_count") == before_hist + 1


@pytest.mark.asyncio
async def test_scrape_attempts_total_increments_on_failure_with_error_type(
    db_session, monkeypatch
):
    product = await _make_product(db_session, "Failure")

    async def fake_scrape(url):
        raise RetryableScrapeError("simulated timeout")

    monkeypatch.setattr("worker_app.main.scrape_product", fake_scrape)

    before = _metric_value(
        "scrape_attempts_total", {"outcome": "failure", "error_type": "RetryableScrapeError"}
    )

    job = {
        "job_id": "metrics-test-failure",
        "product_id": product.id,
        "url": product.daraz_url,
        "attempt": 1,
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [],
    }
    await process_job(job)

    after = _metric_value(
        "scrape_attempts_total", {"outcome": "failure", "error_type": "RetryableScrapeError"}
    )
    assert after == before + 1


@pytest.mark.asyncio
async def test_jobs_dead_lettered_total_increments_with_terminal_reason(
    db_session, monkeypatch
):
    product = await _make_product(db_session, "Terminal")

    async def fake_scrape(url):
        raise TerminalScrapeError("404")

    monkeypatch.setattr("worker_app.main.scrape_product", fake_scrape)

    before = _metric_value("jobs_dead_lettered_total", {"reason": "terminal"})

    job = {
        "job_id": "metrics-test-terminal",
        "product_id": product.id,
        "url": product.daraz_url,
        "attempt": 1,
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [],
    }
    await process_job(job)

    after = _metric_value("jobs_dead_lettered_total", {"reason": "terminal"})
    assert after == before + 1


# ---- queue instrumentation ----


@pytest.mark.asyncio
async def test_jobs_enqueued_total_increments_on_enqueue():
    before = _metric_value("jobs_enqueued_total")
    await enqueue_job(product_id=1, url="https://www.daraz.pk/x-i1-s1.html")
    after = _metric_value("jobs_enqueued_total")
    assert after == before + 1


# ---- alert instrumentation ----


@pytest.mark.asyncio
async def test_alerts_fired_total_increments_when_rule_fires(db_session, monkeypatch):
    monkeypatch.setattr("shared.alerts.send_notification", _noop_send)

    product = await _make_product(db_session, "Mine")
    competitor = await _make_product(db_session, "Theirs")
    db_session.add(ProductCompetitor(product_id=product.id, competitor_product_id=competitor.id))
    db_session.add(
        AlertRule(
            product_id=product.id,
            rule_type="undercut",
            channel="webhook",
            destination="https://example.com/hook",
        )
    )
    await db_session.commit()

    db_session.add(
        PriceSnapshot(
            product_id=competitor.id, price=Decimal("90.00"), currency="Rs",
            in_stock=True, raw_title="x",
        )
    )
    db_session.add(
        PriceSnapshot(
            product_id=product.id, price=Decimal("100.00"), currency="Rs",
            in_stock=True, raw_title="x",
        )
    )
    await db_session.commit()

    before = _metric_value("alerts_fired_total", {"rule_type": "undercut", "channel": "webhook"})
    events = await evaluate_alerts(db_session, product.id)
    assert len(events) == 1

    after = _metric_value("alerts_fired_total", {"rule_type": "undercut", "channel": "webhook"})
    assert after == before + 1


async def _noop_send(channel, destination, subject, body, payload):
    return None


# ---- notifier instrumentation ----


@pytest.mark.asyncio
async def test_alert_deliveries_total_and_duration_increment_on_success(monkeypatch):
    async def fake_send_email(destination, subject, body):
        return None

    monkeypatch.setattr("shared.notifiers._send_email", fake_send_email)

    before_count = _metric_value(
        "alert_deliveries_total", {"channel": "email", "status": "sent"}
    )
    before_hist = _metric_value("alert_delivery_duration_seconds_count")

    await send_notification("email", "buyer@example.com", "subject", "body", {})

    assert (
        _metric_value("alert_deliveries_total", {"channel": "email", "status": "sent"})
        == before_count + 1
    )
    assert _metric_value("alert_delivery_duration_seconds_count") == before_hist + 1


@pytest.mark.asyncio
async def test_alert_deliveries_total_increments_on_failure():
    before = _metric_value(
        "alert_deliveries_total", {"channel": "carrier-pigeon", "status": "failed"}
    )

    with pytest.raises(NotifyError):
        await send_notification("carrier-pigeon", "loft-1", "subject", "body", {})

    after = _metric_value(
        "alert_deliveries_total", {"channel": "carrier-pigeon", "status": "failed"}
    )
    assert after == before + 1


# ---- gauge refresh reflects actual state ----


@pytest.mark.asyncio
async def test_refresh_gauges_reflects_actual_redis_and_postgres_state(db_session):
    active = await _make_product(db_session, "ActiveOne")
    active2 = await _make_product(db_session, "ActiveTwo")
    inactive = Product(
        name="InactiveOne", daraz_url="https://www.daraz.pk/inactiveone-i1-s1.html",
        is_active=False,
    )
    db_session.add(inactive)
    await db_session.commit()

    await enqueue_job(product_id=active.id, url=active.daraz_url)
    await enqueue_job(product_id=active2.id, url=active2.daraz_url)
    await schedule_retry(
        {"job_id": "x", "product_id": active.id, "url": active.daraz_url}, delay_seconds=300
    )

    await refresh_gauges()

    assert _metric_value("queue_depth") == 2.0
    assert _metric_value("delayed_queue_depth") == 1.0
    assert _metric_value("dead_letter_depth") == 0.0
    assert _metric_value("products_tracked_total", {"is_active": "true"}) == 2.0
    assert _metric_value("products_tracked_total", {"is_active": "false"}) == 1.0
