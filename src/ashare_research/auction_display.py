"""Allowlisted display metadata; never computes strategy or personal positions."""
import hashlib
import json
import re
from datetime import datetime

from .auction_query import AuctionQueryError


def validate_display(value):
    if not isinstance(value, dict) or not {"exposure_weight", "prior_trade_date"} <= set(value) or set(value) - {"exposure_weight", "prior_trade_date", "stock_names"}:
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
    names = value.get("stock_names", {})
    if not isinstance(names, dict) or len(names) > 10000:
        raise AuctionQueryError("DATA_INVALID: 股票名称映射无效")
    for code, name in names.items():
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not isinstance(name, str) or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9 *().（）-]{1,32}", name):
            raise AuctionQueryError("DATA_INVALID: 股票名称无效")
    return dict(value)


def _display_rows(reader, result, display=None):
    if display is None:
        meta, rows = reader.store.read(result["server_id"], result["trade_date"], result["stage"])
        if hashlib.sha256(meta + rows).hexdigest() != result["source_sha256"]:
            raise AuctionQueryError("SNAPSHOT_BUSY: 名称与排名快照不一致")
        display = validate_display(json.loads(meta).get("display", {
            "exposure_weight": None, "prior_trade_date": None}))
    names = display.get("stock_names", {})
    return [{**row, **({"name": names[row["code"]]} if row["code"] in names else {})}
            for row in result["rows"]]


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
                            "rows":_display_rows(reader, data), "total":data["total"],
                            "data_healthy":data["data_healthy"], "source_sha256":data["source_sha256"]}
                break
            except AuctionQueryError as exc:
                if str(exc).split(":",1)[0] != "DATA_MISSING":
                    raise
    return {**result, "rows":_display_rows(reader, result, display), "display":display, "previous_qualified":previous}
