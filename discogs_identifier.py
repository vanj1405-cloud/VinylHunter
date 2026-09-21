from __future__ import annotations

"""
Discogs record / pressing identifier for CrateRadar.

Supports:
- Barcode
- Catalog number
- Matrix / Runout (Side A / Side B)
- Optional artist/title/year/country hints

The module never treats a search hit as proof. Candidate releases are fetched
from Discogs and identifiers are compared against the release's actual
identifier metadata before a confidence score is assigned.

Environment:
    DISCOGS_TOKEN=<your Discogs personal access token>

No token is hardcoded in this file.
"""

import os
import re
import time
import unicodedata
from typing import Any

import requests


API_BASE = "https://api.discogs.com"

USER_AGENT = os.getenv(
    "CRATERADAR_DISCOGS_USER_AGENT",
    "CrateRadar/0.1 +https://localhost",
)

TOKEN_ENV_NAMES = (
    "DISCOGS_TOKEN",
    "DISCOGS_USER_TOKEN",
    "DISCOGS_PERSONAL_ACCESS_TOKEN",
)


def _token() -> str:
    for name in TOKEN_ENV_NAMES:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.replace("–", "-").replace("—", "-").replace("’", "'")
    text = text.casefold()
    return re.sub(r"\s+", " ", text).strip()


def _norm_identifier(value: Any) -> str:
    """
    Aggressive comparison form:
    'YEX 605-2' == 'YEX-605-2'
    'ST-A-712285-A' == 'ST A 712285 A'
    """
    text = _norm_text(value)
    return re.sub(r"[^a-z0-9]+", "", text)


