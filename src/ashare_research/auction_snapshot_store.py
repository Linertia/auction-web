"""Storage boundary: returns published bytes, never runs auction algorithms."""
from __future__ import annotations

import hashlib
import json
import re
import os
import csv
import io
from pathlib import Path
from typing import Protocol
from datetime import datetime
from urllib.parse import urlsplit

MAX_BYTES = 8 * 1024 * 1024
STEMS = {"final": "fixed-0925-v2", "corrected": "fixed-0925-v2-corrected",
         "0915": "gate-0915", "0920": "gate-0920", "0924": "gate-0924"}


class AuctionQueryError(ValueError):
    """Safe messages only: no paths, credentials or provider response bodies."""


class SnapshotStore(Protocol):
    def read(self, server_id: str, trade_date: str, stage: str) -> tuple[bytes, bytes]: ...


def _read(path: Path, root: Path) -> bytes:
    if not path.resolve().is_relative_to(root):
        raise AuctionQueryError("SOURCE_OUTSIDE_ALLOWLIST: 快照不在授权目录")
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise AuctionQueryError("SOURCE_TOO_LARGE: 单文件超过8MiB限制")
        return content
    except OSError:
        raise AuctionQueryError("SOURCE_UNAVAILABLE: 快照不存在或不可读取") from None


class LocalSnapshotStore:
    def __init__(self, servers: dict[str, Path]):
        self.servers = {key: Path(value).resolve() for key, value in servers.items()}

    def read(self, server_id: str, trade_date: str, stage: str) -> tuple[bytes, bytes]:
        root = self.servers[server_id]
        stem = STEMS[stage]
        folder = root / trade_date
        if not folder.resolve().is_relative_to(root):
            raise AuctionQueryError("SOURCE_OUTSIDE_ALLOWLIST: 日期目录不在授权范围")
        try:
            candidates = list(folder.glob(f"run-v2-*/{stem}.json"))
            if len(candidates) > 128:
                raise AuctionQueryError("TOO_MANY_RUNS: 当日运行目录超过128个")
            if not candidates:
                raise AuctionQueryError("DATA_MISSING: 该日期/阶段没有已发布快照")
            if any(not path.resolve().is_relative_to(root) for path in candidates):
                raise AuctionQueryError("SOURCE_OUTSIDE_ALLOWLIST: 快照越过授权边界")
            path = (max(candidates) if stage == "final" else
                    max(candidates, key=lambda item: item.stat().st_mtime_ns))
            all_path = path.with_name(stem + "-all.csv")
            before = (path.stat().st_mtime_ns, all_path.stat().st_mtime_ns)
            raw_meta, raw_rows = _read(path, root), _read(all_path, root)
            if before != (path.stat().st_mtime_ns, all_path.stat().st_mtime_ns):
                raise AuctionQueryError("SNAPSHOT_BUSY: 快照正在发布，请稍后重试")
            return raw_meta, raw_rows
        except OSError:
            raise AuctionQueryError("DATA_INVALID: 快照缺失或未完整发布") from None


