# auction-web

[中文说明](README.zh-CN.md) | English

Private, read-only auction snapshot website, REST API and Streamable HTTP MCP.
The production collector remains elsewhere. No snapshots, databases, account
data, credentials, environment files or production configuration belong here.

## Vercel Git import

Import the private GitHub repository. Choose the repository root (`.`) as Root
Directory and the FastAPI framework preset. Keep Build Command, Install Command
and Output Directory at their framework defaults. Python 3.12 is declared in
`pyproject.toml`; `index.py` exports the ASGI `app`. Dependencies are installed
from `requirements.txt`. The function deadline is 30 seconds.

The webpage is rendered by the function, not a public source/static directory.
Never publish Python source or application configuration as browser assets.

## Server-side environment configuration

Configure these in the Vercel dashboard for production and, if required, preview.
Do not use public/client-side variable prefixes. Do not add values to this repo.

| Variable | Value / purpose |
| --- | --- |
| `AUCTION_PUBLIC_URL` | Fixed HTTPS website origin, no trailing slash or `/mcp` |
| `AUCTION_SNAPSHOT_BACKEND` | `supabase` |
| `AUCTION_SERVER_IDS` | Authorized server IDs as JSON, e.g. `["local"]` |
| `SUPABASE_URL` | Hosted HTTPS project origin: https://project-ref.supabase.co |
| `SUPABASE_SECRET_KEY` | Modern server-only sb_secret_ key; never browser-visible |
| `SUPABASE_STORAGE_BUCKET` | `auction-snapshots`, private only |
| `SUPABASE_STORAGE_PREFIX` | `auction` |
| `AUCTION_AUTH_USERS_JSON` | Per-user SHA-256 Bearer hashes and authorized server IDs |
| `UPSTASH_REDIS_REST_URL` | HTTPS Redis REST endpoint |
| `UPSTASH_REDIS_REST_TOKEN` | Redis control identity for rate limiting and audit |

Bearer whitelist schema: each user ID maps to an object containing `sha256`
(64 hex characters), `servers` (authorized IDs), and optional `expires_at`
(Unix seconds). Generate strong per-user credentials outside this repository;
only their hashes are configured in the server. Never include the actual token
in a URL, command argument, commit, browser storage or log.

For external OAuth instead of the private Bearer whitelist, additionally set
`AUCTION_OAUTH_ISSUER`, `AUCTION_OAUTH_JWKS_URL`, and
`AUCTION_OAUTH_GRANTS_JSON` (subject IDs mapped to authorized server ID lists).
The external identity provider must issue RS256 tokens with scope `auction:read`
and audience `<AUCTION_PUBLIC_URL>/mcp`. This repository is not an OAuth issuer.

Sanitized Supabase Storage objects must already be published:

```
auction/objects/<SHA256>.json
auction/<server_id>/<trade_date>/<stage>/latest.json
```

The pointer contains only `{"sha256":"<content hash>"}`. The object is the
existing sanitized metadata/CSV bundle. Final and corrected stages are separate.
Project secret keys bypass RLS: separate named query/publisher keys permit
independent revocation but NOT read/write permission isolation. The application
query entry is read-only; a leaked project secret still permits writes. Strict
least privilege requires a separate Auth/JWT + Storage RLS design, not implemented.
Legacy S3 remains available with backend=s3 and its original AWS variables/IAM.
Redis needs the existing rate-limit EVAL (INCR/EXPIRE) and audit XADD permissions.
Vercel Marketplace's paired `KV_REST_API_URL` / `KV_REST_API_TOKEN` variables
are also supported. Explicit `UPSTASH_REDIS_REST_*` configuration takes priority;
the two credential pairs are never mixed. Do not use the read-only Redis token.
After updating production authentication or storage variables, redeploy before
testing: existing deployments retain their original environment configuration.

