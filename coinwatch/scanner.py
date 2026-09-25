import logging
import threading
from urllib.parse import urlsplit

from .fetch import Fetcher

log = logging.getLogger(__name__)


def normalize_scan_mode(mode, include_discovery=True):
    if mode not in ('coins', 'dealers', 'both'):
        raise ValueError('Choose a scan mode: coins, dealers, or both.')
    if not include_discovery:
        if mode == 'dealers':
            raise ValueError('Dealer scanning cannot be combined with --no-discovery.')
        return 'coins'
    return mode


def get_scan_search(db, mode, search_id):
    if search_id is None:
        return None
    if mode == 'dealers':
        raise ValueError('A wanted search requires a coin or combined scan.')
    return db.get_search(search_id)


def validate_web_queries(web_queries, kind, selected_search):
    if type(web_queries) is not int or not 1 <= web_queries <= 50:
        raise ValueError('Choose between 1 and 50 web queries.')
    if web_queries > 1 and (kind != 'manual' or not selected_search or not selected_search['include_web']):
        raise ValueError('Multiple web queries require a manual scan of one wanted search with wider-web search enabled.')


class Scanner:
    def __init__(self, db, scrape=None, progress=None, stop_event=None):
        self.db = db
        self.scrape = scrape
        self.progress = progress or (lambda **kw: None)
        self.stop_event = stop_event or threading.Event()
        self.run_id = None

    def _scan_web(self, searches, errors, notes, query_limit=1):
        from .searches import web_query_variants
        from .web_search import WebSearchError, load_api_key, search_web
        searches = [search for search in searches if search['include_web']]
        if not searches:
            return 0, 0
        try:
            api_key = load_api_key(self.db.path.parent)
        except WebSearchError as e:
            errors.append(f'Wider web search: {e}')
            return 0, 0
        if not api_key:
            notes.append('Wider web search skipped: configure a Tavily API key in Settings to enable it.')
            return 0, 0
        if len(searches) > 5:
            notes.append(f'Wider web search is limited to five wanted searches per scan; {len(searches) - 5} remain for a later scan.')
        queries, leads = 0, 0
        for search in searches[:5]:
            if self.stop_event.is_set():
                break
            variants = web_query_variants(search, query_limit) if query_limit > 1 else [search['web_query']]
            gathered, search_errors, successful, saved_count = {}, [], False, 0
            for index, query in enumerate(variants):
                if self.stop_event.is_set() or not self.db.renew_lease(self.run_id):
                    self.stop_event.set()
                    break
                try:
                    current = self.db.get_search(search['id'])
                except ValueError:
                    current = None
                if not current or not current['include_web'] or current['web_query'] != search['web_query']:
                    notes.append(f"Wanted search \"{search['name']}\" changed or was removed; remaining web queries were skipped.")
                    break
                self.progress(phase=f'Searching the wider web ({index + 1}/{len(variants)})', source=search['name'])
                options = {'max_results': 20}
                if index >= 5:
                    hosts = (urlsplit(url).hostname for url in gathered)
                    options['exclude_domains'] = list(dict.fromkeys(
                        host.lower().removeprefix('www.') for host in hosts
                        if host and '.' in host and not host.rsplit('.', 1)[-1].isdigit()))[:150]
                queries += 1
                error, halt_broad_scan = None, False
                try:
                    results = search_web(query, api_key, **options)
                except WebSearchError as e:
                    error = str(e)
                    halt_broad_scan = query_limit > 1
                except Exception as e:
                    # Transport exceptions can embed authorization headers or keys.
                    log.warning('Web search failed for wanted search %s (%s)', search['id'], type(e).__name__)
                    error = 'Wider web search failed. Please try again later.'
                else:
                    successful = True
                    for result in results:
                        gathered.setdefault(result['url'], result)
                if self.stop_event.is_set():
                    break
                if error:
                    search_errors.append(error)
                recorded = self.db.record_web_search(
                    search['id'], list(gathered.values()) if successful else None,
                    '; '.join(search_errors) or None, expected_query=search['web_query'], run_id=self.run_id, merge=True)
                if not recorded:
                    if not self.db.renew_lease(self.run_id):
                        self.stop_event.set()
                    else:
                        notes.append(f"Wanted search \"{search['name']}\" changed or was removed; its outdated web response was discarded.")
                    break
                leads += len(gathered) - saved_count
                saved_count = len(gathered)
                if error:
                    errors.append(f"{search['name']}: {error}")
                if halt_broad_scan:
                    notes.append('Remaining broad-search queries were skipped after the provider error.')
                    break
        return queries, leads

    def _scan_dealers(self, fetcher, errors, notes):
        from .discovery import DEALER_QUERIES, DiscoveryError, discover, discover_web
        from .web_search import WebSearchError, load_api_key

        known = self.db.known_domains()
        try:
            api_key = load_api_key(self.db.path.parent)
        except WebSearchError as exc:
            errors.append(f'Web dealer discovery: {exc}')
            api_key = ''
        try:
            query_index = int(self.db.settings().get('dealer_query_index', '0')) % len(DEALER_QUERIES)
        except ValueError:
            query_index = 0
        count = 0
        checks = []
        if api_key:
            checks.append(('Tavily dealer search', lambda: discover_web(fetcher, known, api_key, query_index=query_index)))
            notes.append('Dealer discovery: Tavily (up to two basic searches) and the public directory.')
        else:
            notes.append('Dealer discovery: public directory only; add a Tavily API key in Settings for wider-web discovery.')
        checks.append(('Dealer directory', lambda: discover(fetcher, known, limit=20-count)))
        for label, check in checks:
            if self.stop_event.is_set():
                break
            self.progress(phase='Finding dealers', source=label)
            try:
                found = check()
            except DiscoveryError as exc:
                found = exc.candidates
                errors.append(f'{label}: {exc}')
            except Exception:
                found = []
                errors.append(f'{label}: could not complete this check. Please try again later.')
            if self.stop_event.is_set() or not self.db.renew_lease(self.run_id):
                self.stop_event.set()
                break
            if label == 'Tavily dealer search':
                self.db.set_internal('dealer_query_index', (query_index + 2) % len(DEALER_QUERIES))
            for candidate in found:
                if self.stop_event.is_set():
                    break
                try:
                    if not self.db.save_candidate(candidate, run_id=self.run_id):
                        self.stop_event.set()
                        break
                    known.add(urlsplit(candidate['url']).hostname.lower().removeprefix('www.'))
                    count += 1
                except Exception as exc:
                    errors.append(f'Candidate skipped: {exc}')
        return count

    def run(self, kind='manual', source_ids=None, include_discovery=True, mode='both', search_id=None, web_queries=1):
        mode = normalize_scan_mode(mode, include_discovery)
        selected_search = get_scan_search(self.db, mode, search_id)
        validate_web_queries(web_queries, kind, selected_search)
        query_limit = web_queries
        run_kind = f'manual-{mode}' if kind == 'manual' else kind
        run_id = self.db.claim_run(run_kind)
        self.run_id = run_id
        if not run_id:
            return {'status': 'busy', 'mode': mode, 'search_id': search_id, 'summary': 'A scan is already running.'}
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(20):
                if not self.db.renew_lease(run_id):
                    self.stop_event.set()
                    break
        thread = threading.Thread(target=heartbeat, name='scan-lease', daemon=True)
        thread.start()
        errors, notes, seen, new, candidates = [], [], 0, 0, 0
        web_queries, web_leads, matches = 0, 0, None
        status = 'complete'
        try:
            if self.scrape:
                scrape = self.scrape
            else:
                from .sources import scrape_source
                scrape = scrape_source
            fetcher = Fetcher(stop_event=self.stop_event)
            sources = self.db.list_sources() if mode in ('coins', 'both') else []
            for source in sources:
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
            if mode in ('coins', 'both') and not self.stop_event.is_set():
                searches = [selected_search] if selected_search else self.db.list_searches(enabled_only=True)
                web_queries, web_leads = self._scan_web(searches, errors, notes, query_limit=query_limit)
                if selected_search:
                    try:
                        current_search = self.db.get_search(selected_search['id'])
                        _, matches = self.db.search_matches(selected_search['id'], per_page=1)
                    except ValueError:
                        notes.append('The selected wanted search was removed during this scan.')
                    else:
                        notes.append(f"Wanted search \"{current_search['name']}\": {matches} matching catalog listings.")
            if mode in ('dealers', 'both') and not self.stop_event.is_set():
                candidates = self._scan_dealers(fetcher, errors, notes)
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
            if mode == 'coins':
                summary = f'Coin scan: {seen} listings checked; {new} newly found.'
            elif mode == 'dealers':
                summary = f'Dealer scan: {candidates} dealer candidates.'
            else:
                summary = f'Combined scan: {seen} listings checked; {new} newly found; {candidates} dealer candidates.'
            if web_queries:
                notes.append(f'Wider web: {web_queries} searches attempted; {web_leads} potential matches saved.')
            if notes:
                summary += '\n' + '\n'.join(notes)
            if errors:
                summary += '\n' + '\n'.join(errors)
            self.db.finish_run(run_id, status, summary)
            self.progress(phase='Idle', source='', last_error='\n'.join(errors))
        return {'status': status, 'mode': mode, 'search_id': search_id, 'summary': summary, 'seen': seen, 'new': new,
                'candidates': candidates, 'matches': matches, 'web_queries': web_queries, 'web_leads': web_leads, 'errors': errors}
