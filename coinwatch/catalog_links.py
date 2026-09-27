"""Pure, bounded discovery of product links on public dealer catalog pages.

These are leads only. A seller product page must still pass sale verification and
the full wanted-search matcher before it can appear as an available coin.
"""
import json
import re
from itertools import islice
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .fetch import FetchError, validate_url_shape
from .sale_checks import (_BID, _INFO_HOSTS, _INFO_PATH, _amount, _types, _visible,
                          _walk, individual_ancient_coin_title)
from .searches import web_matcher

MAX_PRODUCTS = 20
MAX_NEXT_PAGES = 2
_CARDS = ('.product-card, .product-item, .product, .card-wrapper, .card--product, .is-product, '
          '[itemtype*="schema.org/Product"], [data-hook="product-list-grid-item"], '
          'table.productListingData > tr, table.productListingData > tbody > tr')
_RELATED = ('nav, header, footer, aside, [role="navigation"], .related, .related-products, '
            '.related_products, .upsells, .up-sells, .cross-sells, .recommendations, '
            '.product-recommendations')
_PRODUCT_PATH = re.compile(r'/(?:products?|product-page|item|itm|listing)/|/product_info\.php$|-p-\d+\.html$', re.I)
_ACTION_PATH = re.compile(r'/(?:cart|basket|checkout|account|my-account|login|log-in|signin|sign-in|logout|register|wishlist|compare|wp-admin|download)(?:/|\.|$)', re.I)
_ACTION_KEYS = {'action', 'add', 'add-to-cart', 'add_to_cart', 'addtocart', 'cart', 'checkout', 'buy',
                'bid', 'remove', 'delete', 'add_to_wishlist', 'add-to-wishlist', 'wishlist', 'compare'}
_PAGE_KEYS = {'page', 'p', 'paged'}
_SORT_KEYS = {'sort', 'sort_by', 'orderby', 'order'}
_AUCTION_HOSTS = ('biddr.com', 'liveauctioneers.com', 'invaluable.com', 'the-saleroom.com')
_MONEY = re.compile(r'(?:[$£€]|\b(?:USD|EUR|GBP|AUD|CAD|CHF|SGD)\b)\s*(\d[\d.,]*)|'
                    r'(\d[\d.,]*)\s*(?:[$£€]|\b(?:USD|EUR|GBP|AUD|CAD|CHF|SGD)\b)', re.I)
_BUY = re.compile(r'\b(?:add to (?:cart|basket)|buy now|in den warenkorb|ajouter au panier|añadir al carrito)\b', re.I)


def same_dealer(url1, url2):
    """Allow only www equivalence, with the same effective standard web port."""
    try:
        left, right = validate_url_shape(url1), validate_url_shape(url2)
        return (left.hostname.lower().removeprefix('www.') == right.hostname.lower().removeprefix('www.')
                and (left.port or (443 if left.scheme == 'https' else 80))
                == (right.port or (443 if right.scheme == 'https' else 80)))
    except (FetchError, ValueError, TypeError):
        return False


def _information_url(parts):
    host = parts.hostname.lower().removeprefix('www.')
    path = unquote(parts.path)
    return (any(host == domain or host.endswith('.' + domain) for domain in _INFO_HOSTS + _AUCTION_HOSTS)
            or bool(_INFO_PATH.search(path) or re.search(r'/(?:auction[-_]|auction_catalog)', path, re.I)))


def _safe_url(base, href):
    if not isinstance(href, str) or not href or any(ord(char) < 33 for char in href):
        return ''
    try:
        parts = validate_url_shape(urljoin(base, href))
        if not same_dealer(base, urlunsplit(parts)) or _information_url(parts):
            return ''
        path = unquote(parts.path)
        if (_ACTION_PATH.search(path) or re.search(r'\.(?:pdf|zip|jpg|jpeg|png|gif|webp|svg|xml|json)$', path, re.I)):
            return ''
        query = parse_qsl(parts.query, keep_blank_values=True)
        if any(key.lower() in _ACTION_KEYS or (key.lower() in {'sold', 'archive', 'auction'} and value.lower() not in {'0', 'false', 'no', ''})
               for key, value in query):
            return ''
        clean = sorted((key, value) for key, value in query if not key.lower().startswith('utm_')
                       and key.lower() not in {'gclid', 'fbclid', 'msclkid', 'oscsid', 'ceid'})
        netloc = parts.hostname.lower()
        if parts.port and parts.port != (443 if parts.scheme == 'https' else 80):
            netloc += ':' + str(parts.port)
        return urlunsplit((parts.scheme, netloc, parts.path or '/', urlencode(clean), ''))
    except (FetchError, ValueError, TypeError):
        return ''


def _key(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.removeprefix('www.'), parts.path.rstrip('/') or '/', parts.query, ''))


def is_catalog_url(url):
    """Conservative URL hint for retrying retained catalogs; never sale evidence."""
    clean = _safe_url(url, url)
    if not clean:
        return False
    parts = urlsplit(clean)
    query = dict(parse_qsl(parts.query))
    path = unquote(parts.path)
    if _PRODUCT_PATH.search(path) or any(key.lower() in {'products_id', 'product_id', 'coinid', 'zpg'} for key in query):
        return False
    return bool(re.search(r'/(?:collections?|product-category|categories|category)/[^/]+|/greek-c-\d+\.html$|/[^/]+-c-\d+\.html$', path, re.I)
                or path.rstrip('/').lower() in {'/shop', '/store', '/catalog'}
                or (path.endswith('/index.php') and re.fullmatch(r'\d+(?:_\d+)*', query.get('cPath', '')))
                or (path.endswith('/roman-and-greek-coins.asp') and query.get('vpar', '').isdigit()))


