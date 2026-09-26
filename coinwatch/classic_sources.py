"""Bounded parsers for explicitly verified classic dealer catalog formats."""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .fetch import FetchError, validate_url_shape
from .models import Listing, ScrapeResult

MAX_PAGES = 20
_ACT_CATEGORIES = {'/greek-c-22.html': 'Greek', '/roman-c-23.html': 'Roman', '/byzantine-c-24.html': 'Byzantine'}
_NONCOIN = re.compile(r'\b(?:books?|catalogue|replica|reproduction|lot of|lots of|bulk|coin groups?)\b|^\s*\d+\s+(?:ancient\s+)?(?:greek|roman|byzantine)\s+coins?\b', re.I)
_RANGE = re.compile(r'Displaying\s+(\d+)\s+to\s+(\d+)\s+\(of\s+([\d,]+)\s+products?\)', re.I)


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _title_status(title):
    marker = r'(sold|reserved|vendu|r[eé]serv[eé])'
    prefix = re.search(r'^\W*' + marker + r'\b(?!\s+(?:in|at|by|during)\b)', title, re.I)
    suffix = re.search(r'(?:[-–—:]\s*|[\[(]\s*)' + marker + r'\s*[\])]?\s*$', title, re.I)
    match = prefix or suffix
    if match:
        return 'sold' if match.group(1).lower() in ('sold', 'vendu') else 'reserved'
    return None


def _same_host_url(base, href):
    target = validate_url_shape(urljoin(base, href))
    if target.hostname != urlsplit(base).hostname:
        raise ValueError('catalog link left the verified dealer host')
    return target


def _money(value):
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?', value):
        raise ValueError('missing or invalid fixed price')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or not 0 < amount < Decimal('1000000000000'):
            raise InvalidOperation
    except InvalidOperation as exc:
        raise ValueError('missing or invalid fixed price') from exc
    return format(amount, '.2f')


def _act_card(card, base, category):
    link = card.select_one('.card-title a[href]')
    title = _text(link)
    if not title:
        raise ValueError('missing product title')
    target = _same_host_url(base, link['href'])
    identity = re.search(r'-p-(\d+)\.html$', target.path)
    if not identity:
        raise ValueError('missing stable product identity')
    ident = identity.group(1)
    if _NONCOIN.search(title):
        return ident, None
    button = card.select_one('.btn-buy[data-product-id]')
    if button is None or button['data-product-id'] != ident:
        raise ValueError(f'product {ident}: missing matching stock control')
    stock = button.get('data-in-stock', '')
    if not re.fullmatch(r'\d+', stock):
        raise ValueError(f'product {ident}: stock quantity is unknown')
    # The seller disables <=0 as Sold and >=999 as Reserved in its public page script.
    quantity = int(stock)
    title_status = _title_status(title)
    availability = 'sold' if quantity == 0 or title_status == 'sold' else 'reserved' if quantity >= 999 or title_status == 'reserved' else 'available'
    if availability == 'available' and (button.has_attr('disabled') or button.get('aria-disabled') == 'true' or 'disabled' in button.get('class', [])):
        raise ValueError(f'product {ident}: purchase control is disabled without a stock status')
    price = _money(card.get('data-product-price', ''))
    shown = _text(card.select_one('.card-subtitle'))
    amounts = re.findall(r'([\d,]+\.\d{2})\s*AUD\b', shown)
    if price not in [_money(value.replace(',', '')) for value in amounts]:
        raise ValueError(f'product {ident}: current AUD price is not visible')
    image = card.select_one('img[src]')
    image_url = ''
    if image:
        try:
            image_url = urlunsplit(_same_host_url(base, image['src'].replace(' ', '%20')))
        except (FetchError, ValueError):
            pass  # Optional artwork must not discard a complete price/stock record.
    product_url = urlunsplit((target.scheme, target.netloc, target.path, '', ''))
    return ident, Listing(ident, product_url, title[:500], price, 'AUD', image_url=image_url,
                          category=category, availability=availability)


def _next_act_page(soup, base, expected):
    for anchor in soup.select('.pagination a[href]'):
        parts = urlsplit(urljoin(base, anchor['href']))
        query = parse_qs(parts.query)
        if query.get('page') != [str(expected)]:
            continue
        target = _same_host_url(base, anchor['href'])
        if target.path != urlsplit(base).path or set(query) - {'page', 'sort', 'ceid'}:
            raise ValueError('unexpected catalog pagination link')
        clean = [(key, value) for key, value in parse_qsl(target.query) if key != 'ceid']
        return urlunsplit((target.scheme, target.netloc, target.path, urlencode(clean), ''))
    raise ValueError('catalog reports more products but the next page link is missing')


