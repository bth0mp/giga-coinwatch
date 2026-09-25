from datetime import datetime, timezone
import threading
import pytest

from coinwatch.db import Database
from coinwatch.models import Listing, ScrapeResult
from coinwatch.runtime import schedule_state
from coinwatch.scanner import Scanner


def test_schedule_preserves_london_wall_clock_in_summer_and_winter():
    config = {'timezone':'Europe/London','scan_time':'09:00','last_scheduled_date':''}
    summer = schedule_state(config, datetime(2026,9,25,7,59,tzinfo=timezone.utc))
    assert not summer['due']
    assert summer['next_due'] == '2026-09-25T08:00:00+00:00'
    winter = schedule_state(config, datetime(2026,12,25,8,59,tzinfo=timezone.utc))
    assert not winter['due']
    assert winter['next_due'] == '2026-12-25T09:00:00+00:00'


def test_schedule_coalesces_downtime_and_does_not_repeat_completed_day():
    config = {'timezone':'Europe/London','scan_time':'09:00','last_scheduled_date':'2026-09-20'}
    now = datetime(2026,9,25,11,tzinfo=timezone.utc)
    assert schedule_state(config,now)['due']
    config['last_scheduled_date'] = '2026-09-25'
    result = schedule_state(config,now)
    assert not result['due']
    assert result['next_due'] == '2026-09-26T08:00:00+00:00'


def test_yesterday_missed_is_caught_up_even_before_todays_time():
    config = {'timezone':'Europe/London','scan_time':'09:00','last_scheduled_date':'2026-09-22'}
    result = schedule_state(config,datetime(2026,9,25,6,tzinfo=timezone.utc))
    assert result['due'] and result['occurrence'] == '2026-09-24'


def test_first_scheduled_run_catches_up_from_persisted_due_time():
    config = {'timezone':'Europe/London','scan_time':'09:00','last_scheduled_date':'', 'next_due':'2026-09-24T08:00:00+00:00'}
    result = schedule_state(config,datetime(2026,9,25,6,tzinfo=timezone.utc))
    assert result['due'] and result['occurrence'] == '2026-09-24'


def test_scanner_preserves_catalog_when_one_source_fails(tmp_path):
    db = Database(tmp_path/'catalog.db')
    db.initialize([{'id':s,'name':s,'url':f'https://{s}.example','adapter':'test','enabled':True} for s in ('good','bad')])
    def scrape(source,fetcher):
        if source['id'] == 'bad':
            raise RuntimeError('site blocked')
        return ScrapeResult([Listing('one','https://good.example/one','Roman denarius','30','GBP')],1)
    scanner = Scanner(db, scrape=scrape)
    result = scanner.run(include_discovery=False)
    assert result['status'] == 'partial'
    assert db.list_listings(view='all')[1] == 1
    assert 'site blocked' in db.get_source('bad')['last_error']
    assert db.get_source('good')['baseline_complete']
    scanner.run(include_discovery=False)
    assert db.list_listings(view='all')[1] == 1
    assert db.list_listings(view='new')[1] == 0


def test_scanner_busy_does_not_start_another_run(tmp_path):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    db.claim_run('manual')
    result = Scanner(db).run(include_discovery=False)
    assert result['status'] == 'busy'
    assert len(db.runs()) == 1


@pytest.mark.parametrize('mode,listing_count,candidate_count', [('coins', 1, 0), ('dealers', 0, 1), ('both', 1, 1)])
def test_scan_modes_only_run_selected_work(tmp_path, monkeypatch, mode, listing_count, candidate_count):
    db = Database(tmp_path/'catalog.db')
    db.initialize([dict(id='shop', name='Shop', url='https://shop.example', adapter='test', enabled=True)])
    def scrape(source, fetcher):
        return ScrapeResult([Listing('a', 'https://shop.example/a', 'Athens tetradrachm', '50', 'GBP')], 1)
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [
        dict(url='https://dealer.example/', name='New dealer', reason='Ancient coins with fixed prices')])
    result = Scanner(db, scrape=scrape).run(mode=mode)
    assert result['status'] == 'complete'
    assert result['mode'] == mode
    assert db.list_listings(view='all')[1] == listing_count
    assert len(db.list_candidates()) == candidate_count
    assert db.runs()[0]['kind'] == f'manual-{mode}'
    if mode == 'dealers':
        assert db.get_source('shop')['last_attempt'] is None
        assert 'listings checked' not in result['summary']
    elif mode == 'coins':
        assert 'dealer candidates' not in result['summary']


