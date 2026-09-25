import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from coinwatch.__main__ import InstanceLock
from coinwatch.db import Database
from coinwatch.models import Listing


def command(*args):
    return subprocess.run([sys.executable,'-m','coinwatch',*map(str,args)],capture_output=True,text=True,timeout=20)


def test_backup_restore_preserves_saved_listing_and_baseline(tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    db = Database(source/'catalog.db')
    db.initialize([dict(id='shop',name='Shop',url='https://example.com',adapter='test',enabled=False)])
    run = db.claim_run('manual')
    db.record_source_result(run,db.get_source('shop'),[Listing('a','https://example.com/a','Roman silver','23.50','GBP')],True,1,None)
    db.toggle_saved(db.list_listings(view='all')[0][0]['id'])
    db.finish_run(run,'complete','Done')
    backup = tmp_path/'export.db'
    assert command('--data-dir',source,'backup',backup).returncode == 0
    target = tmp_path/'restored'
    restored = command('--data-dir',target,'restore',backup)
    assert restored.returncode == 0, restored.stderr
    restored_db = Database(target/'catalog.db')
    assert restored_db.list_listings(view='saved')[1] == 1
    assert restored_db.list_listings(view='new')[1] == 0
    assert restored_db.get_source('shop')['baseline_complete'] == 1


def test_restore_rejected_while_instance_running(tmp_path):
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    backup = tmp_path/'backup.db'
    db.backup(backup)
    with InstanceLock(tmp_path):
        result = command('--data-dir',tmp_path,'restore',backup)
    assert result.returncode == 1
    assert 'already running' in result.stderr
