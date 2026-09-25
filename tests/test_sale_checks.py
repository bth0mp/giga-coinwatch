import json
from types import SimpleNamespace

import pytest

from coinwatch.sale_checks import verify_sale
from coinwatch.fetch import FetchError


URL = 'https://dealer.example/product/athens-owl'
TITLE = 'Greek Athens AR tetradrachm owl, 450 BC'


class Fetcher:
    def __init__(self, html='', url=URL, error=None):
        self.html, self.url, self.error, self.requests = html, url, error, []

    def get(self, url):
        self.requests.append(url)
        if self.error:
            raise self.error
        return SimpleNamespace(url=self.url, text=self.html)


def product_html(*, title=TITLE, stock='In stock', button='<button>Add to cart</button>',
                 price='<span class="price">£125.00</span>', schema=None, extra=''):
    script = '<script type="application/ld+json">' + json.dumps(schema) + '</script>' if schema else ''
    return f'<html><head>{script}</head><body><main><article class="product"><h1>{title}</h1>{price}<p class="stock">{stock}</p>{button}{extra}</article></main></body></html>'


def product_schema(**updates):
    product = {'@context': 'https://schema.org', '@type': 'Product', 'name': TITLE, 'url': URL,
               'offers': {'@type': 'Offer', 'price': '125.00', 'priceCurrency': 'GBP',
                          'availability': 'https://schema.org/InStock', 'url': URL}}
    product.update(updates)
    return product


def test_dom_product_requires_actual_price_stock_and_enabled_purchase():
    fetcher = Fetcher(product_html())
    result = verify_sale({'url': URL, 'title': 'Search-engine title', 'snippet': 'Search snippet'}, fetcher)
    assert result['sale_status'] == 'available'
    assert result['title'] == TITLE and result['url'] == URL
    assert result['price'] == '125.00' and result['currency'] == 'GBP'
    assert result['sale_checked_at'].endswith('+00:00')
    assert fetcher.requests == [URL]


def test_primary_jsonld_product_supplies_price_currency_and_stock():
    result = verify_sale({'url': URL, 'title': 'Search title'}, Fetcher(product_html(
        stock='', price='', schema=product_schema())))
    assert result['sale_status'] == 'available'
    assert result['price'] == '125.00' and result['currency'] == 'GBP'


@pytest.mark.parametrize('stock', ['Sold', 'Out of stock', 'Sold out', 'Reserved', 'Pre-order', 'Available on backorder', 'Ausverkauft', 'Vendu'])
def test_sold_dom_overrules_stale_instock_jsonld(stock):
    result = verify_sale({'url': URL}, Fetcher(product_html(stock=stock, schema=product_schema())))
    assert result['sale_status'] == 'rejected'


@pytest.mark.parametrize('button', [
    '<button disabled>Add to cart</button>', '<button aria-disabled="true">Buy now</button>',
    '<fieldset disabled><button>Add to cart</button></fieldset>',
    '<div hidden><button>Add to cart</button></div>', '<button style="display:none">Add to cart</button>',
    '<a href="#">Add to cart</a>', '<a href="javascript:void(0)">Buy now</a>',
    '<button>Enquire about this coin</button>', '',
])
def test_missing_or_disabled_purchase_is_never_available(button):
    result = verify_sale({'url': URL}, Fetcher(product_html(button=button, schema=product_schema())))
    assert result['sale_status'] != 'available'


def test_recommended_product_does_not_turn_article_into_available_listing():
    article = {'@type': 'Article', 'headline': 'A guide to Athens owl coins', 'url': URL}
    related = product_schema(url='https://dealer.example/product/related')
    html = '<main><article><h1>A guide to Athens owl coins</h1><p>Collecting history</p><section class="related-products">' + product_html(schema=[article, related]) + '</section></article></main>'
    result = verify_sale({'url': URL, 'title': TITLE, 'snippet': '£125 in stock buy now'}, Fetcher(html))
    assert result['sale_status'] == 'rejected'


def test_catalog_cards_and_aggregate_offer_are_not_single_coin_sales():
    html = '<main><h1>Ancient coins for sale</h1><div class="products">' + product_html(schema=product_schema(
        offers={'@type': 'AggregateOffer', 'lowPrice': '20', 'highPrice': '100', 'priceCurrency': 'GBP'})) + '</div></main>'
    assert verify_sale({'url': URL}, Fetcher(html))['sale_status'] != 'available'


