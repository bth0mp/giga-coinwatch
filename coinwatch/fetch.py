"""Conservative public-web fetching with DNS pinning and robots checks."""
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

USER_AGENT = 'giga-coinwatch/0.1 (personal ancient-coin catalog monitor)'
MAX_BYTES = 5 * 1024 * 1024


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    text: str


def decode_text(body, content_type):
    match = re.search(r'charset\s*=\s*["\']?([^;\s"\'>]+)', content_type, re.I)
    if not match and 'html' in content_type.lower():
        match = re.search(r'<meta\b[^>]*charset\s*=\s*["\']?([^;\s"\'>]+)', body[:4096].decode('ascii', errors='ignore'), re.I)
    if match:
        try:
            return body.decode(match.group(1), errors='replace')
        except LookupError:
            pass
    try:
        return body.decode('utf-8-sig')
    except UnicodeDecodeError:
        # Legacy shops often omit a charset while using Windows-1252 HTML.
        return body.decode('windows-1252', errors='replace')


def validate_url_shape(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError('Only public HTTP(S) URLs without credentials are supported.')
        if any(ord(ch) < 33 for ch in url) or '\\' in url:
            raise ValueError('Invalid URL characters.')
        if p.port not in (None, 80, 443):
            raise ValueError('Only standard web ports are supported.')
        host = p.hostname.lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError('Private network addresses are not allowed.')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError('Private network addresses are not allowed.')
        return p
    except (ValueError, TypeError) as e:
        raise FetchError(str(e)) from e


def validate_url(url):
    p = validate_url_shape(url)
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError as e:
        raise FetchError(f'Cannot resolve {p.hostname}: {e}') from e
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FetchError('The URL resolves to a private or reserved address.')
    return p, addresses[0][4][0]


class RobotsRules:
    def __init__(self, text):
        groups, agents, rules = [], [], []
        seen_rule = False
        for line in text.splitlines():
            line = line.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (part.strip() for part in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if seen_rule:
                    groups.append((agents, rules))
                    agents, rules, seen_rule = [], [], False
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow', 'crawl-delay'):
                seen_rule = True
                rules.append((key, value))
        if agents:
            groups.append((agents, rules))
        selected, score = [], -1
        for agents, rules in groups:
            specificity = max((len(a) if a != '*' else 0 for a in agents if a == '*' or a in USER_AGENT.split('/', 1)[0].lower()), default=-1)
            if specificity > score:
                selected, score = list(rules), specificity
            elif specificity == score and specificity >= 0:
                selected.extend(rules)
        self.rules = selected
        self.delay = 0
        for kind, value in selected:
            if kind == 'crawl-delay':
                try:
                    self.delay = max(self.delay, float(value))
                except ValueError:
                    pass

    def allowed(self, url):
        def normalized(value):
            def replace(match):
                char = chr(int(match.group(1), 16))
                return char if char in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~' else match.group(0).upper()
            return re.sub(r'%([a-fA-F0-9]{2})', replace, value)
        p = urlsplit(url)
        path = p.path or '/'
        if p.query:
            path += '?' + p.query
        path = normalized(path)
        matches = []
        for kind, value in self.rules:
            if kind not in ('allow', 'disallow') or not value:
                continue
            value = normalized(value)
            end = value.endswith('$')
            pattern = value[:-1] if end else value
            regex = '^' + re.escape(pattern).replace(r'\*', '.*') + ('$' if end else '')
            if re.search(regex, path):
                matches.append((len(pattern.replace('*', '')), kind == 'allow'))
        return max(matches)[1] if matches else True


class Fetcher:
    def __init__(self, delay=1.0, timeout=15, stop_event=None, budget=900):
        self.delay = delay
        self.timeout = timeout
        self.stop_event = stop_event or threading.Event()
        self.deadline = time.monotonic() + budget
        self._robots = {}
        self._last = {}

    def _check(self):
        if self.stop_event.is_set():
            raise FetchError('Scan stopped.')
        if time.monotonic() >= self.deadline:
            raise FetchError('Scan time budget reached; coverage is partial.')

    def _pause(self, seconds):
        self._check()
        if seconds > max(0, self.deadline-time.monotonic()):
            raise FetchError('Scan time budget reached.')
        if self.stop_event.wait(max(0, seconds)):
            raise FetchError('Scan stopped.')

    def _request(self, url):
        self._check()
        p, address = validate_url(url)
        host = p.hostname
        delay = max(self.delay, self._robots.get(f'{p.scheme}://{p.netloc}', RobotsRules('')).delay)
        if delay > 60:
            raise FetchError('Site crawl delay exceeds this scanner\'s supported interval.')
        self._pause(max(0, delay - (time.monotonic()-self._last.get(host, 0))))
        self._last[host] = time.monotonic()
        port = p.port or (443 if p.scheme == 'https' else 80)
        conn = http.client.HTTPConnection(host, port, timeout=min(self.timeout, max(1, self.deadline-time.monotonic())))
        try:
            # Connect to the already validated address, never re-resolve on connect.
            sock = socket.create_connection((address, port), timeout=conn.timeout)
            if p.scheme == 'https':
                try:
                    sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
                except BaseException:
                    sock.close()
                    raise
            conn.sock = sock
            path = urlunsplit(('', '', p.path or '/', p.query, ''))
            conn.request('GET', path, headers={'Host': p.netloc, 'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml,application/xml,text/plain,application/json', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = conn.getresponse()
            content_type = response.getheader('Content-Type', '')
            if response.status == 200 and content_type and not any(t in content_type.lower() for t in ('text/', 'json', 'xml')):
                raise FetchError('Expected a public text/catalog page.')
            chunks, length = [], 0
            while length <= MAX_BYTES and not response.isclosed():
                self._check()
                sock.settimeout(min(self.timeout, max(0.1, self.deadline-time.monotonic())))
                chunk = response.read1(min(65536, MAX_BYTES+1-length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
            body = b''.join(chunks)
            if len(body) > MAX_BYTES:
                raise FetchError('Page exceeds the safe download limit.')
            if response.length is not None and response.length > 0:
                raise FetchError('The server returned an incomplete page; coverage is partial.')
            text = decode_text(body, content_type)
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), text
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f'Fetch failed for {host}: {e}') from e
        finally:
            conn.close()

    def _request_retry(self, url):
        for attempt in range(3):
            try:
                status, headers, text = self._request(url)
            except FetchError:
                if attempt == 2:
                    raise
                self._pause(2 ** attempt)
                continue
            if status not in (429, 500, 502, 503, 504) or attempt == 2:
                return status, headers, text
            retry = headers.get('retry-after', '')
            wait = 2 ** (attempt+1)
            if retry.isdigit():
                wait = float(retry)
            elif retry:
                try:
                    retry_date = parsedate_to_datetime(retry)
                    if retry_date.tzinfo is None:
                        retry_date = retry_date.replace(tzinfo=timezone.utc)
                    wait = max(0, (retry_date-datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    raise FetchError(f'HTTP {status}: site requested a later retry with an unrecognized Retry-After value.') from None
            if wait > 30:
                raise FetchError(f'HTTP {status}: site requested a later retry.')
            self._pause(wait)
        raise FetchError('Request failed.')

    def _ensure_robots(self, url):
        p = validate_url_shape(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin in self._robots:
            return self._robots[origin]
        robots_url = origin + '/robots.txt'
        for _ in range(5):
            status, headers, text = self._request_retry(robots_url)
            if status in (301, 302, 303, 307, 308) and headers.get('location'):
                robots_url = urljoin(robots_url, headers['location'])
                continue
            if status in (404, 410):
                rules = RobotsRules('')
            elif status == 200:
                if '<html' in text[:500].lower():
                    raise FetchError('robots.txt returned an HTML challenge; access is unverified.')
                rules = RobotsRules(text)
            else:
                raise FetchError(f'robots.txt could not be checked (HTTP {status}).')
            self._robots[origin] = rules
            return rules
        raise FetchError('Too many robots.txt redirects.')

    def get(self, url):
        for _ in range(6):
            validate_url_shape(url)
            if not self._ensure_robots(url).allowed(url):
                raise FetchError('robots.txt disallows this catalog URL.')
            status, headers, text = self._request_retry(url)
            if status in (301, 302, 303, 307, 308) and headers.get('location'):
                url = urljoin(url, headers['location'])
                continue
            if status != 200:
                raise FetchError(f'HTTP {status} fetching {urlsplit(url).hostname}.')
            if any(marker in text[:12000].lower() for marker in ('cf-chl-', 'verify you are human', 'just a moment...</title>')):
                raise FetchError('The site requires a browser challenge; monitoring is paused for this run.')
            return Page(url, text)
        raise FetchError('Too many redirects.')
