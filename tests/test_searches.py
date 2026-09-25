import pytest

from coinwatch.db import Database
from coinwatch.fetch import FetchError
from coinwatch.models import Listing


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
