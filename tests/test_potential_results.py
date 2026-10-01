from datetime import datetime, timedelta, timezone

import pytest

from coinwatch.db import Database
from coinwatch.runtime import Runtime


def stored_results(tmp_path, rows, **criteria):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([])
    search_id = db.save_search(dict(keywords='Boeotia', category='Greek',
                                    include_web=False, **criteria))
    defaults = dict(title='Boeotia AR hemidrachm 400 BC', sale_status='unverified',
                    sale_reason='Seller denied automated access (HTTP 403).',
                    sale_checked_at=datetime.now(timezone.utc).isoformat())
    db.record_web_search(search_id, [dict(defaults, **row) for row in rows])
    return db, search_id


def test_blocked_matching_listing_is_visible_with_unknown_price_and_currency(tmp_path):
    db, search_id = stored_results(tmp_path, [dict(url='https://dealer.example/coin',
                                   snippet='In stock! Buy now for GBP 80.')],
                                   currency='GBP', max_price='100')
    before = db.search_results(search_id)
    result = Runtime(db).search_results(search_id)
    assert result['results'] == []
    assert len(result['potential_results']) == 1
    row = result['potential_results'][0]
    assert row['verification'] == 'unverified'
    assert row['verification_label'] == 'Unverified'
    assert 'HTTP 403' in row['verification_note']
    assert row['price'] == row['currency'] == ''
    assert result['potential_counts'] == dict(verified=0, unverified=1, expired=0)
    assert db.search_results(search_id) == before


