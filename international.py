"""International record-store adapters for CrateRadar / Vinyl Hunter.

Drop-in replacement for providers/international.py.

Production-ready V1 adapters are enabled for:
- HHV
- Juno Records
- Rough Trade
- Record City
- Turntable Lab
- Artrockstore

The remaining stores stay in qualification-only mode until their access path is
proven. This module does NOT alter the Israeli engine or the Flask/UI layer.
"""
from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, asdict
from urllib.parse import quote, quote_plus, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

INTERNATIONAL_REGISTRY = [
    {"name":"HHV","country":"DE","base":"https://www.hhv.de","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Juno Records","country":"UK","base":"https://www.juno.co.uk","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Rough Trade","country":"UK/US","base":"https://www.roughtrade.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Norman Records","country":"UK","base":"https://www.normanrecords.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Dusty Groove","country":"US","base":"https://www.dustygroove.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Boomkat","country":"UK","base":"https://boomkat.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Record City","country":"JP","base":"https://www.recordcity.jp","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"iMusic","country":"DK","base":"https://imusic.co","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Record Shop X","country":"FI","base":"https://www.recordshopx.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Turntable Lab","country":"US","base":"https://www.turntablelab.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
    {"name":"Artrockstore","country":"US","base":"https://www.artrockstore.com","status":"testing","source_type":"INTERNATIONAL_STORE"},
]

ACTIVE_ADAPTERS = {
    "HHV": "hhv",
    "Juno Records": "juno",
    "Rough Trade": "roughtrade",
    "Record City": "recordcity",
    "Turntable Lab": "shopify",
    "Artrockstore": "shopify",
}

SEARCH_HINTS = {
    "Juno Records": [
        "https://www.juno.co.uk/search/?q%5Ball%5D%5B%5D={q}",
        "https://www.juno.co.uk/search/?q%5Ball%5D={q}",
    ],
    "Rough Trade": [
        "https://www.roughtrade.com/en-gb/search?q={q}",
        "https://www.roughtrade.com/search?q={q}",
    ],
    "Record City": [
        "https://www.recordcity.jp/en/catalog?keyword={q}",
        "https://www.recordcity.jp/en/search?q={q}",
        "https://www.recordcity.jp/catalog?keyword={q}",
    ],
    "Turntable Lab": [
        "https://www.turntablelab.com/search?q={q}",
    ],
    "Artrockstore": [
        "https://www.artrockstore.com/search?q={q}",
    ],
    # HHV changes its public search routes periodically, so probe a few safe
    # public catalogue/search forms and accept the first useful one.
    "HHV": [
        "https://www.hhv.de/en/records/catalog?search={q}",
        "https://www.hhv.de/en/records/search?search={q}",
        "https://www.hhv.de/en/records?q={q}",
    ],
    "Norman Records": ["https://www.normanrecords.com/search?q={q}"],
    "Boomkat": ["https://boomkat.com/search?q={q}"],
    "Dusty Groove": ["https://www.dustygroove.com/search.php?sf={q}"],
    "Record Shop X": ["https://www.recordshopx.com/search/?q={q}"],
}

PRODUCT_PATH_HINTS = {
    "Juno Records": ("/products/",),
    "Rough Trade": ("/product/",),
    "Record City": ("/catalog/", "/item/", "/product/"),
    "Turntable Lab": ("/products/",),
    "Artrockstore": ("/products/",),
    "HHV": ("/records/item/", "/records/artikel/"),
}

VINYL_WORDS = ("vinyl", " lp", "2lp", "3lp", "12\"", "7\"", "record")
NON_VINYL = (" cd", "cassette", "tape", "dvd", "blu-ray", "blu ray", "shirt", "t-shirt", "poster", "book", "mug")

@dataclass
class InternationalResult:
    source: str
    title: str
    url: str
    item_price: float | None = None
    currency: str = ""
    stock: str = "UNKNOWN"
    country: str = ""
    ships_to_israel: str = "UNKNOWN"
    media_condition: str = ""
    sleeve_condition: str = ""
    pressing: str = ""
    edition: str = ""
    match_score: float = 0.0
    adapter: str = ""
    evidence: str = ""


def registry():
    return [dict(x) for x in INTERNATIONAL_REGISTRY]


def _norm(value):
    value = html.unescape(str(value or "")).lower()
    value = value.replace("–", "-").replace("—", "-").replace("’", "'")
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def _tokens(value):
    stop = {"the", "of", "and", "a", "an", "vinyl", "lp", "record"}
    return {x for x in _norm(value).split() if len(x) > 1 and x not in stop}


def _score(title, artist, album):
    t = _tokens(title)
    a = _tokens(artist)
    b = _tokens(album)
    if not t:
        return 0.0
    ah = len(t & a)
    bh = len(t & b)
    if a and ah < max(1, min(2, len(a))):
        return 0.0
    need = max(1, int(round(len(b) * 0.70))) if b else 0
    if b and bh < need:
        return 0.0
    n = " " + _norm(title) + " "
    if any(x in n for x in NON_VINYL) and not any(x in n for x in VINYL_WORDS):
        return 0.0
    ar = ah / len(a) if a else 1.0
    br = bh / len(b) if b else 1.0
    bonus = 0.10 if any(x in n for x in VINYL_WORDS) else 0.0
    return min(1.0, 0.36 * ar + 0.54 * br + bonus)


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(s, url, timeout=8):
    r = s.get(url, timeout=(3.5, float(timeout)), allow_redirects=True)
    return r


def _price_currency(text):
    text = html.unescape(str(text or ""))
    patterns = [
        (r"£\s*([\d,.]+)", "GBP"),
        (r"€\s*([\d,.]+)", "EUR"),
        (r"\$\s*([\d,.]+)", "USD"),
        (r"¥\s*([\d,.]+)", "JPY"),
        (r"([\d,.]+)\s*€", "EUR"),
        (r"([\d,.]+)\s*£", "GBP"),
    ]
    for pat, cur in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1).replace(",", "")), cur
            except ValueError:
                pass
    return None, ""


