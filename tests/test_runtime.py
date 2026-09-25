from datetime import datetime, timezone
import threading

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


def test_failed_manual_scan_does_not_consume_daily_occurrence(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    monkeypatch.setattr('coinwatch.runtime.Scanner.run', lambda self,kind: {'status':'failed'})
    Runtime(db)._work('manual')
    assert db.settings()['last_scheduled_date'] == ''


def test_scheduled_failure_is_recorded_without_endless_catchup(tmp_path, monkeypatch):
    from coinwatch.runtime import Runtime
    db = Database(tmp_path/'catalog.db')
    db.initialize([])
    monkeypatch.setattr('coinwatch.runtime.schedule_state', lambda _: {'due':True,'occurrence':'2026-09-25','next_due':'2026-09-26T08:00:00+00:00'})
    monkeypatch.setattr('coinwatch.runtime.Scanner.run', lambda self,kind: {'status':'failed'})
    Runtime(db)._work('scheduled')
    assert db.settings()['last_scheduled_date'] == '2026-09-25'
