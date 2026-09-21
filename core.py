from __future__ import annotations
# VINYL HUNTER CORE: stability-fixed
import re, json, html, time
import threading
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup


UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/152.0.0.0 Safari/537.36'
)

S = requests.Session()
S.headers.update({
    'User-Agent': UA,
    'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8'
})


@dataclass
class Result:
    store: str
    title: str
    url: str
    price: float | None
    currency: str = 'ILS'
    stock: str = 'UNKNOWN'
    evidence: str = ''
    path: str = ''
    score: float = 0.0
    media_condition: str = ''
    sleeve_condition: str = ''


STOP = {
    'the', 'of', 'and', 'a', 'an',
    'vinyl', 'lp', 'record'
}

NON_VINYL = [
    'cd',
    'compact disc',
    'דיסק',
    'חולצה',
    't shirt',
    't-shirt',
    'shirt',
    'tee',
    'מידה',
    'slipmat',
    'slip mat',
    'turntable slip mat',
    'סליפמאט',
    'poster',
    'פוסטר',
    'book',
    'ספר',
    'mug',
    'ספל',
    'patch',
    'פאץ',
    'hoodie',
    'קפוצון',
    'cassette',
    'קסטה',
    'dvd',
    'blu ray',
    'blu-ray',
    'puzzle',
    'puzzles',
    'פאזל',
    'פאזלים',
    'gift card',
    'giftcard',
    'גיפט קארד'
]

VARIANT_WORDS = [
    'live',
    'remix',
    'tribute',
    'soundtrack',
    'karaoke',
    'cover version',
    'instrumental',
    'box',
    'box set',
    'boxset',
    'מארז'
]


def norm(s):
    s = html.unescape(str(s or ''))
    s = (
        s.lower()
        .replace('–', '-')
        .replace('—', '-')
        .replace('’', "'")
    )
    return re.sub(r'[^\w\u0590-\u05ff]+', ' ', s).strip()


def tokens(s):
    return {
        x
        for x in norm(s).split()
        if len(x) > 1 and x not in STOP
    }


def is_vinyl_candidate(title: str) -> bool:
    n = norm(title)
    return not any(norm(x) in n for x in NON_VINYL)


def relevance(title, artist, album):
    tn = norm(title)
    bn = norm(album)

    t = tokens(title)
    a = tokens(artist)
    b = tokens(album)

    artist_hits = len(a & t)
    album_hits = len(b & t)

    if a and artist_hits < max(1, min(2, len(a))):
        return 0.0

    need = max(1, int(round(len(b) * 0.72))) if b else 0

    if b and album_hits < need:
        return 0.0

    for w in VARIANT_WORDS:
        nw = norm(w)

        if nw in tn and nw not in bn:
            return 0.0

    if not is_vinyl_candidate(title):
        return 0.0

    album_ratio = (album_hits / len(b)) if b else 1.0
    artist_ratio = (artist_hits / len(a)) if a else 1.0

    vinyl_bonus = 0.12 if any(
        x in tn
        for x in ('vinyl', ' lp', '2lp', 'תקליט')
    ) else 0.0

    return min(
        1.0,
        0.55 * album_ratio
        + 0.33 * artist_ratio
        + vinyl_bonus
    )


def match(title, artist, album):
    return relevance(title, artist, album) >= 0.72


def stock_from(text):
    x = norm(text)

    negatives = [
        'המוצר אינו במלאי',
        'לא במלאי',
        'אזל מהמלאי',
        'אזל המלאי',
        'אזל',
        'out of stock',
        'sold out',
        'unavailable'
    ]

    positives = [
        'במלאי',
        'נותרו',
        'in stock',
        'add to cart',
        'הוספה לסל',
        'הוסף לסל'
    ]

    if any(norm(z) in x for z in negatives):
        return 'OUT_OF_STOCK'

    if any(norm(z) in x for z in positives):
        return 'IN_STOCK'

    return 'UNKNOWN'


def price_from(text):
    patterns = [
        r'₪\s*([\d,.]+)',
        r'([\d,.]+)\s*₪'
    ]

    for pattern in patterns:
        m = re.search(pattern, text or '')

        if not m:
            continue

        try:
            return float(
                m.group(1).replace(',', '')
            )
        except Exception:
            pass

    return None



def tav8_price_from(text):
    """Extract Tav8's real selling price while ignoring placeholder 0 ₪ values."""
    text = text or ''

    # Tav8 catalog cards expose the normal price as "מחיר 149 ₪".
    m = re.search(r'מחיר\s*([\d,.]+)\s*₪', text, flags=re.I)
    if m:
        try:
            value = float(m.group(1).replace(',', ''))
            if value > 0:
                return value
        except Exception:
            pass

    # Fallback: choose the first positive ILS price, never a 0 ₪ placeholder.
    vals = re.findall(r'(?:₪\s*([\d,.]+)|([\d,.]+)\s*₪)', text)
    for left, right in vals:
        raw = left or right
        try:
            value = float(raw.replace(',', ''))
            if value > 0:
                return value
        except Exception:
            pass

    return None



_TLS = threading.local()

