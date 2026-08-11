from urllib.parse import urlsplit, urlunsplit


class InvalidProductUrlError(ValueError):
    """Raised when a URL isn't a usable Daraz product URL."""


def normalize_daraz_url(url: str) -> str:
    """Strip query params and fragments so the same product added via
    different search/referral links (?spm=..., &search=1, tracking utm_*,
    etc.) doesn't create duplicate rows. The product's identity lives
    entirely in the path (e.g. -i517693314-s2605217277.html).

    Also rejects anything that isn't a real Daraz URL — checked by hostname
    label rather than a hardcoded country-TLD list (daraz.pk, daraz.com.bd,
    daraz.lk, ...) so we don't need to keep that list in sync.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise InvalidProductUrlError(f"not a valid URL: {url!r}")

    hostname = parts.netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    if "daraz" not in hostname.split("."):
        raise InvalidProductUrlError(f"not a daraz.com URL: {url!r}")

    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
