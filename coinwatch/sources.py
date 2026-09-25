"""Small, bounded parsers for verified public fixed-price catalog pages."""

from __future__ import annotations

import re
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import Listing, ScrapeResult


MAX_PAGES = 20
_EXCLUDED = re.compile(r"\b(uncleaned|unidentified|unsearched|lot(?:s)?\b|bulk|grab bag|auction|bidding|bid now|replica|reproduction|copy coin)\b", re.I)
_MODERN = re.compile(r"\b(modern|victoria|elizabeth|george v\b|george vi\b|20th century|21st century|18\d{2}|19\d{2}|20\d{2}|50p|sixpence)\b", re.I)
_BULK = re.compile(r"^\s*\d+\s+(?:ancient|roman|greek|byzantine|celtic)\s+coins?\b", re.I)
_ANCIENT = re.compile(r"\b(roman|greek|byzantine|celtic|ptolema|seleuk|seleucid|phoenici|ancient|bc\b|bce\b|ad\b|aureus|denarius|follis|antoninianus|tetradrachm|drachm|sestertius|nummus|obol|solidus|stater)\b", re.I)
_PRICE = re.compile(r"(?:£|€|\$)\s*([\d,.]+)")


def _text(node) -> str:
    if isinstance(node, str):
        return node
    return node.get_text(" ", strip=True) if node else ""


def _price(node, currency: str) -> str:
    if node:
        current = node.select_one("ins, .price-new, .price-item--sale")
        if current and _text(current):
            node = current
    value = _text(node)
    match = _PRICE.search(value)
    if not match:
        raise ValueError("missing price")
    try:
        raw = match.group(1)
        if "," in raw and "." in raw:
            raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
        elif "," in raw:
            raw = raw.replace(",", ".") if len(raw.rsplit(",", 1)[1]) == 2 else raw.replace(",", "")
        elif "." in raw and len(raw.rsplit(".", 1)[1]) == 3:
            raw = raw.replace(".", "")
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("invalid price") from exc
    if amount <= 0:
        raise ValueError("invalid price")
    return f"{amount:.2f}"


def _absolute(base: str, href: str | None) -> str:
    if not href:
        raise ValueError("missing product URL")
    url = urljoin(base, href)
    if urlsplit(url).hostname != urlsplit(base).hostname:
        raise ValueError("product URL left dealer site")
    return url


def _category(title: str, default: str = "Ancient") -> str:
    if re.search(r"\b(roman|rome|republic)\b", title, re.I):
        return "Roman"
    if re.search(r"\b(greek|ptolema|seleuk|seleucid)\b", title, re.I):
        return "Greek"
    if re.search(r"\b(celtic|bellovaque|ambiani)\b", title, re.I):
        return "Celtic"
    if re.search(r"\b(byzantine|hyperperion|solidus|tremissis)\b", title, re.I):
        return "Byzantine"
    if re.search(r"\b(drachm|tetradrachm|obol)\b", title, re.I):
        return "Greek"
    if re.search(r"\b(stater|statère)\b", title, re.I):
        return "Celtic"
    if re.search(r"\b(roman|aureus|denarius|follis|antoninianus|sestertius|nummus)\b", title, re.I):
        return "Roman"
    return default


def _reject(title: str) -> bool:
    return bool(_EXCLUDED.search(title) or _MODERN.search(title) or _BULK.search(title))


def _listing(card, base: str, *, ident: str | None, link, title, price, currency: str, availability="available", image=None, category="Ancient") -> Listing:
    name = _text(title)
    if not ident or not name:
        raise ValueError("missing product identity or title")
    url = _absolute(base, link.get("href") if link else None)
    image_url = urljoin(base, image.get("src") or image.get("data-src") or "") if image else ""
    return Listing(str(ident), url, name, _price(price, currency), currency, image_url=image_url,
                   category=_category(name, category), availability=availability)


def _silbury(card, base):
    title = card.select_one(".woocommerce-loop-product__title")
    if _reject(_text(title)):
        return None
    ident = next((c.removeprefix("post-") for c in card.get("class", []) if c.startswith("post-")), None)
    classes = card.get("class", [])
    if "instock" not in classes and "outofstock" not in classes:
        raise ValueError("missing availability status")
    status = "available" if "instock" in classes else "reserved" if "reserved" in _text(card).lower() else "sold"
    return _listing(card, base, ident=ident, link=card.select_one("a.woocommerce-LoopProduct-link"),
                    title=title, price=card.select_one(".price"),
                    currency="GBP", availability=status, image=card.select_one("img"), category="Roman")


def _palmyra(card, base):
    title = card.select_one(".woocommerce-loop-product__title")
    if _reject(_text(title)):
        return None
    ident = next((c.removeprefix("post-") for c in card.get("class", []) if c.startswith("post-")), None)
    classes = card.get("class", [])
    button = card.select_one("a.button")
    if "instock" in classes and button and "add to cart" in _text(button).lower():
        status = "available"
    elif "outofstock" in classes:
        status = "sold"
    else:
        raise ValueError("missing availability control")
    return _listing(card, base, ident=ident, link=card.select_one("a.woocommerce-LoopProduct-link"),
                    title=title, price=card.select_one(".price"), currency="USD", availability=status,
                    image=card.select_one("img"), category="Roman")


