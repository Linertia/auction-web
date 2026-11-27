"""Allowlisted display metadata; never computes strategy or personal positions."""
import math
import re
from datetime import datetime

from .auction_query import AuctionQueryError

SORT_FIELDS = frozenset({
    "score", "matched_amount_yuan", "unmatched_buy_amount_yuan", "unmatched_sell_amount_yuan",
    "unmatched_buy_delta_yuan", "unmatched_sell_delta_yuan",
})
MARKET_STATE_WEIGHTS = {"normal": 1, "l1_low": .5, "h1_hot": .5, "both": .25}
POOL_GROUPS = ("original", "opened", "pending", "s")
POOL_NUMBERS = ("score", "gap_pct", "matched_amount_yuan", "turnover_pct")
PUBLIC_NAME = re.compile(r"[\u4e00-\u9fffA-Za-z0-9 *().（）-]{1,32}")


def validate_expanded_pool(value):
    if not isinstance(value, list) or len(value) > 10000:
        raise AuctionQueryError("DATA_INVALID: 扩展选股池格式无效")
    codes = set()
    previous_rank = 0
    for row in value:
        if (not isinstance(row, dict) or set(row) != {
            "code", "name", "original_rank", "group", "in_recommended_pool", *POOL_NUMBERS
        } or not isinstance(row["code"], str) or not re.fullmatch(r"[0-9]{6}", row["code"])
            or row["code"] in codes or type(row["original_rank"]) is not int
            or not previous_rank < row["original_rank"] <= 10000 or row["group"] not in POOL_GROUPS
            or row["in_recommended_pool"] is not True or not isinstance(row["name"], str)
            or (row["name"] and not PUBLIC_NAME.fullmatch(row["name"]))
            or any(type(row[key]) not in (int, float) or not math.isfinite(row[key]) for key in POOL_NUMBERS)):
            raise AuctionQueryError("DATA_INVALID: 扩展选股池明细无效")
        codes.add(row["code"])
        previous_rank = row["original_rank"]


def validate_display(value):
    if not isinstance(value, dict) or not {"exposure_weight", "prior_trade_date"} <= set(value) or set(value) - {"exposure_weight", "prior_trade_date", "stock_names", "market_state", "market_timing", "frame_coverage", "expanded_pool"}:
        raise AuctionQueryError("DATA_INVALID: 展示元数据无效")
    weight, prior = value["exposure_weight"], value["prior_trade_date"]
    if weight is not None and (type(weight) not in (int, float) or weight not in (.25, .5, 1)):
        raise AuctionQueryError("DATA_INVALID: 市场仓位结果无效")
    if "market_state" in value and (
        not isinstance(value["market_state"], str) or value["market_state"] not in MARKET_STATE_WEIGHTS
        or MARKET_STATE_WEIGHTS[value["market_state"]] != weight
    ):
        raise AuctionQueryError("DATA_INVALID: 市场档位与仓位不一致")
    if "market_timing" in value and value["market_timing"] not in ("pre_auction", "review_after_auction_started"):
        raise AuctionQueryError("DATA_INVALID: 市场状态时点无效")
    coverage = value.get("frame_coverage", 1)
    if type(coverage) not in (int, float) or not math.isfinite(coverage) or not 0 <= coverage <= 1:
        raise AuctionQueryError("DATA_INVALID: 门帧覆盖率无效")
    if prior is not None:
        if not isinstance(prior, str) or len(prior) != 8 or not prior.isdigit():
            raise AuctionQueryError("DATA_INVALID: 上一交易日无效")
        try:
            datetime.strptime(prior, "%Y%m%d")
        except ValueError:
            raise AuctionQueryError("DATA_INVALID: 上一交易日无效") from None
    names = value.get("stock_names", {})
    if not isinstance(names, dict) or len(names) > 10000:
        raise AuctionQueryError("DATA_INVALID: 股票名称映射无效")
    for code, name in names.items():
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not isinstance(name, str) or not PUBLIC_NAME.fullmatch(name):
            raise AuctionQueryError("DATA_INVALID: 股票名称无效")
    if "expanded_pool" in value:
        validate_expanded_pool(value["expanded_pool"])
    return dict(value)


def _display_rows(result, display):
    names = display.get("stock_names", {})
    return [{**row, **({"name": names[row["code"]]} if not row.get("name") and row["code"] in names else {})}
            for row in result["rows"]]


def _validated_display(value):
    return validate_display(value if value is not None else {
        "exposure_weight": None, "prior_trade_date": None})


def read_display(reader, *args, include_previous=True, sort_by=None):
    if type(include_previous) is not bool or (
        sort_by is not None and (not isinstance(sort_by, str) or sort_by not in SORT_FIELDS)
    ):
        raise AuctionQueryError("PARAMETERS_INVALID: 展示排序或昨日报表参数无效")
    result, display, summary = reader._query(*args, display_query=True, sort_by=sort_by)
    server, day = result["server_id"], result["trade_date"]
    display = _validated_display(display)
    prior = display["prior_trade_date"]
    if prior and prior >= day:
        raise AuctionQueryError("DATE_MISMATCH: 上一交易日必须早于查询日")
    public_display = {key: value for key, value in display.items() if key != "expanded_pool"}
    output = {**result, "rows": _display_rows(result, display), "display": public_display,
              "recommendation_summary": summary}
    if not include_previous:
        return output
    previous = {"status":"not_published", "trade_date":prior, "rows":[], "total":None}
    if prior:
        # Never infer D-1 by subtracting a calendar day, or substitute another frame.
        for prior_stage in ("corrected", "final", "history"):
            try:
                data, prior_display, _ = reader._query(
                    prior, server, prior_stage, 100, 0, None, True, display_query=True)
                previous = {"status":"published", "trade_date":prior, "stage":prior_stage,
                            "rows":_display_rows(data, _validated_display(prior_display)), "total":data["total"],
                            "data_healthy":data["data_healthy"], "source_sha256":data["source_sha256"]}
                break
            except AuctionQueryError as exc:
                if str(exc).split(":",1)[0] != "DATA_MISSING":
                    raise
    return {**output, "previous_qualified": previous}