def _stock(text):
    n = _norm(text)
    negatives = ("out of stock", "sold out", "notify me", "currently not available", "not available", "ausverkauft")
    positives = ("in stock", "add to cart", "add to basket", "same day shipping", "left in stock", "available from our supplier", "ready for shipment", "in den warenkorb")
    if any(x in n for x in negatives):
        return "OUT_OF_STOCK"
    if any(x in n for x in positives):
        return "IN_STOCK"
    return "UNKNOWN"


def _jsonld_product(soup):
    products = []
    for tag in soup.select('script[type="application/ld+json"]'):
        raw = tag.string or tag.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        for x in stack:
            if isinstance(x, dict) and "@graph" in x and isinstance(x["@graph"], list):
                stack.extend(x["@graph"])
        for x in stack:
            if isinstance(x, dict) and str(x.get("@type", "")).lower() == "product":
                products.append(x)
    return products


def _parse_product_page(store, url, artist, album, timeout=8, session=None):
    s = session or _session()
    r = _get(s, url, timeout)
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.select_one("h1")
    page_title = " ".join(h1.stripped_strings) if h1 else ""
    body = " ".join(soup.stripped_strings)

    title = page_title
    price = None
    currency = ""
    stock = "UNKNOWN"
    evidence = []

    for p in _jsonld_product(soup):
        title = str(p.get("name") or title)
        offers = p.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            raw_price = offers.get("price") or offers.get("lowPrice")
            if raw_price is not None:
                try:
                    price = float(str(raw_price).replace(",", ""))
                    currency = str(offers.get("priceCurrency") or currency)
                    evidence.append("JSON-LD price")
                except ValueError:
                    pass
            av = _norm(offers.get("availability"))
            if "instock" in av or av.endswith("in stock"):
                stock = "IN_STOCK"
            elif "outofstock" in av or "soldout" in av:
                stock = "OUT_OF_STOCK"
            if av:
                evidence.append("JSON-LD availability")

    if price is None:
        price, currency = _price_currency(body)
    if stock == "UNKNOWN":
        stock = _stock(body)

    score = _score(title, artist, album)
    if score < 0.70:
        # Some pages have only the album in H1 and artist just above it.
        meta_title = soup.title.get_text(" ", strip=True) if soup.title else title
        alt_score = _score(meta_title, artist, album)
        if alt_score > score:
            title, score = meta_title, alt_score
    if score < 0.70:
        return None, "product relevance below threshold"

    media = ""
    sleeve = ""
    pressing = ""
    nbody = body
    mm = re.search(r"(?:Medium|Media)\s*[:\-]?\s*(Mint|Near Mint|NM\+?|VG\+?|VG|G\+?|G|Fair|Poor)", nbody, re.I)
    sm = re.search(r"(?:Cover|Sleeve)\s*[:\-]?\s*(Mint|Near Mint|NM\+?|VG\+?|VG|G\+?|G|Fair|Poor)", nbody, re.I)
    pm = re.search(r"(?:Pressing|Pressung)\s*[:\-]?\s*([^|\n]{2,50})", nbody, re.I)
    if mm: media = mm.group(1).strip()
    if sm: sleeve = sm.group(1).strip()
    if pm: pressing = pm.group(1).strip()

    return InternationalResult(
        source=store["name"], title=title, url=r.url,
        item_price=price, currency=currency, stock=stock,
        country=store["country"], ships_to_israel="UNKNOWN",
        media_condition=media, sleeve_condition=sleeve, pressing=pressing,
        match_score=round(score, 3), adapter=ACTIVE_ADAPTERS.get(store["name"], "html"),
        evidence="; ".join(evidence) or "product page",
    ), ""


