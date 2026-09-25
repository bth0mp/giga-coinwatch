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
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
            ''')
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
        with self.connect() as c:
            return c.execute("UPDATE runs SET heartbeat=? WHERE id=? AND status='running'", (utcnow(), run_id)).rowcount > 0

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

    def save_candidate(self, candidate):
        url = public_url_shape(candidate['url'])
        domain = urlsplit(url).hostname.removeprefix('www.').lower()
        with self.connect() as c:
            c.execute('''INSERT INTO candidates(domain,name,url,evidence_url,discovered_from,reason,created_at) VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(domain) DO UPDATE SET evidence_url=excluded.evidence_url,reason=excluded.reason
                      WHERE candidates.status='pending' AND excluded.evidence_url<>'' ''',
                      (domain, candidate.get('name') or domain, url, candidate.get('evidence_url', ''), candidate.get('discovered_from', ''), candidate.get('reason', ''), utcnow()))

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
