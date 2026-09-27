from datetime import datetime, timezone
import time

import pytest

from coinwatch.db import Database
from coinwatch.fetch import Page
from coinwatch.runtime import Runtime
from coinwatch.scanner import Scanner


ROOT = 'https://dealer.example'
TITLE = 'Greek Boeotia silver stater 400 BC'


def card(path, title=TITLE):
    return f'<li class="product"><a href="{path}"><h2>{title}</h2></a><span class="price">£125</span></li>'


def catalog(*cards, next_page=''):
    return '<main><h1>Boeotia coins</h1><ul class="products">' + ''.join(cards) + '</ul>' + (
        f'<a rel="next" href="{next_page}">Next</a>' if next_page else '') + '</main>'


def product(title=TITLE, stock='In stock'):
    return f'<main><article class="product"><h1>{title}</h1><span class="price">£125</span><p class="stock">{stock}</p><button>Add to cart</button></article></main>'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([])
    watch = db.save_search(dict(name='Boeotia', keywords='Boeotia', include_web=True))
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-key')
    pages, fetched, requests = {}, [], []

    class FakeFetcher:
        def __init__(self, stop_event=None, budget=900):
            self.deadline = time.monotonic() + budget

        def get(self, url):
            fetched.append(url)
            return Page(url, pages[url])

    monkeypatch.setattr('coinwatch.scanner.Fetcher', FakeFetcher)
    def search(query, key, **options):
        requests.append(options)
        return [dict(url=ROOT + '/collections/boeotia', title='Boeotia coins', snippet='Coins for sale')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    return db, watch, pages, fetched, requests


def test_deep_scan_follows_catalog_pagination_and_verifies_only_matching_in_stock_coins(setup):
    db, watch, pages, fetched, requests = setup
    pages[ROOT + '/collections/boeotia'] = catalog(card('/product/one'), card('/product/three'), next_page='?page=2')
    pages[ROOT + '/collections/boeotia?page=2'] = catalog(card('/product/two'), card('/product/other', 'Greek Athens owl tetradrachm 450 BC'), next_page='?page=2')
    pages[ROOT + '/product/one'] = product()
    pages[ROOT + '/product/two'] = product('Greek Boiotia silver obol 400 BC')
    pages[ROOT + '/product/three'] = product(stock='Sold out')
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=3, web_depth='advanced')
    available = db.search_results(watch, verified_only=True)['results']
    assert result['status'] == 'complete'
    assert {row['url'] for row in available} == {ROOT + '/product/one', ROOT + '/product/two'}
    assert all(row['price'] == '125.00' and row['currency'] == 'GBP' for row in available)
    assert len(fetched) == len(set(fetched)) == 5
    assert result['web_queries'] == 3 and result['web_leads'] == 2
    assert all(row['search_depth'] == 'advanced' for row in requests)
    assert '6 Tavily credits' in result['summary']
    assert '3 product links' in result['summary']


def test_standard_scan_does_not_explore_catalog_links(setup):
    db, watch, pages, fetched, requests = setup
    pages[ROOT + '/collections/boeotia'] = catalog(card('/product/one'))
    result = Scanner(db).run(mode='coins', search_id=watch)
    assert result['web_leads'] == 0
    assert fetched == [ROOT + '/collections/boeotia']
    assert all(row.get('search_depth', 'basic') == 'basic' for row in requests)


def test_deep_scan_revisits_retained_categories_during_identity_cooldown(setup, monkeypatch):
    db, watch, pages, fetched, _ = setup
    pages[ROOT + '/collections/boeotia'] = catalog(card('/product/one'))
    pages[ROOT + '/product/one'] = product()
    db.record_web_search(watch, [dict(url=ROOT + '/collections/boeotia', title='Boeotia coins',
        sale_status='unverified', sale_checked_at=datetime.now(timezone.utc).isoformat(),
        sale_reason='A single primary product could not be identified.')])
    monkeypatch.setattr('coinwatch.web_search.search_web', lambda *a, **kw: [])
    result = Scanner(db).run(mode='coins', search_id=watch, web_depth='advanced')
    assert result['web_leads'] == 1
    assert len(fetched) == 2


def test_deep_scan_retains_unchecked_paid_results_when_time_runs_out(setup, monkeypatch):
    db, watch, _, _, requests = setup
    def search(query, key, **options):
        requests.append(options)
        return [dict(url=ROOT + f'/product/{i}', title=TITLE) for i in range(3)]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    def verify(row, fetcher):
        fetcher.delegate.deadline = 0
        return dict(row, sale_status='unverified', sale_reason='Web-check time limit reached; check later.')
    monkeypatch.setattr('coinwatch.sale_checks.verify_sale', verify)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=2, web_depth='advanced')
    rows = db.search_results(watch)['results']
    assert result['status'] == 'partial' and len(requests) == 2
    assert len(rows) == 3
    assert sum(row['sale_reason'].startswith('Awaiting') for row in rows) == 2
    assert db.search_results(watch, verified_only=True)['results'] == []


@pytest.mark.parametrize('depth', ['auto', '', None, 2])
def test_invalid_depth_rejected_before_claiming_run(setup, depth):
    db, watch, *_ = setup
    for runner in (Scanner(db).run, Runtime(db).start_scan):
        with pytest.raises(ValueError, match='depth'):
            runner(mode='coins', search_id=watch, web_depth=depth)
    assert db.runs() == []