def scrape_classic(source, fetcher):
    """Keep readable rows on failure, but complete only a consistent full category sweep."""
    if source.get('adapter') == 'gascogne':
        return _scrape_gascogne(source, fetcher)
    listings, seen, errors = [], set(), []
    pages, total, previous_end = 0, None, 0
    base = source.get('url', '')
    try:
        start = validate_url_shape(base)
        category = _ACT_CATEGORIES.get(start.path)
        if (source.get('adapter') != 'ancient-coin-traders' or start.hostname != 'www.ancientcointraders.com'
                or not category or start.query):
            raise ValueError('choose a verified Ancient Coin Traders Greek, Roman, or Byzantine category URL')
        next_url = base
        for number in range(1, MAX_PAGES + 1):
            page = fetcher.get(next_url)
            pages += 1
            actual = _same_host_url(base, page.url)
            if actual.path != start.path:
                raise ValueError('catalog redirected outside the selected ancient category')
            soup = BeautifulSoup(page.text, 'html.parser')
            cards = soup.select('.is-product')
            ranges = {tuple(int(value.replace(',', '')) for value in match) for match in _RANGE.findall(_text(soup))}
            if len(ranges) != 1:
                raise ValueError('catalog product count is missing or inconsistent')
            first, last, reported_total = ranges.pop()
            if total is None:
                total = reported_total
            if reported_total != total or first != previous_end + 1 or last < first or last > total or len(cards) != last - first + 1:
                raise ValueError('catalog page range, card count, or total changed during the sweep')
            previous_end = last
            for card in cards:
                try:
                    ident, item = _act_card(card, page.url, category)
                    if ident in seen:
                        raise ValueError(f'repeated product {ident}; page coverage is uncertain')
                    seen.add(ident)
                    if item:
                        listings.append(item)
                except (FetchError, ValueError, TypeError) as exc:
                    errors.append(str(exc))
            if last == total:
                return ScrapeResult(listings, pages, not errors, '; '.join(errors[:3]) or None)
            next_url = _next_act_page(soup, base, number + 1)
        raise ValueError(f'catalog exceeds the {MAX_PAGES}-page safety limit')
    except Exception as exc:
        errors.append(str(exc))
        return ScrapeResult(listings, pages, False, '; '.join(errors[:3]))


def _visible(node):
    return not any(tag.has_attr('hidden') or tag.get('aria-hidden') == 'true' or
                   re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', tag.get('style', ''), re.I)
                   for tag in [node, *node.parents] if tag.name)


def _gascogne_price(node):
    if node is None or not _visible(node):
        raise ValueError('missing visible EUR price')
    fragment = BeautifulSoup(str(node), 'html.parser')
    for child in list(fragment.select('script, style, del, s, strike, [hidden], [aria-hidden="true"]')):
        child.decompose()
    for child in list(fragment.find_all(style=True)):
        if not _visible(child):
            child.decompose()
    match = re.fullmatch(r'([\d,]+\.\d{2})\s*(?:€|EUR)', _text(fragment))
    if not match:
        raise ValueError('missing unambiguous current EUR price')
    return _money(match.group(1).replace(',', ''))


def _gascogne_url(base, href, *, product=False):
    """Only normalize the dealer's observed HTTP links to its verified HTTPS origin."""
    target = _same_host_url(base, href)
    query = parse_qs(target.query)
    allowed = {'cPath', 'products_id', 'osCsid'} if product else {'cPath', 'sort', 'page', 'osCsid'}
    path = '/catalog/product_info.php' if product else '/catalog/index.php'
    if target.path != path or set(query) - allowed or query.get('cPath') != ['136']:
        raise ValueError('link left the verified Gascogne Greek catalog')
    if product and not re.fullmatch(r'\d+', query.get('products_id', [''])[0]):
        raise ValueError('missing stable Gascogne product identity')
    if any(len(values) != 1 for values in query.values()):
        raise ValueError('ambiguous Gascogne catalog link')
    clean = [(key, value) for key, value in parse_qsl(target.query) if key != 'osCsid']
    return urlunsplit(('https', target.netloc, target.path, urlencode(clean), ''))


