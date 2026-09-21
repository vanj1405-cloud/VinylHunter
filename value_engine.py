from __future__ import annotations

"""
CrateRadar / Vinyl Hunter — Smart Value Engine

Core rule:
    Cheapest != Best.

The engine distinguishes:
- BEST_LOCAL: best local value, without pretending local sticker price is delivered price.
- CHEAPEST_DELIVERED: only offers with a VERIFIED item+shipping total.
- BEST_VALUE: price + condition + match confidence + source confidence + delivery certainty.
- COLLECTOR_PICK: edition/pressing desirability, with "rare" wording only when market scarcity
  is supported by an actual availability count.

Foreign offers with unknown shipping are NEVER allowed to win "Cheapest Delivered".
Discogs market-floor prices are NEVER treated as delivered totals.
"""

import json
import os
import statistics
import threading
import time
from typing import Any

import requests

_FX_LOCK = threading.Lock()
_FX_CACHE: tuple[float, dict[str, float]] = (0.0, {})
_FX_TTL = 6 * 60 * 60
FX_URL = os.getenv("CRATERADAR_FX_URL", "https://open.er-api.com/v6/latest/USD")
TARGET_CURRENCY = "ILS"


def _f(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _condition_score(value: str) -> float:
    text = str(value or "").upper().replace(" ", "")
    table = [
        ("MINT(M)", 1.00), ("MINT", 1.00), ("M", 1.00),
        ("NEARMINT(NMORM-)", .97), ("NEARMINT", .97), ("NM", .97), ("M-", .97),
        ("VERYGOODPLUS(VG+)", .88), ("VG+", .88),
        ("VERYGOOD(VG)", .76), ("VG", .76),
        ("GOODPLUS(G+)", .58), ("G+", .58),
        ("GOOD(G)", .45), ("G", .45),
        ("FAIR(F)", .25), ("POOR(P)", .10),
    ]
    for needle, score in table:
        if needle in text:
            return score
    return .68  # unknown condition: neutral, not a reward


def _source_score(source_type: str) -> float:
    return {
        "ISRAEL_STORE": 1.00,
        "LOCAL_ONLINE": .98,
        "INTERNATIONAL_STORE": .91,
        "DISCOGS_MARKETPLACE": .84,
    }.get(str(source_type or ""), .80)


def _manual_fx() -> dict[str, float]:
    """Optional JSON mapping of currency -> ILS per 1 unit, e.g. {"USD":3.7,"EUR":4.1}."""
    raw = os.getenv("CRATERADAR_FX_RATES_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out = {str(k).upper(): float(v) for k, v in data.items() if float(v) > 0}
        out["ILS"] = 1.0
        return out
    except Exception:
        return {}


def fx_to_ils() -> tuple[dict[str, float], str]:
    """
    Return ILS-per-unit FX rates.
    Never silently falls back to invented rates.
    """
    global _FX_CACHE
    manual = _manual_fx()
    if manual:
        return manual, "ENV"

    now = time.time()
    with _FX_LOCK:
        ts, cached = _FX_CACHE
        if cached and now - ts < _FX_TTL:
            return dict(cached), "LIVE_CACHE"

    try:
        r = requests.get(FX_URL, timeout=(3.5, 6.0), headers={"User-Agent": "CrateRadar/0.2"})
        r.raise_for_status()
        payload = r.json()
        rates = payload.get("rates") or {}
        usd_to_ils = _f(rates.get("ILS"))
        if not usd_to_ils:
            raise RuntimeError("ILS missing from FX feed")

        ils_per_unit = {"ILS": 1.0, "USD": usd_to_ils}
        for code, per_usd in rates.items():
            n = _f(per_usd)
            if n and n > 0:
                ils_per_unit[str(code).upper()] = usd_to_ils / n

        with _FX_LOCK:
            _FX_CACHE = (now, dict(ils_per_unit))
        return ils_per_unit, "LIVE"
    except Exception:
        with _FX_LOCK:
            _, cached = _FX_CACHE
            if cached:
                return dict(cached), "STALE_CACHE"
        return {"ILS": 1.0}, "UNAVAILABLE"


def _edition_interest(r: dict) -> tuple[float, list[str]]:
    tags = {str(x).upper() for x in (r.get("edition_tags") or [])}
    text = " ".join([
        str(r.get("pressing") or ""),
        str(r.get("edition") or ""),
        str(r.get("display_title") or ""),
    ]).lower()

    score = 0.0
    reasons = []
    rules = [
        ("LIMITED", .22, "limited edition"),
        ("MONO", .18, "mono edition"),
        ("BOX SET", .12, "box set"),
        ("PICTURE DISC", .07, "picture disc"),
        ("ANNIVERSARY", .05, "anniversary edition"),
    ]
    for tag, pts, reason in rules:
        if tag in tags:
            score += pts
            reasons.append(reason)

    if "first press" in text or "1st press" in text or "first pressing" in text:
        score += .30
        reasons.append("first-pressing claim")
    if "promo" in text:
        score += .18
        reasons.append("promo")
    if "test pressing" in text:
        score += .35
        reasons.append("test pressing")

    # Verified scarcity only if a source provides a current number for sale.
    available = r.get("market_availability_count")
    try:
        available = int(available)
    except Exception:
        available = None

    verified_scarcity = False
    if available is not None:
        if available <= 3:
            score += .55
            reasons.append(f"only {available} currently for sale")
            verified_scarcity = True
        elif available <= 8:
            score += .32
            reasons.append(f"{available} currently for sale")
            verified_scarcity = True
        elif available <= 20:
            score += .12
            reasons.append(f"{available} currently for sale")

    return min(score, 1.0), reasons, verified_scarcity


def _relative_price_score(price: float | None, median_price: float | None) -> float:
    if price is None or median_price is None or median_price <= 0:
        return .55
    ratio = price / median_price
    if ratio <= .70:
        return 1.00
    if ratio <= .85:
        return .90
    if ratio <= 1.00:
        return .78
    if ratio <= 1.15:
        return .65
    if ratio <= 1.35:
        return .50
    return .32


def enrich_results(results: list[dict]) -> tuple[list[dict], dict]:
    rates, fx_status = fx_to_ils()

    # First pass: conversion and delivery certainty.
    for r in results:
        cur = str(r.get("currency") or "ILS").upper()
        price = _f(r.get("price"))
        rate = rates.get(cur)
        item_ils = round(price * rate, 2) if price is not None and rate else None

        shipping_price = _f(r.get("shipping_price"))
        shipping_currency = str(r.get("shipping_currency") or cur).upper()
        shipping_rate = rates.get(shipping_currency)
        shipping_ils = (
            round(shipping_price * shipping_rate, 2)
            if shipping_price is not None and shipping_rate
            else None
        )

        shipping_verified = shipping_price is not None and shipping_rate is not None
        is_local = r.get("source_type") in ("ISRAEL_STORE", "LOCAL_ONLINE")

        # A local sticker price is useful for BEST_LOCAL but we do not call it "delivered"
        # unless a shipping value was explicitly supplied.
        delivered_ils = (
            round(item_ils + shipping_ils, 2)
            if item_ils is not None and shipping_verified
            else None
        )

        r["item_price_ils"] = item_ils
        r["shipping_price_ils"] = shipping_ils
        r["shipping_verified"] = bool(shipping_verified)
        r["delivered_price_ils"] = delivered_ils
        r["fx_rate_to_ils"] = rate
        r["fx_status"] = fx_status
        r["local_price_known"] = bool(is_local and item_ils is not None)

        interest, reasons, scarcity = _edition_interest(r)
        r["collector_interest_score"] = round(interest, 3)
        r["collector_reasons"] = reasons
        r["verified_scarcity"] = scarcity

    # Medians for context-aware price scoring.
    local_prices = [
        r["item_price_ils"] for r in results
        if r.get("stock") == "IN_STOCK"
        and r.get("source_type") in ("ISRAEL_STORE", "LOCAL_ONLINE")
        and r.get("item_price_ils") is not None
    ]
    delivered_prices = [
        r["delivered_price_ils"] for r in results
        if r.get("stock") == "IN_STOCK" and r.get("delivered_price_ils") is not None
    ]
    local_median = statistics.median(local_prices) if local_prices else None
    delivered_median = statistics.median(delivered_prices) if delivered_prices else None

    for r in results:
        if r.get("stock") != "IN_STOCK":
            r["value_score"] = 0.0
            continue

        is_local = r.get("source_type") in ("ISRAEL_STORE", "LOCAL_ONLINE")
        comparable_price = r.get("delivered_price_ils")
        median = delivered_median
        delivery_certainty = 1.0 if comparable_price is not None else .25

        if is_local:
            comparable_price = r.get("item_price_ils")
            median = local_median
            # Local price is a valid local-value signal even when home-delivery shipping is unknown.
            delivery_certainty = .86

        price_score = _relative_price_score(comparable_price, median)
        match_score = max(0.0, min(1.0, _f(r.get("score"), .5)))
        condition = _condition_score(r.get("media_condition"))
        source = _source_score(r.get("source_type"))
        collector = _f(r.get("collector_interest_score"), 0.0)

        # Value is not "lowest number wins". Strong match, source, condition and certainty matter.
        value = (
            .34 * price_score
            + .22 * match_score
            + .16 * condition
            + .12 * source
            + .12 * delivery_certainty
            + .04 * collector
        )
        r["value_score"] = round(value, 3)

    return results, {
        "fx_status": fx_status,
        "fx_target": TARGET_CURRENCY,
        "local_median_ils": round(local_median, 2) if local_median is not None else None,
        "delivered_median_ils": round(delivered_median, 2) if delivered_median is not None else None,
    }


def _public_pick(r: dict | None, badge: str, reason: str) -> dict | None:
    if not r:
        return None
    return {
        "badge": badge,
        "reason": reason,
        "store": r.get("store", ""),
        "display_title": r.get("display_title", ""),
        "url": r.get("url", ""),
        "price": r.get("price"),
        "currency": r.get("currency", ""),
        "item_price_ils": r.get("item_price_ils"),
        "shipping_price_ils": r.get("shipping_price_ils"),
        "delivered_price_ils": r.get("delivered_price_ils"),
        "media_condition": r.get("media_condition", ""),
        "source_type": r.get("source_type", ""),
        "value_score": r.get("value_score", 0),
        "market_availability_count": r.get("market_availability_count"),
        "collector_reasons": r.get("collector_reasons", []),
        "discogs_release_id": r.get("discogs_release_id"),
    }


def build_picks(results: list[dict]) -> dict:
    available = [r for r in results if r.get("stock") == "IN_STOCK"]

    local = [
        r for r in available
        if r.get("source_type") in ("ISRAEL_STORE", "LOCAL_ONLINE")
        and r.get("item_price_ils") is not None
    ]
    best_local = max(local, key=lambda r: r.get("value_score", 0), default=None)

    delivered = [r for r in available if r.get("delivered_price_ils") is not None]
    cheapest_delivered = min(
        delivered, key=lambda r: r["delivered_price_ils"], default=None
    )

    # Best value is conservative: local offers are eligible even without known delivery shipping;
    # foreign offers need verified delivery totals.
    value_pool = [
        r for r in available
        if r.get("source_type") in ("ISRAEL_STORE", "LOCAL_ONLINE")
        or r.get("delivered_price_ils") is not None
    ]
    best_value = max(value_pool, key=lambda r: r.get("value_score", 0), default=None)

    collector_pool = [
        r for r in available
        if r.get("collector_interest_score", 0) >= .18
    ]
    collector = max(
        collector_pool,
        key=lambda r: (
            1 if r.get("verified_scarcity") else 0,
            r.get("collector_interest_score", 0),
            r.get("score", 0),
        ),
        default=None,
    )

    discogs = [
        r for r in available
        if r.get("source_type") == "DISCOGS_MARKETPLACE"
        and r.get("item_price_ils") is not None
    ]
    discogs_floor = min(discogs, key=lambda r: r["item_price_ils"], default=None)

    return {
        "best_local": _public_pick(
            best_local, "BEST LOCAL VALUE",
            "Best balance of local price, match quality and condition."
        ),
        "cheapest_delivered": _public_pick(
            cheapest_delivered, "CHEAPEST VERIFIED DELIVERED",
            "Lowest item + verified shipping total. Unknown shipping is excluded."
        ),
        "best_value": _public_pick(
            best_value, "BEST VALUE",
            "Weighted value — not simply the lowest sticker price."
        ),
        "collector_pick": _public_pick(
            collector, "COLLECTOR PICK",
            "Pressing/edition interest; rarity wording is used only with verified market scarcity."
        ),
        "discogs_floor": _public_pick(
            discogs_floor, "DISCOGS MARKET FLOOR",
            "Lowest Discogs release-level listing price found; shipping is not included unless explicitly known."
        ),
    }


def apply_value_engine(results: list[dict]) -> tuple[list[dict], dict]:
    enriched, meta = enrich_results(results)
    return enriched, {**meta, "picks": build_picks(enriched)}
