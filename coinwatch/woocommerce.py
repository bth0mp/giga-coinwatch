"""Bounded WooCommerce catalogs for individually audited fixed-price dealers."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .models import Listing, ScrapeResult


MAX_PAGES = 50
# Currency was confirmed on primary product pages, not inferred from '$'.
CATALOGS = {
    'bargainbinancients': ('USD', 'Greek', 'https://bargainbinancients.com/product-category/greek-coins/'),
    'pashiz': ('GBP', 'Greek', 'https://www.pashizcoins.com/product-category/greek/'),
    'praefectuscoins-com': ('USD', 'Ancient', 'https://praefectuscoins.com/product-category/showcase/'),
    'vilmarnumismatics-com': ('USD', 'Ancient', 'https://vilmarnumismatics.com/product-category/ancient-coins/'),
    'goldenrule': ('USD', 'Ancient', 'https://www.goldenruleenterprises.org/product-category/ancient-coins/'),
    'bactrianumis-com': ('USD', 'Greek', 'https://bactrianumis.com/product-category/oriental-greek-coins/'),
}
_DIVI = {'praefectuscoins-com', 'vilmarnumismatics-com'}
_EXCLUDED = re.compile(
    r'\b(?:uncleaned|unidentified|unsearched|bulk|grab bag|replica|reproduction|copy|modern|'
    r'banknotes?|medals?|postcards?|jewell?ery|glass|flask|tessera|tesserae|amulet|intaglio|'
    r'fibula|brooch|arrowhead|oil lamp|ring|pendant)\b|\blot(?:s)?\s+(?:of|\d)|'
    r'^\s*\d+\s+(?:ancient|roman|greek|byzantine|celtic)\s+coins?\b', re.I)
_COIN = re.compile(
    r'\b(?:coins?|denarius|denarii|aureus|drachm|drachma|tetradrachm|hemidrachm|didrachm|'
    r'obol|diobol|hemiobol|trihemiobol|tetrobol|triobol|stater|sestertius|sestertii|as|'
    r'dupondius|follis|antoninianus|nummus|nummi|solidus|tremissis|siliqua|cistophorus|'
    r'pentanummium|decanummium|tetrassarion|pentassarion|assarion|assaria|shekel|prutah|'
    r'quadrans|quadrigatus|centenionalis|chalkous|dichalkon|trichalkon|hekte|unit|hemilitron)\b|'
    r'\b(?:AE|Æ|AR|AV)\s*\d', re.I)


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _dealer_url(base, href):
    if not href:
        raise ValueError('missing product URL')
    url = urljoin(base, href)
    parsed, origin = urlsplit(url), urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or parsed.hostname != origin.hostname
            or parsed.username or parsed.password or parsed.port not in (None, 80, 443)):
        raise ValueError('URL left the validated dealer site')
    if parsed.query:
        raise ValueError('unexpected query in catalog or product URL')
    return urlunsplit((origin.scheme, origin.netloc, parsed.path, '', ''))


def _page_number(base, url):
    clean = _dealer_url(base, url)
    path, root = urlsplit(clean).path, urlsplit(base).path.rstrip('/')
    if path.rstrip('/') == root:
        return 1, base
    match = re.fullmatch(re.escape(root) + r'/page/([1-9]\d*)/?', path)
    if not match:
        raise ValueError('pagination left the validated catalog scope')
    return int(match[1]), clean.rstrip('/') + '/'


def _price(card, currency):
    node = card.select_one('.price')
    if node is None:
        raise ValueError('missing price')
    node = node.select_one('ins') or node
    # A range or two current amounts is not one fixed purchase price.
    raw = _text(node)
    symbol = {'USD': '$', 'GBP': '£'}[currency]
    match = re.fullmatch(re.escape(symbol) + r'\s*([\d,]+(?:\.\d{1,2})?)', raw)
    if not match:
        raise ValueError('missing or ambiguous fixed price/currency')
    number = match[1]
    if ',' in number and not re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?', number):
        raise ValueError('invalid price grouping')
    try:
        value = Decimal(number.replace(',', ''))
    except InvalidOperation as exc:
        raise ValueError('invalid price') from exc
    if value <= 0:
        raise ValueError('invalid price')
    return f'{value:.2f}'


def _identity(card):
    ids = [match[1] for value in card.get('class', []) if (match := re.fullmatch(r'post-(\d+)', value))]
    if len(ids) != 1:
        raise ValueError('missing product identity')
    return ids[0]


def _sale_marker(card, title):
    markers = r'(sold(?:\s+out)?|reserved|on\s+hold|unavailable|out\s+of\s+stock)'
    # Restrict title evidence to current prefix/suffix markers; provenance such
    # as "EX COLLECTION SOLD IN 1926" must not change present availability.
    match = re.search(r'^\s*[\[(]?\s*' + markers + r'\b(?!\s+(?:in|at|by|on)\b)', title, re.I)
    match = match or re.search(r'(?:[-–—:|]|[\[(])\s*' + markers + r'\s*[\])]?[.!]?\s*$', title, re.I)
    texts = [match[1]] if match else []
    texts.extend(_text(node) for node in card.select(
        '.stock, .stock-status, .soldout, .sold-out, .reserved, .woosticker_sold, .soldout_ribbon_left, .soldout_ribbon_right'))
    for text in texts:
        if re.fullmatch(r'sold(?:\s+out)?|unavailable|out\s+of\s+stock', text.strip(), re.I):
            return 'sold'
        if re.fullmatch(r'reserved|on\s+hold', text.strip(), re.I):
            return 'reserved'
    return None


def _parse_card(card, base, source_id, currency, category):
    ident = _identity(card)
    title_node = card.select_one('.woocommerce-loop-product__title, .wd-entities-title')
    title = _text(title_node)
    if not title:
        raise ValueError('missing product title')
    classes = set(card.get('class', []))
    if _EXCLUDED.search(title) or any('lots-and-wholesale' in x or 'antiquities' in x for x in classes):
        return None
    # Showcase mixes coins with objects, including some objects mislabeled Greek.
    # Require a denomination or a coin-sized metal description, not just 'Ancient'.
    if source_id == 'praefectuscoins-com' and not _COIN.search(title):
        return None
    marker = _sale_marker(card, title)
    price_text = _text(card.select_one('.price'))
    if not price_text or (('outofstock' in classes or marker) and price_text.lower() in ('sold', 'sold out', 'reserved')):
        return None
    link = title_node.select_one('a[href]') or card.select_one('a.woocommerce-LoopProduct-link, a.product-image-link')
    url = _dealer_url(base, link.get('href') if link else None)
    control = card.select_one('.add_to_cart_button')
    if control and control.get('data-product_id') != ident:
        raise ValueError('purchase control product identity mismatch')
    if marker:
        availability = marker
    elif 'outofstock' in classes:
        availability = 'sold'
    elif {'instock', 'purchasable', 'product-type-simple'} <= classes:
        if source_id not in _DIVI and (not control or control.has_attr('disabled') or control.get('aria-disabled') == 'true'):
            raise ValueError('missing enabled purchase control')
        availability = 'available'
    else:
        raise ValueError('missing stock or purchase status')
    price = _price(card, currency)
    image = card.select_one('img')
    image_url = ''
    if image:
        for attribute in ('data-src', 'data-lazy-src', 'src'):
            candidate = urljoin(base, image.get(attribute, ''))
            if image.get(attribute) and urlsplit(candidate).scheme in ('https', 'http'):
                image_url = candidate
                break
    categories = ' '.join(x.removeprefix('product_cat-') for x in classes if x.startswith('product_cat-'))
    for expression, name in [(r'roman', 'Roman'), (r'byzantine', 'Byzantine'), (r'celtic', 'Celtic'), (r'greek|hellenistic', 'Greek')]:
        if re.search(expression, categories, re.I):
            category = name
            break
    return Listing(ident, url, title, price, currency, image_url=image_url, category=category, availability=availability)


def scrape_woocommerce(source, fetcher):
    """Follow observed category pagination; retain valid cards on any partial failure."""
    result = ScrapeResult()
    errors = []
    source_id = source.get('id')
    config = CATALOGS.get(source_id)
    if not config or source.get('url', '').rstrip('/') != config[2].rstrip('/'):
        return ScrapeResult(complete=False, error='WooCommerce source or catalog scope has not been validated.')
    currency, category, base = config
    pending, visited, seen_products = {1: base}, set(), set()
    expected_total = None
    while pending:
        if result.pages >= MAX_PAGES:
            errors.append(f'Catalog page limit ({MAX_PAGES}) reached; more pages remain.')
            break
        number = min(pending)
        url = pending.pop(number)
        if number in visited:
            continue
        visited.add(number)
        try:
            page = fetcher.get(url)
            returned_number, _ = _page_number(base, page.url)
            if returned_number != number:
                raise ValueError('catalog redirected to a different page')
            soup = BeautifulSoup(page.text, 'html.parser')
            result.pages += 1
            count = _text(soup.select_one('.woocommerce-result-count'))
            total_match = re.search(r'(?:of|all)\s+([\d,]+)\s+results?', count, re.I)
            if not total_match and re.search(r'Showing the single result', count, re.I):
                total_match = re.match(r'(1)', '1')
            if total_match:
                total = int(total_match[1].replace(',', ''))
                if expected_total is not None and expected_total != total:
                    errors.append('Catalog product count changed during pagination.')
                expected_total = total
            cards = soup.select('.product.type-product')
            if cards and not total_match:
                errors.append('Missing catalog product count; completeness cannot be confirmed.')
            if not cards and not re.search(r'No products were found', _text(soup.select_one('.woocommerce-info, .woocommerce-no-products-found')), re.I):
                raise ValueError('no recognized catalog products or explicit empty catalog')
            repeated = 0
            for card in cards:
                try:
                    ident = _identity(card)
                    if ident in seen_products:
                        repeated += 1
                        continue
                    seen_products.add(ident)
                    listing = _parse_card(card, page.url, source_id, currency, category)
                    if listing:
                        result.listings.append(listing)
                except ValueError as exc:
                    identity = next((value for value in card.get('class', []) if value.startswith('post-')), 'unknown')
                    errors.append(f'Product {identity}: {exc}')
            if repeated:
                errors.append(f'{repeated} repeated product IDs on catalog page {number}.')
                if repeated == len(cards):
                    break
            for anchor in soup.select('.woocommerce-pagination a[href], a.page-numbers[href]'):
                next_number, next_url = _page_number(base, urljoin(page.url, anchor['href']))
                if next_number not in visited:
                    pending[next_number] = next_url
        except Exception as exc:
            errors.append(f'{url}: {exc}')
            break
    if expected_total is not None and len(seen_products) != expected_total:
        errors.append(f'Catalog reports {expected_total} products; observed {len(seen_products)} unique product IDs.')
    result.complete = not errors
    if errors:
        result.error = '; '.join(errors[:12]) + (f'; {len(errors) - 12} more errors.' if len(errors) > 12 else '')
    return result
