import json
from types import SimpleNamespace

import pytest

from coinwatch.catalog_links import catalog_links, is_catalog_url, same_dealer
from coinwatch.searches import validate_search


BASE = 'https://dealer.example/collections/greek'
SEARCH = validate_search({'keywords': 'Boiotian, Boetia', 'category': 'Greek', 'currency': 'GBP', 'max_price': '200'})
TITLE = 'Boetia AR hemidrachm 395-340 BC'


def page(html, url=BASE):
    return SimpleNamespace(url=url, text=html)


def card(url='/products/boetia', title=TITLE, price='$300.00 USD', extra=''):
    return f'<div class="card-wrapper"><h3 class="card__heading"><a href="{url}">{title}</a></h3><span class="price">{price}</span>{extra}</div>'


def test_extracts_matching_shop_products_without_approving_price_or_currency():
    found = catalog_links(page('<main>'+card()+card('/products/ionia', 'Ionia AR drachm 300 BC')+'</main>'), SEARCH)
    assert found == {'products': [{'url': 'https://dealer.example/products/boetia', 'title': TITLE, 'snippet': ''}], 'next_pages': []}


def test_relevance_preserves_extra_words_and_exclusions_but_not_unrelated_query_snippets():
    search = validate_search({'keywords': 'Boeotia owl', 'exclude_terms': 'replica', 'category': 'Greek'})
    html = card(title=TITLE)+card('/products/replica', TITLE+' owl replica')+card('/products/owl', TITLE+' owl')
    assert [r['url'] for r in catalog_links(page(html), search)['products']] == ['https://dealer.example/products/owl']


def test_extracts_structured_itemlist_only_with_individual_product_offer_evidence():
    data = {'@type': 'ItemList', 'itemListElement': [
        {'@type': 'ListItem', 'item': {'@type': 'Product', 'name': TITLE, 'url': '/products/one',
                                    'offers': {'@type': 'Offer', 'price': '125.00', 'priceCurrency': 'EUR'}}},
        {'@type': 'ListItem', 'item': {'@type': 'Product', 'name': TITLE, 'url': '/products/no-offer'}},
    ]}
    html = '<main><h1>Greek coins</h1></main><script type="application/ld+json">'+json.dumps(data)+'</script>'
    assert [r['url'] for r in catalog_links(page(html), SEARCH)['products']] == ['https://dealer.example/products/one']


def test_navigation_related_products_and_editorial_recommendations_are_not_catalogs():
    for html in ('<nav>'+card()+'</nav>', '<aside>'+card()+'</aside>', '<main class="related-products">'+card()+'</main>',
                 '<main><article><h1>History of Boeotia</h1>'+card()+'</article></main>',
                 '<script type="application/ld+json">{"@type":"Article"}</script>'+card()):
        assert catalog_links(page(html), SEARCH) == {'products': [], 'next_pages': []}
    assert not catalog_links(page(card(), 'https://dealer.example/blog/boeotia'), SEARCH)['products']


@pytest.mark.parametrize('url', [
    'https://other.example/products/boetia', 'http://dealer.example/products/boetia',
    'https://127.0.0.1/products/boetia', 'https://dealer.example:8443/products/boetia',
    '/cart?add=1', '/products/boetia?add-to-cart=1', '/products/boetia?action=',
    '/products/boetia?add_to_wishlist=1', '/checkout', '/products/boetia.pdf',
    '/collections/boeotia', '/auctions/lot-1', '/products/boetia?sold=1', 'javascript:alert(1)',
])
def test_untrusted_actions_nonproducts_and_archive_links_are_never_returned(url):
    assert not catalog_links(page(card(url)), SEARCH)['products']


def test_canonical_dedup_keeps_product_identity_and_drops_tracking_fragments():
    html = card('/products/coin?id=7&utm_source=test#coin')+card('https://www.dealer.example/products/coin?id=7&fbclid=x')
    found = catalog_links(page(html), SEARCH)['products']
    assert len(found) == 1 and found[0]['url'] == 'https://dealer.example/products/coin?id=7'


def test_observed_pagination_is_same_category_and_advances_only():
    pager = '''<nav class="pagination"><a href="?page=1">1</a><a href="?page=2">2</a><a href="?page=3">3</a>
    <a href="?page=540">Last</a><a href="/collections/roman?page=2">Other</a>
    <a href="?page=2&filter=medieval">Other filter</a><a href="https://other.example/collections/greek?page=2">Other dealer</a></nav>'''
    assert catalog_links(page(card()+pager), SEARCH)['next_pages'] == [BASE+'?page=2', BASE+'?page=3']
    assert catalog_links(page(card()+'<a rel="next" href="?page=2">Next</a>', BASE+'?page=2'), SEARCH)['next_pages'] == []


def test_path_pagination_and_unmatched_first_page_can_still_find_next_category_page():
    base='https://dealer.example/product-category/greek/'
    html=card('/products/ionia', 'Ionia AR drachm 300 BC')+'<a rel="next" href="/product-category/greek/page/2/">Next</a>'
    assert catalog_links(page(html, base), SEARCH) == {'products': [], 'next_pages': [base+'page/2/']}


def test_pagination_without_any_shop_product_evidence_is_not_followed():
    assert catalog_links(page('<h1>Ancient Greek coins article</h1><a rel="next" href="?page=2">Next</a>'), SEARCH)['next_pages'] == []
    assert not catalog_links(page(card(price='Learn more')), SEARCH)['products']


