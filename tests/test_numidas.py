from types import SimpleNamespace

from coinwatch.sources import scrape_source

URL = 'https://www.numidas.at/m%C3%BCnzen/greek/'
SOURCE = dict(id='numidas-at', adapter='numidas', url=URL)


def card(ident, title, price='940', stock='InStock', quantity='1', button_class=''):
    return f'''<div id="cc-m-product-{ident}" itemscope itemtype="http://schema.org/Product">
    <h4 itemprop="name">{title}</h4><img itemprop="image" src="https://images.example/coin.jpg">
    <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
    <p itemprop="price" content="{price}">{price},00 €</p>
    <meta itemprop="priceCurrency" content="EUR"><meta itemprop="availability" content="{stock}">
    <span itemprop="inventoryLevel"><meta itemprop="value" content="{quantity}"></span></div>
    <button data-action="addToCart" class="{button_class}">In den Warenkorb</button></div>'''


def run(body):
    return scrape_source(SOURCE, SimpleNamespace(get=lambda url: SimpleNamespace(url=url, text=body)))


def test_numidas_uses_product_inventory_and_anchor_identity():
    result = run(card('1234', 'Attica, Athens AR Tetradrachm') +
                 card('5678', 'Troas, Assos AR Triobol', stock='OutOfStock', quantity='0',
                      button_class='cc-addtocard-disabled'))
    assert result.complete and result.pages == 1
    assert [(x.external_id, x.currency, x.price, x.availability) for x in result.listings] == [
        ('1234', 'EUR', '940.00', 'available'), ('5678', 'EUR', '940.00', 'sold')]
    assert result.listings[0].url == URL + '#cc-m-product-1234'
    assert result.listings[0].category == 'Greek'


def test_numidas_conflicting_stock_keeps_good_rows_but_not_complete():
    result = run(card('1234', 'Attica, Athens AR Tetradrachm', quantity='0') +
                 card('5678', 'Pontus, Mithradates VI AE'))
    assert not result.complete and len(result.listings) == 1
    assert 'stock' in result.error.lower()
    assert not run('<main>Temporary error</main>').complete


def test_numidas_rejects_unpriced_or_disabled_available_coin():
    for bad in [card('1234', 'Greek coin', price='NaN'),
                card('1234', 'Greek coin', button_class='cc-addtocard-disabled'),
                card('1234', 'Greek coin').replace('content="EUR"', 'content="USD"')]:
        assert not run(bad).complete
        assert not run(bad).listings


def test_numidas_filters_lots_and_rejects_unknown_pagination():
    assert not run(card('1234', 'Lot of 4 Greek coins')).listings
    assert not run(card('1234', 'Modern Greece 1975 coin')).listings
    assert run(card('1234', 'SOLD Athens tetradrachm')).listings[0].availability == 'sold'
    result = run(card('1234', 'Greek coin') + '<a rel="next" href="?page=2">Next</a>')
    assert not result.complete
