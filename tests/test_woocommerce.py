"""Small catalog fragments retain the markup observed on the audited dealers."""
import importlib.util
from types import SimpleNamespace

import pytest


URL = 'https://bargainbinancients.com/product-category/greek-coins/'


class Fetcher:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, url):
        self.requests.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value if hasattr(value, 'url') else SimpleNamespace(url=url, text=value)


def card(ident=368, title='Abydos Troas Apollo / Eagle', stock='instock purchasable', price='$36.00', *, tag='li', heading='woocommerce-loop-product__title', button=True, extra=''):
    control = f'<a class="button add_to_cart_button" data-product_id="{ident}" href="?add-to-cart={ident}">Add to cart</a>' if button else ''
    return f'''<{tag} class="product type-product post-{ident} product-type-simple {stock} {extra}">
      <a class="woocommerce-LoopProduct-link product-image-link" href="/product/coin-{ident}/"><img src="data:image/svg+xml;base64,AA" data-src="/images/coin-{ident}.jpg"></a>
      <h2 class="{heading}"><a href="/product/coin-{ident}/">{title}</a></h2>
      <span class="price">{price}</span>{control}</{tag}>'''


def catalog(cards, total=None, links=''):
    count = f'<p class="woocommerce-result-count">Showing all {total} results</p>' if total is not None else ''
    return count + '<ul class="products">' + cards + '</ul><nav class="woocommerce-pagination">' + links + '</nav>'


def scrape(pages, *, ident='bargainbinancients', url=URL):
    # Keep the first red failure explicit when the adapter has not been implemented.
    assert importlib.util.find_spec('coinwatch.woocommerce'), 'WooCommerce adapter is missing'
    from coinwatch.woocommerce import scrape_woocommerce
    fetcher = Fetcher(pages)
    return scrape_woocommerce({'id': ident, 'url': url}, fetcher), fetcher


def test_standard_cards_keep_sale_price_stock_and_lazy_image():
    sale = '<del>$50.00</del><ins><span class="woocommerce-Price-amount">$36.00</span></ins>'
    cards = card(price=sale) + card(369, stock='outofstock', button=False)
    result, _ = scrape({URL: catalog(cards, 2)})
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.price, x.currency, x.availability) for x in result.listings] == [
        ('368', '36.00', 'USD', 'available'), ('369', '36.00', 'USD', 'sold')]
    assert result.listings[0].image_url == 'https://bargainbinancients.com/images/coin-368.jpg'
    assert result.listings[0].category == 'Greek'


def test_numeric_and_next_pagers_are_followed_once_in_order():
    second, third = URL + 'page/2/', URL + 'page/3/'
    pages = {URL: catalog(card(), 3, f'<a href="{third}">3</a><a href="{second}">2</a><a class="next" href="{second}">Next</a>'),
             second: catalog(card(369), 3, f'<a href="{URL}">1</a><a href="{third}">Next</a>'),
             third: catalog(card(370), 3)}
    result, fetcher = scrape(pages)
    assert result.complete and result.pages == 3
    assert fetcher.requests == [URL, second, third]
    assert len(result.listings) == 3


@pytest.mark.parametrize('ident,url,currency,price', [
    ('pashiz', 'https://www.pashizcoins.com/product-category/greek/', 'GBP', '£23.00'),
    ('bactrianumis-com', 'https://bactrianumis.com/product-category/oriental-greek-coins/', 'USD', '$199'),
])
def test_supported_theme_and_currency(ident, url, currency, price):
    result, _ = scrape({url: catalog(card(tag='div', heading='wd-entities-title', price=price), 1)}, ident=ident, url=url)
    assert result.complete and len(result.listings) == 1
    assert result.listings[0].currency == currency


@pytest.mark.parametrize('ident,url', [
    ('praefectuscoins-com', 'https://praefectuscoins.com/product-category/showcase/'),
    ('vilmarnumismatics-com', 'https://vilmarnumismatics.com/product-category/ancient-coins/'),
])
def test_validated_divi_theme_has_stock_classes_without_catalog_cart_button(ident, url):
    name = 'CROTON SILVER STATER - EX COLLECTION SOLD IN 1926 - GREEK BRUTTIUM'
    result, _ = scrape({url: catalog(card(title=name, button=False, extra='product_cat-greek-coins'), 1)}, ident=ident, url=url)
    assert result.complete and result.listings[0].title == name
    assert result.listings[0].category == 'Greek'


@pytest.mark.parametrize('title,extra,status', [
    ('SOLD - Athens AR tetradrachm', '', 'sold'),
    ('RESERVED - Athens AR tetradrachm', '', 'reserved'),
    ('Athens AR tetradrachm - SOLD', '', 'sold'),
    ('Athens AR tetradrachm [RESERVED]', '', 'reserved'),
    ('Athens AR tetradrachm', '<span class="woosticker_sold">Sold Out</span>', 'sold'),
    ('Athens AR tetradrachm', '<span class="stock-status">On hold</span>', 'reserved'),
    ('Athens AR tetradrachm - EX COLLECTION SOLD IN 1926', '', 'available'),
])
def test_explicit_current_sale_markers_override_stale_available_metadata(title, extra, status):
    html = card(title=title).replace('</li>', extra + '</li>')
    result, _ = scrape({URL: catalog(html, 1)})
    assert result.complete and result.listings[0].availability == status