def test_woocommerce_pagination_without_rel_next_is_observed():
    url = 'https://dealer.example/product-category/greek/'
    html = card() + '<nav class="woocommerce-pagination"><a class="next page-numbers" href="/product-category/greek/page/2/">→</a></nav>'
    assert catalog_links(page(html, url), SEARCH)['next_pages'] == [url+'page/2/']


def test_primary_product_and_auction_pages_do_not_expand_recommendations():
    assert not catalog_links(page(card(), 'https://dealer.example/products/one'), SEARCH)['products']
    assert not catalog_links(page('<main><h1>Current bid 50 USD</h1>'+card()+'</main>'), SEARCH)['products']
    schema={'@type':'Product','name':TITLE,'url':BASE,'offers':{'price':'20','priceCurrency':'USD'}}
    assert not catalog_links(page('<script type="application/ld+json">'+json.dumps(schema)+'</script>'+card()), SEARCH)['products']


def test_products_capped_at_twenty_and_titles_bounded():
    html=''.join(card('/products/coin-'+str(i), TITLE+' '+('x'*600)) for i in range(30))
    found=catalog_links(page(html), SEARCH)
    assert len(found['products']) == 20 and all(len(row['title']) <= 500 for row in found['products'])


@pytest.mark.parametrize('url,expected', [
    (BASE,True), (BASE+'?page=2',True), ('https://dealer.example/product-category/ancient/greek',True),
    ('https://dealer.example/greek-c-22.html',True), ('https://dealer.example/catalog/index.php?cPath=136',True),
    ('https://dealer.example/products/coin',False), ('https://dealer.example/collections/greek/products/coin',False),
    ('https://dealer.example/catalog/product_info.php?products_id=4',False), ('https://dealer.example/blog/coins',False),
    ('https://dealer.example/',False), ('https://en.numista.com/catalogue/boeotia',False),
])
def test_conservative_catalog_url_hint(url,expected):
    assert is_catalog_url(url) is expected


def test_same_dealer_uses_only_www_equivalence_and_matching_effective_port():
    assert same_dealer(BASE,'https://www.dealer.example:443/products/coin')
    assert not same_dealer(BASE,'http://dealer.example/products/coin')
    assert not same_dealer(BASE,'https://shop.dealer.example/products/coin')
    assert not same_dealer(BASE,'https://dealer.example:8443/products/coin')
    assert not same_dealer(BASE,'https://user:pass@dealer.example/products/coin')


def test_merconis_short_mint_title_is_a_lead_under_an_explicit_ancient_catalog():
    url = 'https://petasoscoins.com/en/greek.html'
    html = '''<title>Ancient Greek Coins - Petasos Coins</title><base href="https://petasoscoins.com/">
    <main><div class="shopProduct template_productOverview">
      <div class="currentPrice">40,00 €</div><input type="submit" value="Add to cart">
      <a href="en/quickview-57/product/boeotia-thebes-shield-trident.html" title="Quickview: Boeotia, Thebes; shield/trident"><img alt=""></a>
      <h2><a class="productTitle" href="en/greek/product/boeotia-thebes-shield-trident.html#p_12237-0">Boeotia, Thebes; shield/trident</a></h2>
    </div><div class="pagination block"><a href="en/greek.html?page_standard=2">2</a>
    <a href="en/greek.html?page_standard=8">Last</a></div></main>'''
    found = catalog_links(page(html, url), SEARCH)
    assert found == {'products': [{'url': 'https://petasoscoins.com/en/greek/product/boeotia-thebes-shield-trident.html',
                                   'title': 'Boeotia, Thebes; shield/trident', 'snippet': ''}],
                     'next_pages': [url+'?page_standard=2']}
    assert is_catalog_url(url) and is_catalog_url(url+'?page_standard=2')


def test_short_mint_title_needs_explicit_catalog_context_and_keeps_exclusions():
    short = card(title='Boeotia, Thebes; shield/trident')
    assert not catalog_links(page(short), SEARCH)['products']
    for title in ('Boeotia book', 'Boeotia replica', 'Boeotia token'):
        assert not catalog_links(page('<title>Ancient Greek Coins</title>'+card(title=title)), SEARCH)['products']


def test_foreign_base_cannot_rewrite_relative_product_links():
    html = '<title>Ancient Greek Coins</title><base href="https://other.example/">'+card()
    assert catalog_links(page(html), SEARCH) == {'products': [], 'next_pages': []}


def test_primary_card_heading_is_used_without_price_or_action_text():
    html = '''<li class="product"><a href="/products/boetia"><span class="price">1995 €</span>
    <h2 class="woocommerce-loop-product__title">Boetia AR stater</h2></a></li>'''
    assert catalog_links(page(html), SEARCH)['products'] == [
        {'url': 'https://dealer.example/products/boetia', 'title': 'Boetia AR stater', 'snippet': ''}]


@pytest.mark.parametrize('status', ['class="product outofstock"', 'class="product product-archive"'])
def test_explicit_sold_archive_cards_do_not_spend_product_checks_but_allow_pagination(status):
    html = f'<article {status}><h3><a href="/products/boetia">{TITLE}</a></h3><span class="price">130€</span><strong class="product-archive-label">VENDU</strong></article>'
    html += '<nav class="pagination"><a href="?page=2">Next</a></nav>'
    assert catalog_links(page(html), SEARCH) == {'products': [], 'next_pages': [BASE+'?page=2']}
