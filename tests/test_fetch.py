import pytest

from coinwatch.fetch import FetchError, RobotsRules, validate_url


@pytest.mark.parametrize('url', ['http://127.0.0.1/a', 'http://[::1]/', 'http://192.168.1.1/', 'http://169.254.169.254/latest', 'file:///etc/passwd', 'https://user:pass@example.com', 'http://2130706433/'])
def test_private_and_nonweb_destinations_rejected(url):
    with pytest.raises(FetchError):
        validate_url(url)


def test_robots_wildcards_and_specific_allow():
    rules = RobotsRules('User-agent: *\nDisallow: /*?sort\nDisallow: /private/\nAllow: /private/public$\n')
    assert not rules.allowed('https://example.com/products?sort=price')
    assert not rules.allowed('https://example.com/private/file')
    assert rules.allowed('https://example.com/private/public')
    assert not rules.allowed('https://example.com/private/public/extra')
    assert rules.allowed('https://example.com/product/123')


def test_specific_robot_group_overrides_general_group():
    rules = RobotsRules('User-agent: *\nAllow: /\n\nUser-agent: Coinwatch\nDisallow: /\n')
    assert not rules.allowed('https://example.com/shop')


def test_robots_decode_unreserved_percent_escapes():
    rules = RobotsRules('User-agent: *\nDisallow: /private/\n')
    assert not rules.allowed('https://example.com/%70rivate/item')
    assert not rules.allowed('https://example.com/priv%61te/item')


def test_http_date_retry_after_defers_instead_of_retrying_immediately(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0)
    later = format_datetime(datetime.now(timezone.utc)+timedelta(minutes=10), usegmt=True)
    monkeypatch.setattr(fetcher, '_request', lambda url: (429, {'retry-after':later}, ''))
    with pytest.raises(FetchError, match='later retry'):
        fetcher._request_retry('https://example.com/')


@pytest.mark.parametrize('status', [429, 503])
@pytest.mark.parametrize('retry_after', ['120', 'private api_key=secret'])
def test_deferred_retry_preserves_http_status_without_echoing_retry_header(monkeypatch, status, retry_after):
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0)
    monkeypatch.setattr(fetcher, '_request', lambda url: (status, {'retry-after': retry_after}, ''))
    with pytest.raises(FetchError) as caught:
        fetcher._request_retry('https://example.com/')
    assert str(caught.value).startswith(f'HTTP {status}:')
    assert retry_after not in str(caught.value)


