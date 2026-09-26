from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from coinwatch.db import Database
from coinwatch.runtime import Runtime
from coinwatch.web import create_app


def saved_results(tmp_path):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([])
    watch = db.save_search(dict(keywords='Hadrian', category='Roman', currency='GBP', max_price='100'))
    checked = datetime.now(timezone.utc).isoformat(timespec='seconds')
    good = dict(url='https://shop.example/good', title='Roman Hadrian denarius',
                sale_status='available', price='90', currency='GBP', sale_checked_at=checked)
    db.record_web_search(watch, [good,
        dict(good, url='https://shop.example/expensive', price='101'),
        dict(good, url='https://shop.example/currency', currency='EUR'),
        dict(good, url='https://shop.example/unrelated', title='Greek Athens owl tetradrachm'),
        dict(good, url='https://shop.example/expired',
             sale_checked_at=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()),
        dict(good, url='https://shop.example/blocked', sale_status='unverified',
             sale_reason='Seller refused automated access (HTTP 403).'),
        dict(good, url='https://shop.example/article', sale_status='rejected',
             title='Hidden reference article', sale_reason='Information page <script>bad()</script>')])
    return db, watch


def test_legacy_web_results_obey_all_search_criteria_without_changing_saved_rows(tmp_path):
    db, watch = saved_results(tmp_path)
    before = db.search_results(watch)
    results = Runtime(db).search_results(watch)
    assert [row['url'] for row in results['results']] == ['https://shop.example/good']
    assert db.search_results(watch) == before


def test_hidden_results_summary_distinguishes_criteria_expiry_and_failed_checks(tmp_path):
    db, watch = saved_results(tmp_path)
    results = Runtime(db).search_results(watch)
    assert results['candidate_count'] == 7
    summary = {(row['status'], row['reason']): row['count'] for row in results['check_summary']}
    assert sum(summary.values()) == 6
    assert summary[('Not matching', 'The listing does not match this saved search.')] == 3
    assert summary[('Expired', 'Availability needs a new check.')] == 1
    assert summary[('Unverified', 'Seller refused automated access (HTTP 403).')] == 1
    response = TestClient(create_app(db, Runtime(db))).get(f'/wanted/{watch}')
    assert response.status_code == 200
    assert 'Why results are hidden' in response.text
    assert 'Across all 7 stored candidate links' in response.text
    assert 'Seller refused automated access (HTTP 403).' in response.text
    assert '&lt;script&gt;bad()&lt;/script&gt;' in response.text
    assert '<script>bad()</script>' not in response.text
    assert 'Hidden reference article' not in response.text


@pytest.mark.parametrize('change', [{'max_price': '80'}, {'currency': 'EUR'}])
def test_changed_price_or_currency_rejects_inflight_results_atomically(tmp_path, change):
    db, watch = saved_results(tmp_path)
    search = db.get_search(watch)
    rows = db.search_results(watch)['results']
    db.save_search(dict(search, **change), watch)
    assert db.get_search(watch)['web_query'] == search['web_query']
    assert not db.record_web_search(watch, rows, expected_query=search['web_query'], expected_search=search)
    assert db.search_results(watch)['results'] == []


def test_renaming_search_does_not_discard_inflight_matching_results(tmp_path):
    db, watch = saved_results(tmp_path)
    search = db.get_search(watch)
    rows = db.search_results(watch)['results']
    db.save_search(dict(search, name='Renamed'), watch)
    assert db.record_web_search(watch, rows, expected_query=search['web_query'], expected_search=search)