def _gascogne_detail(row, base, fetcher):
    cells = row.find_all('td', recursive=False)
    if len(cells) != 4:
        raise ValueError('unrecognized Gascogne catalog row')
    link = cells[1].select_one('a[href]')
    title = _text(link)
    if not title:
        raise ValueError('missing product title')
    product_url = _gascogne_url(base, link['href'], product=True)
    ident = parse_qs(urlsplit(product_url).query)['products_id'][0]
    if _NONCOIN.search(title):
        return ident, None
    catalog_price = _gascogne_price(cells[2])
    page = fetcher.get(product_url)
    actual = _gascogne_url(base, page.url, product=True)
    if parse_qs(urlsplit(actual).query)['products_id'] != [ident]:
        raise ValueError(f'product {ident}: detail redirected to a different coin')
    soup = BeautifulSoup(page.text, 'html.parser')
    forms = soup.select('form[name="cart_quantity"]')
    if len(forms) != 1 or not _visible(forms[0]):
        raise ValueError(f'product {ident}: missing primary product form')
    form = forms[0]
    action = _same_host_url(base, form.get('action', ''))
    action_query = parse_qs(action.query)
    hidden = form.select_one('input[name="products_id"]')
    if (action.path != '/catalog/product_info.php' or action_query.get('products_id') != [ident]
            or action_query.get('action') != ['add_product'] or hidden is None or hidden.get('value') != ident
            or form.get('method', '').lower() != 'post'):
        raise ValueError(f'product {ident}: missing matching purchase form identity')
    headings = [heading for heading in form.select('h1') if _visible(heading)]
    if len(headings) != 2 or _text(headings[1]) != title:
        raise ValueError(f'product {ident}: detail title does not match the catalog')
    price = _gascogne_price(headings[0])
    if price != catalog_price:
        raise ValueError(f'product {ident}: price changed during the sweep')
    stock_text = ' '.join(_text(node) for node in form.select('.stock, .availability, [itemprop="availability"]') if _visible(node))
    title_status = _title_status(title)
    sold = title_status == 'sold' or re.search(r'\b(?:sold|vendu|out of stock|rupture de stock|unavailable)\b', stock_text, re.I)
    reserved = title_status == 'reserved' or re.search(r'\b(?:reserved|r[eé]serv[eé])\b', stock_text, re.I)
    buttons = form.select('button, input[type="submit"], input[type="image"]')
    enabled = any(_visible(button) and not button.has_attr('disabled') and button.get('aria-disabled') != 'true'
                  and 'disabled' not in button.get('class', [])
                  and re.search(r'\bAdd to Cart\b', _text(button) or button.get('value', '') or button.get('alt', ''), re.I)
                  for button in buttons)
    if not sold and not reserved and not enabled:
        raise ValueError(f'product {ident}: availability is unverified; no enabled Add to Cart')
    image_url = ''
    image = cells[0].select_one('img[src]')
    if image:
        try:
            parts = _same_host_url(base, image['src'].replace(' ', '%20'))
            image_url = urlunsplit(('https', parts.netloc, parts.path, parts.query, ''))
        except (FetchError, ValueError):
            pass
    return ident, Listing(ident, product_url, title[:500], price, 'EUR', image_url=image_url,
                          category='Greek', availability='sold' if sold else 'reserved' if reserved else 'available')


def _scrape_gascogne(source, fetcher):
    listings, seen, errors = [], set(), []
    pages, total, previous_end = 0, None, 0
    base = source.get('url', '')
    try:
        start = validate_url_shape(base)
        if (start.hostname != 'www.gascogne-monnaie.com' or start.path != '/catalog/index.php'
                or parse_qs(start.query) != {'cPath': ['136']}):
            raise ValueError('choose the verified Gascogne Greek category URL (cPath=136)')
        next_url = _gascogne_url(base, base)
        for number in range(1, MAX_PAGES + 1):
            page = fetcher.get(next_url)
            pages += 1
            actual = _gascogne_url(base, page.url)
            if parse_qs(urlsplit(actual).query).get('page', ['1']) != [str(number)]:
                raise ValueError('catalog redirected to an unexpected page')
            soup = BeautifulSoup(page.text, 'html.parser')
            tables = soup.select('table.productListingData')
            if len(tables) != 1:
                raise ValueError('missing primary Gascogne catalog table')
            rows = tables[0].select(':scope > tr, :scope > tbody > tr')
            ranges = {tuple(int(value.replace(',', '')) for value in match) for match in _RANGE.findall(_text(soup))}
            if len(ranges) != 1:
                raise ValueError('catalog product count is missing or inconsistent')
            first, last, reported_total = ranges.pop()
            if total is None:
                total = reported_total
            if reported_total != total or first != previous_end + 1 or last < first or last > total or len(rows) != last - first + 1:
                raise ValueError('catalog page range, row count, or total changed during the sweep')
            previous_end = last
            for row in rows:
                try:
                    ident, item = _gascogne_detail(row, page.url, fetcher)
                    if ident in seen:
                        raise ValueError(f'repeated product {ident}; page coverage is uncertain')
                    seen.add(ident)
                    if item:
                        listings.append(item)
                except Exception as exc:
                    errors.append(str(exc))
            if last == total:
                return ScrapeResult(listings, pages, not errors, '; '.join(errors[:3]) or None)
            next_url = None
            for anchor in soup.select('a[href]'):
                target = urlsplit(urljoin(base, anchor['href']))
                query = parse_qs(target.query)
                if query.get('page') == [str(number + 1)] and query.get('cPath') == ['136']:
                    next_url = _gascogne_url(base, anchor['href'])
                    break
            if not next_url:
                raise ValueError('catalog reports more products but the next page link is missing')
        raise ValueError(f'catalog exceeds the {MAX_PAGES}-page safety limit')
    except Exception as exc:
        errors.append(str(exc))
        return ScrapeResult(listings, pages, False, '; '.join(errors[:3]))