def _vpuk(card, base):
    title = card.select_one(".card__heading a")
    if not title or not _ANCIENT.search(_text(title)) or _reject(_text(title)):
        return None
    ident = card.select_one('input[name="product-id"]')
    button = card.select_one(".quick-add__submit, form button")
    if not button:
        raise ValueError("missing availability control")
    status = "sold" if button and "sold out" in _text(button).lower() and "add to cart" not in _text(button).lower() else "available"
    return _listing(card, base, ident=ident.get("value") if ident else None, link=title, title=title,
                    price=card.select_one(".price-item--sale") or card.select_one(".price-item--regular"), currency="GBP",
                    availability=status, image=card.select_one("img"))


def _premium(card, base):
    title = card.select_one("a.product-item__title")
    if not title or _reject(_text(title)):
        return None
    ident = card.select_one('input[name="product-id"]')
    button = card.select_one("button[type=submit], buy-button button")
    if not button:
        raise ValueError("missing availability control")
    status = "sold" if button and "sold out" in _text(button).lower() else "available"
    return _listing(card, base, ident=ident.get("value") if ident else None, link=title, title=title,
                    price=card.select_one(".price-list .price, .price"), currency="GBP",
                    availability=status, image=card.select_one("img.product-item__primary-image"), category="Roman")


def _historynumis(card, base):
    title = card.select_one(".elementor-title")
    if _reject(_text(title)):
        return None
    link = card.select_one("a.elementor-product-link")
    url = link.get("href", "") if link else ""
    match = re.search(r"/(\d+)-[^/]+\.html(?:\?.*)?$", url)
    if not card.select_one("form.elementor-atc") and "out-of-stock" not in _text(card).lower():
        raise ValueError("missing availability control")
    status = "sold" if "out-of-stock" in _text(card).lower() else "available"
    return _listing(card, base, ident=match.group(1) if match else None, link=link,
                    title=title, price=card.select_one(".elementor-price"),
                    currency="EUR", availability=status, image=card.select_one("img"))


def _shanna_detail_title(card, base, fetcher):
    ident = card.get("data-item-id")
    if not ident:
        raise ValueError("missing product identity")
    link = card.select_one("a.ProductList-item-link")
    url = _absolute(base, link.get("href") if link else None)
    detail = fetcher.get(url)
    if urlsplit(detail.url).hostname != urlsplit(base).hostname:
        raise ValueError("detail page left dealer site")
    soup = BeautifulSoup(detail.text, "html.parser")
    article = soup.select_one("article.ProductItem")
    if not article or article.get("data-item-id") != ident:
        raise ValueError("detail product identity does not match catalog card")
    title = _text(article.select_one(".ProductItem-details-title"))
    if not title:
        paragraphs = [_text(p) for p in article.select(".ProductItem-details-excerpt p") if _text(p)]
        title = " ".join(paragraphs[:2])
    title = " ".join(title.split())[:300].rstrip()
    if not title:
        raise ValueError("missing detail title and description")
    return title


def _shanna(card, base, title=None):
    title = title or card.select_one(".ProductList-title")
    if not _text(title):
        raise ValueError(f"product {card.get('data-item-id') or '(no ID)'}: missing title")
    if _reject(_text(title)):
        return None
    status = "sold" if "sold-out" in card.get("class", []) else "available"
    return _listing(card, base, ident=card.get("data-item-id"), link=card.select_one("a.ProductList-item-link"),
                    title=title, price=card.select_one(".product-price"),
                    currency="USD", availability=status, image=card.select_one("img.ProductList-image"), category="Greek")


def _roman_republic(card, base):
    title = card.select_one(".name a")
    if _reject(_text(title)):
        return None
    button = card.select_one(".button-group")
    button_text = _text(button).lower()
    if "sold out" in button_text:
        status = "sold"
    elif "add to cart" in button_text:
        status = "available"
    else:
        raise ValueError("missing availability control")
    return _listing(card, base, ident=card.get("data-product-id"), link=title, title=title,
                    price=card.select_one(".price"), currency="EUR", availability=status,
                    image=card.select_one(".product-img img"), category="Roman")


def _mrb(card, base):
    if not _text(card):
        return None  # The image link precedes the text link for each item.
    if _reject(_text(card)):
        return None
    ident = parse_qs(urlsplit(card.get("href", "")).query).get("id", [None])[0]
    return _listing(card, base, ident=ident, link=card, title=card, price=card.parent,
                    currency="USD", image=card.parent.select_one("img"), category="Roman")


