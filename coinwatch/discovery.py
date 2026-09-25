"""Bounded dealer leads from web search and public directories, with shop evidence."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


DIRECTORIES = (
    "http://augustuscoins.com/ed/dealers.html#fixed",
)

# Rotate through different stock categories so later scans explore beyond the same results.
DEALER_QUERIES = (
    'ancient coins fixed price dealers online shop -auction -auctions',
    'Greek Roman coins for sale independent coin dealer shop -auction -auctions',
    'Byzantine coins for sale fixed price coin shop -auction -auctions',
    'Greek tetradrachm drachm ancient coins dealer buy online -auction -auctions',
    'Roman denarius sestertius coins for sale dealer shop -auction -auctions',
    'Celtic ancient coins for sale fixed price dealer -auction -auctions',
)

_PRICE = re.compile(r"(?:£|€|\$)\s*\d[\d,.]*")
_ANCIENT = re.compile(r"\b(ancient|roman|greek|byzantine|celtic|denarius|tetradrachm|solidus|aureus|drachm|sestertius)\b", re.I)
_COIN = re.compile(r"\b(coins?|denarius|denarii|tetradrachm|solidus|aureus|drachm|sestertius|antoninianus|diobol|obol|stater|follis)\b", re.I)
_BUY = re.compile(r"\b(add to cart|add to basket|buy now|in stock|view item|purchase)\b", re.I)
_AUCTION = re.compile(r"\b(auction|place bid|current bid|starting bid|bid now)\b", re.I)
_BAD_PATH = re.compile(r"/(?:cart|checkout|account|login|auction|auctions|forum|wiki|reference|archive)(?:/|$)", re.I)
_SHOP_LINK = re.compile(r"(ancient|roman|greek|coin|shop|store|catalog|inventory|product)", re.I)
_DEALER = re.compile(r"\b(dealers?|shop|store|for sale|buy|fixed.price|add to cart|in stock)\b", re.I)
_NON_DEALERS = {'facebook.com', 'instagram.com', 'youtube.com', 'reddit.com', 'pinterest.com',
                'x.com', 'twitter.com', 'tiktok.com', 'wikipedia.org'}


class DiscoveryError(Exception):
    """A directory or candidate check failed; partial evidence remains usable."""

    def __init__(self, candidates: list[dict], failures: list[dict]):
        self.candidates = candidates
        self.failures = failures
        super().__init__("; ".join(f"{x['url']}: {x['error']}" for x in failures))


def _domain(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return None
        host = host.lower().removeprefix("www.")
        if host == "localhost" or not "." in host:
            return None
        try:
            # Dealer discovery operates on domain names; no bare IP needs inspection.
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        if host.endswith((".local", ".internal", ".test", ".invalid")):
            return None
        return host
    except ValueError:
        return None


def _links(html: str, base: str):
    soup = BeautifulSoup(html, "html.parser")
    base_domain = _domain(base)
    found = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base, anchor["href"])
        domain = _domain(href)
        if not domain or domain == base_domain or domain in found:
            continue
        if _BAD_PATH.search(urlsplit(href).path):
            continue
        label = anchor.get_text(" ", strip=True)
        if not _SHOP_LINK.search(label + " " + href):
            continue
        found.add(domain)
        yield domain, href, label or domain


def _has_fixed_price_ancient_stock(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("article, li.product, .product, .product-item, .product-layout, .ProductList-item, .elementor-product-miniature, .card-wrapper"):
        text = node.get_text(" ", strip=True)
        if _ANCIENT.search(text) and _COIN.search(text) and _PRICE.search(text) and _BUY.search(text) and not _AUCTION.search(text):
            return True
    return False


def _shop_pages(html: str, base: str):
    soup = BeautifulSoup(html, "html.parser")
    domain = _domain(base)
    prioritized = []
    for card in soup.select(".product, li.product, .product-item, article.product"):
        text = card.get_text(" ", strip=True)
        if not (_ANCIENT.search(text) and _COIN.search(text) and _PRICE.search(text)):
            continue
        anchor = card.select_one('a[href*="/product/"], a.woocommerce-LoopProduct-link[href], h2 a[href], h3 a[href]')
        if anchor:
            href = urljoin(base, anchor["href"])
            if _domain(href) == domain and not _BAD_PATH.search(urlsplit(href).path):
                prioritized.append(href)
    for href in dict.fromkeys(prioritized):
        yield href
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base, anchor["href"])
        if href not in prioritized and _domain(href) == domain and not _BAD_PATH.search(urlsplit(href).path) and _SHOP_LINK.search(anchor.get_text(" ", strip=True) + " " + href):
            yield href


def _inspect(fetcher, url: str):
    pending = [url]
    seen = set()
    while pending and len(seen) < 3:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        page = fetcher.get(current)
        if _domain(page.url) != _domain(url):
            return None, page.url
        if _has_fixed_price_ancient_stock(page.text):
            return page.url, None
        # Newly seen product links outrank generic navigation left from a parent page.
        next_links = [href for href in _shop_pages(page.text, page.url) if href not in seen][:3]
        pending = list(dict.fromkeys(next_links + pending))
    return None, None


def _candidate(fetcher, url, name, origin, failures):
    evidence = None
    try:
        evidence, redirected = _inspect(fetcher, url)
    except Exception as exc:
        failures.append({'url': url, 'error': str(exc)})
        reason = f'Inaccessible during inspection: {exc}'
    else:
        if redirected:
            reason = f'Unclear: dealer link redirects to another domain ({_domain(redirected)}).'
        else:
            reason = ('Public page shows ancient coin stock with a fixed-price purchase control.'
                      if evidence else 'Unclear: inspected public pages did not confirm available fixed-price ancient coins.')
    return {'domain': _domain(url), 'url': url, 'name': name[:160],
            'evidence_url': evidence or '', 'discovered_from': origin, 'reason': reason}


def discover_web(fetcher, known_domains, api_key, *, query_index=0, limit=10):
    """Run at most two basic queries; inspect at most ten new dealer domains."""
    from .web_search import WebSearchError, search_web

    limit = max(0, min(limit, 10))
    if not limit:
        return []
    known = {domain for value in known_domains if (domain := _domain('https://' + value))}
    excluded = sorted(known)
    candidates, failures, examined = [], [], set()
    stop = getattr(fetcher, 'stop_event', None)
    for offset in range(2):
        if (stop and stop.is_set()) or len(examined) >= limit:
            break
        query = DEALER_QUERIES[(query_index + offset) % len(DEALER_QUERIES)]
        origin = 'Tavily: ' + query
        start = ((query_index + offset) * 150) % max(1, len(excluded))
        batch = (excluded[start:] + excluded[:start])[:150]
        try:
            results = search_web(query, api_key, max_results=20, exclude_domains=batch)
        except WebSearchError as exc:
            failures.append({'url': origin, 'error': str(exc)})
            continue
        except Exception:
            # Never expose arbitrary transport errors that may include credentials.
            failures.append({'url': origin, 'error': 'Web dealer search failed. Please try again later.'})
            continue
        for result in results:
            if (stop and stop.is_set()) or len(examined) >= limit:
                break
            url = result['url']
            domain = _domain(url)
            text = result['title'] + ' ' + result.get('snippet', '') + ' ' + url
            if (not domain or domain in examined or any(domain == old or domain.endswith('.' + old) for old in known | _NON_DEALERS)
                    or _BAD_PATH.search(urlsplit(url).path)
                    or not (_ANCIENT.search(text) and _COIN.search(text) and _DEALER.search(text))):
                continue
            examined.add(domain)
            candidates.append(_candidate(fetcher, url, domain, origin, failures))
    if failures:
        raise DiscoveryError(candidates, failures)
    return candidates


def discover(fetcher, known_domains: set[str], limit: int = 20) -> list[dict]:
    """Return up to ``limit`` evidence-backed leads; expose all fetch failures.

    The caller should pass known and dismissed domains together. The fetcher
    enforces robots, public DNS destinations, redirect policy and rate limits.
    """
    if limit < 1:
        return []
    limit = min(limit, 20)
    known = {_domain("https://" + value) if "://" not in value else _domain(value) for value in known_domains}
    candidates: list[dict] = []
    failures: list[dict] = []
    examined = set()
    for directory in DIRECTORIES:
        try:
            page = fetcher.get(directory)
            leads = list(_links(page.text, page.url))
            if not leads:
                raise ValueError("no parseable dealer links")
        except Exception as exc:
            failures.append({"url": directory, "error": str(exc)})
            continue
        for domain, url, name in leads:
            if domain in known or domain in examined or len(examined) >= limit:
                continue
            examined.add(domain)
            candidates.append(_candidate(fetcher, url, name, directory, failures))
        if len(examined) >= limit:
            break
    if failures:
        raise DiscoveryError(candidates, failures)
    return candidates
