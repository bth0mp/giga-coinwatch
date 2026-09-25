"""One process scheduler, with durable per-day occurrence bookkeeping."""
import logging
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .scanner import Scanner, get_scan_search, normalize_scan_mode

log = logging.getLogger(__name__)


def schedule_state(settings, now=None):
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(settings.get('timezone', 'Europe/London'))
    local = now.astimezone(zone)
    hour, minute = map(int, settings.get('scan_time', '09:00').split(':'))
    def instant(date):
        # First occurrence on repeated hours; missing spring hours move forward.
        return datetime.combine(date, time(hour, minute), zone).astimezone(timezone.utc)
    today = local.date()
    scheduled = instant(today)
    last = settings.get('last_scheduled_date', '')
    occurrence = today if now >= scheduled else today-timedelta(days=1)
    persisted_due = None
    try:
        raw_due = settings.get('next_due', '')
        if raw_due:
            persisted_due = datetime.fromisoformat(raw_due)
            if persisted_due.tzinfo is None:
                persisted_due = None
    except ValueError:
        pass
    overdue_first = persisted_due is not None and persisted_due <= now
    due = occurrence.isoformat() > last and (bool(last) or now >= scheduled or overdue_first)
    next_time = scheduled if now < scheduled and last < today.isoformat() else instant(today+timedelta(days=1))
    if due:
        next_time = instant(occurrence)
    return {'due': due, 'occurrence': occurrence.isoformat(), 'next_due': next_time.isoformat()}


class Runtime:
    def __init__(self, db):
        self.db = db
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._worker = None
        self._scanner = None
        self._scheduler = None
        self._state = {'running': False, 'mode': '', 'search_id': None, 'phase': 'Idle', 'source': '', 'last_error': ''}

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def snapshot(self):
        with self._lock:
            state = dict(self._state)
        state['next_due'] = schedule_state(self.db.settings())['next_due']
        return state

    def search_results(self, search_id):
        from .web_search import WebSearchError, load_api_key
        search = self.db.get_search(search_id)
        result = self.db.search_results(search_id)
        if search['include_web']:
            try:
                api_key = load_api_key(self.db.path.parent)
            except WebSearchError as e:
                result.update(status='error', error=str(e))
            else:
                if not api_key and not result['results']:
                    result.update(status='unconfigured', error='Configure a Tavily API key in Settings to enable wider web searches.')
        return result

    def start(self):
        if self._scheduler and self._scheduler.is_alive():
            return
        self._stop.clear()
        self._scheduler = threading.Thread(target=self._loop, name='daily-scheduler', daemon=True)
        self._scheduler.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                state = schedule_state(self.db.settings())
                self.db.set_internal('next_due', state['next_due'])
                if state['due']:
                    self.start_scan('scheduled')
            except Exception:
                log.exception('Scheduler check failed')
            self._stop.wait(30)

    def start_scan(self, kind='manual', mode='both', search_id=None):
        mode = normalize_scan_mode(mode)
        get_scan_search(self.db, mode, search_id)
        with self._lock:
            if self._state['running'] or self._stop.is_set():
                return False
            self._state.update(running=True, mode=mode, search_id=search_id, phase='Starting scan', source='', last_error='')
            self._worker = threading.Thread(target=self._work, args=(kind, mode, search_id), name='catalog-scan', daemon=True)
            self._worker.start()
        return True

    def _work(self, kind, mode='both', search_id=None):
        try:
            self._scanner = Scanner(self.db, progress=self._update, stop_event=self._stop)
            result = self._scanner.run(kind, mode=mode, search_id=search_id)
            if mode == 'both' and search_id is None and (result['status'] == 'complete' or (kind == 'scheduled' and result['status'] in ('partial', 'failed'))):
                state = schedule_state(self.db.settings())
                if state['due']:
                    self.db.set_internal('last_scheduled_date', state['occurrence'])
            if result['status'] == 'busy':
                self._update(last_error=result['summary'])
        except Exception as e:
            log.exception('Background scan failed')
            self._update(last_error=str(e))
        finally:
            self._update(running=False, mode='', search_id=None, phase='Idle', source='')

    def stop(self):
        self._stop.set()
        for thread in (self._scheduler, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout=45)
        if self._worker and self._worker.is_alive() and self._scanner and self._scanner.run_id:
            self.db.finish_run(self._scanner.run_id, 'interrupted', 'Application stopped; committed observations were preserved.')
