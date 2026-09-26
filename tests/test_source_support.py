from coinwatch.db import Database
from coinwatch.source_support import describe_support


def test_source_review_survives_upgrade_and_user_pause(tmp_path):
    db = Database(tmp_path / 'catalog.db')
    original = dict(id='dealer', name='Dealer', url='https://dealer.example/ancient',
                    adapter='', enabled=False)
    db.initialize([original])
    assert db.get_source('dealer')['support_status'] == 'needs_review'
    reviewed = dict(original, adapter='woocommerce', enabled=True, support_status='ready',
                    support_checked='2026-09-26', note='Ancient catalog validated.')
    db.initialize([reviewed])
    row = db.get_source('dealer')
    assert row['support_status'] == 'ready'
    assert row['support_checked'] == '2026-09-26'
    assert not row['enabled']  # An update must not undo a user pause.
    db.set_source_enabled('dealer', True)
    db.initialize([reviewed])
    assert db.get_source('dealer')['enabled']


def test_support_descriptions_distinguish_blocked_and_unbuilt_sources():
    assert describe_support(dict(adapter='', support_status='access_blocked'))['label'] == 'Access blocked'
    assert describe_support(dict(adapter='', support_status='needs_adapter'))['label'] == 'Parser needed'
    assert describe_support(dict(adapter='', support_status='permission_required'))['label'] == 'Permission needed'
    assert describe_support(dict(adapter='woocommerce', enabled=False))['label'] == 'Ready to enable'
    assert describe_support(dict(adapter='woocommerce', enabled=True))['label'] == 'Active'


def test_registry_scope_change_resets_baseline_but_metadata_change_does_not(tmp_path):
    db = Database(tmp_path / 'catalog.db')
    original = dict(id='dealer', name='Dealer', url='https://dealer.example/roman',
                    adapter='woocommerce', enabled=True)
    db.initialize([original])
    with db.connect() as c:
        c.execute("UPDATE sources SET baseline_complete=1,last_count=3,last_pages=1")
    db.initialize([dict(original, note='Checked again', support_status='ready')])
    assert db.get_source('dealer')['baseline_complete']
    db.initialize([dict(original, url='https://dealer.example/ancient')])
    assert not db.get_source('dealer')['baseline_complete']
    assert db.get_source('dealer')['last_count'] == 0
