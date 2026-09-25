"""Conservative, one-page verification of individual fixed-price ancient coins."""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .fetch import validate_url_shape

_INFO_HOSTS = ('wikipedia.org', 'wikimedia.org', 'numista.com', 'acsearch.info', 'coinarchives.com',
               'numisbids.com', 'sixbid.com', 'wildwinds.com', 'numismatics.org', 'reddit.com',
               'catawiki.com', 'cointalk.com',
               'facebook.com', 'instagram.com', 'youtube.com', 'pinterest.com', 'x.com', 'twitter.com')
_INFO_PATH = re.compile(r'/(?:blog|blogs|article|articles|news|wiki|reference|references|guide|guides|auction|auctions|archive|sold)(?:/|$)', re.I)
_COIN = re.compile(r'\b(?:coin|coins|denarius|denarii|denier|tetradrachm\w*|drachm\w*|drachme|obol\w*|stater\w*|aureus|solidus|solidi|sesterti\w*|follis|antoninian\w*|nummus|tremissis|didrachm\w*|hemidrachm\w*|as|dupondius|münz\w*|monnaie\w*|moneda\w*|monet[ae]|moeda\w*)\b', re.I)
# "Antigua/antique/antike" alone means old, not necessarily an ancient-period coin.
_ANCIENT = re.compile(r'\b(?:ancient|roman(?:s|[ao]s?|[ei])?|romain(?:es?|s)?|greek|griech\w*|grieg[oa]s?|grecs?|grecques?|greco|greca|greci|greche|römisch\w*|byzant\w*|bizantin\w*|celtic|denarius|tetradrachm\w*|hemidrachm\w*|didrachm\w*|aureus|solidus|sesterti\w*|follis|antoninian\w*|nummus|obol\w*|stater\w*|drachm\w*|tremissis|B\.?C\.?E?|A\.?D|v\.\s*Chr|\d{1,4}\s*(?:a\.?\s*C|av\.?\s*J[.\s-]*C))\b', re.I)
_EXCLUDED = re.compile(r'\b(?:replica|reproduction|copy|copies|book|books|guide|catalogue|catalog|bulk|lot of|lots of|lotes?|postcards?|postal(?:es)?|billetes?|banknotes?|uncleaned|unidentified|unsearched|modern|medal|medallion|token|202\d|201\d|200\d|19\d\d|18\d\d|50p)\b|^\s*\d+\s+(?:(?:ancient|roman|greek|byzantine|celtic)\s+)+coins?\b', re.I)
_NEGATIVE = re.compile(r'\b(?:sold(?:\s*out)?|out[ -]of[ -]stock|outofstock|reserved|unavailable|not available|pre[ -]?order|back[ -]?order\w*|ausverkauft|nicht verfügbar|vendu\w*|vendido\w*|agotado\w*|esaurito|épuisé|indisponible)\b', re.I)
_IN_STOCK = re.compile(r'\b(?:in[ -]stock|instock|available|auf lager|vorrätig|verfügbar|en stock|disponible|disponibili\w*|disponivel|disponível|em estoque)\b', re.I)
_BUY = re.compile(r'\b(?:add to (?:cart|basket|bag)|buy now|purchase|in den warenkorb|ajouter au panier|ajouter au chariot|aggiungi al carrello|añadir al carrito|agregar al carrito|adicionar ao carrinho|comprar|kopen)\b', re.I)
_BID = re.compile(r"\b(?:current bid|starting bid|place(?: a)? bid|bid now|bidding ends|auction ends|buyer's premium|buyer’s premium)\b", re.I)
_RELATED = '.related, .related-products, .related_products, .upsells, .up-sells, .cross-sells, .recommendations, .product-recommendations, nav, header, footer, aside'
_PRICE_SELECTORS = ('ins .amount', 'ins', '.price-new', '.price-item--sale', '[itemprop="price"]', '.price', '.product-price', '.ProductItem-product-price', '.woocommerce-Price-amount')
_CURRENCIES = {'USD', 'GBP', 'EUR', 'CAD', 'AUD', 'NZD', 'CHF', 'JPY', 'CNY', 'HKD', 'SGD', 'SEK', 'NOK', 'DKK', 'PLN', 'CZK', 'HUF', 'RON', 'BRL', 'INR', 'ZAR', 'TRY', 'ILS'}


