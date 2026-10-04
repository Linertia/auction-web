"""Read-only deployment subset; original data contract preserved verbatim."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

@dataclass
class AuctionFeature:
    cycle_id: str
    observed_at: str
    source_time: str
    code: str
    name: str
    rank: int
    gap_pct: float
    matched_price: float
    matched_volume_lots: float
    matched_amount_yuan: float
    unmatched_buy_amount_yuan: float
    unmatched_sell_amount_yuan: float
    turnover_pct: float
    unmatched_buy_turnover_pct: float
    unmatched_sell_turnover_pct: float
    score: float
    float_shares: float
    float_share_source: str
    score_delta_6s: float = math.nan
    score_delta_15s: float = math.nan
    score_drawdown_15s: float = math.nan
    rank_change_15s: float = math.nan
    sell_growth_6s: float = math.nan
    positive_streak: int = 0
    sign_flips_12s: int = 0
    source_agreement: str = "未校验"
    trend_state: str = "观察不足"
    candidate_status: str = "未入池"
    certainty_mark: str = ""
    trend_notes: list[str] = field(default_factory=list)
    history_tier: str = "未检查"
    qualified: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)
    display_rank: int | None = None
    observation_type: str = "普通"
    score_previous_gate: float = math.nan
    score_change_previous_gate: float = math.nan
    score_history: list[str] = field(default_factory=list)
    action_hint: str = ""