def _session():
    sess = getattr(_TLS, 'session', None)

    if sess is None:
        sess = requests.Session()
        sess.headers.update({
            'User-Agent': UA,
            'Accept-Language': 'he-IL,he;q=0.9,en;q=0.8'
        })
        _TLS.session = sess

    return sess


def get(url, timeout=7):
    r = _session().get(
        url,
        timeout=(3.0, float(timeout)),
        allow_redirects=True
    )

    r.raise_for_status()

    return r


def _context_for_anchor(a):
    for tag in ('li', 'article', 'div'):
        p = a.find_parent(tag)

        if not p:
            continue

        classes = ' '.join(
            p.get('class') or []
        ).lower()

        if any(
            k in classes
            for k in ('product', 'item', 'card', 'grid')
        ):
            return ' '.join(p.stripped_strings)

    return ' '.join(a.stripped_strings)


HTML_ONLY_WOO = {'Beatnik', 'Shablool', 'Third Ear', 'My Records', 'The Vinyl Room', 'Party Concept'}


def woo(store, base, artist, album):
    q = quote_plus(
        f'{artist} {album}'
    )

    urls = [
        (
            f'{base.rstrip("/")}'
            f'/wp-json/wc/store/v1/products'
            f'?search={q}&per_page=20'
        ),
        (
            f'{base.rstrip("/")}'
            f'/?s={q}&post_type=product'
        )
    ]

    out = []
    errs = []
    traces = []

    if store not in HTML_ONLY_WOO:
        try:
            r = get(urls[0])

            traces.append({
                'path': 'woocommerce',
                'url': r.url,
                'status': r.status_code,
                'bytes': len(r.content)
            })

            data = r.json()

            traces[-1]['items'] = (
                len(data)
                if isinstance(data, list)
                else None
            )

            for p in data if isinstance(data, list) else []:
                title = p.get('name', '')
                link = p.get('permalink', '')

                score = relevance(
                    title,
                    artist,
                    album
                )

                if not link or score < 0.72:
                    continue

                prices = p.get('prices') or {}
                raw = prices.get('price')

                minor = int(
                    prices.get(
                        'currency_minor_unit',
                        2
                    ) or 2
                )

                price = (
                    float(raw) / (10 ** minor)
                    if str(raw or '').isdigit()
                    else None
                )

                ss = p.get('is_in_stock')

                if ss is True:
                    st = 'IN_STOCK'
                elif ss is False:
                    st = 'OUT_OF_STOCK'
                else:
                    st = 'UNKNOWN'

                out.append(
                    Result(
                        store=store,
                        title=title,
                        url=link,
                        price=price,
                        stock=st,
                        evidence='Woo Store API stock field',
                        path='woocommerce',
                        score=score
                    )
                )

        except Exception as e:
            errs.append(
                'woo:'
                + type(e).__name__
                + ':'
                + str(e)[:120]
            )

            traces.append({
                'path': 'woocommerce',
                'url': urls[0],
                'error': type(e).__name__
            })
    else:
        traces.append({
            'path': 'woocommerce',
            'url': urls[0],
            'skipped': True,
            'reason': 'html-only store'
        })

    if not out:
        try:
            r = get(urls[1])

            traces.append({
                'path': 'html-search',
                'url': r.url,
                'status': r.status_code,
                'bytes': len(r.content)
            })

            soup = BeautifulSoup(
                r.text,
                'html.parser'
            )

            links = 0
            accepted = 0
            # Some WooCommerce stores redirect an exact search directly
            # to the matching product page instead of showing search results.
            if '/product/' in r.url:
                h1 = soup.select_one(
                    'h1.product_title, h1.entry-title, h1'
                )

                title = (
                    ' '.join(h1.stripped_strings)
                    if h1
                    else ''
                )

                score = relevance(
                    title,
                    artist,
                    album
                )

                if title and score >= 0.72:
                    product = soup.select_one(
                        'div.product.type-product, '
                        'article.product.type-product, '
                        '[itemtype*="Product"], '
                        '#product'
                    )

                    context = (
                        ' '.join(product.stripped_strings)
                        if product
                        else title
                    )

                    st = stock_from(context)
                    price = price_from(context)

                    out.append(
                        Result(
                            store=store,
                            title=title,
                            url=r.url,
                            price=price,
                            stock=st,
                            evidence='direct search redirect',
                            path='html-direct-product',
                            score=score
                        )
                    )

                    accepted += 1
            for a in soup.select('a[href]'):
                title = ' '.join(
                    a.stripped_strings
                )

                link = urljoin(
                    r.url,
                    a.get('href')
                )

                if not title:
                    continue

                if '/product/' not in link:
                    continue

                links += 1

                score = relevance(
                    title,
                    artist,
                    album
                )

                if score < 0.72:
                    continue

                context = _context_for_anchor(a)

                st = stock_from(context)
                price = price_from(context)

                if st != 'UNKNOWN' or price is not None:
                    evidence = 'store search card'
                else:
                    evidence = 'store search page'

                out.append(
                    Result(
                        store=store,
                        title=title,
                        url=link,
                        price=price,
                        stock=st,
                        evidence=evidence,
                        path='html-search',
                        score=score
                    )
                )

                accepted += 1

            traces[-1]['product_links_seen'] = links
            traces[-1]['accepted'] = accepted

        except Exception as e:
            errs.append(
                'html:'
                + type(e).__name__
                + ':'
                + str(e)[:120]
            )

            traces.append({
                'path': 'html-search',
                'url': urls[1],
                'error': type(e).__name__
            })

    return dedupe(out), errs, traces



