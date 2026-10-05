"""Allowlisted display metadata; never computes strategy or personal positions."""
import hashlib
import json
from datetime import datetime

from .auction_query import AuctionQueryError


def validate_display(value):
    if not isinstance(value, dict) or set(value) != {"exposure_weight", "prior_trade_date"}:
        raise AuctionQueryError("DATA_INVALID: 展示元数据无效")
    weight, prior = value["exposure_weight"], value["prior_trade_date"]
    if weight is not None and (type(weight) not in (int, float) or weight not in (.25, .5, 1)):
        raise AuctionQueryError("DATA_INVALID: 市场仓位结果无效")
    if prior is not None:
        if not isinstance(prior, str) or len(prior) != 8 or not prior.isdigit():
            raise AuctionQueryError("DATA_INVALID: 上一交易日无效")
        try:
            datetime.strptime(prior, "%Y%m%d")
        except ValueError:
            raise AuctionQueryError("DATA_INVALID: 上一交易日无效") from None
    return dict(value)


def read_display(reader, *args):
    result = reader.rank_auction_candidates(*args)
    server, day, stage = result["server_id"], result["trade_date"], result["stage"]
    raw_meta, raw_rows = reader.store.read(server, day, stage)
    if hashlib.sha256(raw_meta + raw_rows).hexdigest() != result["source_sha256"]:
        raise AuctionQueryError("SNAPSHOT_BUSY: 展示与排名快照不一致")
    display = validate_display(json.loads(raw_meta).get("display", {
        "exposure_weight":None, "prior_trade_date":None}))
    prior = display["prior_trade_date"]
    previous = {"status":"not_published", "trade_date":prior, "rows":[], "total":None}
    if prior:
        if prior >= day:
            raise AuctionQueryError("DATE_MISMATCH: 上一交易日必须早于查询日")
        # Never infer D-1 by subtracting a calendar day, or substitute another frame.
        for prior_stage in ("corrected", "final"):
            try:
                data = reader.rank_auction_candidates(prior,server,prior_stage,100,0,None,True)
                previous = {"status":"published", "trade_date":prior, "stage":prior_stage,
                            "rows":data["rows"], "total":data["total"],
                            "data_healthy":data["data_healthy"], "source_sha256":data["source_sha256"]}
                break
            except AuctionQueryError as exc:
                if str(exc).split(":",1)[0] != "DATA_MISSING":
                    raise
    return {**result, "display":display, "previous_qualified":previous}
