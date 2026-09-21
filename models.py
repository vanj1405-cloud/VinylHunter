from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

@dataclass
class GlobalResult:
    source: str
    title: str
    url: str
    item_price: Optional[float] = None
    currency: str = "ILS"
    stock: str = "UNKNOWN"
    source_type: str = "INTERNATIONAL_STORE"
    country: str = ""
    shipping_price: Optional[float] = None
    delivered_price: Optional[float] = None
    ships_to_israel: str = "UNKNOWN"  # YES / NO / UNKNOWN
    condition: str = ""
    media_condition: str = ""
    sleeve_condition: str = ""
    seller_rating: Optional[float] = None
    official: bool = False
    official_level: str = ""  # ARTIST / LABEL / NONE
    edition: str = ""
    pressing: str = ""
    match_score: float = 0.0

    def finalize_delivered_price(self):
        if self.item_price is not None and self.shipping_price is not None:
            self.delivered_price = self.item_price + self.shipping_price
        return self
