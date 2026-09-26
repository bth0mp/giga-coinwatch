"""Saved wanted-search validation and explicit seller-text matching."""
import re
import shlex
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode


TEXT_FIELDS = {'name': 120, 'keywords': 500, 'coin_type': 200, 'mint': 200,
               'ruler': 200, 'exclude_terms': 500, 'category': 100, 'currency': 3}
CRITERIA = ('keywords', 'coin_type', 'mint', 'ruler', 'category', 'exclude_terms', 'currency', 'max_price')
# Deliberately narrow: alternate spellings of the same region, not general synonyms.
BOEOTIA_SPELLINGS = ('Boeotia', 'Boiotia', 'Boetia', 'Boeotian', 'Boiotian', 'Boetian', 'Béotie', 'Beocia', 'Beozia')
_BOEOTIA_WORDS = {'boeotia', 'boiotia', 'boetia', 'boeotian', 'boiotian', 'boetian', 'beotie', 'beocia', 'beozia'}
_GREEK = re.compile(r'\b(?:greek|griech\w*|grieg[oa]s?|grecs?|grecques?|greco|greca|greci|greche|greg[oa]s?|grecia|grece)\b')


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


def _raw_terms(text):
    """Keep explicit double quotes so literal phrases are never alias-expanded."""
    lexer = shlex.shlex(text, posix=False)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.quotes = '"'
    lexer.escape = ''
    return [term for term in lexer if normalize(term)]


def _concepts(text):
    return ' '.join('boeotia' if word in _BOEOTIA_WORDS else word for word in normalize(text).split())


def _term_specs(text):
    specs = []
    for raw in _raw_terms(text):
        quoted = raw.startswith('"') and raw.endswith('"')
        value = normalize(raw) if quoted else _concepts(raw)
        if not quoted and set(value.split()) == {'boeotia'}:
            value = 'boeotia'
        specs.append((f' {value} ', quoted))
    return specs


def _contains(spec, literal, concepts):
    value, quoted = spec
    return value in (literal if quoted else concepts)


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
    include = [spec for key in CRITERIA[:5] for spec in _term_specs(search[key])]
    exclude = _term_specs(search['exclude_terms'])
    ceiling = Decimal(search['max_price']) if search['max_price'] else None

    def matches(title, category, currency, price):
        text = f' {normalize(title or "")} {normalize(category or "")} '
        concepts = f' {_concepts(text)} '
        if not all(_contains(spec, text, concepts) for spec in include) or any(_contains(spec, text, concepts) for spec in exclude):
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


def web_matcher(search):
    """Match verified seller fields only; search snippets and URL hints are not evidence."""
    matches = matcher({**search, 'category': ''})
    categories = _term_specs(search['category'])

    def matches_row(row):
        title = row.get('title') or ''
        if not matches(title, '', row.get('currency') or '', row.get('price') or ''):
            return False
        literal, concepts = f' {normalize(title)} ', f' {_concepts(title)} '
        for spec in categories:
            if spec == (' greek ', False):
                # Boeotia is Greek geography; other categories still need explicit title evidence.
                if not (_GREEK.search(literal) or ' boeotia ' in concepts):
                    return False
            elif not _contains(spec, literal, concepts):
                return False
        return True
    return matches_row


def _query_cores(search):
    parts, alias_seen = [], False
    for key in CRITERIA[:5]:
        for raw in _raw_terms(search[key]):
            words = set(normalize(raw).split())
            if not raw.startswith('"') and words and words <= _BOEOTIA_WORDS:
                if not alias_seen:
                    parts.append(None)
                    alias_seen = True
            else:
                parts.append(raw)
    if alias_seen:
        return [' '.join(f'"{spelling}"' if part is None else part for part in parts)
                for spelling in BOEOTIA_SPELLINGS]
    return [' '.join(search[k] for k in CRITERIA[:5] if search[k])]


def with_web_query(search):
    query = _query_cores(search)[0] + ' ancient coins buy fixed price'
    for term in terms(search['exclude_terms']):
        query += ' -' + ('"' + term + '"' if ' ' in term else term)
    search['web_query'] = query
    search['web_url'] = 'https://www.google.com/search?' + urlencode({'q': query})
    return search


def web_query_variants(search, count=1):
    """Vary known spellings and purchase intent while preserving all other constraints."""
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError('Choose a whole number from 1 to 50 web queries.')
    cores = _query_cores(search)
    excluded = ''.join(' -' + ('"' + term + '"' if ' ' in term else term)
                       for term in terms(search['exclude_terms']))
    phrases = (
        'ancient coins buy fixed price',
        'ancient coin in stock',
        'ancient coin buy now',
        'ancient coin add to cart',
        'ancient coins for sale',
        'Münzen Festpreis auf Lager kaufen',
        'monnaies prix fixe en stock',
        'monedas precio fijo comprar',
        'monete prezzo fisso disponibili',
        'moedas preço fixo em estoque',
    )
    alias_seen = len(cores) > 1
    first = f'{cores[0]} {phrases[0]}{excluded}'
    queries = [first] if len(first) <= 1000 else []
    if len(queries) == count:
        return queries
    angles = ('', ' available', ' purchase', ' order online', ' shop stock')
    for round_number in range(len(cores) if alias_seen else len(angles)):
        for index, phrase in enumerate(phrases):
            selected_core = cores[(round_number + index) % len(cores)]
            angle = '' if alias_seen else angles[round_number]
            query = f'{selected_core} {phrase}{angle}{excluded}'
            # Saved searches can already be near the provider's length limit.
            # Skip an overlong variation instead of silently losing a criterion.
            if len(query) <= 1000 and query not in queries:
                queries.append(query)
            if len(queries) == count:
                return queries
    return queries
