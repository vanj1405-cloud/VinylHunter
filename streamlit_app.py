
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from io import BytesIO

import requests
import streamlit as st
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import pytesseract

import core
import discogs_identifier
import discogs_market
import international as international_provider
import value_engine


st.set_page_config(
    page_title="Vinyl Hunter",
    page_icon="💿",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------- secrets -> environment ----------
def _secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value or "").strip()
    except Exception:
        return str(os.getenv(name, default) or "").strip()

for _name in ("DISCOGS_TOKEN", "DISCOGS_USERNAME"):
    _value = _secret(_name)
    if _value:
        os.environ[_name] = _value


# ---------- UI ----------
st.markdown(
    """
<style>
:root{
  --bg:#090909;
  --panel:#111;
  --line:#242424;
  --text:#f2eee8;
  --muted:#96918a;
  --accent:#e87532;
}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg);color:var(--text)}
[data-testid="stHeader"]{background:rgba(9,9,9,.75)}
.block-container{max-width:1180px;padding-top:1.4rem;padding-bottom:5rem}
h1,h2,h3{letter-spacing:-.025em}
.vh-brand{font-weight:900;letter-spacing:.08em;font-size:.86rem}
.vh-brand b{color:var(--accent)}
.vh-hero{padding:1.4rem 0 1rem}
.vh-hero h1{font-size:clamp(3rem,8vw,6.4rem);line-height:.88;margin:.2rem 0 1rem}
.vh-hero h1 em{font-style:italic;color:var(--accent)}
.vh-muted{color:var(--muted);font-size:.9rem}
.offer{
  border:1px solid var(--line); background:var(--panel);
  padding:1rem 1.05rem; margin:.65rem 0;
}
.offer-top{display:flex;justify-content:space-between;gap:1rem;align-items:start}
.offer-store{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
.offer-title{font-size:1.08rem;font-weight:700;margin:.25rem 0}
.offer-price{font-size:1.35rem;font-weight:800;white-space:nowrap}
.badge{display:inline-block;border:1px solid #343434;padding:.18rem .42rem;margin:.18rem .2rem 0 0;font-size:.68rem;color:#c9c3bb}
.smartbox{border:1px solid rgba(232,117,50,.35);background:#111;padding:1rem;margin:.55rem 0}
.smartbox small{color:var(--accent);letter-spacing:.09em}
[data-testid="stBottomBlockContainer"]{background:var(--bg)}
.stButton>button,.stLinkButton>a{
  border-radius:0!important;border:1px solid #3a3a3a!important;
}
.stButton>button[kind="primary"]{background:var(--accent)!important;color:#111!important;border-color:var(--accent)!important;font-weight:800}
[data-baseweb="tab-list"]{gap:.25rem;overflow-x:auto}
[data-baseweb="tab"]{white-space:nowrap}
@media(max-width:700px){
  .block-container{padding-left:1rem;padding-right:1rem;padding-top:.5rem}
  .vh-hero h1{font-size:3.7rem}
  .offer-top{display:block}
  .offer-price{margin-top:.6rem}
  [data-baseweb="tab-list"]{position:sticky;top:0;z-index:100;background:#090909;padding:.45rem 0}
}
</style>
<div class="vh-brand">VINYL <b>HUNTER</b> · MOBILE</div>
""",
    unsafe_allow_html=True,
)


# ---------- helpers ----------
CACHE_TTL = 300
SEARCH_DEADLINE = 18.0


def has_hebrew(s: str) -> bool:
    return bool(re.search(r"[\u0590-\u05FF]", str(s or "")))


