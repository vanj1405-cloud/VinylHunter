from __future__ import annotations

import re
import time
import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from threading import Lock

from flask import Flask, jsonify, render_template, request

import core

try:
    from providers import international as international_provider
except Exception:
    international_provider = None

try:
    from providers import discogs_identifier
except Exception:
    discogs_identifier = None

try:
    from providers import discogs_market
except Exception:
    discogs_market = None

try:
    from providers import value_engine
except Exception:
    value_engine = None


_QUERY_RESOLVE_CACHE: dict[tuple[str, str], tuple[str, str]] = {}

def _has_hebrew(value: str) -> bool:
    return bool(re.search(r'[\u0590-\u05FF]', str(value or '')))


def _discogs_token() -> str:
    return (
        os.getenv('DISCOGS_TOKEN')
        or os.getenv('DISCOGS_USER_TOKEN')
        or os.getenv('DISCOGS_PERSONAL_ACCESS_TOKEN')
        or ''
    ).strip()


def _resolve_hebrew_query(artist: str, album: str = '') -> tuple[str, str, dict]:
    """Resolve Hebrew/alias input to Discogs' canonical Latin artist/release names.

    Falls back safely to the user's original text when Discogs cannot resolve it.
    Results are cached in-process so repeated searches do not add another network call.
    """
    artist = str(artist or '').strip()
    album = str(album or '').strip()
    key = (artist.casefold(), album.casefold())

    if key in _QUERY_RESOLVE_CACHE:
        ra, rb = _QUERY_RESOLVE_CACHE[key]
        return ra, rb, {
            'used': (ra != artist or rb != album),
            'cached': True,
            'input_artist': artist,
            'input_album': album,
            'resolved_artist': ra,
            'resolved_album': rb,
        }

    if not (_has_hebrew(artist) or _has_hebrew(album)):
        _QUERY_RESOLVE_CACHE[key] = (artist, album)
        return artist, album, {
            'used': False,
            'cached': False,
            'input_artist': artist,
            'input_album': album,
            'resolved_artist': artist,
            'resolved_album': album,
        }

    token = _discogs_token()
    if not token:
        return artist, album, {
            'used': False,
            'cached': False,
            'reason': 'DISCOGS_TOKEN unavailable',
            'input_artist': artist,
            'input_album': album,
            'resolved_artist': artist,
            'resolved_album': album,
        }

    try:
        headers = {
            'Authorization': f'Discogs token={token}',
            'User-Agent': 'VinylHunter/3.0',
        }

        # Artist-only mode: resolve the artist alias directly.
        if not album:
            r = requests.get(
                'https://api.discogs.com/database/search',
                params={'q': artist, 'type': 'artist', 'per_page': 5, 'page': 1},
                headers=headers,
                timeout=3.5,
            )
            r.raise_for_status()
            rows = (r.json() or {}).get('results') or []
            resolved_artist = str(rows[0].get('title') or artist).strip() if rows else artist
            resolved_album = ''

        # Artist + album: release search often resolves both Hebrew aliases at once.
        else:
            r = requests.get(
                'https://api.discogs.com/database/search',
                params={
                    'q': f'{artist} {album}'.strip(),
                    'type': 'release',
                    'format': 'Vinyl',
                    'per_page': 8,
                    'page': 1,
                },
                headers=headers,
                timeout=3.5,
            )
            r.raise_for_status()
            rows = (r.json() or {}).get('results') or []

            resolved_artist, resolved_album = artist, album
            for row in rows:
                title = str(row.get('title') or '').strip()
                if ' - ' not in title:
                    continue
                a, b = title.split(' - ', 1)
                if a.strip() and b.strip():
                    resolved_artist, resolved_album = a.strip(), b.strip()
                    break

        _QUERY_RESOLVE_CACHE[key] = (resolved_artist, resolved_album)
        return resolved_artist, resolved_album, {
            'used': (resolved_artist != artist or resolved_album != album),
            'cached': False,
            'input_artist': artist,
            'input_album': album,
            'resolved_artist': resolved_artist,
            'resolved_album': resolved_album,
        }

    except Exception as e:
        return artist, album, {
            'used': False,
            'cached': False,
            'reason': f'{type(e).__name__}:{str(e)[:100]}',
            'input_artist': artist,
            'input_album': album,
            'resolved_artist': artist,
            'resolved_album': album,
        }


