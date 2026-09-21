"""CrateRadar / Vinyl Hunter backend V2 compatibility facade.

Existing Flask/UI code can keep doing `import core`. Israeli search stays on
the proven engine; new global providers live in separate modules.
"""
from providers import israel
from providers.international import INTERNATIONAL_REGISTRY
from providers.marketplaces import MARKETPLACES

# Backward-compatible exports expected by the current app/diagnostics.
Result = israel.Result
STORE_REGISTRY = israel.STORE_REGISTRY
relevance = israel.relevance
match = israel.match
dedupe = israel.dedupe
verify = israel.verify
woo = israel.woo
shopify = israel.shopify
rockstore1970 = israel.rockstore1970
tav8 = israel.tav8
terminalx = israel.terminalx
kamea = israel.kamea
_store_call = israel._store_call

def store_registry():
    return israel.store_registry()

def search(artist, album, verify_pages=True, include_testing=True):
    """Current production search: Israeli providers only."""
    return israel.search(artist, album, verify_pages, include_testing)

def provider_summary():
    return {
        "israel": store_registry(),
        "international_candidates": [dict(x) for x in INTERNATIONAL_REGISTRY],
        "marketplaces": [dict(x) for x in MARKETPLACES],
    }
