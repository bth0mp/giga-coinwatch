"""Transactional catalog storage. Each operation owns a short-lived connection."""
import hashlib
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .searches import CRITERIA, matcher, validate_search, with_web_query


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def public_url_shape(url):
    from .fetch import validate_url_shape
    validate_url_shape(url)
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or '/', p.query, ''))


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self, seeds=None):
        if seeds is None:
            path = Path(__file__).with_name('seeds.json')
            seeds = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        with self.connect() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS sources (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                  adapter TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
                  note TEXT NOT NULL DEFAULT '', baseline_complete INTEGER NOT NULL DEFAULT 0,
                  last_attempt TEXT, last_success TEXT, last_error TEXT,
                  last_count INTEGER DEFAULT 0, last_pages INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS listings (
                  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                  external_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                  price TEXT NOT NULL, currency TEXT NOT NULL, image_url TEXT DEFAULT '',
                  category TEXT DEFAULT 'Ancient', availability TEXT DEFAULT 'available',
                  listed_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                  is_new INTEGER NOT NULL DEFAULT 0, saved INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(source_id,external_id));
                CREATE INDEX IF NOT EXISTS listing_seen ON listings(first_seen DESC);
                CREATE TABLE IF NOT EXISTS aliases (
                  source_id TEXT NOT NULL, url TEXT NOT NULL, listing_id INTEGER REFERENCES listings(id),
                  PRIMARY KEY(source_id,url));
                CREATE TABLE IF NOT EXISTS observations (
                  id INTEGER PRIMARY KEY, listing_id INTEGER REFERENCES listings(id),
                  observed_at TEXT NOT NULL, price TEXT NOT NULL, availability TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL,
                  heartbeat TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, summary TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS source_runs (
                  id INTEGER PRIMARY KEY, run_id TEXT REFERENCES runs(id), source_id TEXT REFERENCES sources(id),
                  checked_at TEXT, complete INTEGER, pages INTEGER, count INTEGER, error TEXT);
                CREATE TABLE IF NOT EXISTS candidates (
                  id INTEGER PRIMARY KEY, domain TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                  url TEXT NOT NULL, evidence_url TEXT DEFAULT '', discovered_from TEXT DEFAULT '',
                  reason TEXT DEFAULT '', status TEXT DEFAULT 'pending', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wanted_searches (
                  id INTEGER PRIMARY KEY, name TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '',
                  coin_type TEXT NOT NULL DEFAULT '', mint TEXT NOT NULL DEFAULT '', ruler TEXT NOT NULL DEFAULT '',
                  exclude_terms TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
                  currency TEXT NOT NULL DEFAULT '', max_price TEXT NOT NULL DEFAULT '',
                  enabled INTEGER NOT NULL DEFAULT 1, include_web INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL, web_status TEXT NOT NULL DEFAULT 'idle',
                  web_error TEXT NOT NULL DEFAULT '', web_checked_at TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS search_web_results (
                  search_id INTEGER NOT NULL REFERENCES wanted_searches(id) ON DELETE CASCADE,
                  url TEXT NOT NULL, title TEXT NOT NULL, snippet TEXT NOT NULL,
                  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, position INTEGER NOT NULL,
                  PRIMARY KEY(search_id,url));
            ''')
            web_columns = {row['name'] for row in c.execute('PRAGMA table_info(search_web_results)')}
            for name, definition in {
                'sale_status': "TEXT NOT NULL DEFAULT 'unverified'", 'price': "TEXT NOT NULL DEFAULT ''",
                'currency': "TEXT NOT NULL DEFAULT ''", 'sale_checked_at': "TEXT NOT NULL DEFAULT ''",
                'sale_reason': "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in web_columns:
                    c.execute(f'ALTER TABLE search_web_results ADD COLUMN {name} {definition}')
            for s in seeds:
                c.execute('INSERT OR IGNORE INTO sources(id,name,url,adapter,enabled,note) VALUES(?,?,?,?,?,?)',
                          (s['id'], s['name'], s['url'], s.get('adapter') or '', bool(s.get('enabled')), s.get('note', '')))
                c.execute('UPDATE sources SET name=?,url=?,adapter=?,note=? WHERE id=?',
                          (s['name'], s['url'], s.get('adapter') or '', s.get('note', ''), s['id']))
                if not s.get('adapter'):
                    c.execute('UPDATE sources SET enabled=0 WHERE id=?', (s['id'],))
            for k, v in dict(timezone='Europe/London', scan_time='09:00', csrf_token=secrets.token_urlsafe(32), last_scheduled_date='').items():
                c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (k, v))

    def list_sources(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM sources ORDER BY enabled DESC,name')]

    def get_source(self, id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM sources WHERE id=?', (id,)).fetchone()
            if row is None:
                raise ValueError('Source not found')
            return dict(row)

    def set_source_enabled(self, id, enabled):
        source = self.get_source(id)
        if enabled and not source['adapter']:
            raise ValueError('This source needs a validated adapter before monitoring can be enabled.')
        with self.connect() as c:
            c.execute('UPDATE sources SET enabled=? WHERE id=?', (bool(enabled), id))

    def add_source(self, url, name=''):
        url = public_url_shape(url.strip())
        host = urlsplit(url).hostname.removeprefix('www.')
        for source in self.list_sources():
            if urlsplit(source['url']).hostname.removeprefix('www.') == host:
                return source['id']
        id = re.sub(r'[^a-z0-9]+', '-', host.lower()).strip('-')
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO sources(id,name,url,note) VALUES(?,?,?,?)',
                      (id, (name.strip() or host)[:160], url, 'Saved for review; extraction is not yet supported.'))
        return id

    def settings(self):
        with self.connect() as c:
            return dict(c.execute('SELECT key,value FROM settings').fetchall())

    def update_settings(self, values):
        allowed = {k: str(v).strip() for k, v in values.items() if k in ('timezone', 'scan_time')}
        if 'timezone' in allowed:
            try:
                ZoneInfo(allowed['timezone'])
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError('Choose a valid IANA time zone, for example Europe/London.') from e
        if 'scan_time' in allowed and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', allowed['scan_time']):
            raise ValueError('Scan time must use HH:MM, from 00:00 to 23:59.')
        with self.connect() as c:
            for k, v in allowed.items():
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))

    def set_internal(self, key, value):
        with self.connect() as c:
            c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def list_listings(self, q='', source='', category='', currency='', max_price=None, view='new', page=1, per_page=30):
        clauses, args = [], []
        if view == 'new':
            clauses += ['l.is_new=1', "l.availability='available'"]
        elif view == 'saved':
            clauses.append('l.saved=1')
        if q:
            clauses.append('l.title LIKE ? ESCAPE \'\\\'')
            args.append('%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        for col, value in [('source_id', source), ('category', category), ('currency', currency)]:
            if value:
                clauses.append(f'l.{col}=?')
                args.append(value)
        if max_price not in (None, ''):
            try:
                price = Decimal(str(max_price))
                if not price.is_finite() or price < 0:
                    raise ValueError('Invalid price')
            except InvalidOperation as e:
                raise ValueError('Enter a valid maximum price.') from e
            if not currency:
                raise ValueError('Choose a currency before filtering by price.')
            clauses.append('CAST(l.price AS REAL)<=?')
            args.append(float(price))
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connect() as c:
            total = c.execute('SELECT COUNT(*) FROM listings l' + where, args).fetchone()[0]
            rows = c.execute('SELECT l.*,s.name AS source_name FROM listings l JOIN sources s ON s.id=l.source_id' + where +
                             ' ORDER BY l.first_seen DESC,l.id DESC LIMIT ? OFFSET ?', args + [per_page, (max(1, page)-1)*per_page]).fetchall()
            return [dict(r) for r in rows], total

    def toggle_saved(self, id):
        with self.connect() as c:
            if not c.execute('UPDATE listings SET saved=1-saved WHERE id=?', (id,)).rowcount:
                raise ValueError('Listing not found')

    def get_search(self, id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM wanted_searches WHERE id=?', (id,)).fetchone()
        if row is None:
            raise ValueError('Wanted search not found')
        return with_web_query(dict(row))

    def list_searches(self, enabled_only=False):
        with self.connect() as c:
            where = ' WHERE enabled=1' if enabled_only else ''
            rows = [dict(r) for r in c.execute('SELECT * FROM wanted_searches' + where + ' ORDER BY web_checked_at,id')]
            for row in rows:
                c.create_function('wanted_match', 4, matcher(row), deterministic=True)
                row['match_count'] = c.execute("SELECT COUNT(*) FROM listings WHERE availability='available' AND wanted_match(title,category,currency,price)").fetchone()[0]
        return [with_web_query(row) for row in rows]

    def save_search(self, values, search_id=None):
        clean = validate_search(values)
        with self.connect() as c:
            if search_id is None:
                columns = ','.join(clean) + ',created_at'
                marks = ','.join('?' for _ in range(len(clean) + 1))
                return c.execute(f'INSERT INTO wanted_searches({columns}) VALUES({marks})',
                                 (*clean.values(), utcnow())).lastrowid
            old = c.execute('SELECT * FROM wanted_searches WHERE id=?', (search_id,)).fetchone()
            if old is None:
                raise ValueError('Wanted search not found')
            if any(clean[key] != old[key] for key in CRITERIA):
                c.execute('DELETE FROM search_web_results WHERE search_id=?', (search_id,))
                c.execute("UPDATE wanted_searches SET web_status='idle',web_error='',web_checked_at='' WHERE id=?", (search_id,))
            assignments = ','.join(f'{key}=?' for key in clean)
            c.execute(f'UPDATE wanted_searches SET {assignments} WHERE id=?', (*clean.values(), search_id))
        return search_id

    def set_search_enabled(self, id, enabled):
        with self.connect() as c:
            if not c.execute('UPDATE wanted_searches SET enabled=? WHERE id=?', (bool(enabled), id)).rowcount:
                raise ValueError('Wanted search not found')

    def delete_search(self, id):
        with self.connect() as c:
            if not c.execute('DELETE FROM wanted_searches WHERE id=?', (id,)).rowcount:
                raise ValueError('Wanted search not found')

    def search_matches(self, id, page=1, per_page=30):
        search = self.get_search(id)
        where = " WHERE l.availability='available' AND wanted_match(l.title,l.category,l.currency,l.price)"
        with self.connect() as c:
            c.create_function('wanted_match', 4, matcher(search), deterministic=True)
            total = c.execute('SELECT COUNT(*) FROM listings l' + where).fetchone()[0]
            rows = c.execute('SELECT l.*,s.name AS source_name FROM listings l JOIN sources s ON s.id=l.source_id' + where +
                             ' ORDER BY l.first_seen DESC,l.id DESC LIMIT ? OFFSET ?',
                             (per_page, (max(1, page) - 1) * per_page)).fetchall()
        return [dict(row) for row in rows], total

    def record_web_search(self, search_id, results=None, error=None, *, expected_query=None, run_id=None, merge=False):
        now = utcnow()
        # Validate the complete result set before changing the last successful snapshot.
        clean = {}
        if results is not None or error is None:
            for result in (results or [])[:1000]:
                url = public_url_shape(result['url'])
                parts = urlsplit(url)
                params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                          if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
                url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))
                sale_status = result.get('sale_status', 'unverified')
                if sale_status not in ('available', 'rejected', 'unverified'):
                    raise ValueError('Invalid web sale status')
                price, currency, checked = str(result.get('price') or ''), str(result.get('currency') or '').upper(), ''
                if result.get('sale_checked_at'):
                    date = datetime.fromisoformat(str(result['sale_checked_at']).replace('Z', '+00:00'))
                    if date.tzinfo is None:
                        raise ValueError('Sale checks need a time zone')
                    checked = date.astimezone(timezone.utc).isoformat(timespec='seconds')
                if sale_status == 'available':
                    try:
                        amount = Decimal(price)
                        if not amount.is_finite() or amount <= 0:
                            raise InvalidOperation
                    except InvalidOperation:
                        raise ValueError('An available web listing needs a positive fixed price') from None
                    if not re.fullmatch('[A-Z]{3}', currency) or not checked:
                        raise ValueError('An available web listing needs a currency and check time')
                clean.setdefault(url, (str(result.get('title') or url)[:500], str(result.get('snippet') or '')[:500],
                                       sale_status, price[:50], currency[:3], checked, str(result.get('sale_reason') or '')[:500]))
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if run_id is not None:
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(timespec='seconds')
                if not c.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND heartbeat>=?", (run_id, cutoff)).fetchone():
                    return False
            search = c.execute('SELECT * FROM wanted_searches WHERE id=?', (search_id,)).fetchone()
            if expected_query is not None and (search is None or not search['include_web'] or
                    with_web_query(dict(search))['web_query'] != expected_query):
                return False
            if search is None:
                raise ValueError('Wanted search not found')
            if results is not None or error is None:
                previous = {row['url']: dict(row) for row in c.execute('SELECT * FROM search_web_results WHERE search_id=? ORDER BY position', (search_id,))}
                combined = {url: (*values[:2], previous.get(url, {}).get('first_seen', now), now, *values[2:])
                            for url, values in clean.items()}
                if merge or error is not None:
                    for url, row in previous.items():
                        combined.setdefault(url, tuple(row[key] for key in ('title', 'snippet', 'first_seen', 'last_seen',
                                                                            'sale_status', 'price', 'currency', 'sale_checked_at', 'sale_reason')))
                c.execute('DELETE FROM search_web_results WHERE search_id=?', (search_id,))
                for position, (url, values) in enumerate(list(combined.items())[:1000]):
                    c.execute('INSERT INTO search_web_results(search_id,url,title,snippet,first_seen,last_seen,sale_status,price,currency,sale_checked_at,sale_reason,position) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                              (search_id, url, *values, position))
            status = 'complete' if error is None else ('partial' if results is not None else 'error')
            c.execute('UPDATE wanted_searches SET web_status=?,web_error=?,web_checked_at=? WHERE id=?',
                      (status, str(error or '')[:500], now, search_id))
        return True

    def search_results(self, search_id, *, verified_only=False):
        search = self.get_search(search_id)
        with self.connect() as c:
            where, args = 'search_id=?', [search_id]
            if verified_only:
                cutoff = (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat(timespec='seconds')
                where += " AND sale_status='available' AND sale_checked_at>=? AND sale_checked_at<=?"
                args.extend((cutoff, utcnow()))
            rows = [dict(r) for r in c.execute('SELECT * FROM search_web_results WHERE ' + where + ' ORDER BY position', args)]
        return dict(status=search['web_status'], results=rows, error=search['web_error'], checked_at=search['web_checked_at'])

    def stats(self):
        with self.connect() as c:
            row = c.execute("SELECT COUNT(*) AS total,COALESCE(SUM(is_new=1 AND availability='available'),0) AS new,COALESCE(SUM(saved),0) AS saved FROM listings").fetchone()
            result = dict(row)
            result['active_sources'] = c.execute('SELECT COUNT(*) FROM sources WHERE enabled=1').fetchone()[0]
        runs = self.runs(1)
        result['last_run'] = runs[0] if runs else None
        return result

    def runs(self, limit=20):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY started_at DESC,rowid DESC LIMIT ?', (limit,))]

    def claim_run(self, kind):
        now = utcnow()
        cutoff = (datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(timespec='seconds')
        id = uuid4().hex
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE runs SET status='interrupted',finished_at=?,summary='Previous process stopped unexpectedly.' WHERE status='running' AND heartbeat<?", (now, cutoff))
            if c.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                return None
            c.execute('INSERT INTO runs(id,kind,started_at,heartbeat,status) VALUES(?,?,?,?,?)', (id, kind, now, now, 'running'))
        return id

    def renew_lease(self, run_id):
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(timespec='seconds')
        with self.connect() as c:
            return c.execute("UPDATE runs SET heartbeat=? WHERE id=? AND status='running' AND heartbeat>=?", (utcnow(), run_id, cutoff)).rowcount > 0

    def finish_run(self, run_id, status, summary):
        with self.connect() as c:
            c.execute("UPDATE runs SET status=?,summary=?,finished_at=?,heartbeat=? WHERE id=? AND status='running'", (status, summary, utcnow(), utcnow(), run_id))

    def record_source_result(self, run_id, source, items, complete, pages, error):
        now, new_count = utcnow(), 0
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            cutoff = (datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat(timespec='seconds')
            if not c.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND heartbeat>=?", (run_id, cutoff)).fetchone():
                raise ValueError('The scan lease has expired or belongs to another run.')
            baseline = c.execute('SELECT baseline_complete FROM sources WHERE id=?', (source['id'],)).fetchone()[0]
            for item in items:
                key = item.external_id or item.url
                old = c.execute('SELECT * FROM listings WHERE source_id=? AND external_id=?', (source['id'], key)).fetchone()
                if old is None and not item.external_id:
                    old = c.execute('SELECT l.* FROM aliases a JOIN listings l ON l.id=a.listing_id WHERE a.source_id=? AND a.url=?', (source['id'], item.url)).fetchone()
                values = (item.url, item.title, str(item.price), item.currency, item.image_url, item.category, item.availability, item.listed_at, now)
                if old:
                    id = old['id']
                    c.execute('UPDATE listings SET url=?,title=?,price=?,currency=?,image_url=?,category=?,availability=?,listed_at=?,last_seen=? WHERE id=?', values + (id,))
                else:
                    new = int(bool(baseline))
                    new_count += new
                    id = c.execute('INSERT INTO listings(source_id,external_id,url,title,price,currency,image_url,category,availability,listed_at,last_seen,first_seen,is_new) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (source['id'], key) + values + (now, new)).lastrowid
                c.execute('INSERT OR IGNORE INTO aliases VALUES(?,?,?)', (source['id'], item.url, id))
                if not old or old['price'] != str(item.price) or old['availability'] != item.availability:
                    c.execute('INSERT INTO observations(listing_id,observed_at,price,availability) VALUES(?,?,?,?)', (id, now, str(item.price), item.availability))
            if complete and not error and not baseline:
                c.execute('UPDATE sources SET baseline_complete=1 WHERE id=?', (source['id'],))
                c.execute('UPDATE listings SET is_new=0 WHERE source_id=?', (source['id'],))
            c.execute('UPDATE sources SET last_attempt=?,last_error=?,last_count=?,last_pages=? WHERE id=?', (now, error or (None if complete else 'Partial coverage'), len(items), pages, source['id']))
            if complete and not error:
                c.execute('UPDATE sources SET last_success=? WHERE id=?', (now, source['id']))
            c.execute('INSERT INTO source_runs(run_id,source_id,checked_at,complete,pages,count,error) VALUES(?,?,?,?,?,?,?)', (run_id, source['id'], now, complete, pages, len(items), error))
        return {'seen': len(items), 'new': new_count}

    def known_domains(self):
        with self.connect() as c:
            urls = [r[0] for r in c.execute('SELECT url FROM sources')]
            domains = {r[0] for r in c.execute('SELECT domain FROM candidates')}
        supplied = Path(__file__).with_name('known-domains.json')
        if supplied.exists():
            domains.update(json.loads(supplied.read_text(encoding='utf-8-sig')))
        return domains | {urlsplit(u).hostname.removeprefix('www.').lower() for u in urls}

    def save_candidate(self, candidate, *, run_id=None):
        url = public_url_shape(candidate['url'])
        domain = urlsplit(url).hostname.removeprefix('www.').lower()
        with self.connect() as c:
            if run_id is not None:
                c.execute('BEGIN IMMEDIATE')
                cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(timespec='seconds')
                if not c.execute("SELECT 1 FROM runs WHERE id=? AND status='running' AND heartbeat>=?", (run_id, cutoff)).fetchone():
                    return False
            c.execute('''INSERT INTO candidates(domain,name,url,evidence_url,discovered_from,reason,created_at) VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(domain) DO UPDATE SET evidence_url=excluded.evidence_url,reason=excluded.reason
                      WHERE candidates.status='pending' AND excluded.evidence_url<>'' ''',
                      (domain, candidate.get('name') or domain, url, candidate.get('evidence_url', ''), candidate.get('discovered_from', ''), candidate.get('reason', ''), utcnow()))
        return True

    def list_candidates(self, status='pending'):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM candidates WHERE status=? ORDER BY created_at DESC,id DESC', (status,))]

    def decide_candidate(self, id, decision):
        if decision not in ('accepted', 'dismissed'):
            raise ValueError('Choose accept or dismiss.')
        with self.connect() as c:
            row = c.execute('SELECT * FROM candidates WHERE id=?', (id,)).fetchone()
        if not row:
            raise ValueError('Candidate not found')
        if decision == 'accepted':
            self.add_source(row['url'], row['name'])
        with self.connect() as c:
            c.execute('UPDATE candidates SET status=? WHERE id=?', (decision, id))

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path.resolve() or destination.exists():
            raise ValueError('Choose a new backup filename; existing files are not overwritten.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c, sqlite3.connect(destination) as target:
            c.backup(target)
        return destination
