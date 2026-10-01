from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from coinwatch.db import Database
from coinwatch.runtime import Runtime
from coinwatch.web import create_app

from test_web import setup_catalog


def findings(tmp_path, count=35):
    client, db, runtime = setup_catalog(tmp_path)
    watch = db.save_search(dict(name='Boeotia', keywords='Boeotia', include_web=True))
    rows = [dict(title=f'Boeotia coin {i:03}', url=f'https://dealer.example/product/{i}',
                 verification='unverified', verification_label='Unverified',
                 verification_note='Seller denied automated access (HTTP 403).',
                 sale_status='unverified', price='999', currency='EUR',
                 sale_checked_at='2026-10-01T12:00:00+00:00') for i in range(count)]
    verified = dict(rows[0], title='Verified Boeotia coin', verification='verified',
                    verification_label='Verified for sale', verification_note='',
                    sale_status='available', price='125', currency='GBP')
    expired = dict(rows[1], title='Older Boeotia coin', verification='expired',
                   verification_label='Needs recheck', verification_note='Availability check is older than 24 hours.',
                   sale_status='available', price='80', currency='EUR')
    rows[:2] = [verified, expired]
    runtime.web_results = dict(status='complete', error='', checked_at='2026-10-01T12:00:00+00:00',
                               results=[verified], potential_results=rows)
    return client, watch


def test_default_shows_potential_listings_with_verification_labels_and_no_unverified_prices(tmp_path):
    client, watch = findings(tmp_path)
    page = BeautifulSoup(client.get(f'/wanted/{watch}').text, 'html.parser')
    cards = page.select('.web-lead')
    assert len(cards) == 30
    assert page.select_one('nav[aria-label="Web result filters"] a[aria-current="page"]').get_text(' ', strip=True).startswith('All potential')
    assert 'Verified for sale' in cards[0].get_text(' ', strip=True)
    assert 'GBP 125' in cards[0].get_text(' ', strip=True)
    assert 'Needs recheck' in cards[1].get_text(' ', strip=True)
    assert 'Last known price: EUR 80' in cards[1].get_text(' ', strip=True)
    assert 'Unverified' in cards[2].get_text(' ', strip=True)
    assert 'HTTP 403' in cards[2].get_text(' ', strip=True)
    assert 'EUR 999' not in page.get_text()
    assert 'Showing 1–30 of 35 potential listings' in page.get_text(' ', strip=True)


def test_every_finding_is_reachable_without_losing_selected_filters(tmp_path):
    client, watch = findings(tmp_path)
    page = BeautifulSoup(client.get(f'/wanted/{watch}?page=2&web_view=unverified').text, 'html.parser')
    nav = page.select_one('nav[aria-label="Web result pages"]')
    link = nav.find('a', string='Next findings')
    assert 'page=2&' in link['href'] and 'web_view=unverified' in link['href'] and 'web_page=2' in link['href']
    second = BeautifulSoup(client.get(link['href']).text, 'html.parser')
    assert len(second.select('.web-lead')) == 3
    assert 'Boeotia coin 034' in second.get_text()
    assert 'Verified Boeotia coin' not in second.get_text()
    assert 'Older Boeotia coin' not in second.get_text()
    previous = second.select_one('nav[aria-label="Web result pages"]').find('a', string='Previous findings')
    assert 'web_view=unverified' in previous['href'] and 'web_page=1' in previous['href']


def test_verified_and_expired_filters_do_not_mix_availability_claims(tmp_path):
    client, watch = findings(tmp_path)
    for view, title in [('verified', 'Verified Boeotia coin'), ('expired', 'Older Boeotia coin')]:
        page = BeautifulSoup(client.get(f'/wanted/{watch}?web_view={view}').text, 'html.parser')
        cards = page.select('.web-lead')
        assert len(cards) == 1 and title in cards[0].get_text()
    assert client.get(f'/wanted/{watch}?web_view=unknown').status_code == 400
    assert client.get(f'/wanted/{watch}?web_page=0').status_code == 400


def test_potential_titles_and_check_reasons_are_escaped(tmp_path):
    client, db, runtime = setup_catalog(tmp_path)
    watch = db.save_search(dict(keywords='Boeotia', include_web=True))
    runtime.web_results.update(potential_results=[dict(title='Boeotia <script>bad()</script>',
        url='https://dealer.example/coin', sale_status='unverified', verification='unverified',
        verification_label='Unverified', verification_note='<img src=x onerror=bad()>',
        sale_checked_at='')])
    text = client.get(f'/wanted/{watch}').text
    assert 'Boeotia &lt;script&gt;bad()&lt;/script&gt;' in text
    assert '&lt;img src=x onerror=bad()&gt;' in text
    assert '<script>bad()' not in text


def test_saved_candidates_flow_through_real_runtime_into_status_filters(tmp_path):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([])
    watch = db.save_search(dict(name='Boeotia', keywords='Boeotia', include_web=False))
    recent = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    defaults = dict(title='Boeotia silver stater 400 BC', sale_status='unverified',
                    sale_reason='Seller denied automated access (HTTP 403).', sale_checked_at=recent)
    db.record_web_search(watch, [dict(defaults, **row) for row in [
        dict(url='https://dealer.example/coin/available', sale_status='available', price='90', currency='GBP'),
        dict(url='https://dealer.example/coin/blocked'),
        dict(url='https://dealer.example/coin/old', sale_status='available', price='85', currency='GBP', sale_checked_at=old),
        dict(url='https://dealer.example/coin/sold', sale_status='rejected', sale_reason='Sold out'),
        dict(url='https://dealer.example/blog/boeotia'),
        dict(url='https://dealer.example/auctions/lot-1'),
        dict(url='https://dealer.example/coin/unrelated', title='Hadrian Roman denarius'),
    ]])
    # No lifespan context: the real read path is exercised without starting scans.
    client = TestClient(create_app(db, Runtime(db)))
    page = BeautifulSoup(client.get(f'/wanted/{watch}').text, 'html.parser')
    cards = page.select('.web-lead')
    assert len(cards) == 3
    assert [card.select_one('.verification-label').get_text() for card in cards] == [
        'Verified for sale', 'Unverified', 'Needs recheck']
    assert {card.select_one('h3 a')['href'] for card in cards} == {
        'https://dealer.example/coin/available', 'https://dealer.example/coin/blocked',
        'https://dealer.example/coin/old'}
    blocked = BeautifulSoup(client.get(f'/wanted/{watch}?web_view=unverified').text, 'html.parser').select('.web-lead')
    assert len(blocked) == 1
    assert 'Price and availability unconfirmed' in blocked[0].get_text()
    assert 'HTTP 403' in blocked[0].get_text()
    assert len(db.search_results(watch)['results']) == 7