@st.cache_data(ttl=3600, show_spinner=False)
def resolve_query(artist: str, album: str) -> tuple[str, str, dict]:
    artist = str(artist or "").strip()
    album = str(album or "").strip()
    token = _secret("DISCOGS_TOKEN")

    if not token or not (has_hebrew(artist) or has_hebrew(album)):
        return artist, album, {"used": False}

    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": "VinylHunterStreamlit/1.0",
    }
    try:
        if album:
            r = requests.get(
                "https://api.discogs.com/database/search",
                params={
                    "q": f"{artist} {album}",
                    "type": "release",
                    "format": "Vinyl",
                    "per_page": 8,
                },
                headers=headers,
                timeout=4,
            )
            r.raise_for_status()
            for row in (r.json() or {}).get("results") or []:
                title = str(row.get("title") or "")
                if " - " in title:
                    a, b = title.split(" - ", 1)
                    return a.strip(), b.strip(), {
                        "used": True,
                        "input": f"{artist} — {album}",
                        "resolved": f"{a.strip()} — {b.strip()}",
                    }
        else:
            r = requests.get(
                "https://api.discogs.com/database/search",
                params={"q": artist, "type": "artist", "per_page": 5},
                headers=headers,
                timeout=4,
            )
            r.raise_for_status()
            rows = (r.json() or {}).get("results") or []
            if rows:
                a = str(rows[0].get("title") or artist).strip()
                return a, "", {
                    "used": a.casefold() != artist.casefold(),
                    "input": artist,
                    "resolved": a,
                }
    except Exception:
        pass

    return artist, album, {"used": False}