@pytest.mark.parametrize('options', [{}, {'kind': 'scheduled', 'search_id': 1}])
def test_advanced_requires_manual_selected_search(setup, options):
    db, *_ = setup
    with pytest.raises(ValueError, match='manual'):
        Scanner(db).run(mode='coins', web_depth='advanced', **options)
    assert db.runs() == []


def test_deep_scan_skips_monitored_catalog_refresh(setup):
    db, watch, pages, _, _ = setup
    pages[ROOT + '/collections/boeotia'] = catalog()
    db.initialize([dict(id='dealer', name='Dealer', url=ROOT, adapter='test', enabled=True)])
    def forbidden(*args):
        raise AssertionError('Deep search must focus on the wider web')
    result = Scanner(db, scrape=forbidden).run(mode='coins', search_id=watch, web_depth='advanced')
    assert result['status'] == 'complete' and result['seen'] == 0


def test_deep_scan_bounds_catalog_pages_and_additional_product_links(setup):
    db, watch, pages, fetched, _ = setup
    for i in range(1, 31):
        url = ROOT + '/collections/boeotia' + (f'?page={i}' if i > 1 else '')
        cards = []
        for j in range(10):
            path = f'/product/{i}-{j}'
            cards.append(card(path))
            pages[ROOT + path] = product()
        pages[url] = catalog(*cards, next_page=f'?page={i+1}')
    result = Scanner(db).run(mode='coins', search_id=watch, web_depth='advanced')
    assert result['status'] == 'complete'
    assert result['web_leads'] == 100
    assert len([url for url in fetched if '/product/' in url]) == 100
    assert len([url for url in fetched if '/collections/' in url]) == 20
    assert len(fetched) == len(set(fetched))


@pytest.mark.parametrize('support', ['permission_required', 'login_required'])
def test_deep_scan_does_not_fetch_restricted_dealers(setup, support):
    db, watch, _, fetched, _ = setup
    db.initialize([dict(id='restricted', name='Restricted dealer', url=ROOT, adapter='',
                        enabled=False, support_status=support)])
    result = Scanner(db).run(mode='coins', search_id=watch, web_depth='advanced')
    assert fetched == [] and result['web_leads'] == 0
    assert 'requires permission or login' in db.search_results(watch)['results'][0]['sale_reason']


def test_deep_scan_does_not_follow_alternatives_after_access_failure(setup, monkeypatch):
    from coinwatch.fetch import FetchError
    db, watch, _, _, _ = setup
    def blocked(self, url):
        raise FetchError('HTTP 403 fetching dealer.example.')
    monkeypatch.setattr('coinwatch.scanner.Fetcher.get', blocked)
    result = Scanner(db).run(mode='coins', search_id=watch, web_depth='advanced')
    assert result['web_leads'] == 0
    assert '0 catalog pages explored; 0 product links found' in result['summary']


def test_deep_combined_mode_rejected_to_preserve_the_displayed_credit_cap(setup):
    db, watch, *_ = setup
    for runner in (Scanner(db).run, Runtime(db).start_scan):
        with pytest.raises(ValueError, match='coins'):
            runner(mode='both', search_id=watch, web_queries=30, web_depth='advanced')
    assert db.runs() == []


def test_deep_scan_stops_when_saved_search_changes_during_paid_queries(setup, monkeypatch):
    db, watch, _, fetched, _ = setup
    def changed(query, key, **options):
        db.save_search(dict(db.get_search(watch), keywords='Athens'), search_id=watch)
        return [dict(url=ROOT + '/product/one', title=TITLE)]
    monkeypatch.setattr('coinwatch.web_search.search_web', changed)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=30, web_depth='advanced')
    assert fetched == [] and result['web_queries'] == 1
    assert db.search_results(watch)['results'] == []


def test_paid_response_arriving_after_deadline_is_saved_as_partial(setup, monkeypatch):
    db, watch, _, fetched, _ = setup
    import coinwatch.scanner as module
    original = module.Fetcher
    fetchers = []
    def make_fetcher(**options):
        value = original(**options)
        fetchers.append(value)
        return value
    monkeypatch.setattr(module, 'Fetcher', make_fetcher)
    def late(*args, **options):
        for fetcher in fetchers:
            fetcher.deadline = 0
        return [dict(url=ROOT + '/product/one', title=TITLE)]
    monkeypatch.setattr('coinwatch.web_search.search_web', late)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=30, web_depth='advanced')
    saved = db.search_results(watch)
    assert result['status'] == saved['status'] == 'partial'
    assert 'budget' in saved['error']
    assert len(saved['results']) == 1 and fetched == []
    assert result['web_queries'] == 1


def test_known_available_offers_refresh_before_new_catalog_children(setup, monkeypatch):
    db, watch, pages, fetched, _ = setup
    category = ROOT + '/collections/boeotia'
    known = ROOT + '/product/known'
    db.record_web_search(watch, [dict(url=url, title=TITLE, sale_status='available', price='125', currency='GBP',
        sale_checked_at='2026-01-01T00:00:00+00:00') for url in (category, known)])
    pages[category] = catalog(card('/product/one'))
    pages[known] = product()
    pages[ROOT + '/product/one'] = product()
    monkeypatch.setattr('coinwatch.web_search.search_web', lambda *a, **kw: [])
    result = Scanner(db).run(mode='coins', search_id=watch, web_depth='advanced')
    assert result['web_leads'] == 2
    assert fetched[:2] == [category, known]