def _compact_barcode(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _identifier_similarity(query: str, candidate: str) -> tuple[float, str]:
    q = _norm_identifier(query)
    c = _norm_identifier(candidate)

    if not q or not c:
        return 0.0, "none"

    if q == c:
        return 1.0, "exact"

    # Strong relaxed match for longer matrix/catalog identifiers where one
    # side contains a pressing suffix/prefix absent from the other.
    if min(len(q), len(c)) >= 6 and (q in c or c in q):
        ratio = min(len(q), len(c)) / max(len(q), len(c))
        if ratio >= 0.78:
            return 0.84, "contained"

    return 0.0, "none"


class DiscogsIdentifier:
    def __init__(self, timeout: float = 10.0, max_candidates: int = 50):
        self.timeout = float(timeout)
        self.max_candidates = int(max_candidates)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
        )

    @property
    def token(self) -> str:
        return _token()

    def configured(self) -> bool:
        return bool(self.token)

    def _auth_headers(self) -> dict[str, str]:
        token = self.token
        if not token:
            return {}
        return {"Authorization": f"Discogs token={token}"}

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(
            f"{API_BASE}{path}",
            params=params or {},
            headers=self._auth_headers(),
            timeout=(4, self.timeout),
        )

        if r.status_code == 401:
            raise RuntimeError(
                "Discogs authentication failed. Check DISCOGS_TOKEN."
            )
        if r.status_code == 429:
            raise RuntimeError(
                "Discogs rate limit reached. Try again shortly."
            )

        r.raise_for_status()
        return r.json()

    def _search(self, **params) -> list[dict]:
        if not self.configured():
            raise RuntimeError(
                "DISCOGS_TOKEN is not configured."
            )

        payload = self._get(
            "/database/search",
            params={
                **params,
                "type": "release",
                "per_page": min(50, self.max_candidates),
                "page": 1,
            },
        )
        return payload.get("results") or []

    def _fetch_release(self, release_id: int) -> dict:
        return self._get(f"/releases/{int(release_id)}")

    def _candidate_ids(
        self,
        barcode: str,
        catalog_number: str,
        matrix_a: str,
        matrix_b: str,
        artist: str,
        title: str,
    ) -> tuple[list[int], list[dict]]:
        """
        Build a candidate release set.

        Discogs' normal database search is not reliable for Matrix / Runout
        strings. For matrix identification we search the release family
        (artist + title), fetch those releases, then verify the user's matrix
        against each release's real identifier metadata.
        """
        ids: list[int] = []
        seen: set[int] = set()
        diagnostics: list[dict] = []

        def add_results(label: str, rows: list[dict]):
            diagnostics.append({
                "search": label,
                "candidate_count": len(rows),
            })
            for row in rows:
                rid = row.get("id")
                try:
                    rid = int(rid)
                except Exception:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                ids.append(rid)
                if len(ids) >= self.max_candidates:
                    return

        if barcode:
            add_results(
                "barcode",
                self._search(barcode=_compact_barcode(barcode)),
            )

        if catalog_number and len(ids) < self.max_candidates:
            add_results(
                "catalog_number",
                self._search(catno=catalog_number),
            )

        if (matrix_a or matrix_b) and len(ids) < self.max_candidates:
            if artist or title:
                params = {}
                if artist:
                    params["artist"] = artist
                if title:
                    params["release_title"] = title
                add_results(
                    "matrix_release_family",
                    self._search(**params),
                )
            else:
                diagnostics.append({
                    "search": "matrix_release_family",
                    "candidate_count": 0,
                    "warning": (
                        "For reliable matrix identification, provide artist "
                        "and/or title so the release family can be scanned."
                    ),
                })

        if not ids and (artist or title):
            params = {}
            if artist:
                params["artist"] = artist
            if title:
                params["release_title"] = title
            add_results(
                "metadata_fallback",
                self._search(**params),
            )

        return ids[: self.max_candidates], diagnostics

    def _release_identifiers(self, release: dict) -> dict[str, list[str]]:
        out = {
            "barcode": [],
            "catalog_number": [],
            "matrix": [],
            "other": [],
        }

        for item in release.get("identifiers") or []:
            kind = _norm_text(item.get("type"))
            value = str(item.get("value") or "").strip()
            description = str(item.get("description") or "").strip()

            if not value:
                continue

            if "barcode" in kind:
                out["barcode"].append(value)
            elif "matrix" in kind or "runout" in kind:
                # Match against the actual engraved/stamped value only.
                # Discogs descriptions such as "Side A" are metadata, not
                # part of the matrix string and must not affect matching.
                out["matrix"].append(value)
            elif "catalog" in kind:
                out["catalog_number"].append(value)
            else:
                out["other"].append(value)

        # Catalog numbers are usually in labels[] rather than identifiers[].
        for label in release.get("labels") or []:
            catno = str(label.get("catno") or "").strip()
            if catno:
                out["catalog_number"].append(catno)

        # Preserve order while deduping.
        for key, values in out.items():
            out[key] = list(dict.fromkeys(values))

        return out

    def _best_identifier_match(
        self,
        query: str,
        candidates: list[str],
        *,
        barcode: bool = False,
    ) -> dict:
        if not query:
            return {
                "supplied": False,
                "score": 0.0,
                "match_type": "not_supplied",
                "query": "",
                "matched_value": "",
            }

        best_score = 0.0
        best_type = "none"
        best_value = ""

        for candidate in candidates:
            if barcode:
                q = _compact_barcode(query)
                c = _compact_barcode(candidate)
                score = 1.0 if q and c and q == c else 0.0
                match_type = "exact" if score else "none"
            else:
                score, match_type = _identifier_similarity(query, candidate)

            if score > best_score:
                best_score = score
                best_type = match_type
                best_value = candidate

        return {
            "supplied": True,
            "score": best_score,
            "match_type": best_type,
            "query": query,
            "matched_value": best_value,
        }

    def _score_release(
        self,
        release: dict,
        *,
        barcode: str,
        catalog_number: str,
        matrix_a: str,
        matrix_b: str,
        artist: str,
        title: str,
        year: str,
        country: str,
    ) -> dict:
        ids = self._release_identifiers(release)

        evidence = {
            "barcode": self._best_identifier_match(
                barcode, ids["barcode"], barcode=True
            ),
            "catalog_number": self._best_identifier_match(
                catalog_number, ids["catalog_number"]
            ),
            "matrix_a": self._best_identifier_match(
                matrix_a, ids["matrix"]
            ),
            "matrix_b": self._best_identifier_match(
                matrix_b, ids["matrix"]
            ),
        }

        weights = {
            "barcode": 1.00,
            "catalog_number": 0.95,
            "matrix_a": 1.35,
            "matrix_b": 1.35,
        }

        supplied_weight = sum(
            weights[key]
            for key, item in evidence.items()
            if item["supplied"]
        )

        matched_weight = sum(
            weights[key] * item["score"]
            for key, item in evidence.items()
            if item["supplied"]
        )

        identifier_score = (
            matched_weight / supplied_weight
            if supplied_weight else 0.0
        )

        metadata_bonus = 0.0
        metadata_matches = []

        release_artist = " / ".join(
            a.get("name", "")
            for a in release.get("artists") or []
            if a.get("name")
        )
        release_title = str(release.get("title") or "")
        release_country = str(release.get("country") or "")
        release_year = str(release.get("year") or "")

        if artist and _norm_text(artist) in _norm_text(release_artist):
            metadata_bonus += 0.015
            metadata_matches.append("artist")

        if title and _norm_text(title) in _norm_text(release_title):
            metadata_bonus += 0.015
            metadata_matches.append("title")

        if year and str(year).strip() == release_year.strip():
            metadata_bonus += 0.01
            metadata_matches.append("year")

        if country and _norm_text(country) == _norm_text(release_country):
            metadata_bonus += 0.01
            metadata_matches.append("country")

        confidence = identifier_score + metadata_bonus

        matched_identifier_count = sum(
            1
            for item in evidence.values()
            if item["supplied"] and item["score"] >= 0.80
        )

        supplied_identifier_count = sum(
            1
            for item in evidence.values()
            if item["supplied"]
        )

        # If a physical identifier was supplied, artist/title metadata alone
        # must never create a plausible pressing match.
        if supplied_identifier_count > 0 and matched_identifier_count == 0:
            confidence = 0.0
        elif matched_identifier_count == 1:
            confidence = min(confidence, 0.94)
        elif matched_identifier_count >= 2:
            confidence = min(confidence, 0.995)

        confidence = round(max(0.0, confidence), 3)

        formats = []
        for fmt in release.get("formats") or []:
            parts = [
                str(fmt.get("name") or "").strip(),
                str(fmt.get("qty") or "").strip(),
                *[str(x).strip() for x in (fmt.get("descriptions") or [])],
            ]
            value = ", ".join(x for x in parts if x)
            if value:
                formats.append(value)

        images = release.get("images") or []
        cover = ""
        for image in images:
            if image.get("type") == "primary":
                cover = image.get("uri150") or image.get("uri") or ""
                break
        if not cover and images:
            cover = images[0].get("uri150") or images[0].get("uri") or ""

        matched_identifiers = []
        for kind, item in evidence.items():
            if item["supplied"] and item["score"] > 0:
                matched_identifiers.append(
                    {
                        "type": kind,
                        "query": item["query"],
                        "matched_value": item["matched_value"],
                        "match_type": item["match_type"],
                        "score": round(item["score"], 3),
                    }
                )

        return {
            "release_id": release.get("id"),
            "artist": release_artist,
            "title": release_title,
            "year": release.get("year"),
            "country": release_country,
            "labels": [
                {
                    "name": x.get("name", ""),
                    "catalog_number": x.get("catno", ""),
                }
                for x in release.get("labels") or []
            ],
            "formats": formats,
            "identifiers": ids,
            "matched_identifiers": matched_identifiers,
            "metadata_matches": metadata_matches,
            "confidence": confidence,
            "discogs_url": release.get("uri") or "",
            "cover_image": cover,
        }

    def identify(
        self,
        *,
        barcode: str = "",
        catalog_number: str = "",
        matrix_a: str = "",
        matrix_b: str = "",
        artist: str = "",
        title: str = "",
        year: str = "",
        country: str = "",
    ) -> dict:
        started = time.time()

        barcode = str(barcode or "").strip()
        catalog_number = str(catalog_number or "").strip()
        matrix_a = str(matrix_a or "").strip()
        matrix_b = str(matrix_b or "").strip()
        artist = str(artist or "").strip()
        title = str(title or "").strip()
        year = str(year or "").strip()
        country = str(country or "").strip()

        supplied = {
            "barcode": bool(barcode),
            "catalog_number": bool(catalog_number),
            "matrix_a": bool(matrix_a),
            "matrix_b": bool(matrix_b),
        }

        if not any(supplied.values()):
            raise ValueError(
                "Provide at least one identifier: barcode, catalog_number, "
                "matrix_a, or matrix_b."
            )

        candidate_ids, diagnostics = self._candidate_ids(
            barcode,
            catalog_number,
            matrix_a,
            matrix_b,
            artist,
            title,
        )

        ranked = []
        fetch_errors = []

        for release_id in candidate_ids:
            try:
                release = self._fetch_release(release_id)
                ranked.append(
                    self._score_release(
                        release,
                        barcode=barcode,
                        catalog_number=catalog_number,
                        matrix_a=matrix_a,
                        matrix_b=matrix_b,
                        artist=artist,
                        title=title,
                        year=year,
                        country=country,
                    )
                )
            except Exception as exc:
                fetch_errors.append(
                    {
                        "release_id": release_id,
                        "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    }
                )

        ranked = [
            row for row in ranked
            if (
                row.get("confidence", 0) > 0
                and row.get("matched_identifiers")
            )
        ]
        ranked.sort(
            key=lambda x: (
                -float(x.get("confidence") or 0),
                -len(x.get("matched_identifiers") or []),
                str(x.get("country") or ""),
                int(x.get("year") or 0),
            )
        )

        top = ranked[:10]

        status = "NO_MATCH"
        if top:
            best = top[0]["confidence"]
            if best >= 0.92:
                status = "HIGH_CONFIDENCE"
            elif best >= 0.72:
                status = "LIKELY"
            else:
                status = "POSSIBLE"

        return {
            "ok": True,
            "status": status,
            "elapsed": round(time.time() - started, 2),
            "input": {
                "barcode": barcode,
                "catalog_number": catalog_number,
                "matrix_a": matrix_a,
                "matrix_b": matrix_b,
                "artist": artist,
                "title": title,
                "year": year,
                "country": country,
            },
            "candidate_count": len(candidate_ids),
            "match_count": len(top),
            "best_match": top[0] if top else None,
            "matches": top,
            "diagnostics": diagnostics,
            "fetch_errors": fetch_errors[:8],
            "matrix_strategy": (
                "release-family scan + real Matrix/Runout verification"
                if (matrix_a or matrix_b)
                else ""
            ),
        }


def identify_record(**kwargs) -> dict:
    return DiscogsIdentifier().identify(**kwargs)
