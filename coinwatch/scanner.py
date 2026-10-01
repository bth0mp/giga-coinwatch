import logging
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from .fetch import Fetcher

log = logging.getLogger(__name__)
DEEP_CATALOG_PAGES = 80
DEEP_PAGES_PER_DEALER = 20
DEEP_PRODUCT_LINKS = 200


def _dealer_host(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def _fair_rows(rows):
    """Give each dealer a turn before returning to its other candidate pages."""
    dealers = {}
    for row in rows:
        dealers.setdefault(_dealer_host(row['url']), deque()).append(row)
    while dealers:
        for host in list(dealers):
            yield dealers[host].popleft()
            if not dealers[host]:
                del dealers[host]


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


def validate_web_minutes(web_minutes, kind, selected_search):
    if type(web_minutes) is not int or not 1 <= web_minutes <= 120:
        raise ValueError('Choose between 1 and 120 web minutes.')
    if web_minutes != 10 and (kind != 'manual' or not selected_search or not selected_search['include_web']):
        raise ValueError('A custom web time limit requires a manual scan of one wanted search with wider-web search enabled.')


def validate_web_depth(web_depth, kind, selected_search, mode='coins'):
    if web_depth not in ('basic', 'advanced'):
        raise ValueError('Choose a web search depth: basic or advanced.')
    if web_depth == 'advanced' and (kind != 'manual' or not selected_search or not selected_search['include_web']):
        raise ValueError('Deep web search requires a manual scan of one wanted search with wider-web search enabled.')
    if web_depth == 'advanced' and mode != 'coins':
        raise ValueError('Deep web search requires coins mode; run dealer discovery separately.')


class _PageCapture:
    """Reuse the verifier's one fetched page without a second seller request."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.page = None

    def get(self, url):
        self.page = self.delegate.get(url)
        return self.page


class Scanner:
    def __init__(self, db, scrape=None, progress=None, stop_event=None):
        self.db = db
        self.scrape = scrape
        self.progress = progress or (lambda **kw: None)
        self.stop_event = stop_event or threading.Event()
        self.run_id = None

    def _scan_web(self, searches, errors, notes, query_limit=1, web_minutes=10, web_depth='basic'):
        from .catalog_links import catalog_links, is_catalog_url, same_dealer
        from .sale_checks import individual_ancient_coin_title, verify_sale
        from .searches import CRITERIA, deep_web_query_variants, web_matcher, web_query_variants
        from .web_search import WebSearchError, load_api_key, search_web
        searches = [search for search in searches if search['include_web']]
        if not searches:
            return 0, 0
        try:
            api_key = load_api_key(self.db.path.parent)
        except WebSearchError as e:
            errors.append(f'Wider web search: {e}')
            api_key = ''
        if not api_key:
            notes.append('New wider-web queries skipped: configure a Tavily API key in Settings to enable them. Retained listings can still be checked.')
        if len(searches) > 5:
            notes.append(f'Wider web search is limited to five wanted searches per scan; {len(searches) - 5} remain for a later scan.')
        sale_fetcher = Fetcher(stop_event=self.stop_event, budget=web_minutes * 60)
        cache, stored, changed = {}, {}, set()
        queries, budget_reached = 0, False
        deep = web_depth == 'advanced'
        catalog_pages, product_links, queued_catalogs = set(), set(), set()
        pages_per_dealer = Counter()
        sources = self.db.list_sources()
        restricted = {_dealer_host(source['url']) for source in sources
                      if source.get('support_status') in ('permission_required', 'login_required')}
        if deep:
            sale_fetcher.blocked_hosts = restricted
        budget_message = f'Wanted-sale verification reached its {web_minutes}-minute budget; remaining wanted-search queries and page checks were skipped.'

        def restricted_url(url):
            host = _dealer_host(url)
            return any(host == domain or host.endswith('.' + domain) for domain in restricted)

        dealer_hosts = list(dict.fromkeys(_dealer_host(source['url']) for source in sources
                            if source.get('support_status') in ('ready', 'needs_adapter')
                            and not restricted_url(source['url'])))
        dealer_batches = [dealer_hosts[i:i + 8] for i in range(0, len(dealer_hosts), 8)]

        def catalog_room(url):
            return (len(catalog_pages) < DEEP_CATALOG_PAGES
                    and pages_per_dealer[_dealer_host(url)] < DEEP_PAGES_PER_DEALER)

        def remember(state, rows):
            # Paid leads survive a deadline and remain unverified until checked.
            for row in rows:
                if row['url'] not in state['gathered'] and row['url'] not in state['retained']:
                    state['pending'].setdefault(row['url'], dict(row, sale_status='unverified',
                        sale_checked_at='', price='', currency='', sale_reason='Awaiting a sale check from this Deep search.'))
            state.update(successful=True, dirty=True)

        def expand(state, page, original_url):
            if (not page or not catalog_room(page.url) or page.url in catalog_pages
                    or restricted_url(page.url) or not same_dealer(original_url, page.url)):
                return
            links = catalog_links(page, state['search'])
            if (not links['products'] and not links['next_pages']
                    and not is_catalog_url(page.url) and original_url not in queued_catalogs):
                return
            catalog_pages.add(page.url)
            pages_per_dealer[_dealer_host(page.url)] += 1
            for row in links['products']:
                url = row['url']
                if len(product_links) >= DEEP_PRODUCT_LINKS:
                    break
                if url in product_links or url in state['gathered'] or restricted_url(url):
                    continue
                product_links.add(url)
                remember(state, [row])
                state['product_queue'].append(row)
            for url in links['next_pages']:
                if (not catalog_room(url) or url in catalog_pages or url in queued_catalogs
                        or restricted_url(url)):
                    continue
                queued_catalogs.add(url)
                row = dict(url=url, title='Dealer catalog page', snippet='Catalog pagination found during Deep search')
                remember(state, [row])
                state['catalog_queue'].append(row)

        def retry_category(row):
            return (deep and catalog_room(row['url']) and is_catalog_url(row['url'])
                    and any(text in row.get('sale_reason', '') for text in (
                        'single primary product', 'individual product identity', 'individual ancient coin',
                        'separate product cards', 'catalog or price range')))

        def retry_product(state, row):
            # A deliberate Deep run can recheck readable parser failures after fixes.
            # Access failures, sold stock and mismatched titles keep their cooldowns.
            return (deep and not is_catalog_url(row['url'])
                    and individual_ancient_coin_title(row.get('title', ''))
                    and state['candidate_matches'](row)
                    and any(text in row.get('sale_reason', '') for text in (
                        'single primary product', 'individual product identity',
                        'individual ancient coin', 'fixed price could not be verified')))

        def can_work(search):
            if self.stop_event.is_set() or not self.db.renew_lease(self.run_id):
                self.stop_event.set()
                return False
            try:
                current = self.db.get_search(search['id'])
            except ValueError:
                current = None
            if (not current or not current['include_web'] or current['web_query'] != search['web_query']
                    or any(current[key] != search[key] for key in CRITERIA)):
                if search['id'] not in changed:
                    notes.append(f"Wanted search \"{search['name']}\" changed or was removed; remaining web work was skipped.")
                    changed.add(search['id'])
                return False
            return True

        def has_budget(state):
            nonlocal budget_reached
            if time.monotonic() < sale_fetcher.deadline:
                return True
            if not budget_reached:
                errors.append(budget_message)
                budget_reached = True
            state['dirty'] = True
            return False

        def cooling_down(row):
            if row.get('sale_status') == 'unverified' and row.get('sale_reason', '').startswith((
                    'Web-check time limit reached;', 'Sale check was cancelled;')):
                return False
            days = {'unverified': 1, 'rejected': 7}.get(row.get('sale_status'))
            # Access restrictions and non-product pages rarely change overnight.
            # Leave daily time for new leads and temporary failures; a deliberate
            # Deep run can still revisit readable parser failures via retry_product.
            if row.get('sale_status') == 'unverified' and any(text in row.get('sale_reason', '') for text in (
                    'HTTP 401', 'HTTP 403', 'robots.txt disallows', 'requires a browser challenge',
                    'requires permission or login', 'single primary product could not be identified',
                    'does not establish an individual product identity', 'contains separate product cards')):
                days = 7
            if not days or not row.get('sale_checked_at'):
                return False
            try:
                checked = datetime.fromisoformat(row['sale_checked_at'].replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                return now - timedelta(days=days) < checked <= now
            except (TypeError, ValueError):
                return False

        def assess(state, row):
            search = state['search']
            if not can_work(search) or not has_budget(state):
                return False
            url = row['url']
            if url in state['gathered']:
                return True
            if url not in cache:
                if deep and (is_catalog_url(url) or url in queued_catalogs) and not catalog_room(url):
                    return True
                previous = state['retained'].get(url, {})
                if cooling_down(previous) and not (retry_category(previous) or retry_product(state, previous)):
                    state['skipped'].add(url)
                    return True
                self.progress(phase='Checking sale listings', source=search['name'])
                if deep:
                    capture = _PageCapture(sale_fetcher)
                    if restricted_url(url):
                        cache[url] = dict(row, sale_status='unverified', price='', currency='',
                            sale_checked_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                            sale_reason='This dealer requires permission or login before automated checks.')
                    else:
                        cache[url] = verify_sale(row, capture)
                        if cache[url]['sale_status'] != 'available':
                            expand(state, capture.page, url)
                else:
                    cache[url] = verify_sale(row, sale_fetcher)
                has_budget(state)
            checked = dict(cache[url])
            if checked['sale_status'] == 'available' and not state['matches'](checked):
                checked.update(sale_status='rejected', price='', currency='',
                               sale_reason='The verified seller title or offer does not match this wanted search\'s criteria.')
            state['gathered'][url] = checked
            state['pending'].pop(url, None)
            state.update(successful=True, dirty=True)
            return True

        def checkpoint(state):
            search = state['search']
            if not can_work(search):
                return False
            if not state['dirty']:
                return True
            current_errors = state['errors'] or ([state['prior_error']] if not state['query_succeeded'] and state['prior_error'] else [])
            message = '; '.join(current_errors + ([budget_message] if budget_reached else [])) or None
            rows = list(state['gathered'].values())
            if deep:
                # Keep known available offers ahead of other retained candidates.
                rows = ([row for url, row in state['retained'].items()
                         if row['sale_status'] == 'available' and url not in state['gathered']]
                        + sorted(rows, key=lambda row: row['sale_status'] != 'available')
                        + list(state['pending'].values()))
            recorded = self.db.record_web_search(
                search['id'], rows if state['successful'] else None,
                message, expected_query=search['web_query'], expected_search=search, run_id=self.run_id, merge=True)
            if not recorded:
                can_work(search)
                return False
            stored.update({(search['id'], url): row for url, row in state['gathered'].items()})
            state['dirty'] = False
            return True

        def check_rows(state, rows, *, explore=True):
            for index, row in enumerate(rows):
                if not assess(state, row):
                    break
                if deep and explore:
                    # Check products promptly; pagination waits until other roots get a turn.
                    while state['product_queue']:
                        child = state['product_queue'].popleft()
                        if not assess(state, child):
                            break
                        if len(state['gathered']) % 20 == 0 and not checkpoint(state):
                            break
                if (index + 1) % 20 == 0 and not checkpoint(state):
                    break
            checkpoint(state)

        states = []
        for search in searches[:5]:
            if not can_work(search):
                continue
            try:
                previous = self.db.search_results(search['id'])
            except ValueError:
                continue
            states.append(dict(search=search, retained={row['url']: row for row in previous['results']},
                               matches=web_matcher(search),
                               candidate_matches=web_matcher({**search, 'currency': '', 'max_price': ''}),
                               gathered={}, pending={}, product_queue=deque(), catalog_queue=deque(), skipped=set(), errors=[],
                               successful=False, dirty=False, query_succeeded=False, prior_error=previous['error']))

        # Refresh known matches for every selected watch before spending time on new candidates.
        for state in states:
            if self.stop_event.is_set() or budget_reached:
                break
            priority = [row for row in state['retained'].values()
                        if row['sale_status'] == 'available' and state['matches'](row)]
            check_rows(state, sorted(priority, key=lambda row: row.get('sale_checked_at') or ''), explore=False)

        for state in states:
            if self.stop_event.is_set() or budget_reached:
                break
            search = state['search']
            variants = (deep_web_query_variants(search, query_limit) if deep else
                        (web_query_variants(search, query_limit) if query_limit > 1 else [search['web_query']])) if api_key else []
            for index, query in enumerate(variants):
                if not can_work(search) or not has_budget(state):
                    break
                targeted = deep and dealer_batches and index % 3 == 2
                scope = 'dealer groups' if targeted else 'the wider web'
                self.progress(phase=f'Searching {scope} ({index + 1}/{len(variants)})', source=search['name'])
                queries += 1
                error, halt_broad_scan = None, False
                try:
                    options = {'search_depth': 'advanced'} if deep else {}
                    if targeted:
                        options['include_domains'] = dealer_batches[(index // 3) % len(dealer_batches)]
                    if deep and restricted:
                        options['exclude_domains'] = sorted(restricted)
                    results = search_web(query, api_key, max_results=20, **options)
                except WebSearchError as e:
                    error = str(e)
                    halt_broad_scan = query_limit > 1
                except Exception as e:
                    # Transport exceptions can embed authorization headers or keys.
                    log.warning('Web search failed for wanted search %s (%s)', search['id'], type(e).__name__)
                    error = 'Wider web search failed. Please try again later.'
                else:
                    state.update(successful=True, dirty=True, query_succeeded=True)
                    if deep:
                        remember(state, results)
                        has_budget(state)
                    else:
                        for result in results:
                            if not assess(state, result):
                                break
                if error:
                    state['errors'].append(error)
                    state['dirty'] = True
                if not checkpoint(state):
                    break
                if error:
                    errors.append(f"{search['name']}: {error}")
                if halt_broad_scan:
                    notes.append('Remaining broad-search queries were skipped after the provider error.')
                    break
                if budget_reached:
                    break

            if deep:
                categories = [row for row in state['retained'].values() if retry_category(row)]
                recoverable = [row for row in state['retained'].values() if retry_product(state, row)]
                roots = list(state['pending'].values()) + categories + recoverable
                check_rows(state, _fair_rows(sorted(roots, key=lambda row: not is_catalog_url(row['url']))))
            remaining = [row for url, row in state['retained'].items() if url not in state['gathered']]
            check_rows(state, sorted(remaining, key=lambda row:
                       (row.get('sale_status') == 'rejected', row.get('sale_checked_at') or '')))
            if deep:
                # Breadth-first pagination: pages discovered here go behind other dealers.
                while state['catalog_queue'] and not budget_reached and not self.stop_event.is_set():
                    if not can_work(search):
                        break
                    check_rows(state, [state['catalog_queue'].popleft()])
        if deep:
            notes.append(f'Deep web search: {queries} advanced queries attempted (up to {queries * 2} Tavily credits); '
                         f'{len(catalog_pages)} catalog pages explored; {len(product_links)} product links found. '
                         f'Limits: {DEEP_CATALOG_PAGES} catalog pages, up to {DEEP_PAGES_PER_DEALER} per dealer, '
                         f'and {DEEP_PRODUCT_LINKS} extra product links, within the selected time limit.')
        skipped = sum(len(state['skipped']) for state in states)
        if skipped:
            notes.append(f'Sale checks: {skipped} recent unsuccessful candidates skipped during their retry cooldown.')
        if stored:
            rejected = len({row['url'] for row in stored.values() if row['sale_status'] == 'rejected'})
            unverified = len({row['url'] for row in stored.values() if row['sale_status'] == 'unverified'})
            notes.append(f'Sale checks: {rejected} rejected; {unverified} unverified.')
        return queries, len({row['url'] for row in stored.values() if row['sale_status'] == 'available'})

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

    def run(self, kind='manual', source_ids=None, include_discovery=True, mode='both', search_id=None, web_queries=1, web_minutes=10, web_depth='basic'):
        mode = normalize_scan_mode(mode, include_discovery)
        selected_search = get_scan_search(self.db, mode, search_id)
        validate_web_queries(web_queries, kind, selected_search)
        validate_web_minutes(web_minutes, kind, selected_search)
        validate_web_depth(web_depth, kind, selected_search, mode)
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
            sources = self.db.list_sources() if mode in ('coins', 'both') and web_depth == 'basic' else []
            sources = [source for source in sources if source['enabled']
                       and (not source_ids or source['id'] in source_ids)]
            catalog_deadline = fetcher.deadline
            reserved_seconds = min(30, max(0, catalog_deadline - time.monotonic()) / len(sources)) if sources else 0
            for index, source in enumerate(sources):
                if self.stop_event.is_set():
                    break
                # One slow shop must not consume the time reserved for later shops.
                # Keep the total budget and the shared robots/rate-limit state intact.
                now = time.monotonic()
                remaining_sources = len(sources) - index - 1
                allowance = max(0, catalog_deadline - now - reserved_seconds * remaining_sources)
                if len(sources) > 1:
                    allowance = min(180, allowance)
                fetcher.deadline = min(catalog_deadline, now + allowance)
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
                web_queries, web_leads = self._scan_web(searches, errors, notes, query_limit=query_limit, web_minutes=web_minutes, web_depth=web_depth)
                if selected_search:
                    try:
                        current_search = self.db.get_search(selected_search['id'])
                        _, matches = self.db.search_matches(selected_search['id'], per_page=1)
                    except ValueError:
                        notes.append('The selected wanted search was removed during this scan.')
                    else:
                        notes.append(f"Wanted search \"{current_search['name']}\": {matches} matching catalog listings.")
            if mode in ('dealers', 'both') and not self.stop_event.is_set():
                candidates = self._scan_dealers(Fetcher(stop_event=self.stop_event), errors, notes)
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
            if mode == 'coins' and web_depth == 'advanced':
                summary = 'Deep web search; monitored catalog matches were read from the existing catalog.'
            elif mode == 'coins':
                summary = f'Coin scan: {seen} listings checked; {new} newly found.'
            elif mode == 'dealers':
                summary = f'Dealer scan: {candidates} dealer candidates.'
            else:
                summary = f'Combined scan: {seen} listings checked; {new} newly found; {candidates} dealer candidates.'
            if web_queries or web_leads:
                notes.append(f'Wider web: {web_queries} searches attempted; {web_leads} fixed-price listings verified available.')
            if notes:
                summary += '\n' + '\n'.join(notes)
            if errors:
                summary += '\n' + '\n'.join(errors)
            self.db.finish_run(run_id, status, summary)
            self.progress(phase='Idle', source='', last_error='\n'.join(errors))
        return {'status': status, 'mode': mode, 'search_id': search_id, 'web_depth': web_depth, 'summary': summary, 'seen': seen, 'new': new,
                'candidates': candidates, 'matches': matches, 'web_queries': web_queries, 'web_leads': web_leads, 'errors': errors}
