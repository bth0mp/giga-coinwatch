"""Small, explicitly audited Wix and Shopify ancient-coin scopes."""
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .fetch import validate_url_shape
from .models import Listing, ScrapeResult


_SCOPES = {
    ('minotaurcoins-com', 'minotaur'): 'https://www.minotaurcoins.com/roman-republic-1',
    ('kinzercoins-com', 'shopify'): 'https://kinzercoins.com/collections/roman-republican',
}
_WIX_APP = '1380b703-ce81-ff05-f115-39571d94dfcd'
_WIX_CATEGORY = 'a51f0aef-af24-3d3d-91ea-9662a382a85a'
_EXCLUDE = re.compile(r'\b(?:replica|reproduction|copy|modern|bulk|bundle|group|pair|set of|lot of|lots of|uncleaned|book|guide|medal|token)\b'
                      r'|^\s*(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(?:\w+\s+){0,3}(?:coins|denarii|bronzes)\b', re.I)
_SOLD = re.compile(r'\b(?:sold|reserved|out of stock)\b', re.I)
_REPRESENTATIVE = re.compile(r'\brepresentative (?:example|image|photo)|\brandom(?:ly)? (?:chosen|selected)|\bwill vary\b', re.I)
MAX_PAGES = 20


def _text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def _price(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal('1000000000000'):
            raise InvalidOperation
        return format(amount, '.2f')
    except (InvalidOperation, ValueError):
        raise ValueError('missing or invalid fixed price') from None


def _product_url(url, base, prefix):
    parts = validate_url_shape(urljoin(base, url))
    expected = urlsplit(base)
    if parts.scheme != expected.scheme or parts.netloc != expected.netloc or not parts.path.startswith(prefix):
        raise ValueError('product URL left the audited seller scope')
    return parts


def _minotaur(body, base):
    soup = BeautifulSoup(body, 'html.parser')
    script = soup.select_one('#wix-warmup-data')
    if not script:
        raise ValueError('Wix catalog metadata is missing')
    apps = json.loads(script.string or '')['appsWarmupData'][_WIX_APP]
    categories = [item['catalog']['category'] for item in apps.values()
                  if isinstance(item, dict) and isinstance(item.get('catalog'), dict) and 'category' in item['catalog']]
    if len(categories) != 1 or categories[0].get('id') != _WIX_CATEGORY or categories[0].get('visible') is not True:
        raise ValueError('the audited Roman Republic category changed')
    metadata = categories[0]['productsWithMetaData']
    rows, total = metadata['list'], metadata['totalCount']
    if type(total) is not int or total < 0 or not isinstance(rows, list):
        raise ValueError('invalid Wix catalog total')
    cards = soup.select('[data-hook="product-list-grid-item"]')
    visible = {}
    for card in cards:
        link = card.select_one('[data-hook="product-item-product-details-link"][href]')
        if not link:
            raise ValueError('missing visible product link')
        path = _product_url(link['href'], base, '/product-page/').path
        if path in visible:
            raise ValueError('duplicate visible product')
        visible[path] = _text(card.select_one('[data-hook="product-item-name"]'))
    errors, listings, ids = [], [], set()
    if total != len(rows) or total != len(cards):
        errors.append('Wix catalog is truncated; the visible cards do not cover its full product total')
    for row in rows:
        try:
            ident, slug, title = row['id'], row['urlPart'], row['name']
            if not all(isinstance(value, str) and value for value in (ident, slug, title)) or not re.fullmatch(r'[a-z0-9-]+', slug):
                raise ValueError('missing product identity or title')
            if ident in ids:
                raise ValueError('duplicate product identity')
            ids.add(ident)
            path = '/product-page/' + slug
            if visible.get(path) != ' '.join(title.split()):
                raise ValueError('visible product does not match its inventory metadata')
            if _EXCLUDE.search(title) or row.get('options') or row.get('productType') != 'physical':
                continue
            inventory = row.get('inventory') or {}
            quantity = inventory.get('quantity')
            if type(quantity) is int and quantity > 1:
                continue  # Pooled inventory is not an individually identified specimen.
            if row.get('currency') != 'SGD' or not row.get('sku'):
                raise ValueError('missing specimen SKU or unexpected currency')
            price = _price(row['price'])
            if _SOLD.search(title) or (row.get('isInStock') is False and inventory.get('status') == 'out_of_stock'):
                availability = 'sold'
            elif (row.get('isInStock') is True and inventory.get('status') == 'in_stock'
                  and row.get('isTrackingInventory') is True and type(quantity) is int and quantity == 1
                  and inventory.get('availableForPreOrder') is False):
                availability = 'available'
            else:
                raise ValueError('individual specimen availability is unverified')
            media = row.get('media') or []
            image = media[0].get('fullUrl', '') if media else ''
            if image:
                validate_url_shape(image)
            listings.append(Listing(ident, urljoin(base, path), title, price, 'SGD', image, 'Roman', availability))
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    return ScrapeResult(listings, 1, not errors, '; '.join(errors[:3]) or None)


def _kinzer_product(body, product_url):
    soup = BeautifulSoup(body, 'html.parser')
    main = soup.select_one('product-info')
    if not main:
        raise ValueError('primary Shopify product is missing')
    title = _text(main.select_one('h1'))
    description = _text(main.select_one('.product__description'))
    if not title:
        raise ValueError('product title is missing')
    if _EXCLUDE.search(title) or _REPRESENTATIVE.search(description):
        return None
    if not re.search(r'\bexact\b.*\b(?:shown|pictured|specimen)\b', description, re.I):
        raise ValueError('the product is not confirmed as the exact specimen shown')
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('@type') == 'Product' and data.get('offers'):
            products.append(data)
    if len(products) != 1:
        raise ValueError('expected one individually priced Shopify product')
    product = products[0]
    offer = product['offers']
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('priceCurrency') != 'USD'
            or product.get('name') != title or not product.get('sku')):
        raise ValueError('ambiguous product identity, currency or offer')
    parts = _product_url(offer.get('url', ''), product_url, '/products/')
    if parts.path != urlsplit(product_url).path:
        raise ValueError('offer belongs to a different product')
    variant = parse_qs(parts.query).get('variant', [])
    form = main.select_one('product-form form[action="/cart/add"]')
    selected = form.select_one('input[name="id"]') if form else None
    if len(variant) != 1 or not variant[0].isdigit() or not selected or selected.get('value') != variant[0]:
        raise ValueError('offer does not identify the selected product variant')
    button = form.select_one('button[name="add"]')
    stock = str(offer.get('availability', '')).rsplit('/', 1)[-1]
    if stock == 'OutOfStock' or _SOLD.search(title):
        availability = 'sold'
    elif (stock == 'InStock' and button and not button.has_attr('disabled')
          and button.get('aria-disabled') != 'true' and re.search('add to cart', _text(button), re.I)):
        availability = 'available'
    else:
        raise ValueError('current purchase availability is unverified')
    image = main.select_one('.product__media img[src]')
    image_url = urljoin(product_url, image['src']) if image else ''
    if image_url:
        validate_url_shape(image_url)
    return Listing(variant[0], product_url + '?variant=' + variant[0], title, _price(offer.get('price')),
                   'USD', image_url, 'Roman', availability)


