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
              "historical_archive_final_v2": "main_board_archive_with_missing_accounted",
              "historical_capture_final_v2": "captured_main_board_observations_not_full_market"}
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
        return self._query(trade_date, server_id, stage, limit, offset, codes, qualified_only)

    def _query(self, trade_date, server_id="local", stage="final", limit=30, offset=0, codes=None,
               qualified_only=False, *, display_query=False, sort_by=None):
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
        meta, ranked, display = self._snapshot(server_id, trade_date, stage)
        wanted = set(codes or [])
        pool = display.get("expanded_pool") if display_query and isinstance(display, dict) else None
        members = {row["code"]: row for row in pool} if pool is not None else {}
        if wanted and wanted - ({row["code"] for row in ranked} | members.keys()):
            raise AuctionQueryError("CODE_NOT_FOUND: 指定代码在该快照中没有有效数据")
        summary = None
        if display_query:
            if stage in ("final", "history", "corrected"):
                candidates = pool if pool is not None else [row for row in ranked if row["qualified"]]
                summary = {"total": len(candidates), "low_score": any(row["score"] < .5 for row in candidates)}
                if pool is not None:
                    summary["source"] = "expanded_pool"
        if pool is not None and qualified_only:
            by_code = {row["code"]: row for row in ranked}
            rows = [{**by_code.get(row["code"], {}), **row, "rank": row["original_rank"]}
                    for row in pool if not wanted or row["code"] in wanted]
        else:
            rows = [row for row in ranked if (not wanted or row["code"] in wanted)
                    and (not qualified_only or row["qualified"])]
            if sort_by is not None:
                rows = self._book_rows(rows, server_id, trade_date, stage, sort_by)
        if pool is not None and not qualified_only:
            rows = [{**row, "in_recommended_pool": row["code"] in members,
                     **({"original_rank": members[row["code"]]["original_rank"]} if row["code"] in members else {})}
                    for row in rows]
        result = {**meta, "server_id": server_id, "trade_date": trade_date,
                  "stage": stage, "total": len(rows), "offset": offset,
                  "limit": limit, "rows": [dict(row) for row in rows[offset:offset + limit]]}
        return (result, display, summary) if display_query else result

    def _snapshot(self, server_id, trade_date, stage):
        ttl = 300 if stage in ("0915", "0920", "final", "history") else 1
        with self._lock:
            key = (server_id, trade_date, stage)
            cached = self._cache.get(key)
            if not cached or time.monotonic() - cached[0] >= ttl:
                snapshot = self._load(self.servers[server_id], trade_date, stage, server_id)
                self._cache[key] = cached = (time.monotonic(), *snapshot)
            self._cache.move_to_end(key)
            while len(self._cache) > 8:
                self._cache.popitem(last=False)
            return cached[1:]

    def _book_rows(self, rows, server_id, trade_date, stage, sort_by):
        previous = {}
        previous_stage = {"0920": "0915", "final": "0920"}.get(stage)
        if previous_stage:
            try:
                _, prior_rows, _ = self._snapshot(server_id, trade_date, previous_stage)
                previous = {row["code"]: row for row in prior_rows}
            except AuctionQueryError as exc:
                if str(exc).split(":", 1)[0] != "DATA_MISSING":
                    raise
        enriched = []
        for row in rows:
            prior = previous.get(row["code"])
            enriched.append({**row,
                **{f"unmatched_{side}_delta_yuan": (
                    row[f"unmatched_{side}_amount_yuan"] - prior[f"unmatched_{side}_amount_yuan"]
                    if prior is not None else None) for side in ("buy", "sell")}})
        return sorted(enriched, key=lambda row: (
            row[sort_by] is None, -(row[sort_by] or 0), row["rank"], row["code"]))

    def _load(self, root: Path, trade_date: str, stage: Stage, server_id: str) -> tuple[dict, list[dict], object]:
        try:
            raw_meta, raw_rows = self.store.read(server_id, trade_date, stage)
            meta = json.loads(raw_meta)
            display = meta.get("display")
            if isinstance(display, dict) and "expanded_pool" in display:
                if stage != "final" and (stage != "history" or not validate_provenance(meta.get("provenance"))):
                    raise AuctionQueryError("DATA_INVALID: 扩展选股池必须绑定正式冻结或有来源标记的历史冻结")
                from .auction_display import validate_expanded_pool
                validate_expanded_pool(display["expanded_pool"])
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
            features, public, excluded, seen, source_ranks = [], {}, [], set(), []
            unranked_count = 0
            reader = csv.DictReader(io.StringIO(raw_rows.decode("utf-8-sig")))
            for index, row in enumerate(reader):
                if index >= MAX_ROWS:
                    raise AuctionQueryError("SOURCE_TOO_LARGE: 快照超过10000行")
                code = row["code"]
                if not re.fullmatch(r"[0-9]{6}", code) or code in seen:
                    raise AuctionQueryError("DATA_INVALID: 股票代码无效或重复")
                seen.add(code)
                if datetime.fromisoformat(row["observed_at"]).astimezone(SHANGHAI).strftime("%Y%m%d") != trade_date:
                    raise AuctionQueryError("DATE_MISMATCH: 明细日期不匹配")
                if row["cycle_id"] != cycle_id:
                    raise AuctionQueryError("SNAPSHOT_BUSY: 快照和明细的采集批次不一致")
                if row["qualified"] not in ("True", "False"):
                    raise AuctionQueryError("DATA_INVALID: qualified字段无效")
                source_rank = int(row['rank'])
                if source_rank < 0 or (source_rank == 0 and row['qualified'] == 'True'):
                    raise AuctionQueryError('RANK_MISMATCH: 未排名记录不能标记为候选')
                if source_rank:
                    source_ranks.append(source_rank)
                numbers, invalid = {}, []
                for field in PUBLIC_NUMBERS:
                    try:
                        value = float(row[field])
                    except (TypeError, ValueError):
                        invalid.append(field)
                        continue
                    if not math.isfinite(value):
                        invalid.append(field)
                    else:
                        numbers[field] = value
                if invalid:
                    # Isolate only numeric incompleteness; identity/date/rank and
                    # publication integrity remain whole-snapshot requirements.
                    excluded.append({'code': code, 'reason': 'DATA_INSUFFICIENT', 'fields': invalid})
                    continue
                if source_rank == 0:
                    # The producer explicitly marks observation-only records as
                    # unranked. Never expand the strategy universe in a reader.
                    unranked_count += 1
                    continue
                feature = AuctionFeature(
                    cycle_id="", observed_at="", source_time="", code=code, name="",
                    rank=source_rank, float_shares=0, float_share_source="", **numbers,
                )
                features.append(feature)
                # Only public market numbers; no account, holding, paths or arbitrary text.
                public[code] = {"code": code, "rank": feature.rank, **numbers,
                                "qualified": row["qualified"] == "True"}
            if not features:
                raise AuctionQueryError("DATA_EMPTY: 快照没有有效竞价明细")
            ranked = rank_auction_features(features)
            if (sorted(source_ranks) != list(range(1, len(source_ranks) + 1))
                or [row.rank for row in ranked] != sorted(row.rank for row in features)):
                raise AuctionQueryError("RANK_MISMATCH: 保存排名与现有排序入口不一致")
            quality = ({'data_quality': {
                'status': 'partial' if excluded else 'complete', 'source_data_healthy': meta['data_healthy'],
                'source_row_count': len(seen), 'valid_row_count': len(features),
                'unranked_observation_count': unranked_count,
                'excluded_row_count': len(excluded), 'excluded_rows': excluded,
                'ranking_scope': 'valid_rows_only', 'full_market_top30_confirmed': False,
            }} if excluded or unranked_count else {})
            provenance = validate_provenance(meta.get("provenance"))
            digest = hashlib.sha256(raw_meta + raw_rows).hexdigest()
            return ({"observed_at": observed.isoformat(), "data_healthy": meta["data_healthy"] and not excluded,
                     **quality,
                     "source_sha256": digest,
                     "cached_seconds_max": 300 if stage in ("0915", "0920", "final", "history") else 1,
                     "interpretation": (("historical_gate_conditioned_result" if provenance['kind'] == 'historical_gate_backfill_v2'
                                         else "historical_archive_final_result") if provenance else "published_monitor_result"),
                     "sampling_scope": (provenance["sampling_scope"] if provenance
                                        else "published_monitor_scope"),
                     **({"provenance": provenance} if provenance else {})},
                    [public[row.code] for row in ranked], display)
        except AuctionQueryError:
            raise
        except (OSError, ValueError, TypeError, KeyError, csv.Error):
            raise AuctionQueryError("DATA_INVALID: 快照缺失、未完整发布或字段损坏") from None
