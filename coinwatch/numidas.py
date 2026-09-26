"""The public, single-page Numidas Greek catalog (Jimdo product microdata)."""

from decimal import Decimal, InvalidOperation
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import Listing, ScrapeResult

CATALOG = 'https://www.numidas.at/m%C3%BCnzen/greek/'


def scrape_numidas(source, fetcher):
    from .sources import _reject
    if source['url'] != CATALOG:
        return ScrapeResult(complete=False, error='Unsupported Numidas catalog scope')
    try:
        page = fetcher.get(CATALOG)
        if page.url != CATALOG:
            raise ValueError('Numidas catalog redirected to a different scope')
        soup = BeautifulSoup(page.text, 'html.parser')
    except Exception as exc:
        return ScrapeResult(complete=False, error=str(exc))
    cards = soup.select('[id^="cc-m-product-"][itemtype$="/Product"]')
    if not cards:
        return ScrapeResult(pages=1, complete=False, error='No Numidas product cards')
    rows, errors, seen = [], [], set()
    for card in cards:
        try:
            ident = card['id'].removeprefix('cc-m-product-')
            name = card.select_one('[itemprop="name"]')
            title = name.get_text(' ', strip=True) if name else ''
            if not ident.isdigit() or not title:
                raise ValueError('Missing product identity or title')
            if _reject(title):
                continue
            offer = card.select_one('[itemprop="offers"]')
            def value(prop):
                node = offer.select_one(f'[itemprop="{prop}"]') if offer else None
                if not node or not node.get('content'):
                    raise ValueError(f'Missing {prop}')
                return node['content']
            amount = Decimal(value('price'))
            if not amount.is_finite() or amount <= 0 or value('priceCurrency') != 'EUR':
                raise ValueError('Invalid EUR price')
            stock = value('availability').rsplit('/', 1)[-1]
            quantity = value('value')
            if not quantity.isdigit():
                raise ValueError('Invalid stock quantity')
            button = card.select_one('button[data-action="addToCart"]')
            purchasable = (button is not None and not button.has_attr('disabled') and
                           'cc-addtocard-disabled' not in button.get('class', []))
            if re.search(r'\b(sold|reserved|verkauft|reserviert)\b', title, re.I):
                status = 'reserved' if re.search(r'\b(reserved|reserviert)\b', title, re.I) else 'sold'
            elif stock == 'InStock' and int(quantity) > 0 and purchasable:
                status = 'available'
            elif stock == 'OutOfStock' and int(quantity) == 0 and not purchasable:
                status = 'sold'
            else:
                raise ValueError('Conflicting or missing stock evidence')
            if ident in seen:
                raise ValueError('Repeated product identity')
            seen.add(ident)
            image = card.select_one('img[itemprop="image"]')
            image_url = image.get('src', '') if image else ''
            if urlsplit(image_url).scheme not in ('http', 'https'):
                image_url = ''
            rows.append(Listing(ident, CATALOG + '#' + card['id'], title, f'{amount:.2f}', 'EUR',
                                image_url=image_url, category='Greek', availability=status))
        except (ValueError, InvalidOperation) as exc:
            errors.append(str(exc))
    if soup.select_one('a[rel~="next"], .pagination, [data-action="loadMore"]'):
        errors.append('Unexpected catalog pagination; coverage is partial')
    return ScrapeResult(rows, 1, not errors, '; '.join(errors[:3]) or None)