def test_potential_results_include_verified_and_expired_with_distinct_price_labels(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    db, search_id = stored_results(tmp_path, [
        dict(url='https://dealer.example/current', sale_status='available', price='80', currency='GBP'),
        dict(url='https://dealer.example/old', sale_status='available', price='90', currency='GBP',
             sale_checked_at=old),
        dict(url='https://dealer.example/blocked', price='95', currency='GBP'),
    ], currency='GBP', max_price='100')
    result = Runtime(db).search_results(search_id)
    assert [row['url'] for row in result['results']] == ['https://dealer.example/current']
    rows = result['potential_results']
    assert [row['verification'] for row in rows] == ['verified', 'expired', 'unverified']
    assert rows[0]['price'] == '80'
    assert rows[1]['price'] == '90'
    assert rows[1]['price_label'] == 'Last known price'
    assert 'new check' in rows[1]['verification_note']
    assert rows[2]['price'] == rows[2]['currency'] == ''
    assert result['potential_counts'] == dict(verified=1, unverified=1, expired=1)


@pytest.mark.parametrize('url', [
    'https://en.numista.com/catalogue/pieces1.html',
    'https://dealer.example/blog/boeotia',
    'https://dealer.example/articles/boeotia',
    'https://dealer.example/auctions/lot-1',
    'https://dealer.example/auction-123/coin',
    'https://www.biddr.com/auctions/dealer/lot-1',
    'https://one.bid/en/coins-boeotia-stater/3409607',
    'https://nomisma.bidinside.com/it/lot/566571/beozia-tebe-statere',
    'https://drouot.com/fr/l/35117327-grecques-beotie-thebes-stater',
    'https://auctions.goldbergcoins.com/m/lot-details/index/catalog/50/lot/108974',
    'https://wannenesgroup.com/lots/578-350-monete-greche-beozia-tebe-statere',
    'https://www.academia.edu/106256729/Boeotian_Silver',
    'https://picryl.com/media/monnaie-statere-argent-thebes-beotie',
    'https://www.alamy.com/ancient-greek-hemidrachm-coin-image337925479.html',
    'https://www.artic.edu/artworks/110697/hemidrachm-coin',
    'https://www.superstock.com/asset/mint-boeotia-coin/4443-19568020',
    'https://collections.museumsvictoria.com.au/items/56268',
    'https://www.numisforums.com/topic/8689-the-mints-of-boeotia',
    'https://www.coincommunity.com/forum/topic.asp?topic_id=123',
    'https://www.worthpoint.com/worthopedia/tanagra-boeotia-silver-obol',
    'https://www.album-online.com/detail/es/NDFhNjRiMA/moneda-griega-beocia',
    'https://coinreplicas.com/product/thebes-boeotia-stater-288-244-b-c',
    'https://www.trustedancientcoins.com/greek-coins-by-area/b-c/boeotia',
    'https://www.ma-shops.de/shops/search.php?searchstr=beotie',
    'https://www.issoire-philatelie.com/808-pieces-de-piece-de-monnaie-grecques-de-beotie',
    'https://picclick.com/Boeotia-Thebes-AR-Stater-123.html',
    'https://dealer.example/collections/boeotia',
    'https://dealer.example/product-category/greek',
    'https://dealer.example/greek-c-22.html',
    'https://dealer.example/catalog/index.php?cPath=136',
    'https://dealer.example/shop',
    'https://dealer.example/coin?sold=1',
    'https://www.todocoleccion.net/s/boeotia',
])
def test_informational_auction_and_catalog_urls_are_not_potential_listings(tmp_path, url):
    db, search_id = stored_results(tmp_path, [dict(url=url)])
    assert Runtime(db).search_results(search_id)['potential_results'] == []


@pytest.mark.parametrize('changes', [
    dict(sale_status='rejected', sale_reason='Sold out'),
    dict(title='Boeotia AR hemidrachm SOLD'),
    dict(title='Boeotia AR hemidrachm replica'),
    dict(title='Boeotia ancient coin book'),
    dict(title='Boeotia ancient coin catalogue'),
    dict(title='Boeotia Stater Coin Pendant, Greek Jewelry'),
    dict(title='Boeotia Greek coin stock photo'),
    dict(title='BOEOTIA Authentic Ancient Greek Coins Reference for SALE'),
    dict(title='Boeotia AR Stater - Online auction / Online bidding'),
    dict(title='Roman Hadrian denarius', snippet='Boeotia Greek coin available for sale'),
])
def test_sold_rejected_noncoin_and_offtopic_candidates_stay_hidden(tmp_path, changes):
    db, search_id = stored_results(tmp_path, [dict(url='https://dealer.example/coin', **changes)])
    assert Runtime(db).search_results(search_id)['potential_results'] == []


@pytest.mark.parametrize('snippet', [
    'Shield-Khantaros. Sold. SKU: 8229515-002. $149.00.',
    'Research Coins: The Coin Shop | 855545. | Sold For $595 |',
    'Ancient Greek Coins. Currently Out of Stock. Free shipping on orders $199+.',
    'Thebes Stater. 340 EUR. TB+. Vendu. Thebes NGC Stater Obverse.',
    'Boeotian Shield Greek REPLICA REPRODUCTION COIN. Size: 21 mm.',
])
def test_explicit_unavailable_or_replica_snippet_cannot_be_a_potential_offer(tmp_path, snippet):
    db, search_id = stored_results(tmp_path, [dict(url='https://dealer.example/coin', snippet=snippet)])
    assert Runtime(db).search_results(search_id)['potential_results'] == []


def test_sold_navigation_and_older_snippets_do_not_override_current_product_evidence(tmp_path):
    db, search_id = stored_results(tmp_path, [
        dict(url='https://dealer.example/lead', snippet='The Coin Shop. New Items. Research Sold Items.'),
        dict(url='https://dealer.example/current', sale_status='available', price='85', currency='GBP',
             snippet='Old search text. Out of stock.'),
    ])
    assert [row['verification'] for row in Runtime(db).search_results(search_id)['potential_results']] == [
        'unverified', 'verified']


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('tied', [False, True])
def test_latest_rejected_alias_suppresses_older_available_offer(tmp_path, reverse, tied):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent = old if tied else datetime.now(timezone.utc).isoformat()
    rows = [dict(url='https://www.dealer.example/coin', sale_status='available',
                 price='85', currency='GBP', sale_checked_at=old),
            dict(url='https://dealer.example/coin', sale_status='rejected', sale_reason='Sold out',
                 sale_checked_at=recent)]
    db, search_id = stored_results(tmp_path, list(reversed(rows)) if reverse else rows)
    result = Runtime(db).search_results(search_id)
    assert result['results'] == result['potential_results'] == []
    assert db.search_results(search_id)['results'][0]['sale_status'] == rows[1 if reverse else 0]['sale_status']


def test_latest_unverified_alias_does_not_reuse_old_available_price(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    db, search_id = stored_results(tmp_path, [
        dict(url='https://www.dealer.example/coin', sale_status='available',
             price='85', currency='GBP', sale_checked_at=old),
        dict(url='https://dealer.example/coin'),
    ])
    rows = Runtime(db).search_results(search_id)['potential_results']
    assert len(rows) == 1
    assert rows[0]['url'] == 'https://dealer.example/coin'
    assert rows[0]['verification'] == 'unverified'
    assert rows[0]['price'] == rows[0]['currency'] == ''


@pytest.mark.parametrize('reverse', [False, True])
def test_tied_rejected_alias_suppresses_unverified_listing(tmp_path, reverse):
    checked = datetime.now(timezone.utc).isoformat(timespec='seconds')
    rows = [dict(url='https://www.dealer.example/coin', sale_checked_at=checked),
            dict(url='https://dealer.example/coin', sale_status='rejected',
                 sale_reason='Sold out', sale_checked_at=checked)]
    db, search_id = stored_results(tmp_path, list(reversed(rows)) if reverse else rows)
    assert Runtime(db).search_results(search_id)['potential_results'] == []


@pytest.mark.parametrize('changes, visible', [
    (dict(price='', currency=''), True),
    (dict(price='95', currency=''), True),
    (dict(price='', currency='GBP'), True),
    (dict(price='NaN', currency='GBP'), True),
    (dict(price='101', currency='GBP'), False),
    (dict(price='95', currency='EUR'), False),
    (dict(price='', currency='EUR'), False),
])
def test_known_price_and_currency_constraints_still_apply(tmp_path, changes, visible):
    db, search_id = stored_results(tmp_path, [dict(url='https://dealer.example/coin', **changes)],
                                   currency='GBP', max_price='100')
    assert bool(Runtime(db).search_results(search_id)['potential_results']) is visible


def test_expired_offers_still_obey_known_price_and_currency_constraints(tmp_path):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    db, search_id = stored_results(tmp_path, [
        dict(url='https://dealer.example/expensive', sale_status='available', price='101', currency='GBP',
             sale_checked_at=old),
        dict(url='https://dealer.example/wrong-currency', sale_status='available', price='85', currency='EUR',
             sale_checked_at=old),
    ], currency='GBP', max_price='100')
    assert Runtime(db).search_results(search_id)['potential_results'] == []


def test_title_criteria_and_exclusions_are_kept_when_price_is_unknown(tmp_path):
    db, search_id = stored_results(tmp_path, [
        dict(url='https://dealer.example/good', title='Boeotia Thebes silver hemidrachm'),
        dict(url='https://dealer.example/wrong-type', title='Boeotia Thebes silver obol'),
        dict(url='https://dealer.example/wrong-mint', title='Boeotia Tanagra silver hemidrachm'),
        dict(url='https://dealer.example/excluded', title='Boeotia Thebes plated silver hemidrachm'),
    ], coin_type='hemidrachm', mint='Thebes', exclude_terms='plated')
    assert [row['url'] for row in Runtime(db).search_results(search_id)['potential_results']] == [
        'https://dealer.example/good']


def test_shorthand_product_and_collection_product_urls_remain_eligible(tmp_path):
    db, search_id = stored_results(tmp_path, [
        dict(url='https://dealer.example/collections/greek/products/boeotia', title='Boeotia AE13'),
        dict(url='https://dealer.example/catalog/product_info.php?products_id=4',
             title='Boeotia Federal Coinage AE bronze'),
    ])
    assert len(Runtime(db).search_results(search_id)['potential_results']) == 2


def test_future_check_cannot_present_a_last_known_available_price(tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    db, search_id = stored_results(tmp_path, [
        dict(url='https://dealer.example/coin', sale_status='available', price='85', currency='GBP',
             sale_checked_at=future),
    ])
    result = Runtime(db).search_results(search_id)
    assert result['results'] == []
    row = result['potential_results'][0]
    assert row['verification'] == 'unverified'
    assert row['price'] == row['currency'] == ''
