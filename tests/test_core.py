from datetime import datetime, timezone
from pathlib import Path

from coinwatch.db import Database
from coinwatch.models import Listing


def make_db(tmp_path):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([dict(id='shop', name='Shop', url='https://example.com', adapter='test', enabled=True)])
    return db


def coin(id='one', price='42.00', url=None):
    return Listing(id, url or f'https://example.com/{id}', f'Roman denarius {id}', price, 'GBP')


def test_partial_baseline_then_new_listing(tmp_path):
    db = make_db(tmp_path)
    source = db.get_source('shop')
    run = db.claim_run('manual')
    db.record_source_result(run, source, [coin()], False, 1, 'page failed')
    assert db.list_listings(view='new')[1] == 0
    assert not db.get_source('shop')['baseline_complete']
    db.record_source_result(run, source, [coin(), coin('two')], True, 2, None)
    assert db.list_listings(view='new')[1] == 0
    db.record_source_result(run, source, [coin(), coin('two'), coin('three')], True, 2, None)
    assert db.list_listings(view='new')[1] == 1


def test_product_identity_survives_url_and_price_change_and_missing_page(tmp_path):
    db = make_db(tmp_path)
    s = db.get_source('shop')
    run = db.claim_run('manual')
    db.record_source_result(run, s, [coin()], True, 1, None)
    db.record_source_result(run, s, [coin(price='30.25', url='https://example.com/renamed')], True, 1, None)
    db.record_source_result(run, s, [], False, 0, 'blocked')
    rows, total = db.list_listings(view='all')
    assert total == 1
    assert rows[0]['price'] == '30.25'
    assert rows[0]['availability'] == 'available'
    assert rows[0]['url'].endswith('/renamed')
    assert db.get_source('shop')['last_error'] == 'blocked'


def test_run_lease_excludes_overlap_and_can_expire(tmp_path):
    db = make_db(tmp_path)
    first = db.claim_run('manual')
    assert first
    assert db.claim_run('scheduled') is None
    with db.connect() as conn:
        conn.execute("UPDATE runs SET heartbeat='2000-01-01T00:00:00+00:00' WHERE id=?", (first,))
    assert db.claim_run('manual')
    assert any(r['status'] == 'interrupted' for r in db.runs())


def test_saved_and_dismissed_decisions_survive_backup(tmp_path):
    db = make_db(tmp_path)
    run = db.claim_run('manual')
    db.record_source_result(run, db.get_source('shop'), [coin()], True, 1, None)
    rows, _ = db.list_listings(view='all')
    db.toggle_saved(rows[0]['id'])
    db.save_candidate(dict(domain='dealer.example', name='Dealer', url='https://dealer.example', reason='Fixed price', evidence_url='https://dealer.example/shop', discovered_from='https://directory.example'))
    candidate = db.list_candidates()[0]
    db.decide_candidate(candidate['id'], 'dismissed')
    backup = tmp_path / 'backup.db'
    db.backup(backup)
    copy = Database(backup)
    assert copy.list_listings(view='saved')[1] == 1
    assert len(copy.list_candidates('dismissed')) == 1
    assert copy.get_source('shop')['baseline_complete']


def test_better_candidate_evidence_preserves_discovery_identity_and_decisions(tmp_path):
    db = make_db(tmp_path)
    lead = dict(url='https://dealer.example', name='Dealer', reason='Unclear')
    db.save_candidate(lead)
    original = db.list_candidates()[0]
    verified = dict(lead, evidence_url='https://dealer.example/roman-coin', reason='Available ancient coin at a fixed price')
    db.save_candidate(verified)
    updated = db.list_candidates()[0]
    assert updated['id'] == original['id'] and updated['created_at'] == original['created_at']
    assert updated['evidence_url'] == verified['evidence_url']
    db.save_candidate(lead)
    assert db.list_candidates()[0]['evidence_url'] == verified['evidence_url']
    db.decide_candidate(updated['id'], 'dismissed')
    db.save_candidate(verified)
    assert db.list_candidates() == [] and len(db.list_candidates('dismissed')) == 1


def test_currency_filter_does_not_compare_incomparable_prices(tmp_path):
    db = make_db(tmp_path)
    run = db.claim_run('manual')
    db.record_source_result(run, db.get_source('shop'), [coin(), Listing('euro','https://example.com/euro','Roman coin','20.00','EUR')], True, 1, None)
    rows, total = db.list_listings(view='all',currency='GBP',max_price='45')
    assert total == 1 and rows[0]['currency'] == 'GBP'


def test_settings_reject_invalid_timezone_and_time(tmp_path):
    import pytest
    db = make_db(tmp_path)
    with pytest.raises(ValueError):
        db.update_settings({'timezone':'No/SuchZone'})
    with pytest.raises(ValueError):
        db.update_settings({'scan_time':'25:12'})
    assert db.settings()['timezone'] == 'Europe/London'


def test_interrupted_scan_cannot_write_after_lease_is_taken_over(tmp_path):
    import pytest
    db = make_db(tmp_path)
    old_run = db.claim_run('manual')
    with db.connect() as c:
        c.execute("UPDATE runs SET heartbeat='2000-01-01T00:00:00+00:00' WHERE id=?", (old_run,))
    assert db.claim_run('scheduled')
    with pytest.raises(ValueError, match='lease'):
        db.record_source_result(old_run, db.get_source('shop'), [coin()], True, 1, None)
    assert db.list_listings(view='all')[1] == 0
    assert not db.get_source('shop')['baseline_complete']


def test_two_real_product_ids_are_distinct_even_if_seller_reuses_url(tmp_path):
    db = make_db(tmp_path)
    run = db.claim_run('manual')
    s = db.get_source('shop')
    db.record_source_result(run, s, [coin('one', url='https://example.com/featured')], True, 1, None)
    db.record_source_result(run, s, [coin('two', url='https://example.com/featured')], True, 1, None)
    assert db.list_listings(view='all')[1] == 2
    assert db.list_listings(view='new')[1] == 1


def test_registry_upgrade_revokes_disabled_adapter_but_preserves_user_pause(tmp_path):
    db = make_db(tmp_path)
    db.initialize([dict(id='shop',name='Shop',url='https://example.com',adapter='',enabled=False,note='Automation restricted')])
    assert not db.get_source('shop')['enabled']
    assert not db.get_source('shop')['adapter']
    db.initialize([dict(id='shop',name='Shop',url='https://example.com',adapter='test',enabled=True,note='Supported')])
    assert db.get_source('shop')['adapter'] == 'test'
    assert not db.get_source('shop')['enabled']