def _mrb_availability(html: str, ident: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        action = urlsplit(form.get("action", "")).path
        values = {x.get("name"): x.get("value") for x in form.select("input[name]")}
        if action.endswith("/cart.pl") and values.get("action") == "add" and values.get("id") == ident:
            if form.select_one('input[type="submit"], button[type="submit"]'):
                return "available"
    if re.search(r"\bsold\b", soup.get_text(" ", strip=True), re.I):
        return "sold"
    raise ValueError("missing availability control on product page")


def _coincom(card, base):
    title = card.select_one(".pb5 ~ b")
    if _reject(_text(title)):
        return None
    ident = card.get("id", "").removeprefix("coin")
    link = card.select_one('a[href*="find1Coins.cgi?inventory="]')
    add = card.select_one('a[href*="addItem.cgi?inventory="]')
    if not add or "add to cart" not in _text(add).lower():
        raise ValueError("missing availability control")
    return _listing(card, base, ident=ident, link=link, title=title, price=card,
                    currency="USD", image=card.select_one("img"), category="Roman")


_ADAPTERS = {
    "silbury": ("li.product", _silbury),
    "palmyra": ("li.product", _palmyra),
    "vpuk": ("#product-grid li.grid__item", _vpuk),
    "premium": (".product-item", _premium),
    "historynumis": (".elementor-product-miniature", _historynumis),
    "shanna": (".ProductList-item", _shanna),
    "roman-republic": (".main-products .product-layout", _roman_republic),
    "mrb": ('a[href*="lotinfo.pl?id="]', _mrb),
    "coincom": ('td[id^="coin"]', _coincom),
}


def _page_url(base: str, number: int, adapter: str) -> str:
    if number == 1:
        return base
    if adapter in {"silbury", "palmyra"}:
        return base.rstrip("/") + f"/page/{number}/"
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(number)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _page_count(soup, adapter: str) -> int:
    # Read numeric page links in the catalog's own pager, not site-wide links.
    selector = {"silbury": ".woocommerce-pagination a.page-numbers", "palmyra": ".woocommerce-pagination a.page-numbers", "vpuk": ".pagination a", "premium": ".pagination a", "historynumis": ".pagination a", "roman-republic": ".pagination a", "coincom": ".pagination a"}.get(adapter)
    if not selector:
        return 1
    numbers = [int(_text(a)) for a in soup.select(selector) if _text(a).isdigit()]
    return max(numbers, default=1)


def scrape_source(source: dict, fetcher) -> ScrapeResult:
    """Return parsed catalog rows; never mark a truncated or failed sweep complete."""
    adapter = source.get("adapter", "")
    if adapter not in _ADAPTERS:
        return ScrapeResult(complete=False, error=f"unsupported adapter: {adapter or 'none'}")
    selector, parser = _ADAPTERS[adapter]
    base = source["url"]
    listings: list[Listing] = []
    pages = 0
    last_page = 1
    seen_ids: set[str] = set()
    card_errors: list[str] = []
    detail_fallbacks = 0
    for number in range(1, MAX_PAGES + 1):
        url = _page_url(base, number, adapter)
        try:
            page = fetcher.get(url)
            body = page.text
            if adapter == "historynumis" and body.lstrip().startswith("{"):
                payload = json.loads(body)
                body = payload["rendered_products"]
            soup = BeautifulSoup(body, "html.parser")
        except Exception as exc:
            return ScrapeResult(listings, pages, False, f"{url}: {exc}")
        pages += 1
        cards = soup.select(selector)
        if not cards:
            return ScrapeResult(listings, pages, False, f"no {adapter} product cards at {url}")
        for card in cards:
            try:
                if adapter == "shanna" and not _text(card.select_one(".ProductList-title")):
                    try:
                        if detail_fallbacks >= 5:
                            raise ValueError("detail fallback limit of five per sweep reached")
                        detail_fallbacks += 1
                        title = _shanna_detail_title(card, page.url, fetcher)
                    except Exception as exc:
                        raise ValueError(f"product {card.get('data-item-id') or '(no ID)'}: missing title; detail fallback: {exc}") from exc
                    item = _shanna(card, page.url, title=title)
                else:
                    item = parser(card, page.url)
            except ValueError as exc:
                # One broken card must not hide valid stock later in the catalog.
                # Retain the error so this sweep cannot replace a complete baseline.
                card_errors.append(f"{url}: {exc}")
                continue
            if item and item.external_id not in seen_ids:
                if adapter == "mrb":
                    try:
                        detail = fetcher.get(item.url)
                        item.availability = _mrb_availability(detail.text, item.external_id)
                    except Exception as exc:
                        return ScrapeResult(listings, pages, False, f"{item.url}: {exc}")
                listings.append(item)
                seen_ids.add(item.external_id)
        if number == 1:
            last_page = _page_count(soup, adapter)
        if number >= last_page:
            if card_errors:
                details = "; ".join(card_errors[:3])
                if len(card_errors) > 3:
                    details += f"; and {len(card_errors) - 3} more"
                return ScrapeResult(listings, pages, False, f"{len(card_errors)} unreadable product cards: {details}")
            return ScrapeResult(listings, pages)
    return ScrapeResult(listings, pages, False, f"catalog exceeds {MAX_PAGES}-page safety limit")
