import json

import httpx
import pytest

from coinwatch import web_search


@pytest.fixture(autouse=True)
def no_environment_key(monkeypatch):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)


def mock_api(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(web_search.httpx, 'Client', lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs))


def test_key_storage_is_local_and_environment_takes_precedence(tmp_path, monkeypatch):
    assert web_search.provider_settings(tmp_path) == {
        'configured': False, 'provider': 'Tavily', 'key_source': ''}
    web_search.save_api_key(tmp_path, 'tvly-local-test-key')
    assert web_search.load_api_key(tmp_path) == 'tvly-local-test-key'
    assert web_search.provider_settings(tmp_path) == {
        'configured': True, 'provider': 'Tavily', 'key_source': 'local'}
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-env-test-key')
    assert web_search.load_api_key(tmp_path) == 'tvly-env-test-key'
    assert web_search.provider_settings(tmp_path)['key_source'] == 'environment'
    web_search.clear_api_key(tmp_path)
    assert not (tmp_path / 'web-search.json').exists()
    assert web_search.load_api_key(tmp_path) == 'tvly-env-test-key'
    monkeypatch.delenv('TAVILY_API_KEY')
    assert web_search.load_api_key(tmp_path) == ''


@pytest.mark.parametrize('key', ['', ' a-key', 'a key', 'a\nkey', 'a\x00key', 'a\x7fkey', 'x' * 513])
def test_invalid_key_cannot_replace_existing_key(tmp_path, key):
    web_search.save_api_key(tmp_path, 'tvly-original-test-key')
    with pytest.raises(web_search.WebSearchError) as exc:
        web_search.save_api_key(tmp_path, key)
    assert 'tvly-original-test-key' not in str(exc.value)
    assert web_search.load_api_key(tmp_path) == 'tvly-original-test-key'


def test_failed_atomic_save_preserves_previous_key(tmp_path, monkeypatch):
    web_search.save_api_key(tmp_path, 'tvly-original-test-key')
    def fail_replace(*args):
        raise OSError('a failure that must not leak the tvly-secret')
    monkeypatch.setattr(web_search.os, 'replace', fail_replace)
    with pytest.raises(web_search.WebSearchError) as exc:
        web_search.save_api_key(tmp_path, 'tvly-new-test-key')
    assert 'tvly-' not in str(exc.value)
    assert web_search.load_api_key(tmp_path) == 'tvly-original-test-key'
    assert len(list(tmp_path.iterdir())) == 1


def test_malformed_key_file_is_not_reported_configured(tmp_path):
    (tmp_path / 'web-search.json').write_text('{"api_key": ["tvly-test-secret"]}', encoding='utf-8')
    assert not web_search.provider_settings(tmp_path)['configured']
    with pytest.raises(web_search.WebSearchError) as exc:
        web_search.load_api_key(tmp_path)
    assert 'tvly-test-secret' not in str(exc.value)


def test_search_uses_basic_request_and_cleans_untrusted_results(monkeypatch):
    received = []
    def respond(request):
        received.append(request)
        return httpx.Response(200, json={'results': [
            {'title': '<b>Alexander</b> &amp; coin', 'url': 'https://Shop.Example/coin?id=42&utm_source=search#photo',
             'content': '<script>alert(1)</script> Babylon\n mint ' + 'x' * 700},
            {'title': 'Duplicate', 'url': 'https://shop.example/coin?id=42&gclid=tracking'},
            {'title': 'Private', 'url': 'http://127.0.0.1/private'},
            {'title': 'Bad', 'url': 'javascript:alert(1)'},
            {'title': 'Credentials', 'url': 'https://user:password@example.com/'},
            {'title': 'Missing URL'},
            {'title': 'No title', 'url': 42},
        ]})
    mock_api(monkeypatch, respond)
    results = web_search.search_web('Alexander Babylon tetradrachm', 'tvly-test-key')
    assert len(received) == 1
    request = received[0]
    assert str(request.url) == 'https://api.tavily.com/search'
    assert request.headers['Authorization'] == 'Bearer tvly-test-key'
    body = json.loads(request.content)
    assert body['query'] == 'Alexander Babylon tetradrachm'
    assert body['search_depth'] == 'basic' and body['max_results'] == 10
    assert not any(body[name] for name in ('include_answer', 'include_images', 'include_raw_content', 'auto_parameters'))
    assert len(results) == 1
    assert results[0]['url'] == 'https://shop.example/coin?id=42'
    assert results[0]['title'] == 'Alexander & coin'
    assert results[0]['snippet'].startswith('Babylon mint ')
    assert len(results[0]['snippet']) <= 500
    assert set(results[0]) == {'url', 'title', 'snippet'}


@pytest.mark.parametrize(('status', 'message'), [
    (401, 'key'), (403, 'access'), (429, 'rate'), (432, 'limit'), (433, 'limit'), (500, 'unavailable')])
def test_provider_errors_are_safe_and_not_retried(monkeypatch, status, message):
    received = []
    def respond(request):
        received.append(request)
        return httpx.Response(status, text='tvly-secret-provider-error')
    mock_api(monkeypatch, respond)
    with pytest.raises(web_search.WebSearchError, match=message) as exc:
        web_search.search_web('test coin', 'tvly-secret')
    assert len(received) == 1
    assert 'tvly-secret' not in str(exc.value)


def test_redirect_does_not_send_key_to_another_host(monkeypatch):
    received = []
    def respond(request):
        received.append(str(request.url))
        return httpx.Response(307, headers={'Location': 'https://evil.example/steal-key'})
    mock_api(monkeypatch, respond)
    with pytest.raises(web_search.WebSearchError, match='redirect'):
        web_search.search_web('test coin', 'tvly-test-key')
    assert received == ['https://api.tavily.com/search']


def test_oversize_response_is_rejected(monkeypatch):
    mock_api(monkeypatch, lambda request: httpx.Response(200, content=b'x' * (1024 * 1024 + 1)))
    with pytest.raises(web_search.WebSearchError, match='large'):
        web_search.search_web('test coin', 'tvly-test-key')


def test_network_errors_do_not_expose_credentials(monkeypatch):
    def respond(request):
        raise httpx.ConnectError('tvly-test-key is private', request=request)
    mock_api(monkeypatch, respond)
    with pytest.raises(web_search.WebSearchError, match='connect') as exc:
        web_search.search_web('test coin', 'tvly-test-key')
    assert 'tvly-test-key' not in str(exc.value)


@pytest.mark.parametrize('body', [b'not JSON', b'{}', b'[]', b'{"results": "invalid"}'])
def test_bad_response_is_not_mistaken_for_zero_matches(monkeypatch, body):
    mock_api(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(web_search.WebSearchError, match='response'):
        web_search.search_web('test coin', 'tvly-test-key')


def test_missing_key_and_empty_query_fail_before_network(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail('No HTTP call should be attempted')
    monkeypatch.setattr(web_search.httpx, 'Client', forbidden)
    with pytest.raises(web_search.WebSearchError, match='key'):
        web_search.search_web('test coin', '')
    with pytest.raises(web_search.WebSearchError, match='query'):
        web_search.search_web('  ', 'tvly-test-key')
