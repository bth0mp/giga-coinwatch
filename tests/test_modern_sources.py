import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from coinwatch.modern_sources import scrape_modern


FIXTURES = Path(__file__).parent / 'fixtures'
MINOTAUR = 'https://www.minotaurcoins.com/roman-republic-1'
KINZER = 'https://kinzercoins.com/collections/roman-republican'
PRODUCT = 'https://kinzercoins.com/products/calabria-brundisium-circa-215-bc-sextans'


class Fetcher:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def get(self, url):
        self.requests.append(url)
        return SimpleNamespace(url=url, text=self.pages[url])


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def source(adapter='minotaur', url=MINOTAUR):
    return dict(id='minotaurcoins-com' if adapter == 'minotaur' else 'kinzercoins-com', adapter=adapter, url=url)


def wix_html(change):
    soup = BeautifulSoup(fixture('minotaur-republic.html'), 'html.parser')
    script = soup.select_one('#wix-warmup-data')
    payload = json.loads(script.string)
    category = payload['appsWarmupData']['1380b703-ce81-ff05-f115-39571d94dfcd']['initialData_default_TPASection_jm22e6wt_default']['catalog']['category']
    change(category, soup)
    script.string = json.dumps(payload)
    return str(soup)


def test_minotaur_reads_four_individual_coins_and_proves_full_category_coverage():
    fetcher = Fetcher({MINOTAUR: fixture('minotaur-republic.html')})
    result = scrape_modern(source(), fetcher)
    assert result.complete and result.pages == 1 and not result.error
    assert [(row.price, row.currency, row.availability) for row in result.listings] == [
        ('175.00', 'SGD', 'available'), ('220.00', 'SGD', 'available'),
        ('250.00', 'SGD', 'available'), ('110.00', 'SGD', 'available')]
    assert result.listings[0].external_id == '95c078cc-dcb3-e74e-7b2d-96b6543972d8'
    assert all(row.category == 'Roman' for row in result.listings)
    assert fetcher.requests == [MINOTAUR]


@pytest.mark.parametrize('change', [
    lambda c, s: c['productsWithMetaData'].update(totalCount=5),
    lambda c, s: c['productsWithMetaData'].update(totalCount=3),
    lambda c, s: s.select('[data-hook="product-list-grid-item"]')[0].decompose(),
    lambda c, s: c.update(id='different-category'),
    lambda c, s: c['productsWithMetaData']['list'][0].update(currency='USD'),
    lambda c, s: c['productsWithMetaData']['list'][0].update(price='NaN'),
    lambda c, s: c['productsWithMetaData']['list'][0].update(isInStock=True, inventory={}),
    lambda c, s: c['productsWithMetaData']['list'][0].update(id=c['productsWithMetaData']['list'][1]['id']),
])
def test_minotaur_changed_incomplete_or_ambiguous_data_never_replaces_baseline(change):
    result = scrape_modern(source(), Fetcher({MINOTAUR: wix_html(change)}))
    assert not result.complete and result.error


def test_minotaur_sold_metadata_and_title_override_in_stock():
    def change(category, soup):
        rows = category['productsWithMetaData']['list']
        rows[0].update(isInStock=False, inventory={'status': 'out_of_stock', 'quantity': 0})
        rows[1]['name'] += ' SOLD!'
        soup.select('[data-hook="product-item-name"]')[1].string = rows[1]['name']
    result = scrape_modern(source(), Fetcher({MINOTAUR: wix_html(change)}))
    assert result.complete
    assert [row.availability for row in result.listings] == ['sold', 'sold', 'available', 'available']


@pytest.mark.parametrize('name', ['Roman replica denarius', 'Roman group of 4 coins', 'Modern silver coin', 'Lot of Roman denarii', '4 Roman coins', 'Two Roman denarii'])
def test_minotaur_excludes_groups_replicas_and_modern_stock(name):
    def change(category, soup):
        category['productsWithMetaData']['list'][0]['name'] = name
        soup.select('[data-hook="product-item-name"]')[0].string = name
    result = scrape_modern(source(), Fetcher({MINOTAUR: wix_html(change)}))
    assert result.complete and len(result.listings) == 3