@pytest.mark.parametrize('extra', ['<p>Current bid: £125</p><button>Place bid</button>', '<p>Starting bid £100</p>', '<button>Bid now</button>'])
def test_auction_controls_override_product_price(extra):
    assert verify_sale({'url': URL}, Fetcher(product_html(extra=extra)))['sale_status'] == 'rejected'


@pytest.mark.parametrize('title', [
    'Greek Athens owl replica coin', 'Book about Roman denarius coins', 'Lot of 12 Roman coins',
    '10 ancient Roman coins', 'Roman glass flask', 'Modern 2024 Roman Britain 50p coin',
])
def test_noncoins_replicas_books_bulk_and_modern_coins_are_rejected(title):
    assert verify_sale({'url': URL}, Fetcher(product_html(title=title)))['sale_status'] == 'rejected'


def test_no_listing_is_accepted_based_on_search_snippet():
    result = verify_sale({'url': URL, 'title': TITLE, 'snippet': 'In stock £125 Add to cart'}, Fetcher('<h1>Welcome to the coin shop</h1>'))
    assert result['sale_status'] != 'available'


def test_unmatched_product_schema_cannot_borrow_stock_from_related_product():
    result = verify_sale({'url': URL}, Fetcher(product_html(stock='', price='', schema=product_schema(
        url='https://dealer.example/product/other-coin'))))
    assert result['sale_status'] != 'available'


def test_cross_domain_redirect_does_not_attribute_sale_to_original_dealer():
    result = verify_sale({'url': URL}, Fetcher(product_html(), 'https://different.example/product/athens-owl'))
    assert result['url'] == URL and result['sale_status'] == 'unverified'


def test_blocked_page_is_unverified_without_leaking_error_details():
    result = verify_sale({'url': URL}, Fetcher(error=FetchError('Access challenge; private details')))
    assert result['sale_status'] == 'unverified'
    assert 'private details' not in result['sale_reason']
    assert result['price'] == result['currency'] == ''


@pytest.mark.parametrize('url', ['https://en.wikipedia.org/wiki/Ancient_coin', 'https://dealer.example/blog/athens', 'https://dealer.example/auction/123',
                               'https://www.catawiki.com/en/l/123-greek-coin', 'https://www.cointalk.com/threads/123',
                               'https://www.todocoleccion.net/s/monedas-antiguas'])
def test_informational_or_auction_urls_are_rejected_without_fetch(url):
    fetcher = Fetcher(product_html())
    assert verify_sale({'url': url}, fetcher)['sale_status'] == 'rejected'
    assert fetcher.requests == []


def test_forvm_catalog_is_not_blanket_blocked():
    url = 'https://www.forumancientcoins.com/catalog/roman-and-greek-coins.asp?param=123'
    fetcher = Fetcher(product_html(), url)
    assert verify_sale({'url': url}, fetcher)['sale_status'] == 'available'


def test_sale_price_takes_priority_over_crossed_out_regular_price():
    price = '<span class="price"><del>£250.00</del><ins>£125.00</ins></span>'
    assert verify_sale({'url': URL}, Fetcher(product_html(price=price)))['price'] == '125.00'


@pytest.mark.parametrize('price', ['<span class="price">£0.00</span>', '<span class="price">125</span>', '<span class="price">$125</span>'])
def test_missing_positive_amount_or_unambiguous_currency_stays_unverified(price):
    assert verify_sale({'url': URL}, Fetcher(product_html(price=price)))['sale_status'] != 'available'


def test_german_product_stock_and_purchase_are_supported():
    result = verify_sale({'url': URL}, Fetcher(product_html(
        title='Griechische Münze Athen AR Tetradrachme 450 v. Chr.', stock='Auf Lager',
        price='<span class="price">125,00 €</span>', button='<button>In den Warenkorb</button>')))
    assert result['sale_status'] == 'available' and result['currency'] == 'EUR'


def test_provenance_auction_and_sale_mentions_do_not_hide_current_stock():
    result = verify_sale({'url': URL}, Fetcher(product_html(extra='<p>Provenance: sold at CNG auction in 2010.</p>')))
    assert result['sale_status'] == 'available'


def test_offer_for_different_product_cannot_supply_sale_evidence():
    schema = product_schema()
    schema['offers']['url'] = 'https://dealer.example/product/another-coin'
    result = verify_sale({'url': URL}, Fetcher(product_html(stock='', price='', schema=schema)))
    assert result['sale_status'] != 'available'


