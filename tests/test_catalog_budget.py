import pytest

from coinwatch.db import Database
from coinwatch.fetch import FetchError
from coinwatch.models import Listing, ScrapeResult
from coinwatch.scanner import Scanner


def catalog_db(tmp_path, count=3):
    db = Database(tmp_path / 'catalog.db')
    db.initialize([dict(id=f'shop-{i:02}', name=f'Shop {i:02}', url=f'https://shop{i}.example',
                        adapter='test', enabled=True) for i in range(count)])
    return db


def test_slow_dealer_cannot_expire_later_dealers_budget(tmp_path, monkeypatch):
    db = catalog_db(tmp_path)
    clock = [100.0]
    monkeypatch.setattr('coinwatch.scanner.time.monotonic', lambda: clock[0])
    def scrape(source, fetcher):
        fetcher._check()
        if source['id'] == 'shop-00':
            clock[0] = fetcher.deadline + 0.001
            fetcher._check()
        clock[0] += 2
        return ScrapeResult([Listing('one', source['url'] + '/one', 'Greek silver stater', '50', 'GBP')], 1)
    result = Scanner(db, scrape=scrape).run(mode='coins')
    assert result['status'] == 'partial'
    assert result['seen'] == 2
    assert all(db.get_source(f'shop-{i:02}')['last_success'] for i in (1, 2))
    assert clock[0] < 400  # The slow dealer cannot occupy the whole 15-minute sweep.


def test_repeated_slow_dealers_leave_time_for_every_remaining_source(tmp_path, monkeypatch):
    db = catalog_db(tmp_path, 16)
    clock = [100.0]
    monkeypatch.setattr('coinwatch.scanner.time.monotonic', lambda: clock[0])
    allocations = []
    def scrape(source, fetcher):
        fetcher._check()
        allocations.append(fetcher.deadline - clock[0])
        clock[0] = fetcher.deadline
        with pytest.raises(FetchError, match='budget'):
            fetcher._check()
        return ScrapeResult([], 0, False, 'Slow seller exceeded its time allowance.')
    result = Scanner(db, scrape=scrape).run(mode='coins')
    assert result['status'] == 'partial'
    assert len(allocations) == 16 and min(allocations) >= 30
    assert max(allocations) <= 180
    assert clock[0] <= 1000  # Keep the existing 15-minute total catalog budget.


def test_single_explicit_source_can_use_full_catalog_budget(tmp_path, monkeypatch):
    db = catalog_db(tmp_path)
    clock = [100.0]
    monkeypatch.setattr('coinwatch.scanner.time.monotonic', lambda: clock[0])
    def scrape(source, fetcher):
        clock[0] += 300
        fetcher._check()
        return ScrapeResult([], 1)
    result = Scanner(db, scrape=scrape).run(mode='coins', source_ids=['shop-02'])
    assert result['status'] == 'complete'
    assert db.get_source('shop-02')['last_success']
    assert db.get_source('shop-00')['last_attempt'] is None
