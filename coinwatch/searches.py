"""Saved wanted-search validation and explicit seller-text matching."""
import re
import shlex
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode


TEXT_FIELDS = {'name': 120, 'keywords': 500, 'coin_type': 200, 'mint': 200,
               'ruler': 200, 'exclude_terms': 500, 'category': 100, 'currency': 3}
CRITERIA = ('keywords', 'coin_type', 'mint', 'ruler', 'category', 'exclude_terms', 'currency', 'max_price')


def normalize(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))
    return ' '.join(re.findall(r'\w+', text, flags=re.UNICODE))


def terms(text):
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.quotes = '"'
    lexer.escape = ''
    try:
        result = list(lexer)
    except ValueError as e:
        raise ValueError('Close double quotes around search phrases.') from e
    if len(result) > 30:
        raise ValueError('Use at most 30 words or phrases per field.')
    return [term for term in result if normalize(term)]


def validate_search(values):
    clean = {}
    for key, limit in TEXT_FIELDS.items():
        value = str(values.get(key) or '').strip()
        if len(value) > limit or any(ord(c) < 32 for c in value):
            raise ValueError(f'{key.replace("_", " ").capitalize()} must be at most {limit} characters without control characters.')
        clean[key] = value
    positive = [clean[k] for k in CRITERIA[:5] if terms(clean[k])]
    terms(clean['exclude_terms'])
    if not positive:
        raise ValueError('Enter keywords, a coin type, mint, ruler, or category to search for.')
    clean['name'] = clean['name'] or ' / '.join(positive)[:120]
    clean['currency'] = clean['currency'].upper()
    if clean['currency'] and not re.fullmatch('[A-Z]{3}', clean['currency']):
        raise ValueError('Use a three-letter currency such as GBP, EUR, or USD.')
    raw_price = str(values.get('max_price') if values.get('max_price') is not None else '').strip()
    clean['max_price'] = ''
    if raw_price:
        try:
            price = Decimal(raw_price)
            if not price.is_finite() or price < 0 or price > Decimal('1000000000000'):
                raise InvalidOperation
        except InvalidOperation as e:
            raise ValueError('Enter a valid non-negative maximum price up to 1 trillion.') from e
        if not clean['currency']:
            raise ValueError('Choose a currency before setting a maximum price.')
        clean['max_price'] = str(price)
    for key in ('enabled', 'include_web'):
        value = values.get(key, True)
        clean[key] = int(value is True or str(value).lower() in ('1', 'true', 'on', 'yes'))
    if clean['include_web'] and len(with_web_query(dict(clean))['web_query']) > 1000:
        raise ValueError('Shorten the combined search terms to use wider-web search (1,000 characters including search hints).')
    return clean


def matcher(search):
    include = [f' {normalize(term)} ' for key in CRITERIA[:5] for term in terms(search[key])]
    exclude = [f' {normalize(term)} ' for term in terms(search['exclude_terms'])]
    ceiling = Decimal(search['max_price']) if search['max_price'] else None

    def matches(title, category, currency, price):
        text = f' {normalize(title or "")} {normalize(category or "")} '
        if not all(term in text for term in include) or any(term in text for term in exclude):
            return False
        if search['currency'] and currency != search['currency']:
            return False
        if ceiling is not None:
            try:
                amount = Decimal(price)
                if not amount.is_finite() or amount < 0 or amount > ceiling:
                    return False
            except (InvalidOperation, TypeError):
                return False
        return True
    return matches


def with_web_query(search):
    query = ' '.join(search[k] for k in CRITERIA[:5] if search[k]) + ' ancient coins buy fixed price'
    for term in terms(search['exclude_terms']):
        query += ' -' + ('"' + term + '"' if ' ' in term else term)
    search['web_query'] = query
    search['web_url'] = 'https://www.google.com/search?' + urlencode({'q': query})
    return search
