import logging
import threading

from .fetch import Fetcher

log = logging.getLogger(__name__)


class Scanner:
    def __init__(self, db, scrape=None, progress=None, stop_event=None):
        self.db = db
        self.scrape = scrape
        self.progress = progress or (lambda **kw: None)
        self.stop_event = stop_event or threading.Event()
        self.run_id = None

    def run(self, kind='manual', source_ids=None, include_discovery=True):
        run_id = self.db.claim_run(kind)
        self.run_id = run_id
        if not run_id:
            return {'status': 'busy', 'summary': 'A scan is already running.'}
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(20):
                if not self.db.renew_lease(run_id):
                    self.stop_event.set()
                    break
        thread = threading.Thread(target=heartbeat, name='scan-lease', daemon=True)
        thread.start()
        errors, seen, new, candidates = [], 0, 0, 0
        status = 'complete'
        try:
            if self.scrape:
                scrape = self.scrape
            else:
                from .sources import scrape_source
                scrape = scrape_source
            fetcher = Fetcher(stop_event=self.stop_event)
            for source in self.db.list_sources():
                if not source['enabled'] or (source_ids and source['id'] not in source_ids):
                    continue
                if self.stop_event.is_set():
                    break
                self.progress(phase='Checking listings', source=source['name'])
                try:
                    result = scrape(source, fetcher)
                    counts = self.db.record_source_result(run_id, source, result.listings, result.complete, result.pages, result.error)
                    seen += counts['seen']
                    new += counts['new']
                    if not result.complete or result.error:
                        errors.append(f"{source['name']}: {result.error or 'partial coverage'}")
                except Exception as e:
                    log.exception('Source scan failed: %s', source['name'])
                    self.db.record_source_result(run_id, source, [], False, 0, str(e)[:700])
                    errors.append(f"{source['name']}: {e}")
            if include_discovery and not self.stop_event.is_set():
                self.progress(phase='Finding dealers', source='Dealer directories')
                try:
                    from .discovery import discover
                    found = discover(fetcher, self.db.known_domains(), limit=20)
                except Exception as e:
                    found = getattr(e, 'candidates', [])
                    errors.append(f'Dealer discovery: {e}')
                    log.warning('Discovery partial: %s', e)
                for candidate in found:
                    try:
                        self.db.save_candidate(candidate)
                        candidates += 1
                    except Exception as e:
                        errors.append(f'Candidate skipped: {e}')
            if self.stop_event.is_set():
                status = 'interrupted'
            elif errors:
                status = 'partial'
        except KeyboardInterrupt:
            self.stop_event.set()
            status = 'interrupted'
        except Exception as e:
            log.exception('Scan failed')
            errors.append(str(e))
            status = 'failed'
        finally:
            heartbeat_stop.set()
            thread.join(timeout=2)
            summary = f'{seen} listings checked; {new} newly found; {candidates} dealer candidates.'
            if errors:
                summary += '\n' + '\n'.join(errors)
            self.db.finish_run(run_id, status, summary)
            self.progress(phase='Idle', source='', last_error='\n'.join(errors))
        return {'status': status, 'summary': summary, 'seen': seen, 'new': new, 'candidates': candidates, 'errors': errors}
