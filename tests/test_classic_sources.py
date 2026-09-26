from types import SimpleNamespace

import pytest

from coinwatch.classic_sources import scrape_classic


BASE = 'https://www.ancientcointraders.com/greek-c-22.html'
SOURCE = dict(adapter='ancient-coin-traders', url=BASE)


def card(ident, title='Athens AR tetradrachm, 449-413 BC', stock='1', price='1500.00', extra=''):
    return f'''<div class="card is-product" data-product-price="{price}">
      <a href="https://www.ancientcointraders.com/greek-athens-p-{ident}.html?ceid=ephemeral"><img src="images/{ident}.jpg"></a>
      <div class="card-body"><h5 class="card-title"><a href="https://www.ancientcointraders.com/greek-athens-p-{ident}.html?ceid=ephemeral">{title}</a></h5>
      <h6 class="card-subtitle">{price} AUD</h6></div>
      <a class="btn-buy" data-product-id="{ident}" data-in-stock="{stock}" href="{BASE}?products_id={ident}&amp;action=buy_now">Add To Cart</a>{extra}</div>'''


def page(cards, start, end, total, next_page=None):
    pager = f'<ul class="pagination"><li><a href="{BASE}?page={next_page}&amp;sort=1a&amp;ceid=ephemeral">Next</a></li></ul>' if next_page else ''
    return f'<h1>Greek</h1><div class="row">{cards}</div><p>Displaying {start} to {end} (of {total} products)</p>{pager}'


class Fetcher:
    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def get(self, url):
        self.requested.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(url=url, text=value)


def test_act_reads_complete_pagination_and_uses_current_inventory_semantics():
    first = page(card('1') + card('2', stock='0'), 1, 2, 3, 2)
    second = page(card('3', stock='999'), 3, 3, 3)
    fetcher = Fetcher({BASE: first, BASE+'?page=2&sort=1a': second})
    result = scrape_classic(SOURCE, fetcher)
    assert result.complete and result.pages == 2
    assert [row.availability for row in result.listings] == ['available', 'sold', 'reserved']
    assert result.listings[0].price == '1500.00' and result.listings[0].currency == 'AUD'
    assert result.listings[0].url == 'https://www.ancientcointraders.com/greek-athens-p-1.html'
    assert all('action=' not in url and 'ceid=' not in url for url in fetcher.requested)


def test_explicit_sold_title_overrides_positive_inventory_and_sale_price_is_current():
    html = page(card('1', title='SOLD: Athens AR tetradrachm', stock='1', price='100.00', extra='<del>150.00 AUD</del>'), 1, 1, 1)
    result = scrape_classic(SOURCE, Fetcher({BASE: html}))
    assert result.complete and result.listings[0].availability == 'sold'
    assert result.listings[0].price == '100.00'


@pytest.mark.parametrize('stock', ['', 'unknown', '-1'])
def test_unknown_stock_never_becomes_complete_or_available(stock):
    html = page(card('1', stock=stock) + card('2'), 1, 2, 2)
    result = scrape_classic(SOURCE, Fetcher({BASE: html}))
    assert not result.complete and len(result.listings) == 1


def test_malformed_price_and_currency_keep_valid_rows_but_make_scan_partial():
    bad = card('1').replace('1500.00 AUD', '1500.00 USD')
    html = page(bad + card('2'), 1, 2, 2)
    result = scrape_classic(SOURCE, Fetcher({BASE: html}))
    assert not result.complete and [row.external_id for row in result.listings] == ['2']


def test_bulk_replica_and_book_cards_are_excluded_without_hiding_valid_coin():
    html = page(card('1', title='Lot of 5 ancient Greek coins') + card('2', title='Replica Greek coin') +
                card('3', title='Book about Greek coins') + card('4'), 1, 4, 4)
    result = scrape_classic(SOURCE, Fetcher({BASE: html}))
    assert result.complete and [row.external_id for row in result.listings] == ['4']


@pytest.mark.parametrize('problem', ['missing_next', 'repeated_page', 'changed_total', 'cross_domain', 'missing_count'])
def test_incomplete_or_untrusted_pagination_never_replaces_complete_inventory(problem):
    first = page(card('1') + card('2'), 1, 2, 3, None if problem == 'missing_next' else 2)
    second = page(card('3'), 3, 3, 3)
    if problem == 'repeated_page': second = first
    if problem == 'changed_total': second = page(card('3'), 3, 3, 4, 3)
    if problem == 'cross_domain': first = first.replace(BASE+'?page=2', 'https://other.example/catalog?page=2')
    if problem == 'missing_count': first = first.replace('Displaying 1 to 2 (of 3 products)', '')
    fetcher = Fetcher({BASE: first, BASE+'?page=2&sort=1a': second})
    result = scrape_classic(SOURCE, fetcher)
    assert not result.complete
    assert all(url.startswith(BASE) for url in fetcher.requested)


