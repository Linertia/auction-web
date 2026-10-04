"""One allowlisted read-only MCP tool, stdio transport only."""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import uuid
import inspect
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .auction_monitor import SHANGHAI
from .auction_query import AuctionQueryError, AuctionRankingReader, Stage
from .contracts.paths import get_paths


def create_server(reader: AuctionRankingReader, audit_path: Path | None, *, executor=None, **settings) -> FastMCP:
    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
    # Startup refuses to run without a writable audit sink.
        with audit_path.open("a", encoding="utf-8"):
            pass
    lock = threading.Lock()
    last_call = [0.0]
    server = FastMCP("auction-monitor-readonly", **settings)

    def rank_auction_candidates(
        trade_date: str, server_id: str = "local", stage: Stage = "final",
        limit: int = 30, offset: int = 0, codes: list[str] | None = None,
        qualified_only: bool = False,
    ) -> dict[str, Any]:
        """Query published V2 rankings, without capture or recomputation.

        trade_date: YYYYMMDD, never a future date. server_id: operator allowlist ID.
        stage: final (original freeze), corrected (explicit later interpretation),
        or 0915/0920/0924 gate. limit 1..100; offset 0..10000.
        codes: optional up to 100 six-digit stock codes. qualified_only defaults false.
        Returns existing market numbers and qualification, not a fresh buy recommendation.
        No URL, SQL, credentials, command or path inputs are accepted.
        """
        if executor is not None:
            return executor(trade_date, server_id, stage, limit, offset, codes, qualified_only)
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        argument_hash = hashlib.sha256(json.dumps(
            [trade_date, server_id, stage, limit, offset, codes, qualified_only],
            ensure_ascii=True, sort_keys=True,
        ).encode()).hexdigest()
        result, error = None, None
        with lock:
            try:
                if time.monotonic() - last_call[0] < 0.25:
                    raise AuctionQueryError("RATE_LIMITED: 最多每秒4次查询，请稍后重试")
                last_call[0] = time.monotonic()
                result = reader.rank_auction_candidates(
                    trade_date, server_id, stage, limit, offset, codes, qualified_only,
                )
                status = "ok"
            except AuctionQueryError as exc:
                error = exc
                status = str(exc).split(":", 1)[0]
            except Exception:
                error = AuctionQueryError("QUERY_FAILED: 查询失败，请管理员检查数据源")
                status = "QUERY_FAILED"
            event = {"timestamp": datetime.now(SHANGHAI).isoformat(),
                     "request_id": request_id, "tool": "rank_auction_candidates",
                     "arguments_sha256": argument_hash, "status": status,
                     "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                     "returned_rows": len(result["rows"]) if result else 0,
                     "source_sha256": result["source_sha256"] if result else None}
            try:
                with audit_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=True) + "\n")
                    handle.flush()
            except OSError:
                raise AuctionQueryError("AUDIT_UNAVAILABLE: 审计不可用，拒绝返回数据") from None
        if error:
            raise error
        return {**result, "request_id": request_id}

    tool = rank_auction_candidates
    if executor is not None and inspect.iscoroutinefunction(executor):
        @wraps(rank_auction_candidates)
        async def remote_rank(*args, **kwargs):
            return await executor(*args, **kwargs)
        tool = remote_rank
    server.add_tool(tool, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ))
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="只读竞价 MCP Server (stdio)")
    parser.add_argument("--config", type=Path, default=get_paths().configs / "auction-mcp.json")
    args = parser.parse_args()
    try:
        payload = json.loads(args.config.read_text(encoding="utf-8"))
        root = get_paths().root
        sources = {key: root / value for key, value in payload["servers"].items()}
        reader = AuctionRankingReader(sources)
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        parser.exit(2, "CONFIG_INVALID: 只读服务器配置无效\n")
    server = create_server(reader, get_paths().resolve("runtime/auction-mcp/audit.jsonl"))
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