def rockstore1970(artist, album):
    """Rockstore 1970 adapter.

    The store can redirect a search directly to a product page whose H1
    contains only the artist. The album title is therefore validated against
    the beginning of the main product container, not the H1 alone.
    """
    q = quote_plus(f'{artist} {album}')
    u = f'https://rockstore1970.co.il/?s={q}&post_type=product'

    out = []
    errs = []
    traces = []

    try:
        r = get(u)

        trace = {
            'path': 'rockstore-search',
            'url': r.url,
            'status': r.status_code,
            'bytes': len(r.content)
        }
        traces.append(trace)

        soup = BeautifulSoup(r.text, 'html.parser')

        # Live-proven behavior: an exact search may redirect straight
        # to /product/... and the H1 may contain only the artist.
        if '/product/' in r.url:
            h1 = soup.select_one(
                'h1.product_title, h1.entry-title, h1'
            )
            artist_title = (
                ' '.join(h1.stripped_strings)
                if h1 else ''
            )

            product = soup.select_one(
                'div.product.type-product, '
                'article.product.type-product, '
                '[itemtype*="Product"], '
                '#product'
            )

            product_text = (
                ' '.join(product.stripped_strings)
                if product else ''
            )

            # Limit matching to the start of the product content so related
            # products farther down the page cannot create false positives.
            main_text = product_text[:1200]
            artist_ok = bool(tokens(artist) & tokens(artist_title))
            album_tokens = tokens(album)
            album_hits = len(album_tokens & tokens(main_text))
            album_need = max(
                1,
                int(round(len(album_tokens) * 0.72))
            ) if album_tokens else 0

            if artist_ok and album_hits >= album_need:
                display_title = f'{artist_title} – {album} LP'

                # First product price belongs to the main product; later
                # .price nodes can belong to related products.
                price_node = None
                if product:
                    price_node = product.select_one(
                        '.summary .price, .entry-summary .price, .price'
                    )
                if price_node is None:
                    price_node = soup.select_one(
                        '.summary .price, .entry-summary .price, .price'
                    )

                price = price_from(
                    ' '.join(price_node.stripped_strings)
                    if price_node else main_text[:500]
                )

                main_lower = norm(main_text[:700])
                negative = stock_from(main_text[:700])

                if negative == 'OUT_OF_STOCK':
                    stock = 'OUT_OF_STOCK'
                    evidence = 'Rockstore product stock text'
                elif (
                    'הוספה לסל' in main_text[:700]
                    or 'add to cart' in main_lower
                ):
                    stock = 'IN_STOCK'
                    evidence = 'Rockstore add-to-cart control'
                else:
                    stock = 'UNKNOWN'
                    evidence = 'Rockstore exact product page'

                out.append(
                    Result(
                        store='Rockstore 1970',
                        title=display_title,
                        url=r.url,
                        price=price,
                        stock=stock,
                        evidence=evidence,
                        path='rockstore-direct-product',
                        score=1.0
                    )
                )

                trace['accepted'] = 1
            else:
                trace['accepted'] = 0
                trace['artist_ok'] = artist_ok
                trace['album_hits'] = album_hits
                trace['album_need'] = album_need
        else:
            # Do not guess at a general result-card structure until it is
            # observed live. Zero results is safer than a false positive.
            trace['accepted'] = 0
            trace['note'] = 'no direct product redirect'

    except Exception as e:
        errs.append(
            type(e).__name__ + ':' + str(e)[:160]
        )
        traces.append({
            'path': 'rockstore-search',
            'url': u,
            'error': type(e).__name__
        })

    return dedupe(out), errs, traces


