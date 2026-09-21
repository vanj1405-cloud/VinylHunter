from __future__ import annotations

"""
Discogs market-floor provider for CrateRadar.

Important limitation:
Discogs' public API exposes release-level marketplace signals such as
num_for_sale and lowest_price. It does not provide a reliable buyer-specific
"cheapest delivered to Israel" result for the whole marketplace.

Therefore this provider:
- finds likely exact vinyl releases for the requested artist/album;
- reads current num_for_sale + lowest_price from release data;
- links directly to that release's Discogs Marketplace page;
- NEVER claims shipping is included;
- NEVER allows a Discogs market-floor price to masquerade as delivered price.
"""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any

import requests

API_BASE = "https://api.discogs.com"
USER_AGENT = os.getenv("CRATERADAR_DISCOGS_USER_AGENT", "CrateRadar/0.2 +https://localhost")
TOKEN_NAMES = ("DISCOGS_TOKEN", "DISCOGS_USER_TOKEN", "DISCOGS_PERSONAL_ACCESS_TOKEN")


def _token() -> str:
    for name in TOKEN_NAMES:
        v = str(os.getenv(name) or "").strip()
        if v:
            return v
    return ""


def _norm(v: Any) -> str:
    text = str(v or "").casefold().replace("–", "-").replace("—", "-")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class DiscogsMarketResult:
    source: str
    title: str
    url: str
    item_price: float | None
    currency: str
    stock: str
    country: str
    ships_to_israel: str
    media_condition: str
    sleeve_condition: str
    pressing: str
    edition: str
    match_score: float
    adapter: str
    evidence: str
    release_id: int
    num_for_sale: int
    year: int | None
    labels: list
    formats: list


class DiscogsMarket:
    def __init__(self, timeout: float = 9.0, max_candidates: int = 12, max_results: int = 5):
        self.timeout = timeout
        self.max_candidates = max_candidates
        self.max_results = max_results
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def configured(self):
        return bool(_token())

    def _headers(self):
        t = _token()
        return {"Authorization": f"Discogs token={t}"} if t else {}

    def _get(self, path, params=None):
        r = self.session.get(
            API_BASE + path,
            params=params or {},
            headers=self._headers(),
            timeout=(4, self.timeout),
        )
        if r.status_code == 429:
            raise RuntimeError("Discogs rate limit reached")
        if r.status_code == 401:
            raise RuntimeError("Discogs authentication failed")
        r.raise_for_status()
        return r.json()

    def _search_ids(self, artist: str, album: str) -> list[tuple[int, float]]:
        if not self.configured():
            return []
        data = self._get("/database/search", {
            "artist": artist,
            "release_title": album,
            "format": "Vinyl",
            "type": "release",
            "per_page": min(50, self.max_candidates),
            "page": 1,
        })
        rows = data.get("results") or []
        scored = []
        for row in rows:
            rid = row.get("id")
            try:
                rid = int(rid)
            except Exception:
                continue
            title = str(row.get("title") or "")
            artist_part, _, album_part = title.partition(" - ")
            score = .58 * _sim(artist, artist_part) + .42 * _sim(album, album_part or title)
            if score >= .56:
                scored.append((rid, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:self.max_candidates]

    def _release(self, rid: int, seed_score: float):
        # Request a stable marketplace currency; Smart Value Engine converts to ILS live.
        d = self._get(f"/releases/{rid}", {"curr_abbr": "USD"})
        num = int(d.get("num_for_sale") or 0)
        low = d.get("lowest_price")
        try:
            low = float(low) if low is not None else None
        except Exception:
            low = None

        artists = ", ".join(a.get("name", "") for a in (d.get("artists") or []) if a.get("name"))
        title = str(d.get("title") or "")
        formats = []
        for f in d.get("formats") or []:
            parts = [str(f.get("name") or "")]
            parts += [str(x) for x in (f.get("descriptions") or [])]
            parts += [str(f.get("text") or "")]
            s = " / ".join(x for x in parts if x)
            if s:
                formats.append(s)

        labels = [
            {"name": x.get("name", ""), "catno": x.get("catno", "")}
            for x in (d.get("labels") or [])
        ]

        country = str(d.get("country") or "")
        year = d.get("year") or None
        pressing = " · ".join(x for x in [country, str(year or "")] if x)
        edition = " / ".join(formats[:2])

        # Seed database score is kept conservative. Release title/artist re-check improves it.
        exact_score = .55 * _sim(artists, "") + seed_score  # seed remains the main relevance signal
        score = max(0.0, min(1.0, seed_score))

        return DiscogsMarketResult(
            source="Discogs",
            title=f"{artists} — {title}" if artists else title,
            url=f"https://www.discogs.com/sell/release/{rid}?ev=rb",
            item_price=low,
            currency="USD",
            stock="IN_STOCK" if num > 0 else "OUT_OF_STOCK",
            country=country,
            ships_to_israel="UNKNOWN",
            media_condition="",
            sleeve_condition="",
            pressing=pressing,
            edition=edition,
            match_score=round(score, 3),
            adapter="discogs_release_market",
            evidence=f"{num} for sale · release-level lowest price; shipping not included",
            release_id=rid,
            num_for_sale=num,
            year=year,
            labels=labels,
            formats=formats,
        )

    def search(self, artist: str, album: str):
        started = time.time()
        ids = self._search_ids(artist, album)
        if not ids:
            return [], {"candidates": 0, "results": 0, "elapsed": round(time.time()-started,2), "errors": []}

        results = []
        errors = []
        with ThreadPoolExecutor(max_workers=min(6, len(ids))) as ex:
            futs = {ex.submit(self._release, rid, score): rid for rid, score in ids}
            for f in as_completed(futs):
                try:
                    results.append(f.result())
                except Exception as e:
                    errors.append(f"{futs[f]}:{type(e).__name__}:{str(e)[:90]}")

        # In-stock first; then relevance; then lower market floor.
        results.sort(key=lambda x: (
            0 if x.stock == "IN_STOCK" else 1,
            -x.match_score,
            x.item_price is None,
            x.item_price or 10**9,
        ))
        return results[:self.max_results], {
            "candidates": len(ids),
            "results": min(len(results), self.max_results),
            "elapsed": round(time.time()-started, 2),
            "errors": errors[:6],
        }


def search(artist: str, album: str):
    return DiscogsMarket().search(artist, album)