def individual_ancient_coin_title(title: str) -> bool:
    """Conservative title-only classification; this does not establish a live sale."""
    return isinstance(title, str) and bool(
        not _EXCLUDED.search(title) and _COIN.search(title) and _ANCIENT.search(title))


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _name(text):
    return ' '.join(re.findall(r'\w+', str(text).casefold()))


def _identity(url):
    parsed = validate_url_shape(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith('utm_') and key.lower() not in ('gclid', 'fbclid')]
    return urlunsplit((parsed.scheme, parsed.netloc.lower().removeprefix('www.'), parsed.path.rstrip('/') or '/', urlencode(query), ''))


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _types(node):
    value = node.get('@type', [])
    return {str(item).rsplit('/', 1)[-1] for item in (value if isinstance(value, list) else [value])}


def _visible(node):
    for parent in [node, *node.parents]:
        if not hasattr(parent, 'attrs'):
            continue
        classes = parent.get('class', [])
        style = re.sub(r'\s+', '', parent.get('style', '').lower())
        if (parent.has_attr('hidden') or parent.get('aria-hidden') == 'true'
                or 'display:none' in style or 'visibility:hidden' in style
                or 'hidden' in classes or 'd-none' in classes):
            return False
    return True


def _purchase(scope, base):
    for control in scope.select('button, input[type="submit"], input[type="button"], a[href], [role="button"]'):
        label = _text(control) + ' ' + str(control.get('value', '')) + ' ' + str(control.get('aria-label', ''))
        if not _BUY.search(label) or not _visible(control):
            continue
        if any(parent.has_attr('disabled') or parent.get('aria-disabled') == 'true'
               or 'disabled' in parent.get('class', []) for parent in [control, *control.parents] if hasattr(parent, 'attrs')):
            continue
        if control.name == 'a':
            href = control.get('href', '')
            if not href or href.startswith('#'):
                continue
            try:
                target = validate_url_shape(urljoin(base, href))
                if target.hostname != urlsplit(base).hostname or not re.search(r'(?:add.?to.?cart|cart|basket|checkout|purchase|buy)', href, re.I):
                    continue
            except ValueError:
                continue
            except Exception:
                continue
        return True
    return False


def _amount(value, *, structured=False):
    text = str(value).strip().replace('\u00a0', '').replace(' ', '')
    if not re.fullmatch(r'\d[\d.,]*', text):
        return ''
    if structured:
        if not re.fullmatch(r'\d+(?:\.\d+)?', text):
            return ''
    elif ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.') if text.rfind(',') > text.rfind('.') else text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.') if len(text.rsplit(',', 1)[1]) == 2 else text.replace(',', '')
    elif text.count('.') == 1 and len(text.rsplit('.', 1)[1]) == 3:
        text = text.replace('.', '')
    try:
        amount = Decimal(text)
        return format(amount, '.2f') if amount.is_finite() and 0 < amount < Decimal('1000000000000') else ''
    except InvalidOperation:
        return ''