def test_later_fetch_failure_and_page_cap_preserve_valid_rows(monkeypatch):
    first = page(card('1') + card('2'), 1, 2, 3, 2)
    failed = scrape_classic(SOURCE, Fetcher({BASE: first, BASE+'?page=2&sort=1a': OSError('unavailable')}))
    assert not failed.complete and len(failed.listings) == 2
    monkeypatch.setattr('coinwatch.classic_sources.MAX_PAGES', 1)
    capped = scrape_classic(SOURCE, Fetcher({BASE: first}))
    assert not capped.complete and len(capped.listings) == 2


def test_homepage_and_nonancient_scopes_are_not_treated_as_verified_categories():
    for url in ('https://www.ancientcointraders.com/', 'https://www.ancientcointraders.com/bulk-c-27.html'):
        fetcher = Fetcher({})
        assert not scrape_classic({**SOURCE, 'url': url}, fetcher).complete
        assert fetcher.requested == []


def test_optional_image_paths_with_spaces_do_not_lose_valid_products():
    html = page(card('1').replace('images/1.jpg', 'images/Athens Owl.jpg'), 1, 1, 1)
    result = scrape_classic(SOURCE, Fetcher({BASE: html}))
    assert result.complete and result.listings[0].image_url.endswith('/images/Athens%20Owl.jpg')


GAS = 'https://www.gascogne-monnaie.com/catalog/index.php?cPath=136'
GAS_SOURCE = dict(adapter='gascogne', url=GAS)


def gas_url(ident):
    return f'https://www.gascogne-monnaie.com/catalog/product_info.php?cPath=136&products_id={ident}'


def gas_row(ident, title='Boetica, Thebes hemidrachm 379-371 BC gF', price='80.00€'):
    url = gas_url(ident).replace('https:', 'http:') + '&osCsid=temporary'
    return f'<tr><td><img src="images/coin {ident}.jpg"></td><td><a href="{url}">{title}</a></td><td align="right">{price}</td><td><a href="{GAS}&action=buy_now&products_id={ident}">Buy Now</a></td></tr>'


def gas_page(rows, first, last, total, next_page=None):
    pager = f'<a href="{GAS.replace("https:", "http:")}&sort=2a&page={next_page}&osCsid=temporary">Next</a>' if next_page else ''
    return f'<h1>Greek coins</h1><table class="productListingData">{rows}</table><p>Displaying {first} to {last} (of {total} products)</p>{pager}'


def gas_detail(ident, title='Boetica, Thebes hemidrachm 379-371 BC gF', price='80.00€', button='<button type="submit">Add to Cart</button>', extra=''):
    return f'''<form name="cart_quantity" method="post" action="{gas_url(ident).replace('https:', 'http:')}&action=add_product&osCsid=temporary">
    <div><h1 style="float:right">{price}</h1><h1>{title}</h1></div><div class="contentText">Ancient Greek silver coin {extra}</div>
    <div class="buttonSet"><input type="hidden" name="products_id" value="{ident}">{button}</div></form>'''


def test_gascogne_full_sweep_checks_product_forms_without_requesting_cart_actions():
    fetcher = Fetcher({GAS: gas_page(gas_row('1'), 1, 1, 2, 2), gas_url('1'): gas_detail('1'),
                       GAS+'&sort=2a&page=2': gas_page(gas_row('2'), 2, 2, 2), gas_url('2'): gas_detail('2')})
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert result.complete and result.pages == 2
    assert len(result.listings) == 2
    assert result.listings[0].url == gas_url('1')
    assert (result.listings[0].price, result.listings[0].currency, result.listings[0].category) == ('80.00', 'EUR', 'Greek')
    assert result.listings[0].image_url.endswith('/images/coin%201.jpg')
    assert all(url.startswith('https://www.gascogne-monnaie.com/') and 'action=' not in url and 'osCsid' not in url for url in fetcher.requested)


@pytest.mark.parametrize('button', ['', '<button disabled>Add to Cart</button>', '<button style="display:none">Add to Cart</button>'])
def test_gascogne_catalog_buy_now_is_insufficient_when_detail_purchase_is_missing(button):
    fetcher = Fetcher({GAS: gas_page(gas_row('1'), 1, 1, 1), gas_url('1'): gas_detail('1', button=button)})
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert not result.complete and result.listings == []


@pytest.mark.parametrize('title,extra', [('SOLD - Boetica hemidrachm 379 BC', ''), ('Boetica hemidrachm 379 BC', '<p class="stock">Out of stock</p>')])
def test_gascogne_sold_evidence_overrides_stale_purchase_control(title, extra):
    fetcher = Fetcher({GAS: gas_page(gas_row('1', title=title), 1, 1, 1), gas_url('1'): gas_detail('1', title=title, extra=extra)})
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert result.complete and result.listings[0].availability == 'sold'


