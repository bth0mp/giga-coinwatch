import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import sys

from .config import data_directory
from .db import Database


class InstanceLock:
    def __init__(self, directory):
        self.path = Path(directory)/'instance.lock'
        self.file = None

    def __enter__(self):
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            raise ValueError('giga-coinwatch is already running for this data directory. Stop it first.') from e
        return self

    def __exit__(self, *args):
        self.file.close()


def configure_logging(directory):
    handlers = [RotatingFileHandler(directory/'coinwatch.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    else:
        # pythonw has no console streams; libraries still expect file-like streams.
        sys.stderr = open(os.devnull, 'w')
        sys.stdout = open(os.devnull, 'w')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s', handlers=handlers)


def main():
    parser = argparse.ArgumentParser(description='giga-coinwatch ancient-coin monitor')
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Start dashboard and background scheduler')
    run.add_argument('--host', default='127.0.0.1')
    run.add_argument('--port', type=int, default=8000)
    scan = commands.add_parser('scan', help='Scan coin listings, discover dealers, or do both')
    scan.add_argument('--mode', choices=('coins', 'dealers', 'both'), default='both', help='Work to run (default: both)')
    scan.add_argument('--no-discovery', action='store_true', help='Legacy alias for --mode coins')
    scan.add_argument('--source', action='append')
    scan.add_argument('--search-id', type=int, help='Run a saved wanted search by its positive ID')
    scan.add_argument('--web-queries', type=int, default=1, help='Web queries for one wanted search, from 1 to 50 (default: 1)')
    commands.add_parser('status')
    commands.add_parser('backup').add_argument('destination', type=Path)
    commands.add_parser('restore').add_argument('source', type=Path)
    args = parser.parse_args()
    if args.command == 'scan' and args.no_discovery and args.mode == 'dealers':
        parser.error('--mode dealers cannot be combined with --no-discovery')
    if args.command == 'scan' and args.search_id is not None:
        if args.search_id <= 0:
            parser.error('--search-id must be a positive integer')
        if args.mode == 'dealers':
            parser.error('--search-id requires --mode coins or --mode both')
    if args.command == 'scan':
        if not 1 <= args.web_queries <= 50:
            parser.error('--web-queries must be between 1 and 50')
        if args.web_queries > 1 and args.search_id is None:
            parser.error('--web-queries above 1 requires --search-id')
    directory = data_directory(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    configure_logging(directory)
    db = Database(directory/'catalog.db')
    try:
        if args.command == 'restore':
            with InstanceLock(directory):
                if not args.source.is_file() or args.source.resolve() == db.path.resolve():
                    raise ValueError('Choose an existing giga-coinwatch backup from a different path.')
                with sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro', uri=True) as source:
                    if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('The backup did not pass its integrity check.')
                    tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {'listings','sources','settings','candidates','runs'}.issubset(tables):
                        raise ValueError('This file is not a giga-coinwatch catalog backup.')
                    if db.path.exists():
                        from datetime import datetime
                        before = directory/f'before-restore-{datetime.now():%Y%m%d-%H%M%S-%f}.db'
                        db.backup(before)
                        print(f'Previous catalog preserved at {before}')
                    with sqlite3.connect(db.path) as target:
                        source.backup(target)
                db.initialize()
                with db.connect() as conn:
                    conn.execute("UPDATE runs SET status='interrupted',finished_at=heartbeat WHERE status='running'")
                print(f'Restored catalog to {db.path}')
            return 0
        db.initialize()
        if args.command == 'backup':
            print(db.backup(args.destination))
        elif args.command == 'status':
            from .runtime import schedule_state
            print(json.dumps({'data_directory': str(directory), 'stats': db.stats(), 'schedule': schedule_state(db.settings()), 'runs': db.runs(3)}, indent=2))
        elif args.command == 'scan':
            from .scanner import Scanner
            with InstanceLock(directory):
                result = Scanner(db).run(source_ids=args.source, include_discovery=not args.no_discovery, mode=args.mode, search_id=args.search_id, web_queries=args.web_queries)
            print(json.dumps(result, indent=2))
            return 1 if result['status'] in ('failed','busy') else 0
        else:
            import uvicorn
            from .runtime import Runtime
            from .web import create_app
            with InstanceLock(directory):
                # Fail on an occupied port before starting the scheduler in lifespan.
                import socket
                probe = socket.socket()
                try:
                    probe.bind((args.host, args.port))
                finally:
                    probe.close()
                uvicorn.run(create_app(db, Runtime(db)), host=args.host, port=args.port, workers=1, log_config=None, access_log=False)
        return 0
    except (ValueError, OSError, sqlite3.Error) as e:
        logging.getLogger(__name__).error('%s', e)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