app = Flask(__name__)

STORES = [x['name'] for x in core.store_registry()]

_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_LOCK = Lock()
CACHE_TTL = 300


def clean_title(title: str) -> str:
    text = str(title or '').strip()
    # Strip store-card UI noise while preserving the actual release/edition title.
    prefixes = [
        r'^מחיר אונליין\s*', r'^אזל מהמלאי\s*', r'^עדכנו אותי כשחזר למלאי\s*',
        r'^הוספה לסל\s*', r'^הוסף לסל\s*', r'^add to cart\s*',
        r'^מבצע!\s*', r'^מידע נוסף\s*', r'^sale!?\s*'
    ]
    changed = True
    while changed:
        changed = False
        for pattern in prefixes:
            new = re.sub(pattern, '', text, flags=re.I).strip()
            if new != text:
                text = new
                changed = True
    # Some Israeli store cards append current/original price copy to the title.
    text = re.sub(r'\s+₪\s*[\d,.]+.*$', '', text).strip()
    return re.sub(r'\s{2,}', ' ', text).strip()


def _norm_text(value: str) -> str:
    value = str(value or '').lower()
    value = value.replace('–', '-').replace('—', '-').replace('’', "'")
    return re.sub(r'[^\w\"]+', ' ', value, flags=re.UNICODE).strip()


def classify_release(title: str, query_album: str) -> dict:
    """Classify release format/edition and flag hard query incompatibilities.

    This intentionally keeps legitimate album variants (anniversary, picture disc,
    coloured vinyl, 2LP/3LP, mono, remix...) but rejects a different product type
    such as a movie soundtrack or a 7-inch single when the user searched for the
    regular album.
    """
    # Artist-only browsing intentionally keeps albums, singles, soundtracks and editions.
    artist_only_mode = not str(query_album or '').strip()

    title_n = _norm_text(title)
    query_n = _norm_text(query_album)

    tags = []
    format_label = 'LP'
    release_type = 'ALBUM'
    reject_reason = ''

    soundtrack_terms = (
        'original motion picture', 'motion picture soundtrack',
        'original soundtrack', 'soundtrack', 'music from the motion picture'
    )
    query_is_soundtrack = any(x in query_n for x in soundtrack_terms)
    is_soundtrack = any(x in title_n for x in soundtrack_terms)

    is_7 = bool(re.search(r'\b7\s*(?:inch|inches|\")', title_n)) or "vinyl 7" in title_n
    query_is_7 = bool(re.search(r'\b7\s*(?:inch|inches|\")', query_n)) or "vinyl 7" in query_n
    explicit_single = bool(re.search(r'\bsingle\b', title_n))

    if is_soundtrack:
        release_type = 'SOUNDTRACK'
        tags.append('SOUNDTRACK')
        if not artist_only_mode and not query_is_soundtrack:
            reject_reason = 'soundtrack differs from requested album'

    if is_7 or explicit_single:
        release_type = 'SINGLE'
        format_label = '7"' if is_7 else 'SINGLE'
        if '7"' not in tags and is_7:
            tags.append('7"')
        if not artist_only_mode and not query_is_7 and not reject_reason:
            reject_reason = 'single/7-inch differs from requested album'

    # Physical format. Prefer the most specific multi-disc notation.
    m = re.search(r'\b([2-9])\s*lp\b', title_n)
    if m:
        format_label = f'{m.group(1)}LP'
    elif re.search(r'\blp\b', title_n) or 'vinyl' in title_n:
        format_label = 'LP'

    edition_rules = [
        ('PRE-ORDER', ('pre order', 'preorder')),
        ('ANNIVERSARY', ('anniversary',)),
        ('PICTURE DISC', ('picture disc',)),
        ('COLORED', ('colored vinyl', 'coloured vinyl', 'color vinyl', 'colour vinyl')),
        ('MONO', (' mono ', 'in mono')),
        ('STEREO', (' stereo ',)),
        ('REMIX', ('remix',)),
        ('HALF-SPEED', ('half speed', 'half-speed')),
        ('DELUXE', ('deluxe',)),
        ('LIMITED', ('limited edition', 'limited ')),
        ('BOX SET', ('box set', 'boxset')),
    ]
    padded = f' {title_n} '
    for label, needles in edition_rules:
        if any(n in padded for n in needles):
            tags.append(label)

    # Keep order while removing duplicates.
    tags = list(dict.fromkeys(tags))
    return {
        'release_type': release_type,
        'format_label': format_label,
        'edition_tags': tags,
        'query_compatible': not bool(reject_reason),
        'reject_reason': reject_reason,
    }


