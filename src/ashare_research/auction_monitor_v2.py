from __future__ import annotations
import math
from typing import Sequence
from .auction_monitor import AuctionFeature

def rank_auction_features(features: Sequence[AuctionFeature]) -> list[AuctionFeature]:
    """The monitor's existing ranking order; no scoring or external effects."""
    return sorted(features, key=lambda row: (
        -row.score if math.isfinite(row.score) else math.inf,
        -row.matched_amount_yuan, row.code,
    ))
