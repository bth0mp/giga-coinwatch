import pytest

from coinwatch.db import Database
from coinwatch.fetch import FetchError
from coinwatch.models import Listing


def test_only_recent_available_web_sales_are_visible_and_sold_rechecks_hide_them(db):
    from datetime import datetime, timedelta, timezone
    from coinwatch.runtime import Runtime
    now = datetime.now(timezone.utc)
    watch = db.save_search({'keywords': 'owl', 'include_web': False})
    def sale(url, status='available', checked=now):
        return dict(url=url, title='Greek Athens owl tetradrachm', sale_status=status, price='120.00', currency='EUR',
                    sale_checked_at=checked.isoformat(), sale_reason='Product page checked')
    db.record_web_search(watch, [sale('https://shop.example/current'),
        sale('https://shop.example/old', checked=now-timedelta(days=2)),
        sale('https://shop.example/sold', status='rejected'),
        dict(url='https://reference.example/article', title='An article')])
    assert len(db.search_results(watch)['results']) == 4
    assert [r['url'] for r in Runtime(db).search_results(watch)['results']] == ['https://shop.example/current']
    db.record_web_search(watch, [sale('https://shop.example/current', status='rejected')], merge=True)
    assert Runtime(db).search_results(watch)['results'] == []


def test_legacy_available_noncoin_titles_are_hidden_without_refreshing_sale_checks(db):
    from datetime import datetime, timezone
    from coinwatch.runtime import Runtime
    watch = db.save_search({'keywords': 'owl', 'include_web': True})
    checked = datetime.now(timezone.utc).isoformat(timespec='seconds')
    titles = ['Greek Athens owl tetradrachm', 'Monedas antiguas España',
              'TARJETA POSTAL NUMISMATICA MONEDAS ANTIGUAS DE HISPANIA']
    db.record_web_search(watch, [dict(url=f'https://shop.example/{i}', title=title,
        sale_status='available', price='120', currency='EUR', sale_checked_at=checked)
        for i, title in enumerate(titles)])
    original = db.search_results(watch)['results']
    visible = Runtime(db).search_results(watch)['results']
    assert [row['title'] for row in visible] == [titles[0]]
    assert visible[0]['sale_checked_at'] == checked
    assert db.search_results(watch)['results'] == original


def test_web_sale_merge_does_not_refresh_old_verification_and_rejects_invalid_price(db):
    from datetime import datetime, timedelta, timezone
    checked = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    watch = db.save_search({'keywords': 'owl'})
    old = dict(url='https://shop.example/old', title='Owl', sale_status='available', price='120',
               currency='GBP', sale_checked_at=checked)
    db.record_web_search(watch, [old])
    db.record_web_search(watch, [dict(url='https://shop.example/new', title='Unchecked')], merge=True)
    assert db.search_results(watch, verified_only=True)['results'] == []
    for price in ('NaN', '0', '-1', 'free'):
        with pytest.raises(ValueError):
            db.record_web_search(watch, [{**old, 'price': price}])


def test_existing_web_results_migrate_as_unverified_without_data_loss(tmp_path):
    import sqlite3
    path = tmp_path / 'old.db'
    old = Database(path)
    old.initialize([])
    watch = old.save_search({'keywords': 'owl'})
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE search_web_results')
        connection.execute('CREATE TABLE search_web_results (search_id INTEGER, url TEXT, title TEXT, snippet TEXT, first_seen TEXT, last_seen TEXT, position INTEGER, PRIMARY KEY(search_id,url))')
        connection.execute("INSERT INTO search_web_results VALUES(?, 'https://shop.example/coin', 'Owl', 'Old lead', '2026-01-01', '2026-01-01', 0)", (watch,))
    old.initialize([])
    old.initialize([])
    rows = old.search_results(watch)['results']
    assert len(rows) == 1 and rows[0]['sale_status'] == 'unverified'
    assert old.search_results(watch, verified_only=True)['results'] == []