Create a PRIVATE auction-snapshots bucket (Public off), JSON MIME only, preferably
16 MiB file limit. No browser/anon Storage policy is needed. Both reader and
publisher verify bucket privacy. Downloads validate the SHA-256 before parsing.
The local publisher consumes existing sanitized exports, never computes strategy:
upload immutable content without upsert, download/verify length and hash, then
upload stage pointer and confirm by download. Failed content cannot update pointer.
Serialize publication per date/stage; pointer timeouts may require confirmation.

Official Python SDK is pinned to supabase==2.32.0; its dependency closure is
requirements-supabase.lock.txt. Hosted project URLs and modern secret keys only.
SDK downloads buffer the object before size checks; do not populate this bucket
with arbitrary large files. See [API keys](https://supabase.com/docs/guides/api/api-keys)
and [current free limits](https://supabase.com/pricing): when checked, 1 GB Storage,
5 GB egress plus 5 GB cached egress, 50 MB platform file limit and possible
inactivity pause after one week. No live Supabase/Vercel verification performed yet.

## First deployment verification

1. Check Vercel reports READY. Missing configuration must fail closed, not
   silently enable anonymous access or fall back to a local production disk.
2. Open `/` on another computer; the query form should load without exposing
   credentials, strategy formulas or Python files.
3. POST `/api/auction/rank` without authentication: expect 401. With a separately
   supplied authorized Bearer header and JSON body
   `{"trade_date":"20260930","server_id":"local","stage":"final","limit":3}`,
   expect 200 only after that exact snapshot has been published to Storage. Compare
   codes, ranks, scores and qualification with the original local query result.
4. Use an MCP client at `/mcp`: initialize, tools/list, tools/call. The only tool
   must be `rank_auction_candidates`; its result must match the REST response
   except the per-request request ID.
5. Verify invalid dates/codes, unauthorized server IDs, and rate limits are
   rejected. Confirm Redis audit events exist without tokens or raw results.
   Audit or Redis failure must refuse data delivery.

Use the stable public origin for MCP requests. Preview origins may need a
separate `AUCTION_PUBLIC_URL`; platform deployment protection can independently
require Vercel login. Do not disable application authentication to bypass it.

## Code provenance and boundaries

`AuctionRankingReader.rank_auction_candidates()` remains the single query
entry. REST and MCP call it without running new scoring or qualification.
`auction_monitor.py` contains only the original AuctionFeature dataclass and
Shanghai timezone; `auction_monitor_v2.py` contains only the original ranking
function. These definitions are retained verbatim from the existing project.
Collectors, provider clients and strategy calculation modules are omitted.
The source research repository and its local stdio entrypoint are unchanged.

The FastAPI layout matches https://vercel.com/docs/frameworks/backend/fastapi .
Local import/protocol checks do not prove a cloud deployment is operational:
cloud build, Supabase/Redis access and cross-device access require live testing.
# Invitation-only website sessions

The website exposes the same phone-login and query interface to everyone;
no administrator setup, account management, role label or compatibility Bearer field.
Administrator provisioning uses a separate local masked-input utility, excluded
from this repository. Existing accounts require explicit consent to reset the
password, enable, restore admin rights and revoke old sessions. Backend management
still enforces administrator authorization; promotion additionally requires the
explicitly configured owner Bearer. MCP remains Bearer/OAuth-only.

Set server-only `AUCTION_LOGIN_ENABLED=1`, `AUCTION_ADMIN_SUBJECTS=["owner"]` and
optionally `AUCTION_ACCOUNT_LIMIT=100`. Passwords are salted scrypt hashes;
Secure HttpOnly SameSite=Strict cookies expire after seven days. New login revokes
the previous session; already displayed data cannot be recalled. This is not
physical-device attestation. Redis is authoritative account storage, not disposable
cache. Deleting/evicting account keys loses accounts; backups are not implemented.

When changing domains, update `AUCTION_PUBLIC_URL` to the new HTTPS origin and
redeploy. Re-login and update MCP clients to the new `/mcp` endpoint.