def _text(node):
    return ' '.join(str(value).strip() for value in node.find_all(string=True)
                    if value.parent.name not in {'script', 'style'} and _visible(value.parent) and str(value).strip())


def _shop_evidence(card):
    text = _text(card)
    return bool(_BUY.search(text) or any(_amount(first or second) for first, second in _MONEY.findall(text)))


def _offer_evidence(product):
    offer = product.get('offers')
    if isinstance(offer, list):
        offer = offer[0] if len(offer) == 1 else None
    return (isinstance(offer, dict) and 'AggregateOffer' not in _types(offer)
            and bool(_amount(offer.get('price', ''), structured=True))
            and bool(re.fullmatch(r'[A-Z]{3}', str(offer.get('priceCurrency', '')))))


def _page_signature(url):
    parts = urlsplit(url)
    path = parts.path.rstrip('/')
    path_page = re.search(r'/page/(\d+)$', path, re.I)
    number = int(path_page.group(1)) if path_page else 1
    if path_page:
        path = path[:path_page.start()]
    rest = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _PAGE_KEYS:
            if not value.isdigit() or number != 1:
                return None
            number = int(value)
        elif key.lower() not in _SORT_KEYS:
            rest.append((key, value))
    return path, tuple(sorted(rest)), number


def catalog_links(page, search):
    """Return at most 20 relevant product leads and 2 observed category page links."""
    empty = {'products': [], 'next_pages': []}
    base = _safe_url(page.url, page.url)
    if not base or _PRODUCT_PATH.search(urlsplit(base).path):
        return empty
    soup = BeautifulSoup(page.text, 'html.parser')
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            nodes.extend(islice(_walk(json.loads(script.string or script.get_text())), 1000))
        except (ValueError, TypeError, RecursionError):
            continue
    if any(_types(node).intersection({'Article', 'NewsArticle', 'BlogPosting'}) for node in nodes):
        return empty
    for node in nodes:
        if 'Product' in _types(node):
            identity = node.get('url') or node.get('@id')
            target = _safe_url(base, identity)
            if target and _key(target) == _key(base):
                return empty  # Product recommendations are not a primary catalog.
    pager_hrefs = [anchor.get('href') for anchor in soup.select(
        'a[rel~="next"][href], link[rel~="next"][href], .pagination a[href], .pagination__list a[href], '
        '.woocommerce-pagination a[href], a.page-numbers[href], nav[aria-label*="agination"] a[href]')]
    for node in soup.select(_RELATED + ', script, style'):
        node.decompose()
    scope = soup.select_one('main, [role="main"], #content') or soup
    if scope.select_one('article h1') or _BID.search(_text(scope)):
        return empty
    preliminary = web_matcher({**search, 'currency': '', 'max_price': ''})
    products, seen, catalog_evidence = [], set(), False

    def consider(href, title, shop_evidence):
        nonlocal catalog_evidence
        if not shop_evidence or not isinstance(title, str):
            return
        title = ' '.join(title.split())[:500]
        if not individual_ancient_coin_title(title):
            return
        target = _safe_url(base, href)
        if not target or _key(target) == _key(base) or is_catalog_url(target):
            return
        catalog_evidence = True
        key = _key(target)
        if key not in seen and len(products) < MAX_PRODUCTS and preliminary({'title': title}):
            seen.add(key)
            products.append({'url': target, 'title': title, 'snippet': ''})

    for card in scope.select(_CARDS)[:500]:
        if not _visible(card) or not _shop_evidence(card):
            continue
        for anchor in card.select('a[href]'):
            if not _visible(anchor):
                continue
            title = _text(anchor) or anchor.get('title', '')
            if not title:
                image = anchor.select_one('img[alt]')
                title = image.get('alt', '') if image else ''
            consider(anchor['href'], title, True)
    for item_list in nodes:
        if 'ItemList' not in _types(item_list):
            continue
        elements = item_list.get('itemListElement', [])
        if not isinstance(elements, list):
            continue
        for element in elements[:500]:
            if not isinstance(element, dict):
                continue
            product = element.get('item', element)
            if isinstance(product, dict) and 'Product' in _types(product):
                consider(product.get('url') or product.get('@id'), product.get('name'), _offer_evidence(product))
    next_pages = []
    signature = _page_signature(base)
    if catalog_evidence and signature:
        candidates = []
        for href in pager_hrefs:
            target = _safe_url(base, href)
            other = _page_signature(target) if target else None
            if other and other[:2] == signature[:2] and signature[2] < other[2] <= signature[2] + MAX_NEXT_PAGES:
                candidates.append((other[2], target))
        for _, target in sorted(candidates):
            if _key(target) not in {_key(value) for value in next_pages}:
                next_pages.append(target)
            if len(next_pages) == MAX_NEXT_PAGES:
                break
    return {'products': products, 'next_pages': next_pages}
