"""Optional Tavily search with local credentials and unverified web leads."""
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .fetch import FetchError, USER_AGENT, validate_url_shape

API_URL = 'https://api.tavily.com/search'
MAX_RESPONSE_BYTES = 1024 * 1024
KEY_FILE = 'web-search.json'


class WebSearchError(ValueError):
    """An expected search/configuration error safe to show in the dashboard."""


def _validate_key(api_key):
    if not isinstance(api_key, str) or not api_key:
        raise WebSearchError('Add a Tavily API key in Settings to search the wider web.')
    if len(api_key) > 512 or any(ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise WebSearchError('The API key must contain only printable characters, with no spaces, and be at most 512 characters.')
    return api_key


def load_api_key(data_dir: Path) -> str:
    environment_key = os.environ.get('TAVILY_API_KEY')
    if environment_key:
        return _validate_key(environment_key)
    try:
        with (Path(data_dir) / KEY_FILE).open('rb') as handle:
            content = handle.read(8193)
        if len(content) > 8192:
            raise ValueError('Oversized credential file')
        settings = json.loads(content)
        if not isinstance(settings, dict):
            raise ValueError('Invalid credential file')
        return _validate_key(settings.get('api_key'))
    except FileNotFoundError:
        return ''
    except (OSError, ValueError, UnicodeError):
        raise WebSearchError('The local web-search settings could not be read. Save the API key again in Settings.') from None


def provider_settings(data_dir: Path) -> dict:
    source = 'environment' if os.environ.get('TAVILY_API_KEY') else 'local'
    try:
        configured = bool(load_api_key(data_dir))
    except WebSearchError:
        configured = False
    return {'configured': configured, 'provider': 'Tavily', 'key_source': source if configured else ''}


def save_api_key(data_dir: Path, api_key: str) -> None:
    _validate_key(api_key)
    temporary = None
    try:
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        # mkstemp uses mode 0600 on POSIX; Windows inherits the data-directory ACL.
        descriptor, temporary = tempfile.mkstemp(prefix='.web-search-', suffix='.tmp', dir=directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump({'api_key': api_key}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, directory / KEY_FILE)
    except OSError:
        raise WebSearchError('The API key could not be saved. Check that the app data folder is writable.') from None
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def clear_api_key(data_dir: Path) -> None:
    try:
        (Path(data_dir) / KEY_FILE).unlink(missing_ok=True)
    except OSError:
        raise WebSearchError('The saved API key could not be removed. Check that the app data folder is writable.') from None


def _clean_text(value):
    if not isinstance(value, str):
        return ''
    soup = BeautifulSoup(value[:20000], 'html.parser')
    for node in soup(['script', 'style']):
        node.decompose()
    text = ' '.join(soup.get_text(' ', strip=True).split())
    return ''.join(char for char in text if not unicodedata.category(char).startswith('C'))[:500]


def _clean_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return ''
    try:
        parsed = validate_url_shape(value)
        host = parsed.hostname.lower()
        if '.' not in host and ':' not in host:
            return ''
        if ':' in host:
            host = f'[{host}]'
        if parsed.port and parsed.port != (443 if parsed.scheme == 'https' else 80):
            host += f':{parsed.port}'
        query = [
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
            if not key.lower().startswith('utm_')
            and key.lower() not in {'gclid', 'dclid', 'fbclid', 'msclkid', 'mc_cid', 'mc_eid'}
        ]
        return urlunsplit((parsed.scheme, host, parsed.path or '/', urlencode(query), ''))
    except (FetchError, ValueError):
        return ''


def _exclude_domains(domains):
    if not isinstance(domains, list) or len(domains) > 150:
        raise WebSearchError('Provide at most 150 domain names to exclude.')
    clean = []
    for domain in domains:
        try:
            if not isinstance(domain, str) or not domain or domain != domain.strip():
                raise ValueError('Invalid hostname')
            host = domain.encode('idna').decode('ascii').lower()
            labels = host.split('.')
            if (len(host) > 253 or len(labels) < 2 or labels[-1].isdigit()
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
                raise ValueError('Invalid hostname')
            validate_url_shape('https://' + host)
        except (FetchError, ValueError, UnicodeError):
            raise WebSearchError('Excluded domains must be public hostnames without paths, ports, credentials, or wildcards.') from None
        if host not in clean:
            clean.append(host)
    return clean


def search_web(query, api_key, *, max_results=10, exclude_domains=None) -> list[dict]:
    """Return search leads, without claiming price or availability verification."""
    _validate_key(api_key)
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise WebSearchError('Enter a search query between 1 and 1,000 characters.')
    if type(max_results) is not int or not 1 <= max_results <= 20:
        raise WebSearchError('Request between 1 and 20 web results.')
    payload = {
        'query': query.strip(), 'search_depth': 'basic', 'max_results': max_results,
        'topic': 'general', 'include_answer': False, 'include_raw_content': False,
        'include_images': False, 'auto_parameters': False,
    }
    if exclude_domains is not None:
        payload['exclude_domains'] = _exclude_domains(exclude_domains)
    try:
        # A fixed destination, no environment proxies and no redirects keep the
        # bearer token at the intended API. Do not automatically spend retry credits.
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', API_URL, json=payload, headers={
                'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json',
                'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': USER_AGENT,
            }) as response:
                errors = {
                    401: 'The Tavily API key was not accepted. Update it in Settings.',
                    403: 'Tavily denied API access. Check the API account permissions.',
                    429: 'Tavily reached its rate limit. Try again later.',
                    432: 'The Tavily plan usage limit has been reached.',
                    433: 'The Tavily account usage limit has been reached.',
                }
                if response.status_code in errors:
                    raise WebSearchError(errors[response.status_code])
                if 300 <= response.status_code < 400:
                    raise WebSearchError('Tavily returned an unexpected redirect; the API key was not forwarded.')
                if response.status_code != 200:
                    raise WebSearchError('Tavily search is currently unavailable. Try again later.')
                chunks, size = [], 0
                for chunk in response.iter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise WebSearchError('Tavily returned a response that is too large.')
                    chunks.append(chunk)
                data = json.loads(b''.join(chunks))
    except httpx.TimeoutException:
        raise WebSearchError('Tavily search timed out. Try again later.') from None
    except httpx.HTTPError:
        raise WebSearchError('Could not connect to Tavily search. Check the internet connection and try again.') from None
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, WebSearchError):
            raise
        raise WebSearchError('Tavily returned an unreadable response. Try again later.') from None
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise WebSearchError('Tavily returned an unexpected search response. Try again later.')
    results, seen = [], set()
    for item in data['results']:
        if not isinstance(item, dict):
            continue
        url = _clean_url(item.get('url'))
        title = _clean_text(item.get('title'))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append({'url': url, 'title': title, 'snippet': _clean_text(item.get('content'))})
        if len(results) >= max_results:
            break
    return results