def _kinzer(base, fetcher):
    listings, errors, seen = [], [], set()
    expected_total, pages, last_page = None, 0, 1
    for number in range(1, MAX_PAGES + 1):
        url = base if number == 1 else base + '?page=' + str(number)
        try:
            page = fetcher.get(url)
            if urlsplit(page.url).path != urlsplit(base).path or urlsplit(page.url).netloc != urlsplit(base).netloc:
                raise ValueError('collection redirected outside its audited scope')
            soup = BeautifulSoup(page.text, 'html.parser')
            pages += 1
            count = re.fullmatch(r'(\d+) products?', _text(soup.select_one('#ProductCount')))
            if not count:
                raise ValueError('collection product total is missing')
            total = int(count.group(1))
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise ValueError('collection total changed during pagination')
            for link in soup.select('.pagination a[href]'):
                parts = urlsplit(urljoin(page.url, link['href']))
                if parts.netloc != urlsplit(base).netloc or parts.path != urlsplit(base).path:
                    raise ValueError('pagination left the audited collection')
                page_number = parse_qs(parts.query).get('page', [''])[0]
                if not page_number.isdigit() or int(page_number) < 1:
                    raise ValueError('invalid pagination link')
                last_page = max(last_page, int(page_number))
            cards = soup.select('#product-grid li.grid__item')
            if not cards and total:
                raise ValueError('collection product cards are missing')
            for card in cards:
                try:
                    link = card.select_one('h3 a[href]')
                    if not link or not _text(link):
                        raise ValueError('product link or title is missing')
                    parts = _product_url(link['href'], base, '/products/')
                    product_url = parts.scheme + '://' + parts.netloc + parts.path
                    if product_url in seen:
                        raise ValueError('repeated product during pagination')
                    seen.add(product_url)
                    if _EXCLUDE.search(_text(link)):
                        continue
                    detail = fetcher.get(product_url)
                    if urlsplit(detail.url).path != parts.path or urlsplit(detail.url).netloc != parts.netloc:
                        raise ValueError('product redirected outside its original identity')
                    item = _kinzer_product(detail.text, product_url)
                    if item:
                        listings.append(item)
                except Exception as exc:
                    errors.append(str(exc))
            if number >= last_page:
                if len(seen) != expected_total:
                    errors.append('collection cards do not cover the complete product total')
                return ScrapeResult(listings, pages, not errors, '; '.join(errors[:3]) or None)
        except Exception as exc:
            return ScrapeResult(listings, pages, False, str(exc))
    return ScrapeResult(listings, pages, False, f'catalog exceeds the {MAX_PAGES}-page safety limit')


def scrape_modern(source, fetcher):
    base = _SCOPES.get((source.get('id'), source.get('adapter')))
    if not base or source.get('url', '').rstrip('/') != base:
        return ScrapeResult(complete=False, error='unsupported modern-shop source or category scope')
    if source['adapter'] == 'shopify':
        return _kinzer(base, fetcher)
    pages = 0
    try:
        page = fetcher.get(base)
        pages = 1
        if page.url.rstrip('/') != base:
            raise ValueError('category redirected outside its audited scope')
        return _minotaur(page.text, base)
    except Exception as exc:
        return ScrapeResult(pages=pages, complete=False, error=str(exc))