def test_minotaur_pool_inventory_and_variants_are_excluded():
    def change(category, soup):
        rows = category['productsWithMetaData']['list']
        rows[0]['inventory']['quantity'] = 5
        rows[1]['options'] = [{'name': 'Grade'}]
    result = scrape_modern(source(), Fetcher({MINOTAUR: wix_html(change)}))
    assert result.complete and len(result.listings) == 2


def test_minotaur_unreadable_metadata_records_the_attempted_page():
    result = scrape_modern(source(), Fetcher({MINOTAUR: '<main>Temporary page</main>'}))
    assert not result.complete and result.pages == 1 and not result.listings


def test_kinzer_exact_specimen_uses_current_offer_variant_and_purchase_control():
    fetcher = Fetcher({KINZER: fixture('kinzer-republic.html'), PRODUCT: fixture('kinzer-republic-product.html')})
    result = scrape_modern(source('shopify', KINZER), fetcher)
    assert result.complete and result.pages == 1
    row, = result.listings
    assert (row.external_id, row.price, row.currency, row.availability) == ('50904331354418', '395.00', 'USD', 'available')
    assert row.url == PRODUCT + '?variant=50904331354418'
    assert fetcher.requests == [KINZER, PRODUCT]


@pytest.mark.parametrize('old,new', [
    ('"priceCurrency":"USD"', '"priceCurrency":"CAD"'),
    ('"price":"395.00"', '"price":"NaN"'),
    ('value="50904331354418"', 'value="99999999999"'),
    ('<button name="add">', '<button name="add" disabled>'),
    ('calabria-brundisium-circa-215-bc-sextans?variant=', 'wrong-coin?variant='),
])
def test_kinzer_ambiguous_purchase_or_offer_is_partial(old, new):
    detail = fixture('kinzer-republic-product.html').replace(old, new)
    assert detail != fixture('kinzer-republic-product.html')
    result = scrape_modern(source('shopify', KINZER), Fetcher({KINZER: fixture('kinzer-republic.html'), PRODUCT: detail}))
    assert not result.complete and not result.listings


def test_kinzer_representative_products_are_excluded_even_with_good_stock_metadata():
    detail = fixture('kinzer-republic-product.html').replace('The exact bronze sextans pictured.', 'Coin shown is a representative example.')
    result = scrape_modern(source('shopify', KINZER), Fetcher({KINZER: fixture('kinzer-republic.html'), PRODUCT: detail}))
    assert result.complete and not result.listings


def test_kinzer_sold_offer_is_kept_as_sold():
    detail = fixture('kinzer-republic-product.html').replace('schema.org/InStock', 'schema.org/OutOfStock').replace('Add to cart', 'Sold out').replace('<button name="add">', '<button name="add" disabled>')
    result = scrape_modern(source('shopify', KINZER), Fetcher({KINZER: fixture('kinzer-republic.html'), PRODUCT: detail}))
    assert result.complete and result.listings[0].availability == 'sold'


def test_kinzer_missing_next_page_retains_rows_but_stays_partial():
    page = fixture('kinzer-republic.html').replace('1 product', '2 products') + '<nav class="pagination"><a href="?page=2">2</a></nav>'
    result = scrape_modern(source('shopify', KINZER), Fetcher({KINZER: page, PRODUCT: fixture('kinzer-republic-product.html')}))
    assert not result.complete and len(result.listings) == 1


def test_kinzer_repeated_page_or_missing_product_count_is_partial():
    page = fixture('kinzer-republic.html').replace('1 product', '2 products') + '<nav class="pagination"><a href="?page=2">2</a></nav>'
    fetcher = Fetcher({KINZER: page, KINZER+'?page=2': page, PRODUCT: fixture('kinzer-republic-product.html')})
    assert not scrape_modern(source('shopify', KINZER), fetcher).complete
    missing = fixture('kinzer-republic.html').replace('1 product', '')
    assert not scrape_modern(source('shopify', KINZER), Fetcher({KINZER: missing})).complete


@pytest.mark.parametrize('item', [source(url='https://www.minotaurcoins.com/shop'), source('shopify', 'https://kinzercoins.com/collections/roman-empire')])
def test_unaudited_scopes_are_rejected_without_fetch(item):
    fetcher = Fetcher({})
    result = scrape_modern(item, fetcher)
    assert not result.complete and not fetcher.requests