def money(v, currency="ILS"):
    if v is None:
        return "Price n/a"
    symbols = {"ILS": "₪", "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}
    try:
        n = float(v)
        txt = f"{n:.0f}" if n.is_integer() else f"{n:.2f}"
    except Exception:
        return f"{v} {currency}"
    return f"{symbols.get(currency, currency + ' ')}{txt}"


def result_dict_local(r):
    d = asdict(r)
    return {
        "store": d.get("store", ""),
        "display_title": d.get("title", ""),
        "url": d.get("url", ""),
        "price": d.get("price"),
        "currency": d.get("currency") or "ILS",
        "stock": d.get("stock") or "UNKNOWN",
        "score": d.get("score") or 0,
        "media_condition": d.get("media_condition") or "",
        "sleeve_condition": d.get("sleeve_condition") or "",
        "country": "IL",
        "source_type": "ISRAEL_STORE",
    }


def result_dict_intl(r):
    d = asdict(r)
    return {
        "store": d.get("source", ""),
        "display_title": d.get("title", ""),
        "url": d.get("url", ""),
        "price": d.get("item_price"),
        "currency": d.get("currency") or "",
        "stock": d.get("stock") or "UNKNOWN",
        "score": d.get("match_score") or 0,
        "media_condition": d.get("media_condition") or "",
        "sleeve_condition": d.get("sleeve_condition") or "",
        "country": d.get("country") or "",
        "source_type": "INTERNATIONAL_STORE",
    }


def result_dict_discogs(r):
    d = asdict(r)
    return {
        "store": "Discogs",
        "display_title": d.get("title", ""),
        "url": d.get("url", ""),
        "price": d.get("item_price"),
        "currency": d.get("currency") or "USD",
        "stock": d.get("stock") or "UNKNOWN",
        "score": d.get("match_score") or 0,
        "media_condition": d.get("media_condition") or "",
        "sleeve_condition": d.get("sleeve_condition") or "",
        "country": d.get("country") or "",
        "source_type": "DISCOGS_MARKETPLACE",
        "discogs_release_id": d.get("release_id"),
        "market_availability_count": d.get("num_for_sale"),
        "release_year": d.get("year"),
        "shipping_price": None,
        "shipping_currency": d.get("currency") or "USD",
    }


def search_international(artist: str, album: str):
    active = getattr(international_provider, "ACTIVE_ADAPTERS", {})
    stores = [s for s in international_provider.registry() if s.get("name") in active]
    results = []

    def run_one(store):
        try:
            rs, _, _ = international_provider.search_store(store, artist, album)
            return rs
        except Exception:
            return []

    if not stores:
        return []

    with ThreadPoolExecutor(max_workers=min(12, len(stores))) as ex:
        futures = [ex.submit(run_one, s) for s in stores]
        for f in as_completed(futures):
            results.extend(f.result())

    dedup = {}
    for r in results:
        key = (getattr(r, "source", ""), getattr(r, "url", "").rstrip("/"))
        old = dedup.get(key)
        if old is None or getattr(r, "match_score", 0) > getattr(old, "match_score", 0):
            dedup[key] = r
    return list(dedup.values())


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def live_search(artist_input: str, album_input: str):
    started = time.time()
    artist, album, resolution = resolve_query(artist_input, album_input)
    artist_only = not bool(album.strip())

    local = []
    intl = []
    disc = []

    ex = ThreadPoolExecutor(max_workers=3)
    futures = {
        "local": ex.submit(core.search, artist, album, True),
        "intl": ex.submit(search_international, artist, album),
    }
    if album:
        futures["discogs"] = ex.submit(discogs_market.search, artist, album)

    deadline = started + SEARCH_DEADLINE
    try:
        remaining = max(0.1, deadline - time.time())
        try:
            local, _diag = futures["local"].result(timeout=remaining)
        except Exception:
            local = []

        remaining = max(0.1, deadline - time.time())
        try:
            intl = futures["intl"].result(timeout=remaining)
        except Exception:
            intl = []

        if "discogs" in futures:
            remaining = max(0.1, deadline - time.time())
            try:
                disc, _meta = futures["discogs"].result(timeout=remaining)
            except Exception:
                disc = []
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    rows = [result_dict_local(x) for x in local]
    rows += [result_dict_intl(x) for x in intl]
    rows += [result_dict_discogs(x) for x in disc]

    # basic dedupe
    seen = set()
    cleaned = []
    for r in rows:
        key = (str(r.get("store")).casefold(), str(r.get("url")).rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(r)

    # Smart Value only makes sense for one album.
    value_meta = {"picks": {}, "fx_status": "NOT_APPLICABLE"}
    if album and cleaned:
        try:
            cleaned, value_meta = value_engine.apply_value_engine(cleaned)
        except Exception:
            pass

    stock_rank = {"IN_STOCK": 0, "UNKNOWN": 1, "OUT_OF_STOCK": 2}
    cleaned.sort(
        key=lambda r: (
            stock_rank.get(r.get("stock"), 1),
            -(r.get("value_score") or 0),
            -(r.get("score") or 0),
        )
    )

    return {
        "artist": artist_input,
        "album": album_input,
        "resolved_artist": artist,
        "resolved_album": album,
        "resolution": resolution,
        "artist_only": artist_only,
        "results": cleaned,
        "smart": value_meta,
        "elapsed": round(time.time() - started, 2),
    }


def preprocess_ocr(image: Image.Image) -> Image.Image:
    img = image.convert("L")
    img = ImageOps.autocontrast(img)
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = img.filter(ImageFilter.SHARPEN)
    if img.width < 1800:
        scale = 1800 / max(1, img.width)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    return img


def ocr_image(image: Image.Image) -> str:
    img = preprocess_ocr(image)
    langs = "eng+heb"
    try:
        return pytesseract.image_to_string(img, lang=langs, config="--psm 6").strip()
    except Exception:
        return pytesseract.image_to_string(img, lang="eng", config="--psm 6").strip()


def meaningful_lines(text: str):
    noise = {
        "apple music", "listen now", "library", "radio", "search",
        "lossless", "dolby atmos", "shuffle", "lyrics", "preview"
    }
    out = []
    for raw in re.split(r"[\r\n]+", text or ""):
        x = re.sub(r"\s+", " ", raw).strip(" |")
        if len(x) < 2:
            continue
        if x.casefold() in noise:
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}", x):
            continue
        out.append(x)
    return out


def guess_music(text: str):
    lines = meaningful_lines(text)
    # editable guess only
    return (
        lines[1] if len(lines) > 1 else "",
        lines[0] if lines else "",
    )


def guess_identifier(text: str):
    compact = re.sub(r"[^\d]", " ", text or "")
    m = re.search(r"\b\d{8,14}\b", compact)
    barcode = m.group(0) if m else ""
    candidates = []
    for line in meaningful_lines(text):
        x = re.sub(r"[^A-Za-z0-9\-\/\.\s]", " ", line)
        x = re.sub(r"\s+", " ", x).strip()
        if re.search(r"[A-Za-z]", x) and re.search(r"\d", x) and 4 <= len(x) <= 65:
            candidates.append(x)
    return barcode, (candidates[0] if candidates else "")


def discogs_sync(release_id: int, action="add"):
    token = _secret("DISCOGS_TOKEN")
    username = _secret("DISCOGS_USERNAME")
    if not token or not username:
        return False, "Discogs sync not configured"

    url = f"https://api.discogs.com/users/{username}/wants/{int(release_id)}"
    headers = {
        "Authorization": f"Discogs token={token}",
        "User-Agent": "VinylHunterStreamlit/1.0",
    }
    try:
        if action == "remove":
            r = requests.delete(url, headers=headers, timeout=8)
        else:
            r = requests.put(url, headers=headers, json={}, timeout=8)
        if r.status_code in (200, 201, 204):
            return True, "Synced with Discogs"
        try:
            msg = (r.json() or {}).get("message") or f"HTTP {r.status_code}"
        except Exception:
            msg = f"HTTP {r.status_code}"
        return False, msg
    except Exception as e:
        return False, str(e)


def render_offer(r: dict, idx: int):
    tags = []
    stock = r.get("stock") or "UNKNOWN"
    tags.append(stock.replace("_", " "))
    if r.get("country"):
        tags.append(str(r["country"]))
    if r.get("media_condition"):
        tags.append("Media " + str(r["media_condition"]))
    if r.get("market_availability_count") is not None:
        tags.append(f"{r['market_availability_count']} for sale")
    if r.get("value_score"):
        tags.append(f"{round(r['value_score']*100)}% value")

    st.markdown(
        f"""
<div class="offer">
  <div class="offer-top">
    <div>
      <div class="offer-store">{r.get('store','')}</div>
      <div class="offer-title">{r.get('display_title','')}</div>
      <div>{''.join(f'<span class="badge">{x}</span>' for x in tags)}</div>
    </div>
    <div class="offer-price">{money(r.get('price'), r.get('currency') or 'ILS')}</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns([1, 1])
    with c1:
        if r.get("url"):
            st.link_button("Open offer ↗", r["url"], use_container_width=True)
    with c2:
        rid = r.get("discogs_release_id")
        if rid:
            if st.button("♡ Want + Discogs", key=f"want_{idx}_{rid}", use_container_width=True):
                ok, msg = discogs_sync(int(rid), "add")
                if ok:
                    st.success(msg)
                else:
                    st.warning(msg)


def render_smart(picks: dict):
    if not picks:
        return
    labels = [
        ("best_local", "BEST LOCAL VALUE"),
        ("cheapest_delivered", "CHEAPEST VERIFIED DELIVERED"),
        ("collector_pick", "COLLECTOR PICK"),
        ("discogs_floor", "DISCOGS MARKET FLOOR"),
    ]
    cols = st.columns(2)
    n = 0
    for key, label in labels:
        p = picks.get(key)
        if not p:
            continue
        with cols[n % 2]:
            st.markdown(
                f"""
<div class="smartbox">
  <small>{label}</small>
  <h3>{p.get('display_title','')}</h3>
  <div class="vh-muted">{p.get('store','')}</div>
  <div style="font-size:1.3rem;font-weight:800;margin-top:.5rem">
    {money(p.get('price'), p.get('currency') or 'ILS')}
  </div>
</div>
""",
                unsafe_allow_html=True,
            )
        n += 1


# ---------- session ----------
for key, default in {
    "search_data": None,
    "scan_ocr": "",
    "scan_artist": "",
    "scan_album": "",
    "press_ocr": "",
    "press_barcode": "",
    "press_serial": "",
    "identify_data": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


tabs = st.tabs(["🔎 Search", "📷 Scan", "🧬 Identify", "♡ Wantlist Sync"])

# SEARCH
with tabs[0]:
    st.markdown(
        """
<div class="vh-hero">
  <div class="vh-muted">SEARCH ISRAEL · WORLDWIDE · DISCOGS</div>
  <h1>Find the <em>right</em> record.</h1>
  <div class="vh-muted">עברית או English · אפשר לחפש רק אמן · Album is optional</div>
</div>
""",
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns(2)
    with c1:
        artist = st.text_input("Artist / אמן", key="artist_search")
    with c2:
        album = st.text_input("Album / אלבום (optional)", key="album_search")

    if st.button("SEARCH ALL STORES →", type="primary", use_container_width=True):
        if not artist.strip():
            st.warning("Enter an artist.")
        else:
            with st.spinner("Hunting across stores…"):
                st.session_state.search_data = live_search(artist.strip(), album.strip())

    data = st.session_state.search_data
    if data:
        if data["resolution"].get("used"):
            st.caption(f"Resolved: {data['resolution'].get('resolved')}")
        st.caption(
            f"{len(data['results'])} matches · {data['elapsed']}s"
            + (" · artist-only mode" if data["artist_only"] else "")
        )

        if not data["artist_only"]:
            render_smart((data.get("smart") or {}).get("picks") or {})

        for i, row in enumerate(data["results"]):
            render_offer(row, i)


# SCAN
with tabs[1]:
    st.header("Scan from your phone")
    st.caption("Screenshot from Apple Music, album artwork, barcode, catalog number or runout.")

    sub1, sub2 = st.tabs(["Album / Apple Music screenshot", "Pressing / serial camera"])

    with sub1:
        img_file = st.file_uploader(
            "Upload screenshot or album image",
            type=["png", "jpg", "jpeg", "webp"],
            key="album_upload",
        )
        cam = st.camera_input("Or take a photo", key="album_camera")
        chosen = cam or img_file

        if chosen and st.button("READ IMAGE", key="read_album", type="primary"):
            image = Image.open(chosen)
            with st.spinner("Reading text…"):
                txt = ocr_image(image)
            st.session_state.scan_ocr = txt
            guessed_artist, guessed_album = guess_music(txt)
            st.session_state.scan_artist = guessed_artist
            st.session_state.scan_album = guessed_album

        if st.session_state.scan_ocr:
            with st.expander("OCR text"):
                st.code(st.session_state.scan_ocr)

            sa = st.text_input("Artist", value=st.session_state.scan_artist, key="scan_artist_edit")
            sb = st.text_input("Album", value=st.session_state.scan_album, key="scan_album_edit")

            if st.button("FIND THIS RECORD →", key="search_scan", use_container_width=True):
                if not sa.strip():
                    st.warning("Check the artist name first.")
                else:
                    with st.spinner("Searching…"):
                        st.session_state.search_data = live_search(sa.strip(), sb.strip())
                    st.success("Search finished. Open the Search tab to see results.")

    with sub2:
        pcam = st.camera_input("Photograph barcode / catalog / matrix", key="press_camera")
        pupload = st.file_uploader(
            "Or upload a close-up",
            type=["png", "jpg", "jpeg", "webp"],
            key="press_upload",
        )
        pchosen = pcam or pupload

        if pchosen and st.button("READ IDENTIFIER", key="read_identifier", type="primary"):
            image = Image.open(pchosen)
            with st.spinner("Reading serial / barcode…"):
                txt = ocr_image(image)
            barcode, serial = guess_identifier(txt)
            st.session_state.press_ocr = txt
            st.session_state.press_barcode = barcode
            st.session_state.press_serial = serial

        if st.session_state.press_ocr:
            with st.expander("OCR text"):
                st.code(st.session_state.press_ocr)
            st.text_input("Barcode", value=st.session_state.press_barcode, key="scan_barcode_edit")
            st.text_input("Catalog / Matrix guess", value=st.session_state.press_serial, key="scan_serial_edit")
            st.info("Copy the detected value into Identify below. Matrix/runout OCR may need manual correction.")


# IDENTIFY
with tabs[2]:
    st.header("Identify My Pressing")
    st.caption("Catalog number, barcode, Matrix A/B. Matrix is verified against real Discogs identifiers.")

    c1, c2 = st.columns(2)
    with c1:
        cat = st.text_input("Catalog number", key="id_cat")
        barcode = st.text_input(
            "Barcode",
            value=st.session_state.press_barcode,
            key="id_barcode",
        )
        matrix_a = st.text_input(
            "Matrix A",
            value=st.session_state.press_serial,
            key="id_matrix_a",
        )
        matrix_b = st.text_input("Matrix B", key="id_matrix_b")
    with c2:
        ia = st.text_input("Artist (optional)", key="id_artist")
        it = st.text_input("Album (optional)", key="id_title")
        iy = st.text_input("Year (optional)", key="id_year")
        ic = st.text_input("Country (optional)", key="id_country")

    if st.button("IDENTIFY PRESSING →", type="primary", use_container_width=True):
        if not any(x.strip() for x in (cat, barcode, matrix_a, matrix_b)):
            st.warning("Enter at least one physical identifier.")
        else:
            with st.spinner("Scanning Discogs release identifiers…"):
                engine = discogs_identifier.DiscogsIdentifier()
                if not engine.configured():
                    st.error("DISCOGS_TOKEN is missing from Streamlit Secrets.")
                else:
                    try:
                        st.session_state.identify_data = engine.identify(
                            barcode=barcode.strip(),
                            catalog_number=cat.strip(),
                            matrix_a=matrix_a.strip(),
                            matrix_b=matrix_b.strip(),
                            artist=ia.strip(),
                            title=it.strip(),
                            year=iy.strip(),
                            country=ic.strip(),
                        )
                    except Exception as e:
                        st.error(str(e))

    idata = st.session_state.identify_data
    if idata:
        best = idata.get("best_match")
        if not best:
            st.warning("No pressing matched those identifiers.")
        else:
            st.success(
                f"{idata.get('status','MATCH')} · "
                f"{round(float(best.get('confidence') or 0)*100,1)}% confidence"
            )
            c1, c2 = st.columns([1, 2])
            with c1:
                if best.get("cover_image"):
                    st.image(best["cover_image"], use_container_width=True)
            with c2:
                st.subheader(f"{best.get('artist','')} — {best.get('title','')}")
                st.write(
                    f"**Year:** {best.get('year') or '—'}  \n"
                    f"**Country:** {best.get('country') or '—'}  \n"
                    f"**Release ID:** {best.get('release_id') or '—'}"
                )
                if best.get("discogs_url"):
                    st.link_button("Open on Discogs ↗", best["discogs_url"])

                rid = best.get("release_id")
                if rid and st.button("♡ ADD EXACT PRESSING TO DISCOGS WANTLIST", use_container_width=True):
                    ok, msg = discogs_sync(int(rid), "add")
                    (st.success if ok else st.warning)(msg)

            matches = idata.get("matches") or []
            if len(matches) > 1:
                with st.expander(f"Other possible pressings ({len(matches)-1})"):
                    for m in matches[1:]:
                        st.write(
                            f"{round(float(m.get('confidence') or 0)*100,1)}% · "
                            f"{m.get('country') or '—'} · {m.get('year') or '—'} · "
                            f"Release #{m.get('release_id') or '—'}"
                        )


# DISCOGS
with tabs[3]:
    st.header("Discogs Wantlist Sync")
    user = _secret("DISCOGS_USERNAME")
    token = _secret("DISCOGS_TOKEN")

    if token and user:
        st.success(f"Connected for personal build: {user}")
        st.write(
            "When Vinyl Hunter knows the exact Discogs `release_id`, "
            "you can add that exact pressing directly to your Discogs Wantlist."
        )
    else:
        st.warning("Add DISCOGS_TOKEN and DISCOGS_USERNAME in Streamlit Secrets.")
        st.code(
            'DISCOGS_TOKEN = "YOUR_TOKEN"\n'
            'DISCOGS_USERNAME = "YOUR_USERNAME"',
            language="toml",
        )

    st.caption(
        "For a public multi-user version later, this should be upgraded to per-user Discogs OAuth."
    )
