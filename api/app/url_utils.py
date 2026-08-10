from urllib.parse import urlunsplit, urlsplit


def normalize_daraz_url(url: str) -> str:
    """Strip query params and fragments so the same product added via
    different search/referral links (?spm=..., &search=1, tracking utm_*,
    etc.) doesn't create duplicate rows. The product's identity lives
    entirely in the path (e.g. -i517693314-s2605217277.html)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