def enrich_result_dict(d: dict, query_album: str) -> dict:
    info = classify_release(d.get('display_title') or d.get('title', ''), query_album)
    d.update(info)
    return d


def result_to_dict(r, query_album: str = ""):
    d = asdict(r)
    d['display_title'] = clean_title(r.title)
    d['available'] = r.stock == 'IN_STOCK'
    d['source_type'] = 'ISRAEL_STORE'
    d['country'] = 'IL'
    d['ships_to_israel'] = 'YES'
    d['price'] = d.get('price')
    d['score'] = d.get('score', 0.0)
    return enrich_result_dict(d, query_album)


def international_result_to_dict(r, query_album: str = ""):
    d = asdict(r)
    out = {
        'store': d.get('source', ''),
        'title': d.get('title', ''),
        'display_title': clean_title(d.get('title', '')),
        'url': d.get('url', ''),
        'price': d.get('item_price'),
        'currency': d.get('currency') or '',
        'stock': d.get('stock') or 'UNKNOWN',
        'available': d.get('stock') == 'IN_STOCK',
        'evidence': d.get('evidence', ''),
        'path': d.get('adapter', ''),
        'score': d.get('match_score', 0.0),
        'media_condition': d.get('media_condition', ''),
        'sleeve_condition': d.get('sleeve_condition', ''),
        'pressing': d.get('pressing', ''),
        'edition': d.get('edition', ''),
        'country': d.get('country', ''),
        'ships_to_israel': d.get('ships_to_israel', 'UNKNOWN'),
        'source_type': 'INTERNATIONAL_STORE',
    }
    return enrich_result_dict(out, query_album)



def discogs_market_result_to_dict(r, query_album: str = ""):
    d = asdict(r)
    out = {
        'store': 'Discogs',
        'title': d.get('title', ''),
        'display_title': clean_title(d.get('title', '')),
        'url': d.get('url', ''),
        'price': d.get('item_price'),
        'currency': d.get('currency') or 'USD',
        'shipping_price': None,
        'shipping_currency': d.get('currency') or 'USD',
        'stock': d.get('stock') or 'UNKNOWN',
        'available': d.get('stock') == 'IN_STOCK',
        'evidence': d.get('evidence', ''),
        'path': d.get('adapter', ''),
        'score': d.get('match_score', 0.0),
        'media_condition': d.get('media_condition', ''),
        'sleeve_condition': d.get('sleeve_condition', ''),
        'pressing': d.get('pressing', ''),
        'edition': d.get('edition', ''),
        'country': d.get('country', ''),
        'ships_to_israel': d.get('ships_to_israel', 'UNKNOWN'),
        'source_type': 'DISCOGS_MARKETPLACE',
        'price_kind': 'MARKET_FLOOR',
        'discogs_release_id': d.get('release_id'),
        'market_availability_count': d.get('num_for_sale'),
        'release_year': d.get('year'),
        'labels': d.get('labels') or [],
        'formats': d.get('formats') or [],
    }
    return enrich_result_dict(out, query_album)


