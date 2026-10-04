"""Read-only deployment subset; original ranking function preserved verbatim."""
from __future__ import annotations

from typing import Sequence
from .auction_monitor import AuctionFeature

def rank_auction_features(features: Sequence[AuctionFeature]) -> list[AuctionFeature]:
    """The monitor's existing ranking order; no scoring or external effects."""
    return sorted(features, key=lambda row: (-row.score, -row.matched_amount_yuan, row.code))