def _dom_price(scope, currency_hint=''):
    quote = re.compile(r'\b(?:price (?:on|upon) request|price on application|contact (?:us )?for (?:a )?price|call for (?:a )?price|prix sur demande|preis auf anfrage)\b', re.I)
    if any(_visible(node.parent) for node in scope.find_all(string=quote)):
        return '', '', True
    candidates = [node for selector in _PRICE_SELECTORS for node in scope.select(selector)]
    node = next((node for node in candidates if _visible(node)), None)
    if node is None:
        # Hidden prices cannot be used, including through a stale JSON-LD fallback.
        return '', '', bool(candidates)
    value = str(node.get('content') or _text(node))
    if re.search(r'\b(?:from|starting|ab|à partir)\b', value, re.I):
        return '', '', True
    currency_node = scope.select_one('[itemprop="priceCurrency"]')
    currency = str(currency_node.get('content') or _text(currency_node)).upper() if currency_node else currency_hint
    for symbol, code in (('£', 'GBP'), ('€', 'EUR'), ('US$', 'USD'), ('CA$', 'CAD'), ('AU$', 'AUD')):
        if symbol in value:
            currency = code
            break
    else:
        code = re.search(r'\b(' + '|'.join(sorted(_CURRENCIES)) + r')\b', value)
        if code:
            currency = code.group(1)
    amounts = re.findall(r'\d[\d.,\s\u00a0]*', value)
    price = _amount(amounts[0], structured=node.has_attr('content')) if len(amounts) == 1 else ''
    return price, currency if currency in _CURRENCIES else '', True


def _product_scope(h1):
    for parent in h1.parents:
        classes = set(parent.get('class', []))
        itemtype = str(parent.get('itemtype', ''))
        if ('schema.org/Product' in itemtype or classes.intersection({
                'product', 'product-detail', 'product-details', 'product-single', 'product-info',
                'product__info-container', 'ProductItem'}) or parent.get('id') in ('product', 'product-details')):
            return parent, True
        if parent.name in ('main', 'body'):
            return parent, False
    return h1.parent, False