@pytest.mark.parametrize('options', [{'mode':'invalid'}, {'mode':'dealers', 'include_discovery':False}])
def test_invalid_scan_mode_does_not_claim_a_run(tmp_path, options):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    with pytest.raises(ValueError):
        Scanner(db).run(**options)
    assert db.runs() == []


def test_legacy_no_discovery_records_coin_scan(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    def unexpected_discovery(*args, **kwargs):
        raise AssertionError('Coin-only scan started dealer discovery')
    monkeypatch.setattr('coinwatch.discovery.discover', unexpected_discovery)
    result = Scanner(db).run(include_discovery=False)
    assert result['status'] == 'complete'
    assert result['mode'] == 'coins'
    assert db.runs()[0]['kind'] == 'manual-coins'


@pytest.mark.parametrize('mode', ['coins', 'dealers'])
def test_single_purpose_manual_scan_does_not_consume_daily_combined_occurrence(tmp_path, monkeypatch, mode):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    Runtime(db)._work('manual', mode)
    assert db.runs()[0]['status'] == 'complete'
    assert db.settings()['last_scheduled_date'] == ''


def test_complete_manual_combined_scan_consumes_daily_occurrence(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    Runtime(db)._work('manual', 'both')
    assert db.settings()['last_scheduled_date'] == '2026-09-25'


def test_runtime_exposes_mode_and_rejects_overlapping_scans(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    entered, release = threading.Event(), threading.Event()
    def discover(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Test did not release discovery')
        return []
    monkeypatch.setattr('coinwatch.discovery.discover', discover)
    runtime = Runtime(db)
    try:
        assert runtime.start_scan(mode='dealers')
        assert entered.wait(5)
        assert runtime.snapshot()['mode'] == 'dealers'
        assert not runtime.start_scan(mode='coins')
        assert len(db.runs()) == 1
    finally:
        release.set()
        runtime.stop()
    assert not runtime.snapshot()['running']
    assert runtime.snapshot()['mode'] == ''


def test_runtime_rejects_invalid_mode_before_starting_worker(tmp_path):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    runtime = Runtime(db)
    with pytest.raises(ValueError):
        runtime.start_scan(mode='invalid')
    assert not runtime.snapshot()['running']
    assert db.runs() == []


def test_failed_manual_scan_does_not_consume_daily_occurrence(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    monkeypatch.setattr('coinwatch.runtime.Scanner.run', lambda self,kind,**kwargs: {'status':'failed'})
    Runtime(db)._work('manual')
    assert db.settings()['last_scheduled_date'] == ''


def test_scheduled_failure_is_recorded_without_endless_catchup(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    monkeypatch.setattr('coinwatch.runtime.Scanner.run', lambda self,kind,**kwargs: {'status':'failed'})
    Runtime(db)._work('scheduled')
    assert db.settings()['last_scheduled_date'] == '2026-09-25'


@pytest.mark.parametrize('mode,search_id', [('coins', 123), ('dealers', 123)])
def test_invalid_wanted_search_does_not_create_scan(tmp_path, mode, search_id):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    with pytest.raises(ValueError):
        Scanner(db).run(mode=mode, search_id=search_id)
    assert db.runs() == []


def test_runtime_unknown_wanted_search_does_not_start_worker(tmp_path):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    runtime = Runtime(db)
    with pytest.raises(ValueError):
        runtime.start_scan(mode='coins', search_id=123)
    assert not runtime.snapshot()['running']
    assert db.runs() == []


def test_dealer_mode_uses_tavily_and_directories_without_scanning_coins(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-key')
    indices = []
    def web(fetcher, known, key, **options):
        assert key == 'test-key'
        indices.append(options['query_index'])
        return [dict(url='https://new.example/coins', name='New shop')]
    def directory(fetcher, known, **options):
        assert 'new.example' in known
        return []
    monkeypatch.setattr('coinwatch.discovery.discover_web', web)
    monkeypatch.setattr('coinwatch.discovery.discover', directory)
    result = Scanner(db).run(mode='dealers')
    assert result['status'] == 'complete' and result['candidates'] == 1
    assert 'Tavily' in result['summary'] and result['seen'] == 0
    Scanner(db).run(mode='dealers')
    assert indices == [0, 2]


def test_web_dealer_failure_keeps_directory_results(tmp_path, monkeypatch):
    from coinwatch.discovery import DiscoveryError
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-key')
    def web(*args, **kwargs):
        raise DiscoveryError([], [dict(url='Tavily', error='Usage limit reached')])
    monkeypatch.setattr('coinwatch.discovery.discover_web', web)
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [dict(url='https://new.example/coins', name='New shop')])
    result = Scanner(db).run(mode='dealers')
    assert result['status'] == 'partial' and result['candidates'] == 1
    assert 'Usage limit reached' in result['summary']


def test_dealer_mode_without_key_is_explicit_and_does_not_call_tavily(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: '')
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    def unexpected(*args, **kwargs):
        raise AssertionError('Tavily called without a key')
    monkeypatch.setattr('coinwatch.discovery.discover_web', unexpected)
    result = Scanner(db).run(mode='dealers')
    assert result['status'] == 'complete' and 'directory only' in result['summary']


def test_late_dealer_results_cannot_write_after_lease_revocation(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-key')
    def web(*args, **kwargs):
        db.finish_run(scanner.run_id, 'interrupted', 'Stopped')
        return [dict(url='https://new.example/coins', name='New shop')]
    monkeypatch.setattr('coinwatch.discovery.discover_web', web)
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    scanner = Scanner(db)
    result = scanner.run(mode='dealers')
    assert result['status'] == 'interrupted' and db.list_candidates() == []


def wanted_search(db, name='Athens owl', **values):
    fields = dict(name=name, keywords='Athens', include_web=True, enabled=True)
    fields.update(values)
    return db.save_search(fields)


def test_wanted_scan_queries_only_selected_watch_and_preserves_catalog(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([dict(id='shop', name='Shop', url='https://shop.example', adapter='test', enabled=True)])
    selected = wanted_search(db)
    other = wanted_search(db, name='Corinth')
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    def search(query, key, *, max_results):
        assert max_results == 20
        return [dict(url='https://example.com/owl', title=query, snippet='Silver owl')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    def scrape(source, fetcher):
        return ScrapeResult([
            Listing('a', 'https://shop.example/a', 'Athens tetradrachm', '50', 'GBP'),
            Listing('b', 'https://shop.example/b', 'Corinth stater', '80', 'GBP')], 1)
    result = Scanner(db, scrape=scrape).run(mode='coins', search_id=selected)
    assert result['status'] == 'complete'
    assert result['search_id'] == selected
    assert result['matches'] == 1
    assert db.list_listings(view='all')[1] == 2
    assert db.search_results(selected)['results'][0]['title'] == db.get_search(selected)['web_query']
    assert db.search_results(other)['status'] == 'idle'


def test_wanted_web_scan_requires_both_watch_opt_in_and_key(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: '')
    def unexpected_search(*args, **kwargs):
        raise AssertionError('Web provider was called without configured credentials')
    monkeypatch.setattr('coinwatch.web_search.search_web', unexpected_search)
    result = Scanner(db).run(mode='coins')
    assert result['status'] == 'complete'
    assert 'configur' in result['summary'].lower()
    assert db.search_results(watch)['status'] == 'idle'
    assert Runtime(db).search_results(watch)['status'] == 'unconfigured'
    db.save_search(dict(db.get_search(watch), include_web=False), search_id=watch)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    result = Scanner(db).run(mode='coins', search_id=watch)
    assert result['status'] == 'complete'
    assert db.search_results(watch)['status'] == 'idle'


def test_web_queries_rotate_oldest_first_with_five_attempt_limit_and_failure_isolation(tmp_path, monkeypatch):
    from coinwatch.web_search import WebSearchError
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watches = [wanted_search(db, name=f'Watch {i}', keywords=f'Mint {i}') for i in range(6)]
    disabled = wanted_search(db, name='Paused watch')
    db.save_search(dict(db.get_search(disabled), enabled=False), search_id=disabled)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    attempts = 0
    def search(query, key, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise WebSearchError('Search quota exhausted. Check provider limits.')
        return [dict(url='https://example.com/coin', title='Coin lead', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    first = Scanner(db).run('scheduled', mode='both')
    assert first['status'] == 'partial'
    states = [db.search_results(watch) for watch in watches]
    assert sum(bool(state['checked_at']) for state in states) == 5
    assert sum(state['status'] == 'error' for state in states) == 1
    assert sum(state['status'] == 'complete' for state in states) == 4
    assert db.search_results(disabled)['status'] == 'idle'
    unqueried = next(watch for watch, state in zip(watches, states) if state['status'] == 'idle')
    next_queries = []
    def search_again(query, key, **kwargs):
        next_queries.append(query)
        return []
    monkeypatch.setattr('coinwatch.web_search.search_web', search_again)
    Scanner(db).run(mode='coins')
    assert next_queries[0] == db.get_search(unqueried)['web_query']
    assert db.search_results(unqueried)['status'] == 'complete'
    assert db.search_results(disabled)['status'] == 'idle'


def test_dealer_scan_never_queries_wanted_web_searches(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    queries = []
    def dealer_search(query, *args, **kwargs):
        assert query != db.get_search(watch)['web_query']
        queries.append(query)
        return []
    monkeypatch.setattr('coinwatch.web_search.search_web', dealer_search)
    assert Scanner(db).run(mode='dealers')['status'] == 'complete'
    assert len(queries) == 2
    assert db.search_results(watch)['status'] == 'idle'


def test_combined_scan_for_one_watch_does_not_fulfill_daily_scan(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: '')
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    Runtime(db)._work('manual', 'both', watch)
    assert db.runs()[0]['status'] == 'complete'
    assert db.settings()['last_scheduled_date'] == ''


def test_unexpected_provider_error_does_not_persist_secrets(tmp_path, monkeypatch, caplog):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    def broken_search(query, key, **kwargs):
        raise RuntimeError('transport failed with key=' + key)
    monkeypatch.setattr('coinwatch.web_search.search_web', broken_search)
    result = Scanner(db).run(mode='coins')
    assert result['status'] == 'partial'
    assert db.search_results(watch)['status'] == 'error'
    assert 'test-secret' not in str(result)
    assert 'test-secret' not in str(db.search_results(watch))
    assert 'test-secret' not in caplog.text


def test_runtime_tracks_selected_watch_without_exposing_web_results_in_snapshot(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    def search(query, key, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError('Test did not release search')
        return [dict(url='https://example.com/owl', title='Owl coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    runtime = Runtime(db)
    try:
        assert runtime.start_scan(mode='coins', search_id=watch, web_queries=3)
        assert entered.wait(5)
        snapshot = runtime.snapshot()
        assert snapshot['search_id'] == watch
        assert snapshot['web_queries'] == 3
        assert 'results' not in snapshot
        assert not runtime.start_scan(mode='both')
    finally:
        release.set()
        if runtime._worker:
            runtime._worker.join(timeout=5)
        runtime.stop()
    assert runtime.snapshot()['search_id'] is None
    assert runtime.snapshot()['web_queries'] == 0
    assert runtime.search_results(watch)['results'][0]['title'] == 'Owl coin'
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: '')
    assert runtime.search_results(watch)['status'] == 'complete'


@pytest.mark.parametrize('change', ['edit', 'delete', 'disable_web'])
def test_wanted_changes_during_web_request_discard_stale_results_and_continue(tmp_path, monkeypatch, change):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    changed = wanted_search(db)
    other = wanted_search(db, name='Corinth', keywords='Corinth')
    old_query = db.get_search(changed)['web_query']
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    def search(query, key, **kwargs):
        if query == old_query:
            if change == 'delete':
                db.delete_search(changed)
            else:
                values = db.get_search(changed)
                values.update(keywords='Alexandria') if change == 'edit' else values.update(include_web=False)
                db.save_search(values, search_id=changed)
        return [dict(url='https://example.com/coin', title=query, snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins')
    assert result['status'] == 'complete'
    assert db.search_results(other)['status'] == 'complete'
    if change != 'delete':
        assert db.search_results(changed)['status'] == 'idle'
        assert db.search_results(changed)['results'] == []


def test_selected_watch_deleted_during_scan_completes_without_stale_match_count(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    def search(query, key, **kwargs):
        db.delete_search(watch)
        return []
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch)
    assert result['status'] == 'complete'
    assert result['matches'] is None


@pytest.mark.parametrize('provider_error', [False, True])
def test_lost_scan_lease_discards_late_web_response_and_stops_later_queries(tmp_path, monkeypatch, provider_error):
    from coinwatch.web_search import WebSearchError
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    first = wanted_search(db)
    second = wanted_search(db, name='Corinth', keywords='Corinth')
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    queries = []
    def search(query, key, **kwargs):
        queries.append(query)
        db.finish_run(db.runs()[0]['id'], 'interrupted', 'Application stopped')
        if provider_error:
            raise WebSearchError('Provider unavailable')
        return [dict(url='https://example.com/coin', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins')
    assert result['status'] == 'interrupted'
    assert len(queries) == 1
    assert db.search_results(first)['status'] == 'idle'
    assert db.search_results(second)['status'] == 'idle'


def test_stop_during_web_request_does_not_store_late_response(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    stop = threading.Event()
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    def search(query, key, **kwargs):
        stop.set()
        return [dict(url='https://example.com/coin', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db, stop_event=stop).run(mode='coins')
    assert result['status'] == 'interrupted'
    assert db.search_results(watch)['status'] == 'idle'


@pytest.mark.parametrize('count', [0, 51, -1, True, 1.5, '2'])
def test_web_query_budget_must_be_an_integer_between_one_and_fifty(tmp_path, count):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    with pytest.raises(ValueError):
        Scanner(db).run(mode='coins', search_id=watch, web_queries=count)
    runtime = Runtime(db)
    with pytest.raises(ValueError):
        runtime.start_scan(mode='coins', search_id=watch, web_queries=count)
    assert not runtime.snapshot()['running']
    assert db.runs() == []


@pytest.mark.parametrize('options,include_web', [({}, True), ({'kind':'scheduled'}, True), ({}, False)])
def test_multiple_web_queries_require_a_manual_selected_opted_in_watch(tmp_path, options, include_web):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db, include_web=include_web)
    options = dict(options)
    if options or not include_web:
        options['search_id'] = watch
    with pytest.raises(ValueError):
        Scanner(db).run(mode='coins', web_queries=2, **options)
    assert db.runs() == []


def test_broad_scan_uses_distinct_queries_deduplicates_and_preserves_earlier_leads(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    db.record_web_search(watch, [dict(url='https://old.example/coin', title='Earlier coin', snippet='Earlier lead')])
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    queries = []
    def search(query, key, **options):
        queries.append(query)
        assert options['max_results'] == 20
        assert not options.get('exclude_domains')
        return [dict(url='https://new.example/shared', title='Shared coin', snippet='Potential match'),
                dict(url=f'https://new.example/coin-{len(queries)}', title='Another coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=3)
    assert result['status'] == 'complete'
    assert len(set(queries)) == 3
    assert result['web_queries'] == 3
    assert result['web_leads'] == 4
    assert len(db.search_results(watch)['results']) == 5
    assert '3 searches attempted' in db.runs()[0]['summary']
    # The normal next-day request must keep the broad scan's accumulated leads.
    monkeypatch.setattr('coinwatch.web_search.search_web', lambda *args, **kwargs: [])
    Scanner(db).run(mode='coins')
    assert len(db.search_results(watch)['results']) == 5


def test_broad_scan_diversifies_after_five_requests_using_only_returned_hosts(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([dict(id='known', name='Known', url='https://registered.example', adapter='test', enabled=False)])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    requests = []
    def search(query, key, **options):
        requests.append(options)
        return [dict(url=f'https://dealer{len(requests)}.example/coin', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=7)
    assert result['web_queries'] == 7
    assert all(not call.get('exclude_domains') for call in requests[:5])
    assert set(requests[5]['exclude_domains']) == {f'dealer{i}.example' for i in range(1, 6)}
    assert 'registered.example' not in requests[6]['exclude_domains']
    assert len(db.search_results(watch)['results']) == 7


def test_broad_scan_keeps_partial_results_and_stops_on_provider_error(tmp_path, monkeypatch):
    from coinwatch.web_search import WebSearchError
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    attempts = []
    def search(query, key, **kwargs):
        attempts.append(query)
        if len(attempts) == 2:
            raise WebSearchError('The Tavily plan usage limit has been reached.')
        return [dict(url='https://dealer.example/coin', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=10)
    assert result['status'] == 'partial'
    assert result['web_queries'] == len(attempts) == 2
    stored = db.search_results(watch)
    assert stored['status'] == 'partial'
    assert len(stored['results']) == 1
    assert 'usage limit' in stored['error']


def test_broad_scan_checks_for_edits_between_requests(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    attempted = []
    def search(query, key, **kwargs):
        attempted.append(query)
        if len(attempted) == 2:
            db.save_search(dict(db.get_search(watch), keywords='Alexandria'), search_id=watch)
        return [dict(url=f'https://dealer.example/coin-{len(attempted)}', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=5)
    assert result['status'] == 'complete'
    assert result['web_queries'] == len(attempted) == 2
    assert db.search_results(watch)['results'] == []
    assert db.search_results(watch)['status'] == 'idle'


def test_broad_scan_keeps_completed_queries_when_later_request_is_cancelled(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    stop = threading.Event()
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    attempted = []
    def search(query, key, **kwargs):
        attempted.append(query)
        if len(attempted) == 2:
            stop.set()
        return [dict(url=f'https://dealer.example/coin-{len(attempted)}', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db, stop_event=stop).run(mode='coins', search_id=watch, web_queries=5)
    assert result['status'] == 'interrupted'
    assert result['web_queries'] == len(attempted) == 2
    stored = db.search_results(watch)['results']
    assert [row['url'] for row in stored] == ['https://dealer.example/coin-1']


def test_broad_scan_accepts_fifty_queries_and_caps_domain_exclusions(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    requests = []
    def search(query, key, **options):
        requests.append(options)
        return [dict(url=f'https://dealer{len(requests)}-{i}.example/coin', title='Coin', snippet='Potential match') for i in range(20)]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=50)
    assert result['status'] == 'complete'
    assert result['web_queries'] == len(requests) == 50
    assert result['web_leads'] == 1000
    assert len(db.search_results(watch)['results']) == 1000
    assert all(len(request.get('exclude_domains', [])) <= 150 for request in requests)
    assert len(requests[-1]['exclude_domains']) == 150


def test_broad_scan_isolates_unexpected_query_errors_without_losing_successes(tmp_path, monkeypatch):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    watch = wanted_search(db)
    monkeypatch.setattr('coinwatch.web_search.load_api_key', lambda _: 'test-secret')
    requests = []
    def search(query, key, **kwargs):
        requests.append(query)
        if len(requests) == 2:
            raise RuntimeError('Internal details: ' + key)
        return [dict(url=f'https://dealer.example/coin-{len(requests)}', title='Coin', snippet='Potential match')]
    monkeypatch.setattr('coinwatch.web_search.search_web', search)
    result = Scanner(db).run(mode='coins', search_id=watch, web_queries=3)
    assert result['status'] == 'partial'
    assert result['web_queries'] == 3
    stored = db.search_results(watch)
    assert stored['status'] == 'partial'
    assert len(stored['results']) == 2
    assert 'test-secret' not in str(stored)
    assert 'test-secret' not in str(result)
