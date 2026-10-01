"""One process scheduler, with durable per-day occurrence bookkeeping."""
import logging
import re
import threading
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from .scanner import Scanner, get_scan_search, normalize_scan_mode, validate_web_minutes, validate_web_queries, validate_web_depth

log = logging.getLogger(__name__)

# Retained search-engine leads can describe museum pieces, images, replicas, or
# auction lots even when their titles look like individual coins.
_NON_LISTING_HOSTS = ('one.bid', 'onebid.pl', 'bidinside.com', 'drouot.com', 'academia.edu',
                      'picryl.com', 'alamy.com', 'artic.edu', 'superstock.com',
                      'museumsvictoria.com.au', 'numisforums.com', 'coincommunity.com',
                      'worthpoint.com', 'album-online.com', 'coinreplicas.com', 'picclick.com')
_NON_LISTING_PATH = re.compile(r'/(?:lots?|forums?|topics?|artworks|stock-photo|greek-coins-by-area|search)(?:/|\.|$)', re.I)
_NON_LISTING_TITLE = re.compile(r'\b(?:pdf|pendant|jewelry|jewellery|stock photo(?:graphy)?|public domain image|'
                                r'coins? reference|online auction|online bidding|internet\s*auktion|enchères)\b', re.I)
_NEGATIVE_SNIPPET = re.compile(r'\b(?:out[ -]of[ -]stock|sold out)\b|'
                               r'(?:^|[.!|;]\s*)(?:sold|vendu|vendido|esaurito|ausverkauft)(?=\s*(?:[.!|;]|$))|'
                               r'\bsold\s+for\s+(?:[£€$]|\d)|'
                               r'\b(?:replica|reproduction)\s+coin\b', re.I)


def _potential_listing_url(url):
    from .catalog_links import _safe_url, is_catalog_url
    if not _safe_url(url, url) or is_catalog_url(url):
        return False
    parts = urlsplit(url)
    host = parts.hostname.lower().removeprefix('www.')
    path = unquote(parts.path)
    if (any(host == domain or host.endswith('.' + domain) for domain in _NON_LISTING_HOSTS)
            or host.startswith(('auction.', 'auctions.')) or _NON_LISTING_PATH.search(path)):
        return False
    if (host == 'todocoleccion.net' or host.endswith('.todocoleccion.net')) and path.startswith('/s/'):
        return False
    # This dealer's product links end in .html; bare numbered pages are categories.
    if host == 'issoire-philatelie.com' and re.fullmatch(r'/\d+-[^/.]+/?', path):
        return False
    return True