@pytest.fixture
def db(tmp_path):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([dict(id='shop', name='Shop', url='https://shop.example', adapter='test', enabled=True)])
    run = db.claim_run('manual')
    items = [
        Listing('1', 'https://shop.example/1', 'Hadrian silver denarius, Róme mint', '49.95', 'GBP', category='Roman'),
        Listing('2', 'https://shop.example/2', 'Hadrian denarius Rome plated', '20', 'GBP', category='Roman'),
        Listing('3', 'https://shop.example/3', 'Hadrian denarius Antioch', '30', 'GBP', category='Roman'),
        Listing('4', 'https://shop.example/4', 'Hadrian denarius Rome', '60', 'GBP', category='Roman'),
        Listing('5', 'https://shop.example/5', 'Hadrian denarius Rome', '25', 'EUR', category='Roman'),
        Listing('6', 'https://shop.example/6', 'Hadrian denarius Rome', '25', 'GBP', category='Roman', availability='sold'),
        Listing('7', 'https://shop.example/7', 'Athens silver tetradrachm owl', '99', 'GBP', category='Greek'),
    ]
    db.record_source_result(run, db.get_source('shop'), items, True, 1, None)
    db.finish_run(run, 'complete', 'Baseline')
    return db


def test_saved_search_matches_baseline_and_intersects_constraints(db):
    id = db.save_search(dict(name='Rome denarii', ruler='hadrian', coin_type='denarius', mint='rome',
                             exclude_terms='plated fourree', category='Roman', currency='gbp', max_price='49.95'))
    rows, total = db.search_matches(id)
    assert total == 1 and rows[0]['external_id'] == '1'
    assert rows[0]['source_name'] == 'Shop' and not rows[0]['is_new']
    assert db.list_searches()[0]['match_count'] == 1


def test_phrases_word_boundaries_and_literal_sql_characters(db):
    id = db.save_search({'keywords': '"silver denarius"'})
    assert db.search_matches(id)[1] == 1
    db.save_search({'keywords': 'drachm'}, id)
    assert db.search_matches(id)[1] == 0
    db.save_search({'keywords': "' OR 1=1 -- %_"}, id)
    assert db.search_matches(id)[1] == 0


@pytest.mark.parametrize('values', [
    {}, {'exclude_terms': 'plated'}, {'keywords': '"unclosed'}, {'keywords': 'coin', 'max_price': '10'},
    {'keywords': 'coin', 'currency': 'GBP', 'max_price': 'NaN'},
    {'keywords': 'coin', 'currency': 'GBP', 'max_price': '-1'},
    {'keywords': 'coin', 'currency': 'GB'}, {'keywords': 'x' * 501},
])
def test_invalid_searches_do_not_persist(db, values):
    with pytest.raises(ValueError):
        db.save_search(values)
    assert db.list_searches() == []


def test_searches_survive_reinitialization_backup_and_edits(db, tmp_path):
    id = db.save_search({'keywords': 'Hadrian', 'include_web': False})
    db.set_search_enabled(id, False)
    db.initialize([])
    assert not db.get_search(id)['enabled']
    assert db.get_search(id)['name'] == 'Hadrian'
    assert not db.get_search(id)['include_web']
    assert db.list_searches(enabled_only=True) == []
    copy = Database(db.backup(tmp_path / 'copy.db'))
    assert copy.search_matches(id)[1] == 5
    db.save_search({'name': 'Owls', 'keywords': 'owl', 'enabled': True}, id)
    assert db.search_matches(id)[1] == 1
    db.delete_search(id)
    with pytest.raises(ValueError, match='not found'):
        db.get_search(id)
    assert db.list_listings(view='all')[1] == 7


def test_web_results_deduplicate_preserve_on_error_and_reset_when_query_changes(db):
    id = db.save_search({'keywords': 'owl'})
    assert db.search_results(id)['status'] == 'idle'
    db.record_web_search(id, [
        dict(url='https://dealer.example/coin?utm_source=search#coin', title='Owl', snippet='A lead'),
        dict(url='https://dealer.example/coin', title='Owl again', snippet='Duplicate'),
    ], None)
    first = db.search_results(id)
    assert first['status'] == 'complete' and len(first['results']) == 1
    assert first['results'][0]['url'] == 'https://dealer.example/coin'
    db.record_web_search(id, None, 'Search limit reached.')
    failed = db.search_results(id)
    assert failed['status'] == 'error' and failed['results'] == first['results']
    db.save_search(dict(db.get_search(id), name='New label'), id)
    assert len(db.search_results(id)['results']) == 1
    db.save_search({'keywords': 'Hadrian'}, id)
    assert db.search_results(id)['status'] == 'idle'
    assert db.search_results(id)['results'] == []
    db.record_web_search(id, [dict(url='https://dealer.example/other', title='Other', snippet='')], None)
    db.record_web_search(id, [], None)
    assert db.search_results(id)['results'] == []
    db.delete_search(id)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM search_web_results').fetchone()[0] == 0