def test_gascogne_sale_price_ignores_crossed_out_price_and_sidebar_products():
    price = '<del>100.00€</del> <span class="productSpecialPrice">80.00€</span>'
    html = gas_page(gas_row('1', price=price), 1, 1, 1) + '<aside><table>'+gas_row('99')+'</table></aside>'
    fetcher = Fetcher({GAS: html, gas_url('1'): gas_detail('1', price=price)})
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert result.complete and [item.price for item in result.listings] == ['80.00']


@pytest.mark.parametrize('bad', ['currency', 'identity', 'fetch', 'redirect', 'hidden_price'])
def test_gascogne_bad_detail_preserves_other_rows_and_makes_result_partial(bad):
    detail = gas_detail('1')
    if bad == 'currency': detail = detail.replace('80.00€', '$80.00 USD')
    if bad == 'identity': detail = gas_detail('99')
    if bad == 'fetch': detail = OSError('unavailable')
    if bad == 'hidden_price': detail = detail.replace('80.00€', '<span hidden>80.00€</span>Ask for price')
    fetcher = Fetcher({GAS: gas_page(gas_row('1')+gas_row('2'), 1, 2, 2), gas_url('1'): detail, gas_url('2'): gas_detail('2')})
    if bad == 'redirect':
        original = fetcher.get
        def redirect(url):
            result = original(url)
            if url == gas_url('1'): result.url = gas_url('99')
            return result
        fetcher.get = redirect
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert not result.complete and [item.external_id for item in result.listings] == ['2']


@pytest.mark.parametrize('bad', ['missing_next', 'changed_total', 'foreign_page', 'foreign_product', 'missing_count'])
def test_gascogne_unknown_pagination_and_foreign_links_are_partial(bad):
    first = gas_page(gas_row('1'), 1, 1, 2, None if bad == 'missing_next' else 2)
    second = gas_page(gas_row('2'), 2, 2, 3 if bad == 'changed_total' else 2)
    if bad == 'foreign_page': first = first.replace('http://www.gascogne-monnaie.com/catalog/index.php', 'https://foreign.example/catalog/index.php')
    if bad == 'foreign_product': first = first.replace('http://www.gascogne-monnaie.com/catalog/product_info.php', 'https://foreign.example/catalog/product_info.php')
    if bad == 'missing_count': first = first.replace('Displaying 1 to 1 (of 2 products)', '')
    fetcher = Fetcher({GAS: first, gas_url('1'): gas_detail('1'), GAS+'&sort=2a&page=2': second, gas_url('2'): gas_detail('2')})
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert not result.complete
    assert all('foreign.example' not in url for url in fetcher.requested)


def test_gascogne_only_verified_category_is_accepted():
    for url in (GAS.replace('136', '999'), GAS+'&action=buy_now', GAS.split('?')[0]):
        fetcher = Fetcher({})
        assert not scrape_classic({**GAS_SOURCE, 'url': url}, fetcher).complete
        assert fetcher.requested == []


@pytest.mark.parametrize('title,availability', [
    ('RESERVED - Athens AR tetradrachm', 'reserved'),
    ('\u1dc5 SOLD : Athens AR tetradrachm', 'sold'),
    ('Athens AR tetradrachm [RESERVED]', 'reserved'),
    ('Athens AR tetradrachm - SOLD', 'sold'),
    ('Athens AR tetradrachm (SOLD)', 'sold'),
    ('Athens AR tetradrachm, ex collection sold in 1926', 'available'),
])
def test_act_current_title_status_overrides_quantity_but_provenance_does_not(title, availability):
    result = scrape_classic(SOURCE, Fetcher({BASE: page(card('1', title=title), 1, 1, 1)}))
    assert result.complete and result.listings[0].availability == availability


@pytest.mark.parametrize('title,extra,availability', [
    ('RESERVED - Boetica hemidrachm 379 BC', '', 'reserved'),
    ('Boetica hemidrachm 379 BC [RESERVED]', '', 'reserved'),
    ('Boetica hemidrachm 379 BC', '<p class="stock">Reserved</p>', 'reserved'),
    ('Boetica hemidrachm 379 BC, ex collection sold in 1926', '', 'available'),
])
def test_gascogne_current_title_or_stock_status_overrides_cart_but_provenance_does_not(title, extra, availability):
    fetcher = Fetcher({GAS: gas_page(gas_row('1', title=title), 1, 1, 1), gas_url('1'): gas_detail('1', title=title, extra=extra)})
    result = scrape_classic(GAS_SOURCE, fetcher)
    assert result.complete and result.listings[0].availability == availability