class S3SnapshotStore:
    """Only GetObject on fixed manifest and immutable content-addressed bundle.

    Bucket/prefix/client are operator configuration, never tool input.
    Reader IAM: s3:GetObject only on prefix/*; no Put/List/Delete permissions.
    """
    def __init__(self, bucket: str, prefix: str = "auction", client=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise AuctionQueryError("CONFIG_INVALID: 对象存储桶名称无效")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", prefix):
            raise AuctionQueryError("CONFIG_INVALID: 对象存储前缀无效")
        self.bucket, self.prefix = bucket, prefix
        if client is None:
            import boto3
            from botocore.config import Config
            access, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
            if not access or not secret:
                raise AuctionQueryError("CONFIG_INVALID: 需要环境注入的只读对象存储凭证")
            client = boto3.client("s3", aws_access_key_id=access, aws_secret_access_key=secret,
                aws_session_token=os.environ.get("AWS_SESSION_TOKEN"),
                region_name=os.environ.get("AWS_REGION","us-east-1"),
                config=Config(connect_timeout=2, read_timeout=3,
                retries={"total_max_attempts": 1}))
        self.client = client

    def _get(self, key: str, maximum: int) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                content = body.read(maximum + 1)
            finally:
                body.close()
            if len(content) > maximum:
                raise AuctionQueryError("SOURCE_TOO_LARGE: 云端快照超过大小限制")
            return content
        except AuctionQueryError:
            raise
        except Exception as exc:
            # Only machine error codes; never include URL/header/provider message.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                raise AuctionQueryError("DATA_MISSING: 云端没有该已发布快照") from None
            raise AuctionQueryError("SOURCE_UNAVAILABLE: 云端只读存储不可用") from None

    def read(self, server_id: str, trade_date: str, stage: str) -> tuple[bytes, bytes]:
        try:
            manifest = json.loads(self._get(
                f"{self.prefix}/{server_id}/{trade_date}/{stage}/latest.json", 65536))
            digest = manifest["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError()
            blob = self._get(f"{self.prefix}/objects/{digest}.json", 2 * MAX_BYTES)
            if hashlib.sha256(blob).hexdigest() != digest:
                raise AuctionQueryError("SOURCE_HASH_MISMATCH: 云端对象完整性检查失败")
            bundle = json.loads(blob)
            raw_meta = bundle["metadata_utf8"].encode("utf-8")
            raw_rows = bundle["rows_csv_utf8"].encode("utf-8")
            if len(raw_meta) > MAX_BYTES or len(raw_rows) > MAX_BYTES:
                raise AuctionQueryError("SOURCE_TOO_LARGE: 云端快照超过大小限制")
            return raw_meta, raw_rows
        except AuctionQueryError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AuctionQueryError("DATA_INVALID: 云端发布清单或对象损坏") from None


def supabase_settings() -> tuple[str, str, str, str]:
    """Operator environment only; never accepted in REST/MCP parameters."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    secret = os.environ.get("SUPABASE_SECRET_KEY", "")
    bucket = os.environ.get("SUPABASE_STORAGE_BUCKET", "auction-snapshots")
    prefix = os.environ.get("SUPABASE_STORAGE_PREFIX", "auction")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname
                 and re.fullmatch(r"[a-z0-9-]+\.supabase\.co", parsed.hostname)
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if (not valid or not re.fullmatch(r"sb_secret_[A-Za-z0-9_-]{16,}", secret)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", bucket)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", prefix)):
        raise AuctionQueryError("CONFIG_INVALID: 需要有效的Supabase服务端环境配置")
    return url, secret, bucket, prefix


def supabase_storage_client(url: str, secret: str):
    """Official pinned SDK; no credential files, Auth refresh or session persistence."""
    try:
        import httpx
        from supabase import ClientOptions, create_client
        # Redirects cannot forward the secret to a different origin; environment
        # proxy variables are not used. SDK exceptions must never escape callers.
        session = httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0),
                               follow_redirects=False, trust_env=False)
        return create_client(url, secret, options=ClientOptions(
            auto_refresh_token=False, persist_session=False,
            storage_client_timeout=3, httpx_client=session)).storage
    except Exception:
        raise AuctionQueryError("SOURCE_UNAVAILABLE: 无法初始化Supabase存储") from None


def require_private_supabase_bucket(storage, bucket: str) -> None:
    """Check metadata without changing bucket permissions or creating a bucket."""
    try:
        metadata = storage.get_bucket(bucket)
        public = metadata.get("public") if isinstance(metadata, dict) else metadata.public
        if public is not False:
            raise AuctionQueryError("BUCKET_NOT_PRIVATE: 拒绝使用非私有Storage桶")
    except AuctionQueryError:
        raise
    except Exception:
        raise AuctionQueryError("SOURCE_UNAVAILABLE: 私有Storage桶不可访问") from None


class SupabaseSnapshotStore:
    """Downloads only published pointers/bundles. Project secret is NOT read-only IAM."""
    def __init__(self, *, client=None):
        url, secret, self.bucket, self.prefix = supabase_settings()
        self._storage = client if client is not None else supabase_storage_client(url, secret)

    def _get(self, key: str, maximum: int) -> bytes:
        try:
            content = self._storage.from_(self.bucket).download(key)
            if not isinstance(content, bytes):
                raise AuctionQueryError("DATA_INVALID: Storage对象不是字节数据")
            if len(content) > maximum:
                raise AuctionQueryError("SOURCE_TOO_LARGE: Storage对象超过大小限制")
            return content
        except AuctionQueryError:
            raise
        except Exception as exc:
            # Inspect structured status/code only; never echo exception messages.
            if str(getattr(exc, "status", "")) == "404" or getattr(exc, "code", "") in (
                "NoSuchKey", "NoSuchObject", "not_found"):
                raise AuctionQueryError("DATA_MISSING: 该日期或阶段没有完整发布的快照") from None
            raise AuctionQueryError("SOURCE_UNAVAILABLE: Supabase存储不可用") from None

    def read(self, server_id: str, trade_date: str, stage: str) -> tuple[bytes, bytes]:
        if (not isinstance(server_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", server_id)
            or not isinstance(trade_date, str) or not re.fullmatch(r"[0-9]{8}", trade_date)
            or not isinstance(stage, str) or stage not in STEMS):
            raise AuctionQueryError("PARAMETERS_INVALID: Storage查询标识无效")
        try:
            datetime.strptime(trade_date, "%Y%m%d")
        except ValueError:
            raise AuctionQueryError("DATE_INVALID: 日期不存在") from None
        require_private_supabase_bucket(self._storage, self.bucket)
        try:
            manifest = json.loads(self._get(
                f"{self.prefix}/{server_id}/{trade_date}/{stage}/latest.json", 65536))
            if (not isinstance(manifest, dict) or set(manifest) != {"sha256"}
                or not isinstance(manifest["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])):
                raise ValueError()
            digest = manifest["sha256"]
            blob = self._get(f"{self.prefix}/objects/{digest}.json", 2 * MAX_BYTES)
            if hashlib.sha256(blob).hexdigest() != digest:
                raise AuctionQueryError("SOURCE_HASH_MISMATCH: Storage对象SHA256不一致")
            bundle = json.loads(blob)
            if not isinstance(bundle, dict) or set(bundle) != {"metadata_utf8", "rows_csv_utf8"}:
                raise ValueError()
            if any(not isinstance(value, str) for value in bundle.values()):
                raise ValueError()
            raw_meta, raw_rows = bundle["metadata_utf8"].encode(), bundle["rows_csv_utf8"].encode()
            if len(raw_meta) > MAX_BYTES or len(raw_rows) > MAX_BYTES:
                raise AuctionQueryError("SOURCE_TOO_LARGE: 快照超过大小限制")
            # Refuse unsanitized metadata/CSV even if their content hash is valid.
            from .auction_query import PUBLIC_NUMBERS
            metadata = json.loads(raw_meta)
            if not isinstance(metadata, dict) or set(metadata) != {"cycle_id", "observed_at", "data_healthy"}:
                raise ValueError()
            columns = csv.DictReader(io.StringIO(raw_rows.decode("utf-8-sig"))).fieldnames
            expected = {"cycle_id", "observed_at", "code", "rank", "qualified", *PUBLIC_NUMBERS}
            if columns is None or set(columns) != expected or len(columns) != len(expected):
                raise ValueError()
            return raw_meta, raw_rows
        except AuctionQueryError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, csv.Error):
            raise AuctionQueryError("DATA_INVALID: 发布指针或去敏对象格式无效") from None