def test_web_search_query_and_manual_link_are_encoded(db):
    id = db.save_search({'coin_type': 'tetradrachm', 'mint': 'Athens', 'exclude_terms': 'plated'})
    search = db.get_search(id)
    assert 'tetradrachm' in search['web_query'] and 'Athens' in search['web_query']
    assert '-plated' in search['web_query']
    assert search['web_url'].startswith('https://www.google.com/search?q=')


def test_web_leads_cannot_store_local_or_script_links(db):
    id = db.save_search({'keywords': 'owl'})
    for url in ['javascript:alert(1)', 'http://127.0.0.1/private']:
        with pytest.raises(FetchError):
            db.record_web_search(id, [dict(url=url, title='Bad', snippet='')], None)
    assert db.search_results(id)['results'] == []


@pytest.mark.parametrize('action', ['edit', 'delete', 'disable_web'])
def test_inflight_results_cannot_restore_deleted_or_changed_search(db, action):
    id = db.save_search({'keywords': 'owl'})
    query = db.get_search(id)['web_query']
    if action == 'delete':
        db.delete_search(id)
    else:
        db.save_search({'keywords': 'Hadrian' if action == 'edit' else 'owl', 'include_web': action != 'disable_web'}, id)
    assert db.record_web_search(id, [dict(url='https://dealer.example/coin', title='Owl')], expected_query=query) is False
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM search_web_results').fetchone()[0] == 0


def test_legacy_search_id_is_not_reused_after_deletion_and_reinitialization(db):
    original = db.save_search({'keywords': 'owl'})
    with db.connect() as c:
        c.execute('UPDATE wanted_searches SET id=41 WHERE id=?', (original,))
        c.execute("DELETE FROM settings WHERE key='last_search_id'")
    db.initialize([])
    query = db.get_search(41)['web_query']
    db.delete_search(41)
    db.initialize([])
    db.initialize([])
    replacement = db.save_search({'keywords': 'owl'})
    assert replacement == 42
    assert db.record_web_search(41, [dict(url='https://dealer.example/stale', title='Stale coin')], expected_query=query) is False
    with pytest.raises(ValueError, match='not found'):
        db.delete_search(41)
    assert db.get_search(replacement)['keywords'] == 'owl'
    assert db.search_results(replacement)['results'] == []


def test_concurrent_search_creates_allocate_distinct_ids_and_preserve_high_water_mark(db):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(lambda i: db.save_search({'keywords': 'owl', 'name': f'Search {i}'}), range(18)))
    assert len(set(ids)) == 18
    assert sorted(ids) == list(range(1, 19))
    for search_id in ids:
        db.delete_search(search_id)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: db.initialize([]), range(6)))
    assert db.save_search({'keywords': 'owl'}) == 19