def tav8(artist, album):
    """Tav8 / התו השמיני adapter.

    Live-observed route:
    SearchResults.aspx?Search=<artist>
      -> exact artist link with CatId_3
      -> artist catalog
      -> exact album product-id.aspx link
      -> product page for stock verification.
    """
    base = 'https://www.tav8.co.il'
    out, errs, traces = [], [], []

    try:
        search_url = (
            base
            + '/SearchResults.aspx?Search='
            + quote_plus(artist)
        )
        sr = get(search_url, 12)
        ss = BeautifulSoup(sr.text, 'html.parser')

        artist_link = None
        wanted_artist = norm(artist)

        for a in ss.select('a.artist-item[href]'):
            label = a.get('data-name') or a.get_text(' ', strip=True)
            if norm(label) == wanted_artist:
                artist_link = urljoin(sr.url, a.get('href', ''))
                break

        traces.append({
            'path': 'tav8-artist-search',
            'url': sr.url,
            'status': sr.status_code,
            'bytes': len(sr.content),
            'artist_found': bool(artist_link)
        })

        if not artist_link:
            return out, errs, traces

        cr = get(artist_link, 15)
        cs = BeautifulSoup(cr.text, 'html.parser')

        # Tav8 repeats the same product URL across title, price, view-product
        # and stock anchors. Group by URL and choose only title anchors that
        # pass the existing global relevance matcher.
        product_urls = {}
        for a in cs.select('a[href*="product-id.aspx"]'):
            href = urljoin(cr.url, a.get('href', ''))
            label = a.get_text(' ', strip=True)
            if href not in product_urls:
                product_urls[href] = []
            if label:
                product_urls[href].append((a, label))

        accepted = []

        # Cheap prefilter on the artist catalog before opening product pages.
        shortlisted = []

        for href, anchors in product_urls.items():
            best = None
            best_score = 0.0

            for a, label in anchors:
                sc = relevance(label, artist, album)
                if sc > best_score:
                    best = (a, label)
                    best_score = sc

            if best and best_score >= 0.72:
                shortlisted.append((href, best, best_score))

        for href, best, best_score in shortlisted:
            a, title = best

            a, title = best

            # Tav8-specific variant guard. The standard album query must not
            # accept box sets, tribute/return variants, or Hebrew "מארז" editions.
            title_n = norm(title)
            album_n = norm(album)
            blocked_variants = ('box', 'box set', 'מארז', 'return to')
            if any(v in title_n and v not in album_n for v in blocked_variants):
                continue

            # Walk upward only until we find the local card containing price
            # or explicit stock text. Avoid consuming neighboring products.
            card_text = ''
            node = a
            for _ in range(7):
                node = getattr(node, 'parent', None)
                if node is None:
                    break
                txt = ' '.join(node.stripped_strings)
                if len(txt) > 3500:
                    break
                if (
                    '₪' in txt
                    or 'אזל' in txt
                    or 'במלאי' in txt
                ):
                    card_text = txt
                    if len(txt) >= len(title) + 15:
                        break

            price = tav8_price_from(card_text)
            card_stock = stock_from(card_text)

            # Product page is authoritative for explicit Tav8 stock wording.
            product_stock = card_stock
            evidence = 'Tav8 artist catalog card'

            try:
                pr = get(href, 10)
                ps = BeautifulSoup(pr.text, 'html.parser')
                page_text = ' '.join(ps.stripped_strings)

                # Tav8 product title is reliable and should still match.
                page_title = (
                    ps.title.get_text(' ', strip=True)
                    if ps.title else title
                )

                if relevance(page_title, artist, album) < 0.72:
                    continue

                # Explicit negative wording must win.
                if 'המוצר אינו במלאי' in page_text:
                    product_stock = 'OUT_OF_STOCK'
                    evidence = 'Tav8 exact product stock text'
                else:
                    page_stock = stock_from(page_text[:5000])
                    if page_stock != 'UNKNOWN':
                        product_stock = page_stock
                        evidence = 'Tav8 exact product stock text'

                if price is None:
                    price = tav8_price_from(page_text[:5000])

            except Exception as e:
                evidence += '; product verification failed:' + type(e).__name__

            accepted.append(
                Result(
                    store='Tav8',
                    title=title,
                    url=href,
                    price=price,
                    stock=product_stock,
                    evidence=evidence,
                    path='tav8-artist-catalog',
                    score=best_score
                )
            )

        traces.append({
            'path': 'tav8-artist-catalog',
            'url': cr.url,
            'status': cr.status_code,
            'bytes': len(cr.content),
            'product_urls_seen': len(product_urls),
            'product_fetches': len(shortlisted),
            'accepted': len(accepted)
        })

        out.extend(accepted)

    except Exception as e:
        errs.append(type(e).__name__ + ':' + str(e)[:160])

    return dedupe(out), errs, traces

def terminalx(artist, album):
    q = quote_plus(
        f'{artist} {album}'
    )

    candidates = []
    errs = []
    traces = []

    urls = [
        f'https://www.terminalx.com/catalogsearch/result/?q={q}'
    ]

    for u in urls:
        try:
            r = get(u)

            traces.append({
                'path': 'terminalx-search',
                'url': r.url,
                'status': r.status_code,
                'bytes': len(r.content)
            })

            soup = BeautifulSoup(
                r.text,
                'html.parser'
            )

            links = 0
            accepted = 0

            for a in soup.select('a[href]'):
                title = ' '.join(
                    a.stripped_strings
                )

                link = urljoin(
                    r.url,
                    a.get('href')
                )

                if not title:
                    continue

                if 'terminalx.com' not in link:
                    continue

                links += 1

                score = relevance(
                    title,
                    artist,
                    album
                )

                if score < 0.72:
                    continue

                context = _context_for_anchor(a)

                st = stock_from(context)
                price = price_from(context)

                if st != 'UNKNOWN' or price is not None:
                    evidence = 'Terminal X search card'
                else:
                    evidence = 'Terminal X search page'

                candidates.append(
                    Result(
                        store='Terminal X',
                        title=title,
                        url=link,
                        price=price,
                        stock=st,
                        evidence=evidence,
                        path='terminalx-search',
                        score=score
                    )
                )

                accepted += 1

            traces[-1]['links_seen'] = links
            traces[-1]['accepted'] = accepted

        except Exception as e:
            errs.append(
                type(e).__name__
                + ':'
                + str(e)[:120]
            )

            traces.append({
                'path': 'terminalx-search',
                'url': u,
                'error': type(e).__name__
            })

        if candidates:
            break

    return dedupe(candidates), errs, traces