def _search_discogs_market(artist: str, album: str):
    if discogs_market is None:
        return [], {'store': 'Discogs', 'errors': ['discogs_market provider import failed']}
    try:
        rows, meta = discogs_market.search(artist, album)
        diag = {
            'store': 'Discogs',
            'adapter': 'discogs_release_market',
            'errors': meta.get('errors') or [],
            'seconds': meta.get('elapsed', 0),
            'trace': [],
            'candidates': meta.get('candidates', 0),
            'source_type': 'DISCOGS_MARKETPLACE',
        }
        return rows, diag
    except Exception as e:
        return [], {
            'store': 'Discogs',
            'adapter': 'discogs_release_market',
            'errors': [f'{type(e).__name__}:{str(e)[:160]}'],
            'seconds': 0,
            'trace': [],
            'candidates': 0,
            'source_type': 'DISCOGS_MARKETPLACE',
        }

def _search_international(artist: str, album: str):
    if international_provider is None:
        return [], [{'store': 'International', 'errors': ['international provider import failed']}]

    active = getattr(international_provider, 'ACTIVE_ADAPTERS', {})
    stores = [
        s for s in international_provider.registry()
        if s.get('name') in active
    ]
    if not stores:
        return [], []

    results = []
    diag = []

    def run_one(store):
        started = time.time()
        rs, errors, trace = international_provider.search_store(store, artist, album)
        return rs, {
            'store': store.get('name', ''),
            'adapter': active.get(store.get('name'), 'unresolved'),
            'errors': errors,
            'seconds': round(time.time() - started, 2),
            'trace': trace,
            'candidates': len(rs),
            'source_type': 'INTERNATIONAL_STORE',
        }

    with ThreadPoolExecutor(max_workers=min(12, len(stores))) as ex:
        futures = [ex.submit(run_one, store) for store in stores]
        for future in as_completed(futures):
            try:
                rs, item = future.result()
                results.extend(rs)
                diag.append(item)
            except Exception as e:
                diag.append({
                    'store': 'International',
                    'errors': [f'{type(e).__name__}:{str(e)[:160]}'],
                    'seconds': 0,
                    'trace': [],
                    'candidates': 0,
                    'source_type': 'INTERNATIONAL_STORE',
                })

    # URL-level dedupe for international results.
    dedup = {}
    for r in results:
        key = (r.source, r.url.rstrip('/'))
        old = dedup.get(key)
        if old is None or r.match_score > old.match_score:
            dedup[key] = r
    results = list(dedup.values())
    return results, diag


def summarize_dicts(results, diag):
    available = [r for r in results if r.get('stock') == 'IN_STOCK']

    # Do not compare GBP/USD/EUR/JPY directly with ILS. Until delivered-price
    # conversion is added, the global "best" badge is restricted to ILS offers.
    comparable = [
        r for r in available
        if r.get('price') is not None and (r.get('currency') in ('', 'ILS', None))
    ]
    best = min(comparable, key=lambda x: x['price']) if comparable else None

    stores_hit = sorted({r.get('store', '') for r in results if r.get('store')})
    stores_available = sorted({r.get('store', '') for r in available if r.get('store')})
    errors = [
        {'store': d.get('store', ''), 'errors': d.get('errors', [])}
        for d in diag if d.get('errors')
    ]
    return {
        'result_count': len(results),
        'available_count': len(available),
        'stores_hit': stores_hit,
        'stores_available': stores_available,
        'best_deal': best,
        'errors': errors,
        'international_count': sum(1 for r in results if r.get('source_type') == 'INTERNATIONAL_STORE'),
        'israel_count': sum(1 for r in results if r.get('source_type') == 'ISRAEL_STORE'),
        'discogs_count': sum(1 for r in results if r.get('source_type') == 'DISCOGS_MARKETPLACE'),
    }