def _candidate_product_links(store, soup, page_url, artist, album, limit=8):
    hints = PRODUCT_PATH_HINTS.get(store["name"], ("/product/", "/products/"))
    found = []
    seen = set()
    for a in soup.select("a[href]"):
        href = urljoin(page_url, a.get("href") or "")
        if not href or href in seen:
            continue
        if urlparse(href).netloc and urlparse(href).netloc != urlparse(store["base"]).netloc:
            continue
        if not any(h in href for h in hints):
            continue
        label = " ".join(a.stripped_strings)
        if not label:
            parent = a.find_parent(["article", "li", "div"])
            label = " ".join(parent.stripped_strings) if parent else ""
        sc = _score(label, artist, album)
        if sc >= 0.62:
            seen.add(href)
            found.append((sc, href))
    found.sort(reverse=True)
    return [u for _, u in found[:limit]]


def _shopify_predictive(store, artist, album, timeout=8, session=None):
    s = session or _session()
    q = quote_plus(f"{artist} {album}")
    url = (
        store["base"].rstrip("/")
        + f"/search/suggest.json?q={q}&resources[type]=product&resources[limit]=10"
    )
    try:
        r = _get(s, url, timeout)
        if r.status_code != 200:
            return [], {"url": r.url, "status": r.status_code, "path": "shopify-predictive"}
        data = r.json()
        products = (((data or {}).get("resources") or {}).get("results") or {}).get("products") or []
        urls = []
        for p in products:
            title = p.get("title", "")
            if _score(title, artist, album) < 0.62:
                continue
            u = p.get("url") or p.get("handle")
            if not u:
                continue
            if not str(u).startswith("http"):
                u = urljoin(store["base"], str(u))
            urls.append(u)
        return urls[:8], {"url": r.url, "status": r.status_code, "path": "shopify-predictive", "items": len(products)}
    except Exception as e:
        return [], {"url": url, "path": "shopify-predictive", "error": type(e).__name__}


