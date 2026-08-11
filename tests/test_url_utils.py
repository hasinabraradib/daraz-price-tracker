import pytest
from app.url_utils import InvalidProductUrlError, normalize_daraz_url


def test_tracking_params_stripped():
    normalized = normalize_daraz_url(
        "https://www.daraz.com.bd/products/-i517693314-s2605217277.html"
        "?spm=a2a0e.searchlist.list.1&search=1"
    )
    assert normalized == "https://www.daraz.com.bd/products/-i517693314-s2605217277.html"


def test_fragment_stripped_too():
    normalized = normalize_daraz_url(
        "https://www.daraz.pk/products/x-i1-s1.html#reviews"
    )
    assert normalized == "https://www.daraz.pk/products/x-i1-s1.html"


def test_url_with_no_query_params_unchanged():
    url = "https://www.daraz.com.bd/products/-i517693314-s2605217277.html"
    assert normalize_daraz_url(url) == url


def test_same_product_different_tracking_params_normalizes_identically():
    a = normalize_daraz_url(
        "https://www.daraz.com.bd/products/-i517693314-s2605217277.html"
        "?spm=a2a0e.searchlist.list.1&search=1"
    )
    b = normalize_daraz_url(
        "https://www.daraz.com.bd/products/-i517693314-s2605217277.html"
        "?spm=different.tracking.param&utm_source=fb"
    )
    c = normalize_daraz_url(
        "https://www.daraz.com.bd/products/-i517693314-s2605217277.html"
    )
    assert a == b == c


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url-at-all",
        "ftp://www.daraz.pk/products/x-i1-s1.html",  # unsupported scheme
        "https://",  # no host
        "",
    ],
)
def test_malformed_urls_rejected(url):
    with pytest.raises(InvalidProductUrlError):
        normalize_daraz_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.com/dp/B08N5WRWNW",
        "https://www.aliexpress.com/item/123.html",
        "https://notdaraz.com/products/x-i1-s1.html",
        "https://darazfake.com/products/x-i1-s1.html",
    ],
)
def test_non_daraz_urls_rejected(url):
    with pytest.raises(InvalidProductUrlError):
        normalize_daraz_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.daraz.pk/products/x-i1-s1.html",
        "https://www.daraz.com.bd/products/x-i1-s1.html",
        "https://www.daraz.lk/products/x-i1-s1.html",
        "https://www.daraz.com.np/products/x-i1-s1.html",
        "https://daraz.pk/products/x-i1-s1.html",  # no www subdomain
    ],
)
def test_valid_daraz_domains_accepted(url):
    normalize_daraz_url(url)  # should not raise