@app.get('/')
def index():
    registry = core.store_registry()
    live_count = sum(1 for x in registry if x.get('status') == 'live')
    return render_template('index.html', store_count=len(STORES), live_count=live_count)


@app.get('/api/stores')
def stores():
    registry = core.store_registry()
    return jsonify({'stores': registry, 'count': len(registry)})


SEARCH_DEADLINE_SECONDS = 18.0


def _collect_future_before_deadline(future, started: float, fallback):
    """Return a future's result only while the global search deadline remains."""
    remaining = SEARCH_DEADLINE_SECONDS - (time.time() - started)
    if remaining <= 0:
        return fallback, 'deadline'
    try:
        return future.result(timeout=remaining), None
    except Exception as e:
        if type(e).__name__ == 'TimeoutError':
            return fallback, 'deadline'
        raise



@app.post('/api/search')
def api_search():
    payload = request.get_json(silent=True) or request.form
    artist_input = str(payload.get('artist') or '').strip()
    album_input = str(payload.get('album') or '').strip()

    if not artist_input:
        return jsonify({'ok': False, 'error': 'Artist is required.'}), 400

    artist, album, query_resolution = _resolve_hebrew_query(artist_input, album_input)
    artist_only = not bool(album.strip())

    # Cache canonicalized queries so Hebrew/English aliases can share a result set.
    key = (artist.casefold(), album.casefold())
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] < CACHE_TTL:
            body = dict(cached[1])
            body['cached'] = True
            body['elapsed'] = round(time.time() - now, 3)
            return jsonify(body)

    started = time.time()

    local_results = []
    local_diag = []
    intl_results = []
    intl_diag = []
    discogs_results = []
    discogs_diag = {}

    # Israel, worldwide stores and Discogs market signals run in parallel.
    # Results are collected only until the global deadline. Slow branches are skipped
    # instead of blocking the whole request.
    deadline_notes = []
    ex = ThreadPoolExecutor(max_workers=3)
    try:
        local_future = ex.submit(core.search, artist, album, True)
        intl_future = ex.submit(_search_international, artist, album)
        discogs_future = ex.submit(_search_discogs_market, artist, album)

        local_pack, local_status = _collect_future_before_deadline(
            local_future, started, ([], [])
        )
        local_results, local_diag = local_pack
        if local_status == 'deadline':
            deadline_notes.append('Israel search deadline reached')

        intl_pack, intl_status = _collect_future_before_deadline(
            intl_future, started, ([], [])
        )
        intl_results, intl_diag = intl_pack
        if intl_status == 'deadline':
            deadline_notes.append('International search deadline reached')

        discogs_pack, discogs_status = _collect_future_before_deadline(
            discogs_future, started, ([], {})
        )
        discogs_results, discogs_diag = discogs_pack
        if discogs_status == 'deadline':
            deadline_notes.append('Discogs search deadline reached')

    except Exception as e:
        return jsonify({
            'ok': False,
            'error': f'Search failed: {type(e).__name__}',
            'details': str(e)[:240]
        }), 500
    finally:
        # Do not wait for slow providers after the response deadline.
        ex.shutdown(wait=False, cancel_futures=True)

    raw_unified = [result_to_dict(r, album) for r in local_results]
    raw_unified.extend(international_result_to_dict(r, album) for r in intl_results)
    raw_unified.extend(discogs_market_result_to_dict(r, album) for r in discogs_results)

    # Hard false-positive protection: keep legitimate editions/pressings but
    # remove clearly different products (e.g. soundtrack or 7-inch single).
    rejected = [r for r in raw_unified if not r.get('query_compatible', True)]
    unified = [r for r in raw_unified if r.get('query_compatible', True)]

    # Keep the existing smart ordering philosophy while avoiding bogus
    # cross-currency numeric comparisons.
    stock_rank = {'IN_STOCK': 0, 'UNKNOWN': 1, 'OUT_OF_STOCK': 2}
    source_rank = {
        'ISRAEL_STORE': 0,
        'LOCAL_ONLINE': 0,
        'INTERNATIONAL_STORE': 1,
        'DISCOGS_MARKETPLACE': 2,
    }
    unified.sort(key=lambda r: (
        stock_rank.get(r.get('stock'), 1),
        source_rank.get(r.get('source_type'), 9),
        -(r.get('score') or 0),
        r.get('store', ''),
        r.get('display_title', ''),
    ))

    value_meta = {'fx_status': 'UNAVAILABLE', 'picks': {}, 'skipped': False}
    if artist_only:
        value_meta = {
            'fx_status': 'NOT_APPLICABLE',
            'picks': {},
            'skipped': True,
            'reason': 'artist-only search spans different releases',
        }
    elif value_engine is not None:
        try:
            unified, value_meta = value_engine.apply_value_engine(unified)
        except Exception as e:
            value_meta = {
                'fx_status': 'ERROR',
                'picks': {},
                'error': f'{type(e).__name__}:{str(e)[:140]}',
            }

    diag = list(local_diag) + list(intl_diag) + ([discogs_diag] if discogs_diag else [])
    summary = summarize_dicts(unified, diag)
    if artist_only:
        summary['best_deal'] = None
    summary['smart_value'] = value_meta
    summary['smart_picks'] = value_meta.get('picks') or {}
    summary['filtered_false_positives'] = len(rejected)
    summary['deadline_seconds'] = SEARCH_DEADLINE_SECONDS
    summary['deadline_reached'] = bool(deadline_notes)
    summary['deadline_notes'] = deadline_notes
    summary['filtered_examples'] = [
        {
            'store': r.get('store', ''),
            'title': r.get('display_title', ''),
            'reason': r.get('reject_reason', ''),
        }
        for r in rejected[:8]
    ]

    body = {
        'ok': True,
        'artist': artist_input,
        'album': album_input,
        'resolved_artist': artist,
        'resolved_album': album,
        'artist_only': artist_only,
        'query_resolution': query_resolution,
        'elapsed': round(time.time() - started, 2),
        'cached': False,
        'results': unified,
        'summary': summary,
        'diagnostics': diag,
    }
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), body)
    return jsonify(body)


