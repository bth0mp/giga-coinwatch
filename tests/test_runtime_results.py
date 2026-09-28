from datetime import datetime, timedelta, timezone

import pytest

from coinwatch.db import Database
from coinwatch.runtime import Runtime


def store(tmp_path, rows):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([])
    watch = db.save_search({'keywords': 'Boeotia', 'category': 'Greek', 'include_web': False})
    now = datetime.now(timezone.utc)
    good = dict(title='Boeotia AR hemidrachm 400 BC', sale_status='available',
                price='85.00', currency='EUR', sale_checked_at=now.isoformat())
    db.record_web_search(watch, [dict(good, **row) for row in rows])
    return db, watch


@pytest.mark.parametrize('reverse', [False, True])
def test_www_aliases_show_latest_matching_sale_without_changing_stored_history(tmp_path, reverse):
    earlier = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    rows = [dict(url='https://www.dealer.example/item.php?id=123', price='90.00', sale_checked_at=earlier),
            dict(url='https://dealer.example/item.php?id=123', price='85.00')]
    db, watch = store(tmp_path, list(reversed(rows)) if reverse else rows)
    before = db.search_results(watch)
    result = Runtime(db).search_results(watch)
    assert [row['url'] for row in result['results']] == ['https://dealer.example/item.php?id=123']
    assert result['results'][0]['price'] == '85.00'
    assert result['candidate_count'] == 2
    assert [(row['status'], row['count']) for row in result['check_summary']] == [('Duplicate', 1)]
    assert db.search_results(watch) == before


def test_only_url_equivalence_deduplicates_distinct_queries_paths_and_schemes_survive(tmp_path):
    rows = [dict(url='https://www.dealer.example:443/item.php?id=123'),
            dict(url='https://dealer.example/item.php?id=123'),
            dict(url='https://dealer.example/item.php?id=124'),
            dict(url='https://dealer.example/other.php?id=123'),
            dict(url='http://dealer.example/item.php?id=123')]
    db, watch = store(tmp_path, rows)
    result = Runtime(db).search_results(watch)
    assert len(result['results']) == 4
    assert {row['url'] for row in result['results']} >= {
        'https://dealer.example/item.php?id=124', 'https://dealer.example/other.php?id=123',
        'http://dealer.example/item.php?id=123'}
    assert result['candidate_count'] == 5


def test_expired_alias_of_visible_offer_is_duplicate_but_separate_expired_offer_is_expired(tmp_path):
    old = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    db, watch = store(tmp_path, [dict(url='https://www.dealer.example/coin', sale_checked_at=old),
                               dict(url='https://dealer.example/coin'),
                               dict(url='https://dealer.example/other-coin', sale_checked_at=old)])
    result = Runtime(db).search_results(watch)
    assert len(result['results']) == 1
    assert {row['status']: row['count'] for row in result['check_summary']} == {'Duplicate': 1, 'Expired': 1}


def test_newer_nonmatching_alias_suppresses_old_matching_offer(tmp_path):
    earlier = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    db, watch = store(tmp_path, [dict(url='https://www.dealer.example/coin', sale_checked_at=earlier),
                               dict(url='https://dealer.example/coin', title='Roman Hadrian denarius')])
    result = Runtime(db).search_results(watch)
    assert result['results'] == []
    assert {row['status']: row['count'] for row in result['check_summary']} == {'Duplicate': 1, 'Not matching': 1}


@pytest.mark.parametrize('sale_status', ['rejected', 'unverified'])
@pytest.mark.parametrize('tie', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_latest_completed_alias_check_suppresses_older_available_or_tied_available(tmp_path, sale_status, tie, reverse):
    old = datetime.now(timezone.utc)-timedelta(hours=1)
    recent = old if tie else old+timedelta(minutes=30)
    rows = [dict(url='https://www.dealer.example/coin', sale_checked_at=old.isoformat()),
            dict(url='https://dealer.example/coin', sale_checked_at=recent.isoformat(),
                 sale_status=sale_status, sale_reason='Sold out' if sale_status == 'rejected' else 'Availability could not be confirmed')]
    db, watch = store(tmp_path, list(reversed(rows)) if reverse else rows)
    before = db.search_results(watch)
    result = Runtime(db).search_results(watch)
    assert result['results'] == []
    assert {row['status']: row['count'] for row in result['check_summary']} == {'Duplicate': 1, sale_status.capitalize(): 1}
    assert db.search_results(watch) == before


def test_pending_alias_without_completed_check_does_not_hide_verified_offer(tmp_path):
    old = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    db, watch = store(tmp_path, [dict(url='https://www.dealer.example/coin', sale_checked_at=old),
                               dict(url='https://dealer.example/coin', sale_status='unverified', sale_checked_at='')])
    result = Runtime(db).search_results(watch)
    assert [row['url'] for row in result['results']] == ['https://www.dealer.example/coin']
    assert {row['status']: row['count'] for row in result['check_summary']} == {'Duplicate': 1}