def kamea(artist, album):
    """Kamea Shopify adapter using search only for product discovery."""
    base = 'https://shop.kameamusic.co.il'
    q = quote_plus(f'{artist} {album}')
    search_url = f'{base}/search?q={q}&type=product'
    candidates, errs, traces = [], [], []

    def kamea_get(url):
        last = None
        for attempt in range(3):
            try:
                x = _session().get(
                    url, timeout=(8.0, 20.0), allow_redirects=True
                )
                x.raise_for_status()
                return x
            except Exception as e:
                last = e
                if attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
        raise last

    try:
        r = kamea_get(search_url)
        soup = BeautifulSoup(r.text, 'html.parser')
        product_urls = []

        for a in soup.select('a[href*="/products/"]'):
            link = urljoin(r.url, a.get('href', '')).split('?', 1)[0]
            if '/products/' in link and link not in product_urls:
                product_urls.append(link)

        trace = {
            'path': 'kamea-shopify-search',
            'url': r.url,
            'status': r.status_code,
            'bytes': len(r.content),
            'products_seen': len(product_urls),
            'accepted': 0
        }
        traces.append(trace)

        def load_product(link):
            x = kamea_get(link + '.js')
            data = x.json()
            description = BeautifulSoup(
                data.get('description') or '', 'html.parser'
            ).get_text(' ', strip=True)
            return link, data, description

        loaded = []
        if product_urls:
            with ThreadPoolExecutor(max_workers=min(4, len(product_urls))) as ex:
                futures = {
                    ex.submit(load_product, link): link
                    for link in product_urls
                }
                for future in as_completed(futures):
                    link = futures[future]
                    try:
                        loaded.append(future.result())
                    except Exception as e:
                        errs.append(
                            'product-json:' + type(e).__name__ + ':'
                            + link.rsplit('/', 1)[-1]
                        )

        for link, data, description in loaded:
            vendor = str(data.get('vendor') or '').strip()
            product_title = str(data.get('title') or '').strip()
            title = ' - '.join(x for x in (vendor, product_title) if x)

            # Kamea also sells CDs and cassettes. The Shopify description
            # exposes the Discogs-derived format for reliable filtering.
            if 'vinyl' not in norm(description):
                continue

            score = relevance(title, artist, album)
            if score < 0.72:
                continue

            try:
                price = float(data.get('price')) / 100.0
            except Exception:
                price = None

            stock = (
                'IN_STOCK' if data.get('available') is True
                else 'OUT_OF_STOCK' if data.get('available') is False
                else 'UNKNOWN'
            )
            candidates.append(Result(
                store='Kamea',
                title=title,
                url=link,
                price=price,
                stock=stock,
                evidence='Kamea Shopify product JSON',
                path='kamea-shopify-search+product-js',
                score=score
            ))

        trace['accepted'] = len(candidates)
    except Exception as e:
        errs.append(type(e).__name__ + ':' + str(e)[:120])
        traces.append({
            'path': 'kamea-shopify-search',
            'url': search_url,
            'error': type(e).__name__
        })

    return dedupe(candidates), errs, traces



def _extract_conditions(text):
    """Extract Discogs-style media/sleeve conditions when a store exposes them."""
    text = str(text or '')
    media = ''
    sleeve = ''
    m = re.search(r'Media\s+Condition\s*:\s*([^|\n\r]+?)(?=Sleeve\s+Condition|Country\s*:|Released\s*:|Genre\s*:|$)', text, flags=re.I)
    if m:
        media = re.sub(r'\s+', ' ', m.group(1)).strip(' -–:')
    m = re.search(r'Sleeve\s+Condition\s*:\s*([^|\n\r]+?)(?=Country\s*:|Released\s*:|Genre\s*:|$)', text, flags=re.I)
    if m:
        sleeve = re.sub(r'\s+', ' ', m.group(1)).strip(' -–:')
    return media, sleeve