@app.post('/api/identify')
def api_identify():
    """
    Identify a specific pressing from one or more physical identifiers.

    JSON/form fields:
      barcode
      catalog_number
      matrix_a
      matrix_b

    Optional tie-breaker hints:
      artist
      title
      year
      country
    """
    payload = request.get_json(silent=True) or request.form

    fields = {
        'barcode': str(payload.get('barcode') or '').strip(),
        'catalog_number': str(payload.get('catalog_number') or '').strip(),
        'matrix_a': str(payload.get('matrix_a') or '').strip(),
        'matrix_b': str(payload.get('matrix_b') or '').strip(),
        'artist': str(payload.get('artist') or '').strip(),
        'title': str(payload.get('title') or '').strip(),
        'year': str(payload.get('year') or '').strip(),
        'country': str(payload.get('country') or '').strip(),
    }

    if not any(
        fields[key]
        for key in ('barcode', 'catalog_number', 'matrix_a', 'matrix_b')
    ):
        return jsonify({
            'ok': False,
            'error': (
                'Provide at least one identifier: barcode, catalog_number, '
                'matrix_a, or matrix_b.'
            ),
        }), 400

    if discogs_identifier is None:
        return jsonify({
            'ok': False,
            'error': 'Discogs identifier provider could not be imported.',
        }), 503

    try:
        engine = discogs_identifier.DiscogsIdentifier()

        if not engine.configured():
            return jsonify({
                'ok': False,
                'error': 'DISCOGS_TOKEN is not configured.',
            }), 503

        result = engine.identify(**fields)
        return jsonify(result)

    except ValueError as e:
        return jsonify({
            'ok': False,
            'error': str(e),
        }), 400
    except Exception as e:
        return jsonify({
            'ok': False,
            'error': f'Identification failed: {type(e).__name__}',
            'details': str(e)[:240],
        }), 500