def test_jsonld_decimal_price_is_not_treated_as_thousands():
    schema = product_schema()
    schema['offers']['price'] = '125.000'
    result = verify_sale({'url': URL}, Fetcher(product_html(stock='', price='', schema=schema)))
    assert result['sale_status'] == 'available' and result['price'] == '125.00'


@pytest.mark.parametrize('extra', ['<p>Sold</p>', '<span class="sold-out">Out of stock</span>'])
def test_plain_sold_badge_overrides_stale_schema_and_buy_button(extra):
    result = verify_sale({'url': URL}, Fetcher(product_html(stock='', schema=product_schema(), extra=extra)))
    assert result['sale_status'] == 'rejected'


@pytest.mark.parametrize('price', ['<span class="price">£100.00–£200.00</span>', '<span class="price">From £100.00</span>'])
def test_price_ranges_do_not_verify_a_single_fixed_price(price):
    result = verify_sale({'url': URL}, Fetcher(product_html(price=price)))
    assert result['sale_status'] != 'available'


def test_goldeneagle_primary_hemidrachm_product_with_payment_method_prices():
    # Minimal sale evidence from the seller HTML, without its marketing description.
    url = 'https://www.goldeneaglecoin.com/item/boetia-ar-hemidrachm-395_340-bc-choice-vf'
    title = 'Boetia AR Hemidrachm 395-340 B.C. Choice VF'
    schema = {'@context': 'https://schema.org/', '@type': 'Product', 'name': title,
              'offers': {'@type': 'Offer', 'url': url, 'priceCurrency': 'USD',
                         'price': '349.00', 'availability': 'https://schema.org/InStock'}}
    html = '<script type="application/ld+json">' + json.dumps(schema) + '</script>'
    html += f'''<main><div class="product-page"><div class="product-info">
      <div><h1>{title}</h1></div><div>As low as: <span>$349.00</span></div>
      <div class="attribute inventory"><div>Inventory</div><div>1 available</div></div>
      <div class="ProductPriceCardStyled add-to-card"><table class="prices-table">
        <tr><th>Qty</th><th>Wire/Check</th><th>Bitcoin</th><th>CC/Paypal</th></tr>
        <tr><td>Any</td><td>$349.00</td><td>$352.49</td><td>$362.96</td></tr>
      </table><button name="addToCart" type="button" value="47166">Add To Cart</button></div>
    </div></div></main>'''
    result = verify_sale({'url': url}, Fetcher(html, url))
    assert result['sale_status'] == 'available'
    assert (result['price'], result['currency'], result['title']) == ('349.00', 'USD', title)
    sold = html.replace('1 available', '0 available')
    assert verify_sale({'url': url}, Fetcher(sold, url))['sale_status'] == 'rejected'


@pytest.mark.parametrize('marker', [' - SOLD', ' (Out of stock)', ' - RESERVED', ' - Pre-order'])
def test_primary_title_sale_status_overrules_stale_stock_and_purchase(marker):
    result = verify_sale({'url': URL}, Fetcher(product_html(title=TITLE + marker, schema=product_schema(name=TITLE + marker))))
    assert result['sale_status'] == 'rejected'


def test_hidden_price_does_not_override_current_visible_price():
    html = product_html(price='<span class="price" hidden>£125.00</span><span class="price">£200.00</span>', schema=product_schema())
    result = verify_sale({'url': URL}, Fetcher(html))
    assert result['sale_status'] == 'available' and result['price'] == '200.00'


@pytest.mark.parametrize('schema', [None, product_schema()])
def test_hidden_only_price_cannot_verify_sale_or_fall_back_to_stale_schema(schema):
    html = product_html(price='<span class="price" hidden>£125.00</span><span>Price upon request</span>', schema=schema)
    assert verify_sale({'url': URL}, Fetcher(html))['sale_status'] != 'available'


def test_visible_request_price_overrides_stale_schema_without_dom_price_class():
    html = product_html(price='<p>Price upon request</p>', schema=product_schema())
    assert verify_sale({'url': URL}, Fetcher(html))['sale_status'] != 'available'


@pytest.mark.parametrize('count', [1, 2])
def test_catalog_product_cards_cannot_supply_primary_product_sale_evidence(count):
    card = '<div class="product"><h2>Greek stater</h2><p class="stock">In stock</p><span class="price">£125</span><a href="/cart?add=123">Add to cart</a></div>'
    html = '<main><div class="product"><h1>Ancient Greek coins</h1><div class="products">' + card * count + '</div></div></main>'
    assert verify_sale({'url': URL}, Fetcher(html))['sale_status'] != 'available'