def shopify(store, base, artist, album, require_vinyl_in_description=False):
    """Generic Shopify adapter: search -> product URLs -> product.js JSON."""
    base = base.rstrip('/')
    q = quote_plus(f'{artist} {album}')
    search_url = f'{base}/search?q={q}&type=product'
    candidates, errs, traces = [], [], []

    def shop_get(url):
        last = None
        for attempt in range(2):
            try:
                x = _session().get(url, timeout=(4.0, 12.0), allow_redirects=True)
                x.raise_for_status()
                return x
            except Exception as e:
                last = e
                if attempt < 1:
                    time.sleep(0.25 * (attempt + 1))
        raise last

    try:
        r = shop_get(search_url)
        soup = BeautifulSoup(r.text, 'html.parser')
        all_product_urls = []
        shortlisted_urls = []

        artist_tokens = tokens(artist)
        album_tokens = tokens(album)

        for a in soup.select('a[href*="/products/"]'):
            link = urljoin(r.url, a.get('href', '')).split('?', 1)[0]

            if '/products/' not in link or link in all_product_urls:
                continue

            all_product_urls.append(link)

            label = ' '.join(a.stripped_strings)
            context = _context_for_anchor(a)
            hay = tokens(label + ' ' + context)

            artist_hits = len(artist_tokens & hay)
            album_hits = len(album_tokens & hay)

            artist_need = max(1, min(2, len(artist_tokens))) if artist_tokens else 0
            album_need = max(1, int(round(len(album_tokens) * 0.50))) if album_tokens else 0

            if (
                artist_hits >= artist_need
                and album_hits >= album_need
            ):
                shortlisted_urls.append(link)

        # Safety fallback: if the search-card prefilter finds nothing,
        # use the original full Shopify result list so we do not lose matches.
        product_urls = shortlisted_urls or all_product_urls

        trace = {
            'path': 'shopify-search',
            'url': r.url,
            'status': r.status_code,
            'bytes': len(r.content),
            'products_seen': len(all_product_urls),
            'product_fetches': len(product_urls),
            'accepted': 0
        }
        traces.append(trace)

        def load_product(link):
            x = shop_get(link + '.js')
            data = x.json()
            description = BeautifulSoup(
                data.get('description') or '', 'html.parser'
            ).get_text(' ', strip=True)
            return link, data, description

        loaded = []
        if product_urls:
            with ThreadPoolExecutor(max_workers=min(2, len(product_urls))) as ex:
                futures = {ex.submit(load_product, link): link for link in product_urls}
                for future in as_completed(futures):
                    link = futures[future]
                    try:
                        loaded.append(future.result())
                    except Exception as e:
                        errs.append('product-json:' + type(e).__name__ + ':' + link.rsplit('/', 1)[-1])

        for link, data, description in loaded:
            product_title = str(data.get('title') or '').strip()
            vendor = str(data.get('vendor') or '').strip()
            product_type = str(data.get('type') or '').strip()
            searchable = ' '.join((product_title, vendor, product_type, description[:1200]))

            if not is_vinyl_candidate(searchable):
                continue
            if require_vinyl_in_description and 'vinyl' not in norm(description):
                continue

            score = relevance(product_title, artist, album)
            if score < 0.72:
                # Some Shopify stores keep the artist only in vendor.
                score = relevance(' - '.join(x for x in (vendor, product_title) if x), artist, album)
            if score < 0.72:
                continue

            try:
                price = float(data.get('price')) / 100.0
            except Exception:
                price = None

            stock = (
                'IN_STOCK' if data.get('available') is True
                else 'OUT_OF_STOCK' if data.get('available') is False
                else 'UNKNOWN'
            )
            media_condition, sleeve_condition = _extract_conditions(description)
            candidates.append(Result(
                store=store,
                title=product_title or searchable[:160],
                url=link,
                price=price,
                stock=stock,
                evidence=f'{store} Shopify product JSON',
                path='shopify-search+product-js',
                score=score,
                media_condition=media_condition,
                sleeve_condition=sleeve_condition
            ))

        trace['accepted'] = len(candidates)
    except Exception as e:
        errs.append(type(e).__name__ + ':' + str(e)[:160])
        traces.append({'path': 'shopify-search', 'url': search_url, 'error': type(e).__name__})

    return dedupe(candidates), errs, traces

def _jsonld_nodes(data):
    if isinstance(data, list):
        for x in data:
            yield from _jsonld_nodes(x)

    elif isinstance(data, dict):
        yield data

        if isinstance(
            data.get('@graph'),
            list
        ):
            for x in data['@graph']:
                yield from _jsonld_nodes(x)


