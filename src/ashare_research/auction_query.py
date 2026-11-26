"""Bounded, read-only access to already published V2 auction rankings."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Literal

from .auction_monitor import AuctionFeature, SHANGHAI
from .auction_monitor_v2 import rank_auction_features
from .auction_snapshot_store import (
    AuctionQueryError, SnapshotStore, LocalSnapshotStore, STEMS, MAX_BYTES, _read,
)

Stage = Literal["final", "corrected", "history", "0915", "0920", "0924"]
MAX_ROWS = 10000
PUBLIC_NUMBERS = ("score", "gap_pct", "matched_price", "matched_volume_lots",
                  "matched_amount_yuan", "turnover_pct", "unmatched_buy_amount_yuan",
                  "unmatched_sell_amount_yuan", "unmatched_buy_turnover_pct",
                  "unmatched_sell_turnover_pct")


def validate_provenance(value: object) -> dict | None:
    """Validate the small public provenance contract used by historical freezes."""
    if value is None:
        return None
    expected = {
        "kind", "sampling_scope", "market_source", "backfill_status",
        "free_share_source_date", "free_share_exact_date", "event_risk_complete",
        "source_row_count", "published_row_count", "excluded_row_count",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise AuctionQueryError("DATA_INVALID: 快照来源标记无效")
    scopes = {"historical_gate_backfill_v2": "gate_conditioned_not_full_market",
              "historical_archive_final_v2": "main_board_archive_with_missing_accounted"}
    if value["kind"] not in scopes:
        raise AuctionQueryError("DATA_INVALID: 快照来源类型无效")
    if value["sampling_scope"] != scopes[value["kind"]]:
        raise AuctionQueryError("DATA_INVALID: 快照样本范围无效")
    if value["market_source"] != "amazingdata_first_match_publish":
        raise AuctionQueryError("DATA_INVALID: 快照行情来源无效")
    if value["backfill_status"] not in {"complete", "partial"}:
        raise AuctionQueryError("DATA_INVALID: 历史补测状态无效")
    source_date = value["free_share_source_date"]
    if not isinstance(source_date, str) or not re.fullmatch(r"[0-9]{8}", source_date):
        raise AuctionQueryError("DATA_INVALID: 流通股本来源日期无效")
    try:
        datetime.strptime(source_date, "%Y%m%d")
    except ValueError:
        raise AuctionQueryError("DATA_INVALID: 流通股本来源日期无效") from None
    if type(value["free_share_exact_date"]) is not bool or type(value["event_risk_complete"]) is not bool:
        raise AuctionQueryError("DATA_INVALID: 历史补测质量标记无效")
    counts = [value[key] for key in ("source_row_count", "published_row_count", "excluded_row_count")]
    if (any(type(item) is not int or item < 0 for item in counts)
        or value["published_row_count"] <= 0
        or value["source_row_count"] != value["published_row_count"] + value["excluded_row_count"]):
        raise AuctionQueryError("DATA_INVALID: 历史补测行数标记无效")
    return dict(value)


class AuctionRankingReader:
    """Sources are fixed by the operator, never accepted from tool arguments."""

    def __init__(self, servers: dict[str, Path], *, store: SnapshotStore | None = None):
        if not servers or len(servers) > 32:
            raise AuctionQueryError("CONFIG_INVALID: 需要1至32个已授权服务器")
        self.servers = {}
        for key, root in servers.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", key):
                raise AuctionQueryError("CONFIG_INVALID: 服务器ID格式错误")
            self.servers[key] = Path(root).resolve()
        self.store = store if store is not None else LocalSnapshotStore(self.servers)
        self._lock = threading.Lock()
        self._cache: OrderedDict = OrderedDict()

    def rank_auction_candidates(
        self, trade_date: str, server_id: str = "local", stage: Stage = "final",
        limit: int = 30, offset: int = 0, codes: list[str] | None = None,
        qualified_only: bool = False,
    ) -> dict:
        if not isinstance(trade_date, str) or not re.fullmatch(r"[0-9]{8}", trade_date):
            raise AuctionQueryError("DATE_INVALID: 日期必须是YYYYMMDD")
        try:
            day = datetime.strptime(trade_date, "%Y%m%d").date()
        except ValueError:
            raise AuctionQueryError("DATE_INVALID: 日期不存在") from None
        if day > datetime.now(SHANGHAI).date():
            raise AuctionQueryError("DATE_INVALID: 不允许查询未来日期")
        if server_id not in self.servers:
            raise AuctionQueryError("SERVER_NOT_ALLOWED: 服务器未预先授权")
        if stage not in STEMS:
            raise AuctionQueryError("STAGE_INVALID: 使用final/corrected/history/0915/0920/0924")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AuctionQueryError("LIMIT_INVALID: limit必须是1至100的整数")
        if type(offset) is not int or not 0 <= offset <= MAX_ROWS:
            raise AuctionQueryError("OFFSET_INVALID: offset必须是0至10000的整数")
        if type(qualified_only) is not bool:
            raise AuctionQueryError("FILTER_INVALID: qualified_only必须是布尔值")
        if codes is not None and (not isinstance(codes, list) or len(codes) > 100 or
            any(not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) for code in codes)):
            raise AuctionQueryError("CODE_INVALID: 最多100个六位数字股票代码")
        with self._lock:
            root = self.servers[server_id]
            key = (server_id, trade_date, stage)
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 1.0:
                meta, ranked = cached[1:]
            else:
                meta, ranked = self._load(root, trade_date, stage, server_id)
                self._cache[key] = (time.monotonic(), meta, ranked)
                self._cache.move_to_end(key)
                while len(self._cache) > 8:
                    self._cache.popitem(last=False)
            wanted = set(codes or [])
            available = {row["code"] for row in ranked}
            if wanted - available:
                raise AuctionQueryError("CODE_NOT_FOUND: 指定代码在该快照中没有有效数据")
            rows = [row for row in ranked if (not wanted or row["code"] in wanted)
                    and (not qualified_only or row["qualified"])]
            return {**meta, "server_id": server_id, "trade_date": trade_date,
                    "stage": stage, "total": len(rows), "offset": offset,
                    "limit": limit, "rows": [dict(row) for row in rows[offset:offset + limit]]}

    def _load(self, root: Path, trade_date: str, stage: Stage, server_id: str) -> tuple[dict, list[dict]]:
        try:
            raw_meta, raw_rows = self.store.read(server_id, trade_date, stage)
            meta = json.loads(raw_meta)
            cycle_id = meta.get("cycle_id")
            if not cycle_id and stage == "corrected" and meta.get("top30"):
                cycle_id = meta["top30"][0]["cycle_id"]
            if not isinstance(cycle_id, str) or not cycle_id:
                raise AuctionQueryError("DATA_INVALID: 快照缺少采集批次")
            observed = datetime.fromisoformat(meta["observed_at"])
            if observed.tzinfo is None or observed.astimezone(SHANGHAI).strftime("%Y%m%d") != trade_date:
                raise AuctionQueryError("DATE_MISMATCH: 快照日期与查询日期不一致")
            if type(meta.get("data_healthy")) is not bool:
                raise AuctionQueryError("DATA_INVALID: 快照缺少数据健康状态")
            features, public = [], {}
            reader = csv.DictReader(io.StringIO(raw_rows.decode("utf-8-sig")))
            for index, row in enumerate(reader):
                if index >= MAX_ROWS:
                    raise AuctionQueryError("SOURCE_TOO_LARGE: 快照超过10000行")
                code = row["code"]
                if not re.fullmatch(r"[0-9]{6}", code) or code in public:
                    raise AuctionQueryError("DATA_INVALID: 股票代码无效或重复")
                if datetime.fromisoformat(row["observed_at"]).astimezone(SHANGHAI).strftime("%Y%m%d") != trade_date:
                    raise AuctionQueryError("DATE_MISMATCH: 明细日期不匹配")
                if row["cycle_id"] != cycle_id:
                    raise AuctionQueryError("SNAPSHOT_BUSY: 快照和明细的采集批次不一致")
                numbers = {field: float(row[field]) for field in PUBLIC_NUMBERS}
                if not all(math.isfinite(value) for value in numbers.values()):
                    raise AuctionQueryError("DATA_INVALID: 必需竞价特征缺失或非有限数")
                if row["qualified"] not in ("True", "False"):
                    raise AuctionQueryError("DATA_INVALID: qualified字段无效")
                feature = AuctionFeature(
                    cycle_id="", observed_at="", source_time="", code=code, name="",
                    rank=int(row["rank"]), float_shares=0, float_share_source="", **numbers,
                )
                features.append(feature)
                # Only public market numbers; no account, holding, paths or arbitrary text.
                public[code] = {"code": code, "rank": feature.rank, **numbers,
                                "qualified": row["qualified"] == "True"}
            if not features:
                raise AuctionQueryError("DATA_EMPTY: 快照没有有效竞价明细")
            ranked = rank_auction_features(features)
            if any(row.rank != index for index, row in enumerate(ranked, 1)):
                raise AuctionQueryError("RANK_MISMATCH: 保存排名与现有排序入口不一致")
            provenance = validate_provenance(meta.get("provenance"))
            digest = hashlib.sha256(raw_meta + raw_rows).hexdigest()
            return ({"observed_at": observed.isoformat(), "data_healthy": meta["data_healthy"],
                     "source_sha256": digest, "cached_seconds_max": 1,
                     "interpretation": (("historical_archive_final_result" if provenance['kind'] == 'historical_archive_final_v2'
                                         else "historical_gate_conditioned_result") if provenance else "published_monitor_result"),
                     "sampling_scope": (provenance["sampling_scope"] if provenance
                                        else "published_monitor_scope"),
                     **({"provenance": provenance} if provenance else {})},
                    [public[row.code] for row in ranked])
        except AuctionQueryError:
            raise
        except (OSError, ValueError, TypeError, KeyError, csv.Error):
            raise AuctionQueryError("DATA_INVALID: 快照缺失、未完整发布或字段损坏") from None