def test_category_tiles_and_mixed_noncoin_or_bulk_stock_are_excluded():
    url = 'https://www.goldenruleenterprises.org/product-category/ancient-coins/'
    cards = '<li class="product-category product"><a href="/category/">Roman</a></li>'
    cards += card(title='MYSIA, PERGAMON, bronze minor, 2-1 century BC')
    for ident, title in enumerate(['Ancient Roman glass flask', 'Roman Tessera Annonaria Bone', 'Lot of 10 Roman coins',
                                   'Modern Greek drachma 1970', 'Greek stater replica', 'Abydos coin (Copy)'], 369):
        cards += card(ident, title=title)
    result, _ = scrape({url: catalog(cards, 7)}, ident='goldenrule', url=url)
    assert result.complete and [x.external_id for x in result.listings] == ['368']


def test_unpriced_cards_and_explicitly_sold_without_numeric_price_are_not_fixed_price_stock():
    url = 'https://vilmarnumismatics.com/product-category/ancient-coins/'
    cards = card(button=False) + card(369, button=False).replace('<span class="price">$36.00</span>', '')
    cards += card(370, stock='outofstock', price='Sold', button=False)
    result, _ = scrape({url: catalog(cards, 3)}, ident='vilmarnumismatics-com', url=url)
    assert result.complete and [x.external_id for x in result.listings] == ['368']


def test_mixed_showcase_requires_coin_evidence_even_when_object_has_coin_category():
    url = 'https://praefectuscoins.com/product-category/showcase/'
    titles = ['Trajan AR Cistophorus, statue of Diana within temple', 'Crispus, Follis, rare heroic bust',
              'Ancient Roman bronze helmeted bust of Minerva', 'Ancient Roman bronze key, 1st century',
              'Asia Minor, Pb23, weight, token or game piece?', 'Seleukid Kings, Antiochos I',
              'Egypt, Alexandria, Claudius, Diobol', 'Troas, Ilion, Æ18, Very Rare']
    cards = ''.join(card(i, title, button=False, extra='product_cat-ancient-greek') for i, title in enumerate(titles, 1))
    result, _ = scrape({url: catalog(cards, len(titles))}, ident='praefectuscoins-com', url=url)
    assert result.complete and [x.external_id for x in result.listings] == ['1', '2', '7', '8']


@pytest.mark.parametrize('bad', [card(369, stock=''), card(369, stock='instock'),
    card(369, price='$10.00 – $20.00'), card(369, price='€36.00'), card(369, price='$0'),
    card(369, button=False), card(369).replace('post-369', 'no-product-id'),
    card(369).replace('data-product_id="369"', 'data-product_id="999"'),
    card(369).replace('/product/coin-369/', 'https://elsewhere.example/coin/')])
def test_malformed_products_make_partial_without_hiding_valid_cards(bad):
    result, _ = scrape({URL: catalog(card() + bad, 2)})
    assert not result.complete and result.error
    assert [x.external_id for x in result.listings] == ['368']


def test_failed_later_page_preserves_prior_results():
    second = URL + 'page/2/'
    result, _ = scrape({URL: catalog(card(), 2, f'<a href="{second}">Next</a>'), second: RuntimeError('HTTP 503')})
    assert not result.complete and '503' in result.error and len(result.listings) == 1


def test_missing_pager_cannot_claim_a_truncated_catalog_is_complete():
    result, _ = scrape({URL: catalog(card(), 10)})
    assert not result.complete and '10' in result.error


def test_missing_result_count_does_not_claim_completeness():
    result, _ = scrape({URL: catalog(card())})
    assert not result.complete and 'count' in result.error


def test_pashiz_theme_page_numbers_outside_woocommerce_pagination_are_followed():
    url = 'https://www.pashizcoins.com/product-category/greek/'
    second = url + 'page/2/'
    first = catalog(card(price='£23'), 2) + f'<nav class="ct-pagination"><a class="page-numbers" href="{second}">2</a></nav>'
    result, fetcher = scrape({url: first, second: catalog(card(369, price='£50'), 2)}, ident='pashiz', url=url)
    assert result.complete and fetcher.requests == [url, second]


def test_repeated_product_page_is_partial_and_deduplicated():
    second = URL + 'page/2/'
    result, _ = scrape({URL: catalog(card(), 2, f'<a href="{second}">Next</a>'), second: catalog(card(), 2)})
    assert not result.complete and 'repeat' in result.error.lower()
    assert len(result.listings) == 1


def test_page_limit_and_foreign_pager_fail_closed(monkeypatch):
    from coinwatch import woocommerce
    monkeypatch.setattr(woocommerce, 'MAX_PAGES', 1)
    second = URL + 'page/2/'
    result, fetcher = scrape({URL: catalog(card(), 2, f'<a href="{second}">Next</a>')})
    assert not result.complete and 'limit' in result.error and fetcher.requests == [URL]
    result, fetcher = scrape({URL: catalog(card(), 1, '<a href="https://other.example/page/2/">Next</a>')})
    assert not result.complete and fetcher.requests == [URL]


def test_empty_or_redirected_unrecognized_catalog_does_not_succeed():
    result, _ = scrape({URL: '<html>Checking your browser</html>'})
    assert not result.complete
    result, _ = scrape({URL: '<p class="woocommerce-info">No products were found matching your selection.</p>'})
    assert result.complete and not result.listings
    result, _ = scrape({URL: SimpleNamespace(url='https://elsewhere.example/', text=catalog(card(), 1))})
    assert not result.complete and not result.listings


def test_unvalidated_source_or_scope_is_not_requested():
    result, fetcher = scrape({}, ident='unrecognized')
    assert not result.complete and not fetcher.requests
    result, fetcher = scrape({}, url='https://bargainbinancients.com/')
    assert not result.complete and not fetcher.requests