def _discogs_username() -> str:
    return os.getenv('DISCOGS_USERNAME', '').strip()


@app.get('/api/discogs/status')
def discogs_sync_status():
    token = _discogs_token()
    username = _discogs_username()
    return jsonify({
        'ok': True,
        'configured': bool(token and username),
        'username': username if username else '',
        'mode': 'PERSONAL_TOKEN',
    })


@app.post('/api/discogs/wantlist')
def discogs_wantlist_sync():
    """Add/remove an exact Discogs release from the configured user's wantlist.

    Personal build: uses DISCOGS_TOKEN + DISCOGS_USERNAME.
    For a public multi-user build, replace this with per-user OAuth.
    """
    payload = request.get_json(silent=True) or {}
    action = str(payload.get('action') or 'add').strip().lower()
    release_id = str(payload.get('release_id') or '').strip()

    if action not in ('add', 'remove'):
        return jsonify({'ok': False, 'error': 'action must be add or remove'}), 400
    if not release_id.isdigit():
        return jsonify({'ok': False, 'error': 'A numeric Discogs release_id is required.'}), 400

    token = _discogs_token()
    username = _discogs_username()
    if not token or not username:
        return jsonify({
            'ok': False,
            'configured': False,
            'error': 'Set DISCOGS_TOKEN and DISCOGS_USERNAME to enable Discogs wantlist sync.'
        }), 503

    url = f'https://api.discogs.com/users/{username}/wants/{release_id}'
    headers = {
        'Authorization': f'Discogs token={token}',
        'User-Agent': 'VinylHunter/4.0',
        'Accept': 'application/json',
    }

    try:
        if action == 'add':
            r = requests.put(url, headers=headers, json={}, timeout=8)
        else:
            r = requests.delete(url, headers=headers, timeout=8)

        if r.status_code not in (200, 201, 204):
            detail = ''
            try:
                detail = (r.json() or {}).get('message') or ''
            except Exception:
                detail = r.text[:180]
            return jsonify({
                'ok': False,
                'status': r.status_code,
                'error': detail or 'Discogs wantlist request failed.'
            }), 502

        return jsonify({
            'ok': True,
            'action': action,
            'release_id': int(release_id),
            'username': username,
        })

    except Exception as e:
        return jsonify({
            'ok': False,
            'error': f'{type(e).__name__}:{str(e)[:180]}'
        }), 502


@app.post('/api/diagnostics')
def diagnostics():
    payload = request.get_json(silent=True) or {}
    artist = str(payload.get('artist') or 'Pink Floyd').strip()
    album = str(payload.get('album') or 'Animals').strip()
    started = time.time()
    rows = core.diagnose(artist, album)
    return jsonify({
        'ok': True,
        'artist': artist,
        'album': album,
        'elapsed': round(time.time() - started, 2),
        'stores': rows,
    })


@app.get('/api/health')
def health():
    intl_count = 0
    if international_provider is not None:
        try:
            intl_count = len(getattr(international_provider, 'ACTIVE_ADAPTERS', {}))
        except Exception:
            intl_count = 0
    return jsonify({
        'ok': True,
        'stores': len(STORES),
        'international_active': intl_count,
        'discogs_identifier_available': discogs_identifier is not None,
        'discogs_identifier_configured': bool(
            discogs_identifier
            and discogs_identifier.DiscogsIdentifier().configured()
        ),
        'discogs_market_available': discogs_market is not None,
        'smart_value_engine_available': value_engine is not None,
    })


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