def test_failed_search_insert_does_not_consume_an_id(db):
    import sqlite3

    first = db.save_search({'keywords': 'owl'})
    with db.connect() as c:
        c.execute("CREATE TRIGGER reject_search BEFORE INSERT ON wanted_searches BEGIN SELECT RAISE(ABORT, 'Test insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='Test insert failure'):
        db.save_search({'keywords': 'owl'})
    with db.connect() as c:
        c.execute('DROP TRIGGER reject_search')
    db.delete_search(first)
    assert db.save_search({'keywords': 'owl'}) == first + 1


def test_long_local_search_requires_shortening_before_web_opt_in(db):
    values = dict(keywords='a' * 490, coin_type='b' * 200, mint='c' * 200, ruler='d' * 200)
    with pytest.raises(ValueError, match='Shorten'):
        db.save_search(values)
    id = db.save_search(dict(values, include_web=False))
    assert db.get_search(id)['include_web'] == 0


@pytest.mark.parametrize('lost_lease', ['interrupted', 'expired'])
def test_late_web_result_cannot_write_after_scan_lease_lost(db, lost_lease):
    id = db.save_search({'keywords': 'owl'})
    run = db.claim_run('manual-coins')
    with db.connect() as c:
        if lost_lease == 'expired':
            c.execute("UPDATE runs SET heartbeat='2000-01-01T00:00:00+00:00' WHERE id=?", (run,))
        else:
            c.execute("UPDATE runs SET status='interrupted' WHERE id=?", (run,))
    assert db.record_web_search(id, [dict(url='https://dealer.example/coin', title='Owl')], run_id=run) is False
    assert db.renew_lease(run) is False
    assert db.search_results(id)['status'] == 'idle'
    assert db.search_results(id)['results'] == []


def test_broad_queries_keep_user_constraints_and_have_international_variants(db):
    from coinwatch.searches import web_query_variants
    id = db.save_search({'keywords': '"silver owl"', 'mint': 'Athens', 'exclude_terms': 'plated "modern copy"'})
    search = db.get_search(id)
    queries = web_query_variants(search, 50)
    assert len(queries) == len(set(queries)) == 50
    assert web_query_variants(search, 1) == [search['web_query']]
    assert all('"silver owl"' in q and 'Athens' in q and '-plated' in q and '-"modern copy"' in q for q in queries)
    assert any('Münzen' in q for q in queries) and any('monnaies' in q for q in queries)
    assert all(len(q) <= 1000 for q in queries)


@pytest.mark.parametrize('count', [0, 51, -1, True, 2.5, '10'])
def test_invalid_broad_query_budget_rejected(db, count):
    from coinwatch.searches import web_query_variants
    id = db.save_search({'keywords': 'Hadrian'})
    with pytest.raises(ValueError):
        web_query_variants(db.get_search(id), count)


def test_long_queries_never_truncate_collector_terms_to_add_variations(db):
    from coinwatch.searches import web_query_variants
    id = db.save_search({'keywords': 'a' * 480, 'mint': 'b' * 190, 'ruler': 'c' * 190, 'category': 'd' * 90})
    queries = web_query_variants(db.get_search(id), 50)
    assert queries
    assert all(len(q) <= 1000 and 'a' * 480 in q and 'd' * 90 in q for q in queries)


def test_broad_results_retain_more_than_twenty_and_daily_scan_does_not_erase_them(db):
    id = db.save_search({'keywords': 'owl'})
    leads = [dict(url=f'https://shop.example/{i}', title=f'Owl {i}') for i in range(80)]
    db.record_web_search(id, leads, merge=True)
    assert len(db.search_results(id)['results']) == 80
    db.record_web_search(id, [dict(url='https://shop.example/1#top', title='Updated owl')], merge=True)
    stored = db.search_results(id)['results']
    assert len(stored) == 80 and stored[0]['title'] == 'Updated owl'
    db.record_web_search(id, [], merge=True)
    assert len(db.search_results(id)['results']) == 80


def test_partial_query_batch_keeps_new_and_previous_leads(db):
    id = db.save_search({'keywords': 'owl'})
    db.record_web_search(id, [dict(url='https://old.example/coin', title='Earlier owl')])
    old = db.search_results(id)['results'][0]
    db.record_web_search(id, [dict(url='https://new.example/coin', title='New owl')], 'Rate limit reached', merge=True)
    result = db.search_results(id)
    assert result['status'] == 'partial' and result['error'] == 'Rate limit reached'
    assert [r['title'] for r in result['results']] == ['New owl', 'Earlier owl']
    assert result['results'][1]['last_seen'] == old['last_seen']
    db.record_web_search(id, None, 'Unavailable', merge=True)
    assert db.search_results(id)['results'] == result['results']


def test_accumulated_leads_cap_retains_latest_batch(db):
    id = db.save_search({'keywords': 'owl'})
    db.record_web_search(id, [dict(url=f'https://old.example/{i}', title='Old') for i in range(1000)], merge=True)
    db.record_web_search(id, [dict(url=f'https://new.example/{i}', title='New') for i in range(20)], merge=True)
    result = db.search_results(id)['results']
    assert len(result) == 1000
    assert all(r['title'] == 'New' for r in result[:20])