def _check_page(result, page):
    original = validate_url_shape(result['url'])
    actual = validate_url_shape(page.url)
    if original.hostname.lower().removeprefix('www.') != actual.hostname.lower().removeprefix('www.'):
        return 'unverified', 'The link redirected to another dealer domain.', '', '', ''
    if _INFO_PATH.search(actual.path):
        return 'rejected', 'This is an informational, archive, or auction page.', '', '', ''
    soup = BeautifulSoup(page.text, 'html.parser')
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes.extend(_walk(json.loads(script.string or script.get_text())))
        except (ValueError, TypeError, RecursionError):
            continue
    for node in soup.select(_RELATED + ', script, style'):
        node.decompose()
    headings = [node for node in soup.select('h1') if _visible(node)]
    if len(headings) != 1:
        return 'unverified', 'A single primary product could not be identified.', '', '', ''
    title = _text(headings[0])[:500]
    if _NEGATIVE.search(title):
        return 'rejected', 'The seller marks this primary item sold, unavailable, reserved, or not ready to ship.', '', '', ''
    if any(_types(node).intersection({'Article', 'NewsArticle', 'BlogPosting'}) for node in nodes):
        return 'rejected', 'This page is an article rather than an individual coin listing.', '', '', ''
    if not individual_ancient_coin_title(title):
        return 'rejected', 'The primary item is not a supported individual ancient coin.', '', '', ''
    scope, explicit_product = _product_scope(headings[0])
    if any(node.select_one('h1, h2, h3, h4, h5, h6, [itemprop="name"]')
           for node in scope.select('.product, .product-card, [itemtype*="schema.org/Product"]')):
        return 'unverified', 'The page contains separate product cards rather than one clear purchase scope.', '', '', ''
    if _BID.search(_text(scope)):
        return 'rejected', 'The page offers bidding rather than a fixed-price purchase.', '', '', ''
    stock_nodes = scope.select('.stock, .availability, .inventory, [itemprop="availability"], [data-availability], button, [role="button"]')
    stock_text = ' '.join(_text(node) + ' ' + str(node.get('href', '')) + ' ' + str(node.get('content', ''))
                          + ' ' + str(node.get('data-availability', '')) for node in stock_nodes if _visible(node))
    stock_text += ' ' + ' '.join(scope.get('class', []))
    # A plain sold badge still overrides stale structured data; historical provenance does not.
    stock_text += ' ' + ' '.join(_text(node) for node in scope.select('p, span, div, strong')
                                 if _visible(node) and _NEGATIVE.fullmatch(_text(node).strip(' .!')))
    if (_NEGATIVE.search(stock_text) or re.search(r'\b0\s+(?:available|in stock)\b', stock_text, re.I)
            or re.search(r'\b(?:this (?:coin|item|product) is|availability:)\s*(?:sold|unavailable|reserved)', _text(scope), re.I)):
        return 'rejected', 'The seller marks this item sold, unavailable, reserved, or not ready to ship.', '', '', ''
    page_id = _identity(page.url)
    product = None
    for node in nodes:
        if 'Product' not in _types(node) or _name(node.get('name', '')) != _name(title):
            continue
        identity = node.get('url') or node.get('@id')
        if isinstance(identity, str):
            if _identity(urljoin(page.url, identity)) != page_id:
                continue
        elif not explicit_product:
            continue
        product = node
        break
    if not explicit_product and product is None:
        return 'unverified', 'The page does not establish an individual product identity.', '', '', ''
    structured_price = structured_currency = structured_stock = ''
    if product:
        offers = product.get('offers', {})
        if isinstance(offers, list):
            offers = offers[0] if len(offers) == 1 else {}
        if isinstance(offers, dict):
            if 'AggregateOffer' in _types(offers):
                return 'rejected', 'The page describes a catalog or price range rather than one coin.', '', '', ''
            offer_url = offers.get('url')
            if offer_url and (not isinstance(offer_url, str) or _identity(urljoin(page.url, offer_url)) != page_id):
                return 'unverified', 'The structured offer belongs to a different product page.', '', '', ''
            availability = str(offers.get('availability', '')).rsplit('/', 1)[-1]
            if availability and availability != 'InStock':
                return 'rejected', 'The product offer is not marked in stock.', '', '', ''
            structured_stock = availability
            structured_price = _amount(offers.get('price', ''), structured=True)
            structured_currency = str(offers.get('priceCurrency', '')).upper()
    if not _purchase(scope, page.url):
        return 'unverified', 'No enabled fixed-price purchase control was found for this product.', '', '', ''
    if not (structured_stock == 'InStock' or _IN_STOCK.search(stock_text)):
        return 'unverified', 'The seller page does not explicitly establish that the coin is in stock.', '', '', ''
    price, currency, dom_price_present = _dom_price(scope, structured_currency)
    if not dom_price_present:
        price, currency = structured_price, structured_currency
    if not price or currency not in _CURRENCIES:
        return 'unverified', 'A positive fixed price and unambiguous currency could not be confirmed.', '', '', ''
    return 'available', 'Seller product page shows a fixed price, in-stock status, and an enabled purchase control.', price, currency, title


def verify_sale(result: dict, fetcher) -> dict:
    """Fetch one seller page; never trust a search snippet as sale evidence."""
    checked = dict(result)
    checked.update(sale_status='unverified', price='', currency='',
                   sale_checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), sale_reason='')
    try:
        parsed = validate_url_shape(result.get('url', ''))
        host = parsed.hostname.lower().removeprefix('www.')
        is_catalog = (host == 'todocoleccion.net' or host.endswith('.todocoleccion.net')) and parsed.path.startswith('/s/')
        if any(host == domain or host.endswith('.' + domain) for domain in _INFO_HOSTS) or _INFO_PATH.search(parsed.path) or is_catalog:
            checked.update(sale_status='rejected', sale_reason='This is an informational, social, archive, or auction link.')
            return checked
        page = fetcher.get(result['url'])
        status, reason, price, currency, title = _check_page(result, page)
        checked.update(sale_status=status, sale_reason=reason, price=price, currency=currency)
        if status == 'available':
            checked['title'] = title
    except Exception:
        checked.update(sale_reason='The seller page could not be inspected reliably; availability is unverified.')
    return checked
