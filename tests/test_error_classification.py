"""Tests for worker_app/scraper.py's exception hierarchy and how real
Playwright/HTTP failures map onto it, plus the "one retry only" rule for
SelectorScrapeError enforced in worker_app/main.py.

Playwright itself is always mocked here — a lightweight fake page/browser
stands in for the real one, so these tests never launch a browser or touch
the network.
"""
import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from worker_app.main import _handle_failure
from worker_app.scraper import (
    ADD_TO_CART_SELECTOR,
    PRICE_SELECTOR,
    TITLE_SELECTOR,
    RetryableScrapeError,
    SelectorScrapeError,
    TerminalScrapeError,
    _raise_for_status,
    scrape_product,
)

from shared.config import settings
from shared.models import Product
from shared.queue import dead_letter_depth, list_dead_letters


@pytest.fixture(autouse=True)
def _no_polite_delay(monkeypatch):
    # POLITE_DELAY_SECONDS is captured as a module constant at import time,
    # so patch it directly rather than shared.config.settings (which
    # wouldn't be re-read). Without this, repeated scrape_product() calls
    # across tests in this file would rate-limit each other for real.
    monkeypatch.setattr("worker_app.scraper.POLITE_DELAY_SECONDS", 0.0)


# ---- status-code classification (pure function, no mocking needed) ----


@pytest.mark.parametrize("status", [404, 410])
def test_gone_status_is_terminal(status):
    with pytest.raises(TerminalScrapeError):
        _raise_for_status(status, "https://www.daraz.pk/products/x-i1-s1.html")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_rate_limit_and_server_errors_are_retryable(status):
    with pytest.raises(RetryableScrapeError):
        _raise_for_status(status, "https://www.daraz.pk/products/x-i1-s1.html")


def test_ok_status_raises_nothing():
    _raise_for_status(200, "https://www.daraz.pk/products/x-i1-s1.html")  # no raise


# ---- fake Playwright harness ----


class _FakeElement:
    def __init__(self, text=""):
        self._text = text

    async def inner_text(self):
        return self._text


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakePage:
    def __init__(
        self,
        *,
        goto_exception=None,
        status=200,
        title_text="Some Product Title",
        price_text="Rs. 158",
        wait_for_selector_exception=None,
        add_to_cart_texts=("Add to Cart",),
    ):
        self._goto_exception = goto_exception
        self._status = status
        self._title_text = title_text
        self._price_text = price_text
        self._wait_for_selector_exception = wait_for_selector_exception
        self._add_to_cart_texts = add_to_cart_texts

    def set_default_timeout(self, ms):
        pass

    async def goto(self, url, wait_until=None):
        if self._goto_exception:
            raise self._goto_exception
        return _FakeResponse(self._status)

    async def wait_for_selector(self, selector, timeout=None):
        if self._wait_for_selector_exception:
            raise self._wait_for_selector_exception

    async def query_selector(self, selector):
        if selector == TITLE_SELECTOR:
            return _FakeElement(self._title_text) if self._title_text is not None else None
        if selector == PRICE_SELECTOR:
            return _FakeElement(self._price_text) if self._price_text is not None else None
        return None

    async def query_selector_all(self, selector):
        if selector == ADD_TO_CART_SELECTOR:
            return [_FakeElement(text) for text in self._add_to_cart_texts]
        return []


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self, user_agent=None):
        return self._page

    async def close(self):
        pass


class _FakeChromium:
    def __init__(self, page):
        self._page = page

    async def launch(self, headless=True):
        return _FakeBrowser(self._page)


class _FakePlaywrightContext:
    def __init__(self, page):
        self.chromium = _FakeChromium(page)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _mock_playwright(monkeypatch, page):
    monkeypatch.setattr(
        "worker_app.scraper.async_playwright", lambda: _FakePlaywrightContext(page)
    )


# ---- timeouts / network errors -> RetryableScrapeError ----


@pytest.mark.asyncio
async def test_goto_timeout_is_retryable(monkeypatch):
    page = _FakePage(goto_exception=PlaywrightTimeoutError("Timeout 20000ms exceeded"))
    _mock_playwright(monkeypatch, page)

    with pytest.raises(RetryableScrapeError):
        await scrape_product("https://www.daraz.pk/products/x-i1-s1.html")


@pytest.mark.asyncio
async def test_connection_error_is_retryable(monkeypatch):
    page = _FakePage(goto_exception=PlaywrightError("net::ERR_CONNECTION_REFUSED"))
    _mock_playwright(monkeypatch, page)

    with pytest.raises(RetryableScrapeError):
        await scrape_product("https://www.daraz.pk/products/x-i1-s1.html")


# ---- selector mismatch -> SelectorScrapeError ----


@pytest.mark.asyncio
async def test_selector_timeout_raises_selector_error(monkeypatch):
    page = _FakePage(
        wait_for_selector_exception=PlaywrightTimeoutError("selector never appeared")
    )
    _mock_playwright(monkeypatch, page)

    with pytest.raises(SelectorScrapeError):
        await scrape_product("https://www.daraz.pk/products/x-i1-s1.html")


@pytest.mark.asyncio
async def test_missing_price_element_raises_selector_error(monkeypatch):
    page = _FakePage(price_text=None)
    _mock_playwright(monkeypatch, page)

    with pytest.raises(SelectorScrapeError):
        await scrape_product("https://www.daraz.pk/products/x-i1-s1.html")


@pytest.mark.asyncio
async def test_successful_scrape_returns_parsed_product(monkeypatch):
    page = _FakePage(price_text="Rs. 1,299", add_to_cart_texts=("Add to Cart", "Buy Now"))
    _mock_playwright(monkeypatch, page)

    result = await scrape_product("https://www.daraz.pk/products/x-i1-s1.html")

    assert result.title == "Some Product Title"
    assert str(result.price) == "1299"
    assert result.currency == "Rs"  # the "." in "Rs." gets stripped along with punctuation
    assert result.in_stock is True


# ---- SelectorScrapeError retries exactly once, then dead-letters ----


@pytest.mark.asyncio
async def test_selector_error_retries_once_then_dead_letters(monkeypatch, db_session):
    """Two SelectorScrapeErrors in a row should dead-letter on the SECOND
    one, well before the (much larger) max-attempts budget is reached —
    proving the "one retry only" rule is enforced independently of
    retry_max_attempts."""
    monkeypatch.setattr(settings, "retry_max_attempts", 5)

    product = Product(
        name="Test product", daraz_url="https://www.daraz.pk/products/test-i1-s1.html"
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)

    job = {
        "job_id": "test-job-selector",
        "product_id": product.id,
        "url": product.daraz_url,
        "attempt": 1,
        "enqueued_at": "2026-01-01T00:00:00+00:00",
        "attempt_history": [],
    }

    # First selector failure: retried, not dead-lettered.
    await _handle_failure(job, SelectorScrapeError("no title element"), started=0.0)
    assert await dead_letter_depth() == 0
    assert job["attempt"] == 2

    # Second selector failure in a row: dead-lettered immediately, even
    # though only 2 of 5 allowed attempts have been used.
    await _handle_failure(job, SelectorScrapeError("no title element"), started=0.0)
    assert await dead_letter_depth() == 1

    [record] = await list_dead_letters()
    assert len(record["attempts"]) == 2
