from datetime import datetime, timedelta, timezone
import time

import pytest

from coinwatch.db import Database
from coinwatch.fetch import Page
from coinwatch.scanner import Scanner


DURABLE_FAILURES = [
    'Seller returned HTTP 401; check the listing in your browser.',
    'Seller denied automated access (HTTP 403); open the listing in your browser.',
    'Seller access was blocked (HTTP 403); availability is unverified.',
    'Seller robots.txt disallows automated checks; open the listing in your browser.',
    'Seller requires a browser challenge; open the listing in your browser.',
    'This dealer requires permission or login before automated checks.',
    'A single primary product could not be identified.',
    'The page does not establish an individual product identity.',
    'The page contains separate product cards rather than one clear purchase scope.',
]


@pytest.fixture
def retry_scan(tmp_path, monkeypatch):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([])
    watch = db.save_search(dict(name='Boeotia', keywords='Boeotia', include_web=True))
    fetched = []
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: '')
    monkeypatch.setattr('coinwatch.web_search.search_web',
                        lambda *a, **kw: pytest.fail('Retrying stored candidates must not require a paid query'))

    class FakeFetcher:
        def __init__(self, stop_event=None, budget=900):
            self.deadline = time.monotonic() + budget

        def get(self, url):
            fetched.append(url)
            return Page(url, '<main><article class="product"><h1>Greek Boeotia silver stater 400 BC</h1>'
                        '<span class="price">£125</span><p class="stock">In stock</p>'
                        '<button>Add to cart</button></article></main>')

    monkeypatch.setattr('coinwatch.scanner.Fetcher', FakeFetcher)

    def run(reason, days, depth='basic'):
        row = dict(url='https://dealer.example/product/coin', title='Greek Boeotia silver stater 400 BC',
                   sale_status='unverified', sale_reason=reason,
                   sale_checked_at=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
        db.record_web_search(watch, [row])
        before = db.search_results(watch)['results']
        result = Scanner(db).run(mode='coins', search_id=watch, web_depth=depth)
        return result, before, db.search_results(watch)['results'], fetched

    return run


@pytest.mark.parametrize('reason', DURABLE_FAILURES)
def test_durable_failure_waits_a_week_without_refreshing_stored_evidence(retry_scan, reason):
    result, before, after, fetched = retry_scan(reason, days=6)
    assert fetched == []
    assert after == before
    assert result['web_queries'] == result['web_leads'] == 0
    assert 'cooldown' in result['summary']


@pytest.mark.parametrize('reason', DURABLE_FAILURES)
def test_durable_failure_can_be_retried_after_a_week(retry_scan, reason):
    result, _, after, fetched = retry_scan(reason, days=8)
    assert fetched == [after[0]['url']]
    assert result['web_queries'] == 0 and result['web_leads'] == 1
    assert after[0]['sale_status'] == 'available'


@pytest.mark.parametrize('reason', [
    'Seller page timed out; retry later.',
    'Seller server error (HTTP 503); retry later.',
    'Seller robots.txt could not be verified; retry later.',
    'Seller rate limit reached (HTTP 429); retry later.',
])
def test_transient_failure_can_be_retried_the_next_day(retry_scan, reason):
    result, _, after, fetched = retry_scan(reason, days=2)
    assert fetched == [after[0]['url']]
    assert result['web_leads'] == 1


@pytest.mark.parametrize('reason', [
    'Web-check time limit reached; increase the time limit or run another scan.',
    'Sale check was cancelled; run another scan to retry.',
])
def test_unfinished_check_can_be_retried_immediately(retry_scan, reason):
    result, _, after, fetched = retry_scan(reason, days=0)
    assert fetched == [after[0]['url']]
    assert result['web_leads'] == 1


def test_deliberate_deep_run_still_retries_readable_identity_failures(retry_scan):
    result, _, after, fetched = retry_scan('A single primary product could not be identified.',
                                          days=2, depth='advanced')
    assert fetched == [after[0]['url']]
    assert result['web_queries'] == 0 and result['web_leads'] == 1


def test_deliberate_deep_run_keeps_durable_access_cooldown(retry_scan):
    result, before, after, fetched = retry_scan(DURABLE_FAILURES[1], days=2, depth='advanced')
    assert fetched == [] and after == before
    assert result['web_queries'] == result['web_leads'] == 0
