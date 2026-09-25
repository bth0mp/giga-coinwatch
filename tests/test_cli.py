import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import json
import pytest

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


@pytest.mark.parametrize('flags,expected_mode', [(['--mode','coins'],'coins'), (['--mode','dealers'],'dealers'), ([], 'both'), (['--no-discovery'],'coins')])
def test_cli_scan_mode_runs_selected_work(tmp_path, monkeypatch, capsys, flags, expected_mode):
    from coinwatch.__main__ import main
    from coinwatch.models import ScrapeResult
    monkeypatch.setattr('coinwatch.sources.scrape_source', lambda *args: ScrapeResult([], 1))
    monkeypatch.setattr('coinwatch.discovery.discover', lambda *args, **kwargs: [dict(url='https://fresh.example/', name='Fresh')])
    monkeypatch.setattr(sys, 'argv', ['coinwatch','--data-dir',str(tmp_path),'scan',*flags])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['mode'] == expected_mode
    db = Database(tmp_path/'catalog.db')
    assert db.runs()[0]['kind'] == 'manual-' + expected_mode
    assert len(db.list_candidates()) == (0 if expected_mode == 'coins' else 1)
    assert any(s['last_attempt'] for s in db.list_sources()) == (expected_mode != 'dealers')


def test_cli_rejects_dealer_mode_with_no_discovery(tmp_path):
    result = command('--data-dir', tmp_path, 'scan', '--mode', 'dealers', '--no-discovery')
    assert result.returncode == 2
    assert '--no-discovery' in result.stderr
    assert not (tmp_path/'catalog.db').exists()


@pytest.mark.parametrize('flags', [['--search-id','0'], ['--search-id','-1'], ['--mode','dealers','--search-id','1']])
def test_cli_rejects_incompatible_or_nonpositive_search_id_before_data_creation(tmp_path, flags):
    result = command('--data-dir', tmp_path, 'scan', *flags)
    assert result.returncode == 2
    assert '--search-id' in result.stderr
    assert not (tmp_path/'catalog.db').exists()


def test_cli_accepts_positive_search_id_and_rejects_missing_watch_without_scan(tmp_path):
    result = command('--data-dir', tmp_path, 'scan', '--mode', 'coins', '--search-id', '123')
    assert result.returncode == 1
    db = Database(tmp_path/'catalog.db')
    assert db.runs() == []