def search_store(store, artist, album, timeout=8):
    """Search one international store and return (results, errors, trace)."""
    name = store["name"]
    adapter = ACTIVE_ADAPTERS.get(name)
    if not adapter:
        return [], ["adapter-unresolved"], []

    s = _session()
    q = quote_plus(f"{artist} {album}")
    traces = []
    errors = []
    product_urls = []

    if adapter == "shopify":
        product_urls, tr = _shopify_predictive(store, artist, album, timeout, s)
        traces.append(tr)

    if not product_urls:
        templates = SEARCH_HINTS.get(name, [])
        for template in templates:
            search_url = template.format(q=q)
            try:
                r = _get(s, search_url, timeout)
                traces.append({"path": "html-search", "url": r.url, "status": r.status_code, "bytes": len(r.content)})
                if r.status_code >= 400:
                    continue
                soup = BeautifulSoup(r.text, "html.parser")
                product_urls = _candidate_product_links(store, soup, r.url, artist, album)
                traces[-1]["product_candidates"] = len(product_urls)
                if product_urls:
                    break
            except Exception as e:
                errors.append(type(e).__name__ + ":" + str(e)[:120])

    # Record City sometimes answers 202 first; retry the resolved search once.
    if name == "Record City" and not product_urls and traces:
        time.sleep(0.7)
        for template in SEARCH_HINTS[name][:1]:
            try:
                r = _get(s, template.format(q=q), timeout)
                traces.append({"path": "recordcity-retry", "url": r.url, "status": r.status_code, "bytes": len(r.content)})
                if r.status_code < 400:
                    soup = BeautifulSoup(r.text, "html.parser")
                    product_urls = _candidate_product_links(store, soup, r.url, artist, album)
                    traces[-1]["product_candidates"] = len(product_urls)
            except Exception as e:
                errors.append("retry:" + type(e).__name__ + ":" + str(e)[:100])

    results = []
    if product_urls:
        workers = min(4, len(product_urls))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_parse_product_page, store, u, artist, album, timeout, None) for u in product_urls]
            for f in as_completed(futs):
                try:
                    item, err = f.result()
                    if item:
                        results.append(item)
                    elif err and err != "product relevance below threshold":
                        errors.append(err)
                except Exception as e:
                    errors.append("product:" + type(e).__name__ + ":" + str(e)[:100])

    # Deduplicate URL and sort by relevance / known price.
    dedup = {}
    for x in results:
        k = x.url.rstrip("/")
        old = dedup.get(k)
        if old is None or x.match_score > old.match_score:
            dedup[k] = x
    results = sorted(dedup.values(), key=lambda x: (-x.match_score, x.item_price is None, x.item_price or 10**9))
    return results, errors, traces


def qualification_probe(store, artist, album, timeout=8):
    """Compatibility probe used by the existing tests/test_international.py.

    For the six V1 stores this now runs the real adapter and exposes the result
    count in the adapter field, so the old test file does not need to change.
    """
    started = time.time()
    name = store["name"]
    adapter = ACTIVE_ADAPTERS.get(name)

    if adapter:
        try:
            results, errors, traces = search_store(store, artist, album, timeout)
            statuses = [x.get("status") for x in traces if isinstance(x.get("status"), int)]
            successful = any(s < 400 for s in statuses)
            status = next((s for s in statuses if s < 400), statuses[0] if statuses else None)
            last_url = next((x.get("url") for x in reversed(traces) if x.get("url")), store["base"])
            return {
                "store": name,
                "online": successful,
                "status_code": status,
                "url": last_url,
                "bytes": 0,
                "seconds": round(time.time() - started, 2),
                "adapter": f"{adapter}/results={len(results)}",
                "ships_to_israel": "UNKNOWN",
                "results": [asdict(x) for x in results],
                "error": " | ".join(errors[:3]),
                "trace": traces,
            }
        except Exception as e:
            return {
                "store": name, "online": False, "status_code": None,
                "url": store["base"], "bytes": 0,
                "seconds": round(time.time() - started, 2),
                "adapter": adapter + "/crashed", "ships_to_israel": "UNKNOWN",
                "results": [], "error": type(e).__name__ + ":" + str(e)[:160], "trace": []
            }

    # Qualification-only path for stores not yet adapted.
    q = quote_plus(f"{artist} {album}")
    templates = SEARCH_HINTS.get(name) or [store["base"]]
    url = templates[0].format(q=q) if "{q}" in templates[0] else templates[0]
    try:
        r = requests.get(url, headers=HEADERS, timeout=(3, timeout), allow_redirects=True)
        ok = r.status_code < 400
        return {
            "store": name, "online": ok, "status_code": r.status_code,
            "url": r.url, "bytes": len(r.content), "seconds": round(time.time()-started,2),
            "adapter": "unresolved", "ships_to_israel": "UNKNOWN", "results": [], "error": "", "trace": []
        }
    except Exception as e:
        return {
            "store": name, "online": False, "status_code": None,
            "url": url, "bytes": 0, "seconds": round(time.time()-started,2),
            "adapter": "unresolved", "ships_to_israel": "UNKNOWN", "results": [],
            "error": type(e).__name__ + ":" + str(e)[:160], "trace": []
        }