def verify(r: Result):
    try:
        for attempt in range(3):
            try:
                x = _session().get(r.url, timeout=(8.0, 15.0), allow_redirects=True)
                x.raise_for_status()
                break
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))

        soup = BeautifulSoup(
            x.text,
            'html.parser'
        )

        jsonld_stock = None

        for sc in soup.select(
            'script[type="application/ld+json"]'
        ):
            try:
                data = json.loads(
                    sc.string or '{}'
                )
            except Exception:
                continue

            for n in _jsonld_nodes(data):
                if str(
                    n.get('@type', '')
                ).lower() != 'product':
                    continue

                off = n.get('offers') or {}

                if (
                    isinstance(off, list)
                    and off
                ):
                    off = off[0]

                if not isinstance(
                    off,
                    dict
                ):
                    continue

                if r.price is None:
                    try:
                        r.price = float(
                            off.get('price')
                        )
                    except Exception:
                        pass

                availability = str(
                    off.get(
                        'availability',
                        ''
                    )
                ).lower()

                if 'outofstock' in availability:
                    jsonld_stock = 'OUT_OF_STOCK'

                elif 'instock' in availability:
                    jsonld_stock = 'IN_STOCK'

        product = soup.select_one(
            'div.product.type-product, '
            'article.product.type-product, '
            '[itemtype*="Product"], '
            '.product-info-main, '
            '#product'
        )

        summary = None

        if product:
            summary = product.select_one(
                '.summary.entry-summary, '
                '.summary, '
                '.product-summary, '
                '.product-info-summary, '
                '.entry-summary'
            )

        scope = summary or product

        stock_bits = []

        if scope:
            for el in scope.select(
                '.stock, '
                '.availability, '
                '.woocommerce-variation-availability, '
                'form.cart, '
                '.single_add_to_cart_button, '
                '[class*="stock"]'
            ):
                txt = ' '.join(
                    el.stripped_strings
                )

                if txt:
                    stock_bits.append(txt)

            if not stock_bits:
                txt = ' '.join(
                    scope.stripped_strings
                )

                if txt:
                    stock_bits.append(txt)

        scoped_text = ' '.join(
            stock_bits
        )

        scoped_stock = (
            stock_from(scoped_text)
            if scoped_text
            else 'UNKNOWN'
        )

        search_stock_known = (
            r.stock in (
                'IN_STOCK',
                'OUT_OF_STOCK'
            )
        )

        preserve_shablool = (
            r.store == 'Shablool'
            and search_stock_known
        )

        preserve_tav8 = (
            r.store == 'Tav8'
            and search_stock_known
        )

        preserve_adapter_stock = preserve_shablool or preserve_tav8

        if jsonld_stock is not None:
            if preserve_adapter_stock:
                if preserve_shablool:
                    r.evidence = (
                        'store search card; '
                        'Shablool page verification ignored'
                    )
                else:
                    r.evidence = (
                        'Tav8 exact product stock text; '
                        'generic JSON-LD verification ignored'
                    )
            else:
                r.stock = jsonld_stock
                r.evidence = (
                    'JSON-LD Offer availability'
                )

        elif not preserve_adapter_stock:
            if scoped_stock == 'OUT_OF_STOCK':
                r.stock = 'OUT_OF_STOCK'
                r.evidence = (
                    'exact product stock control'
                )

            elif scoped_stock == 'IN_STOCK':
                r.stock = 'IN_STOCK'
                r.evidence = (
                    'exact product stock control'
                )

        else:
            if preserve_shablool:
                r.evidence = (
                    'store search card; '
                    'Shablool stock preserved'
                )
            elif preserve_tav8:
                r.evidence = (
                    'Tav8 exact product stock text; '
                    'adapter stock preserved'
                )

        price_text = (
            ' '.join(scope.stripped_strings)
            if scope
            else ''
        )

        if (
            r.price is None
            and price_text
        ):
            if r.store == 'Tav8':
                r.price = tav8_price_from(price_text)
            else:
                r.price = price_from(
                    price_text
                )

        return r

    except Exception as e:
        r.evidence = (
            (
                r.evidence + '; '
                if r.evidence
                else ''
            )
            + 'verification failed:'
            + type(e).__name__
        )

        return r


def dedupe(rs):
    d = {}

    for r in rs:
        parsed = urlparse(
            r.url
        )._replace(
            fragment=''
        )

        k = (
            r.store,
            parsed.geturl().rstrip('/')
        )

        old = d.get(k)

        if (
            old is None
            or r.score > old.score
        ):
            d[k] = r

    return sorted(
        d.values(),
        key=lambda r: (
            -r.score,
            r.store,
            r.title
        )
    )


def _verify_parallel(
    results,
    workers=6
):
    if not results:
        return results

    out = [None] * len(results)

    with ThreadPoolExecutor(
        max_workers=min(
            workers,
            len(results)
        )
    ) as ex:

        futs = {
            ex.submit(
                verify,
                r
            ): i
            for i, r
            in enumerate(results)
        }

        for f in as_completed(futs):
            i = futs[f]

            try:
                out[i] = f.result()

            except Exception:
                out[i] = results[i]

                out[i].evidence = (
                    (
                        out[i].evidence + '; '
                        if out[i].evidence
                        else ''
                    )
                    + 'verification worker failed'
                )

    return [
        r
        for r in out
        if r is not None
    ]


STORE_REGISTRY = [
    {'name': 'Beatnik', 'base': 'https://www.beatnik.co.il', 'adapter': 'woo', 'status': 'live'},
    {'name': 'Shablool', 'base': 'https://shabloolrecords.co.il', 'adapter': 'woo', 'status': 'live'},
    {'name': 'UFO Music', 'base': 'https://www.ufomusic.co.il', 'adapter': 'woo', 'status': 'live'},
    {'name': 'Giora Records', 'base': 'https://www.giorarecords.co.il', 'adapter': 'woo', 'status': 'live'},
    {'name': 'Third Ear', 'base': 'https://third-ear.com', 'adapter': 'woo', 'status': 'live'},
    {'name': 'Rockstore 1970', 'base': 'https://rockstore1970.co.il', 'adapter': 'rockstore', 'status': 'live'},
    {'name': 'Tav8', 'base': 'https://www.tav8.co.il', 'adapter': 'tav8', 'status': 'live'},
    {'name': 'Terminal X', 'base': 'https://www.terminalx.com', 'adapter': 'terminalx', 'status': 'live'},
    {'name': 'Kamea', 'base': 'https://shop.kameamusic.co.il', 'adapter': 'kamea', 'status': 'live'},
    {'name': 'Hod Hamahat', 'base': 'https://hodhamahat.com', 'adapter': 'shopify', 'status': 'live'},
    {'name': 'Vinylia', 'base': 'https://vinyliarecords.co.il', 'adapter': 'shopify', 'status': 'testing'},
    {'name': 'Holit', 'base': 'https://holit-records.co.il', 'adapter': 'shopify', 'status': 'testing'},
    {'name': 'The Vinyl Room', 'base': 'https://thevinylroom.co.il', 'adapter': 'woo', 'status': 'testing'},
    {'name': 'My Records', 'base': 'https://www.my-records.co.il', 'adapter': 'woo', 'status': 'testing'},

    {'name': 'Party Concept', 'base': 'https://partyconcept.co.il', 'adapter': 'woo', 'status': 'live'},
]


