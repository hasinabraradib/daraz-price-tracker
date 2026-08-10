"""Scrapes public Daraz product pages for title, price, currency, and stock status.

We deliberately operate at low volume against public product pages only: one
page per job, throttled by POLITE_DELAY_SECONDS between requests, no
parallelism, and no attempt to bypass bot detection, log in, or access
anything not visible to a normal visitor. This is a personal price-tracking
tool, not a bulk crawler.

Daraz product pages are JS-heavy (client-side rendered), so we drive a real
headless browser via Playwright rather than requests+BeautifulSoup.
"""
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from shared.config import settings

POLITE_DELAY_SECONDS = settings.polite_delay_seconds

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 20_000

# Selectors below were verified against a live daraz.com.bd product page
# (2026-08-11). `.pdp-price` alone would also match the struck-through
# "was" price, so we target the normal-price node specifically.
TITLE_SELECTOR = ".pdp-mod-product-badge-title"
PRICE_SELECTOR = ".pdp-price_type_normal"
ADD_TO_CART_SELECTOR = ".add-to-cart-buy-now-btn"
SOLD_OUT_TEXT_MARKERS = ("sold out", "out of stock")

_last_request_at: float | None = None
_delay_lock = asyncio.Lock()


class ScrapeError(Exception):
    """Raised when a product page can't be scraped, with a clear reason."""


@dataclass
class ScrapedProduct:
    title: str
    price: Decimal
    currency: str
    in_stock: bool


async def _respect_polite_delay() -> None:
    global _last_request_at
    async with _delay_lock:
        now = time.monotonic()
        if _last_request_at is not None:
            remaining = POLITE_DELAY_SECONDS - (now - _last_request_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
        _last_request_at = time.monotonic()


def _parse_price(raw: str) -> Decimal:
    digits = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
    try:
        return Decimal(digits)
    except InvalidOperation as exc:
        raise ScrapeError(f"could not parse price from {raw!r}") from exc


def _parse_currency(raw: str) -> str:
    symbol = "".join(ch for ch in raw if not (ch.isdigit() or ch in ".,- ")).strip()
    if not symbol:
        raise ScrapeError(f"could not determine currency from {raw!r}")
    return symbol


async def _detect_in_stock(page) -> bool:
    """In stock iff an Add to Cart / Buy Now button is present and doesn't
    read as sold out. We haven't observed a real out-of-stock Daraz page to
    confirm this against, so treat it as a best effort worth revisiting."""
    buttons = await page.query_selector_all(ADD_TO_CART_SELECTOR)
    if not buttons:
        return False
    for button in buttons:
        text = (await button.inner_text()).strip().lower()
        if any(marker in text for marker in SOLD_OUT_TEXT_MARKERS):
            return False
    return True


async def scrape_product(url: str) -> ScrapedProduct:
    await _respect_polite_delay()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            page.set_default_timeout(PAGE_TIMEOUT_MS)

            try:
                await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as exc:
                raise ScrapeError(f"timed out loading {url}") from exc

            try:
                await page.wait_for_selector(TITLE_SELECTOR, timeout=PAGE_TIMEOUT_MS)
                # the price node can render a beat after the title does
                await page.wait_for_selector(PRICE_SELECTOR, timeout=PAGE_TIMEOUT_MS)
            except PlaywrightTimeoutError as exc:
                raise ScrapeError(
                    f"product page did not render expected content: {url}"
                ) from exc

            title_el = await page.query_selector(TITLE_SELECTOR)
            price_el = await page.query_selector(PRICE_SELECTOR)

            if title_el is None or price_el is None:
                raise ScrapeError(f"missing title or price element on {url}")

            title = (await title_el.inner_text()).strip()
            # Price is read only from the rendered DOM, never from the URL —
            # some search-referral links carry a stale price in a query
            # param, which doesn't reflect the page's actual current price
            # and won't be present at all for direct product links.
            raw_price_text = (await price_el.inner_text()).strip()

            currency = _parse_currency(raw_price_text)
            price = _parse_price(raw_price_text)

            in_stock = await _detect_in_stock(page)

            return ScrapedProduct(
                title=title, price=price, currency=currency, in_stock=in_stock
            )
        finally:
            await browser.close()