def _web_result_key(url):
    parts = urlsplit(url)
    return (parts.scheme, parts.hostname.lower().removeprefix('www.'),
            parts.port or (443 if parts.scheme == 'https' else 80), parts.path, parts.query)


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
        self._state = {'running': False, 'mode': '', 'search_id': None, 'web_queries': 0, 'web_minutes': 0, 'web_depth': 'basic', 'phase': 'Idle', 'source': '', 'last_error': ''}

    def _update(self, **values):
        with self._lock:
            self._state.update(values)

    def snapshot(self):
        with self._lock:
            state = dict(self._state)
        state['next_due'] = schedule_state(self.db.settings())['next_due']
        return state

    def search_results(self, search_id):
        from .sale_checks import _NEGATIVE, individual_ancient_coin_title
        from .searches import web_matcher
        from .web_search import WebSearchError, load_api_key
        search = self.db.get_search(search_id)
        matches = web_matcher(search)
        result = self.db.search_results(search_id)
        retained = result['results']
        latest = {}
        def check_order(row):
            # UTC ISO timestamps sort chronologically; empty pending checks sort first.
            # A known rejection wins ties because stored check times round to seconds.
            return row['sale_checked_at'] or '', {'available': 0, 'unverified': 1, 'rejected': 2}[row['sale_status']]
        for row in retained:
            key = _web_result_key(row['url'])
            if key not in latest or check_order(row) > check_order(latest[key]):
                latest[key] = row
        now = datetime.now(timezone.utc)
        cutoff = (now-timedelta(hours=24)).isoformat(timespec='seconds')
        result['results'] = [row for row in latest.values() if row['sale_status'] == 'available'
                             and cutoff <= row['sale_checked_at'] <= now.isoformat(timespec='seconds')
                             and individual_ancient_coin_title(row['title']) and matches(row)]
        visible_urls = {row['url'] for row in result['results']}
        # Potential listings retain title criteria without requiring a confirmed price.
        # This is presentation only: stored checks and the verified contract stay intact.
        title_matches = web_matcher({**search, 'currency': '', 'max_price': ''})
        ceiling = Decimal(search['max_price']) if search['max_price'] else None
        potential = []
        for row in latest.values():
            if (row['sale_status'] not in ('available', 'unverified')
                    or not individual_ancient_coin_title(row['title'])
                    or _NEGATIVE.search(row['title']) or not title_matches(row)
                    or _NON_LISTING_TITLE.search(row['title']) or not _potential_listing_url(row['url'])):
                continue
            # Explicit negative claims can rule out a lead, but generic navigation
            # such as "Sold Items" cannot. Fresh seller verification takes precedence.
            if row['url'] not in visible_urls and _NEGATIVE_SNIPPET.search(row['snippet']):
                continue
            if search['currency'] and row['currency'] and row['currency'] != search['currency']:
                continue
            try:
                price = Decimal(row['price'])
            except InvalidOperation:
                price = None
            if ceiling is not None and price is not None and price.is_finite() and price > ceiling:
                continue
            if row['url'] in visible_urls:
                verification, label = 'verified', 'Verified for sale'
                note = 'The seller page confirmed availability and a fixed price within the last 24 hours.'
                price_label = 'Verified price'
            elif row['sale_status'] == 'available' and row['sale_checked_at'] and row['sale_checked_at'] < cutoff:
                verification, label = 'expired', 'Needs recheck'
                note = 'Previously verified; availability and price need a new check.'
                price_label = 'Last known price'
            else:
                verification, label = 'unverified', 'Unverified'
                note = ((row['sale_reason'] if row['sale_status'] == 'unverified' else '')
                        or 'Availability and price need a new check.')
                price_label = 'Price unconfirmed'
            presented = dict(row, verification=verification, verification_label=label,
                             verification_note=note, price_label=price_label)
            if verification == 'unverified':
                presented.update(price='', currency='')
            potential.append(presented)
        result['potential_results'] = potential
        counts = Counter(row['verification'] for row in potential)
        result['potential_counts'] = {state: counts[state] for state in ('verified', 'unverified', 'expired')}
        reasons = Counter()
        for row in retained:
            if row['url'] in visible_urls:
                continue
            if row['url'] != latest[_web_result_key(row['url'])]['url']:
                status, reason = 'Duplicate', 'This URL is superseded by another URL\'s newer or equally recent sale check.'
            elif row['sale_status'] == 'available':
                if not individual_ancient_coin_title(row['title']):
                    status, reason = 'Rejected', 'The primary item is not a supported individual ancient coin.'
                elif not matches(row):
                    status, reason = 'Not matching', 'The listing does not match this saved search.'
                else:
                    status, reason = 'Expired', 'Availability needs a new check.'
            else:
                status, reason = row['sale_status'].capitalize(), row['sale_reason'] or 'No completed sale check yet.'
            reasons[status, reason] += 1
        result['candidate_count'] = len(retained)
        result['check_summary'] = [dict(status=status, reason=reason, count=count)
                                   for (status, reason), count in reasons.most_common()]
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

    def start_scan(self, kind='manual', mode='both', search_id=None, web_queries=1, web_minutes=10, web_depth='basic'):
        mode = normalize_scan_mode(mode)
        selected_search = get_scan_search(self.db, mode, search_id)
        validate_web_queries(web_queries, kind, selected_search)
        validate_web_minutes(web_minutes, kind, selected_search)
        validate_web_depth(web_depth, kind, selected_search, mode)
        with self._lock:
            if self._state['running'] or self._stop.is_set():
                return False
            self._state.update(running=True, mode=mode, search_id=search_id, web_queries=web_queries, web_minutes=web_minutes, web_depth=web_depth, phase='Starting scan', source='', last_error='')
            self._worker = threading.Thread(target=self._work, args=(kind, mode, search_id, web_queries, web_minutes, web_depth), name='catalog-scan', daemon=True)
            self._worker.start()
        return True

    def _work(self, kind, mode='both', search_id=None, web_queries=1, web_minutes=10, web_depth='basic'):
        try:
            self._scanner = Scanner(self.db, progress=self._update, stop_event=self._stop)
            result = self._scanner.run(kind, mode=mode, search_id=search_id, web_queries=web_queries, web_minutes=web_minutes, web_depth=web_depth)
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
            self._update(running=False, mode='', search_id=None, web_queries=0, web_minutes=0, web_depth='basic', phase='Idle', source='')

    def stop(self):
        self._stop.set()
        for thread in (self._scheduler, self._worker):
            if thread and thread.is_alive():
                thread.join(timeout=45)
        if self._worker and self._worker.is_alive() and self._scanner and self._scanner.run_id:
            self.db.finish_run(self._scanner.run_id, 'interrupted', 'Application stopped; committed observations were preserved.')