def _store_call(store, artist, album):
    name = store['name']
    base = store['base']
    adapter = store['adapter']
    if adapter == 'woo':
        return woo(name, base, artist, album)
    if adapter == 'shopify':
        return shopify(name, base, artist, album)
    if adapter == 'rockstore':
        return rockstore1970(artist, album)
    if adapter == 'tav8':
        return tav8(artist, album)
    if adapter == 'terminalx':
        return terminalx(artist, album)
    if adapter == 'kamea':
        return kamea(artist, album)
    return [], [f'unknown-adapter:{adapter}'], []


def store_registry():
    return [dict(x) for x in STORE_REGISTRY]


def search(artist, album, verify_pages=True, include_testing=True):
    """Search configured Israeli stores in parallel."""
    stores = [x for x in STORE_REGISTRY if include_testing or x['status'] == 'live']
    allr = []
    diag = [None] * len(stores)

    def run_job(index, store):
        started = time.time()
        try:
            rs, errs, traces = _store_call(store, artist, album)
        except Exception as e:
            rs, errs, traces = [], [type(e).__name__ + ':' + str(e)[:160]], []
        return index, rs, {
            'store': store['name'],
            'configured_status': store['status'],
            'adapter': store['adapter'],
            'base': store['base'],
            'candidates': len(rs),
            'errors': errs,
            'seconds': round(time.time() - started, 2),
            'trace': traces
        }

    with ThreadPoolExecutor(max_workers=min(6, len(stores))) as ex:
        futures = [ex.submit(run_job, i, store) for i, store in enumerate(stores)]
        for future in as_completed(futures):
            i, rs, item = future.result()
            diag[i] = item
            allr.extend(rs)

    allr = dedupe(allr)

    if verify_pages:
        # Product-page verification is expensive. Only verify results whose
        # adapter did not already provide both an authoritative price and stock.
        # Shopify product JSON, Tav8, Rockstore and Kamea already inspect a
        # product-level source, while search-card results are verified only when
        # a field is still unknown.
        needs_verify = [
            r for r in allr
            if r.stock == 'UNKNOWN' or r.price is None
        ]
        if needs_verify:
            verified = _verify_parallel(needs_verify, workers=10)
            by_key = {(r.store, r.url): r for r in verified}
            allr = [by_key.get((r.store, r.url), r) for r in allr]
        for item in diag:
            failures = [
                {'url': result.url, 'error': result.evidence}
                for result in allr
                if result.store == item['store']
                and ('verification failed:' in result.evidence
                     or 'verification worker failed' in result.evidence)
            ]
            item['verification_errors'] = failures
            item['errors'].extend(
                'verification:' + failure['url'] + ':' + failure['error']
                for failure in failures
            )

    stock_rank = {'IN_STOCK': 0, 'UNKNOWN': 1, 'OUT_OF_STOCK': 2}
    allr.sort(key=lambda r: (
        stock_rank.get(r.stock, 1),
        r.price is None,
        r.price if r.price is not None else 10**9,
        -r.score,
        r.store,
        r.title
    ))
    return allr, diag


def diagnose(artist='Pink Floyd', album='Animals'):
    """Run one adapter-level diagnostic sweep for every configured store.

    A store can be healthy even when the test album is absent. WooCommerce
    Store API JSON failures are treated as a soft fallback when the HTML search
    path itself loads successfully.
    """
    _, diag = search(artist, album, verify_pages=False, include_testing=True)
    out = []
    for item in diag:
        traces = item.get('trace') or []
        http_traces = [t for t in traces if isinstance(t, dict)]
        http_ok = any(t.get('status') == 200 for t in http_traces)
        html_ok = any(
            t.get('status') == 200 and t.get('path') in {
                'html-search', 'html-direct-product', 'rockstore-search',
                'tav8-artist-search', 'tav8-artist-catalog',
                'terminalx-search', 'kamea-shopify-search', 'shopify-search'
            }
            for t in http_traces
        )

        raw_errors = list(item.get('errors') or [])
        # Many Woo stores disable the Store API but serve HTML search perfectly.
        # Do not mark that as a failed search when the fallback path succeeded.
        soft_errors = [
            e for e in raw_errors
            if item.get('adapter') == 'woo'
            and html_ok
            and (e.startswith('woo:JSONDecodeError:') or e.startswith('woo:HTTPError:'))
        ]
        hard_errors = [e for e in raw_errors if e not in soft_errors]
        found = item.get('candidates', 0) > 0
        search_ok = http_ok and not hard_errors
        state = (
            'FOUND' if search_ok and found
            else 'NO_MATCH' if search_ok
            else 'ERROR' if http_ok
            else 'OFFLINE'
        )

        out.append({
            **item,
            'errors': hard_errors,
            'soft_errors': soft_errors,
            'online': http_ok,
            'search_ok': search_ok,
            'result_found': found,
            'state': state,
        })
    return out