def test_complete_content_length_response_does_not_touch_closed_socket(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import urlsplit
    from coinwatch.fetch import Fetcher
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(301)
            self.send_header('Location', '/catalog')
            self.send_header('Content-Length', '5')
            self.end_headers()
            self.wfile.write(b'moved')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = Thread(target=server.serve_forever,daemon=True)
    thread.start()
    url = f'http://example.com:{server.server_port}/redirect'
    monkeypatch.setattr('coinwatch.fetch.validate_url', lambda value: (urlsplit(value),'127.0.0.1'))
    try:
        status, headers, body = Fetcher(delay=0)._request(url)
        assert status == 301 and headers['location'] == '/catalog' and body == 'moved'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_truncated_content_length_response_is_rejected(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import urlsplit
    from coinwatch.fetch import Fetcher
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '9999')
            self.end_headers()
            self.wfile.write(b'<li class="product">One complete card</li>')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://example.com:{server.server_port}/catalog'
    monkeypatch.setattr('coinwatch.fetch.validate_url', lambda value: (urlsplit(value), '127.0.0.1'))
    try:
        with pytest.raises(FetchError, match='incomplete'):
            Fetcher(delay=0)._request(url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_https_request_keeps_original_host_header(monkeypatch):
    from unittest.mock import MagicMock
    from urllib.parse import urlsplit
    from coinwatch.fetch import Fetcher
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 200
    response.getheader.return_value = 'text/html'
    response.isclosed.return_value = True
    response.length = 0
    response.getheaders.return_value = []
    monkeypatch.setattr('coinwatch.fetch.validate_url', lambda value: (urlsplit(value), '93.184.216.34'))
    monkeypatch.setattr('coinwatch.fetch.socket.create_connection', lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr('coinwatch.fetch.ssl.create_default_context', MagicMock())
    monkeypatch.setattr('coinwatch.fetch.http.client.HTTPConnection', lambda *args, **kwargs: connection)
    Fetcher(delay=0)._request('https://example.com/catalog')
    assert connection.request.call_args.kwargs['headers']['Host'] == 'example.com'


@pytest.mark.parametrize(('body', 'content_type', 'expected'), [
    ('Ancient \u00c6 and \u20ac'.encode('utf-8'), 'text/html', 'Ancient \u00c6 and \u20ac'),
    (b'Ancient \xc619', 'text/html', 'Ancient \u00c619'),
    (b'Ancient \xc619', 'text/html; charset=windows-1252', 'Ancient \u00c619'),
    (b'<meta charset="windows-1252">Ancient \xc619', 'text/html', '<meta charset="windows-1252">Ancient \u00c619'),
])
def test_catalog_text_encoding(body, content_type, expected):
    from coinwatch.fetch import decode_text
    assert decode_text(body, content_type) == expected


@pytest.mark.parametrize('host', ['blocked.example', 'www.blocked.example', 'shop.blocked.example', 'BLOCKED.EXAMPLE.'])
def test_permission_restricted_origin_is_never_requested_or_retried(monkeypatch, host):
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0, blocked_hosts=('WWW.Blocked.Example',))
    monkeypatch.setattr(fetcher, '_request', lambda url: pytest.fail('Restricted host must not be requested'))
    monkeypatch.setattr(fetcher, '_pause', lambda seconds: pytest.fail('Permission restrictions must not be retried'))
    with pytest.raises(FetchError, match='permission or login'):
        fetcher.get(f'https://{host}/coin')
    with pytest.raises(FetchError, match='permission or login'):
        fetcher._request_retry(f'https://{host}/coin')


def test_content_redirect_cannot_fetch_permission_restricted_host(monkeypatch):
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0, blocked_hosts=('blocked.example',))
    requested = []
    def request(url):
        requested.append(url)
        if url == 'https://allowed.example/robots.txt':
            return 404, {}, ''
        if url == 'https://allowed.example/coin':
            return 302, {'location': 'https://www.blocked.example/new-coin'}, ''
        pytest.fail('Redirected restricted host must not be requested')
    monkeypatch.setattr(fetcher, '_request', request)
    with pytest.raises(FetchError, match='permission or login'):
        fetcher.get('https://allowed.example/coin')
    assert requested == ['https://allowed.example/robots.txt', 'https://allowed.example/coin']


def test_robots_redirect_cannot_fetch_permission_restricted_host(monkeypatch):
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0, blocked_hosts=('blocked.example',))
    requested = []
    def request(url):
        requested.append(url)
        if url == 'https://allowed.example/robots.txt':
            return 301, {'location': 'https://login.blocked.example/robots.txt'}, ''
        pytest.fail('Restricted robots destination must not be requested')
    monkeypatch.setattr(fetcher, '_request', request)
    with pytest.raises(FetchError, match='permission or login'):
        fetcher.get('https://allowed.example/coin')
    assert requested == ['https://allowed.example/robots.txt']


@pytest.mark.parametrize('host', ['notblocked.example', 'blocked.example.other.test'])
def test_unrelated_host_with_similar_name_remains_fetchable(monkeypatch, host):
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0, blocked_hosts=('blocked.example',))
    requested = []
    def request(url):
        requested.append(url)
        return (404, {}, '') if url.endswith('/robots.txt') else (200, {}, '<h1>Greek coin</h1>')
    monkeypatch.setattr(fetcher, '_request', request)
    assert fetcher.get(f'https://{host}/coin').text == '<h1>Greek coin</h1>'
    assert requested == [f'https://{host}/robots.txt', f'https://{host}/coin']


def test_runtime_host_policy_applies_to_direct_requests_before_dns(monkeypatch):
    from coinwatch.fetch import Fetcher
    fetcher = Fetcher(delay=0)
    fetcher.blocked_hosts = {'blocked.example'}
    monkeypatch.setattr('coinwatch.fetch.validate_url', lambda url: pytest.fail('Blocked request must stop before DNS'))
    with pytest.raises(FetchError, match='permission or login'):
        fetcher._request('https://blocked.example/coin')
