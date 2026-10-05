# auction-web：竞价快照只读查询网站

中文 | [English](README.md)

本仓库提供网页、普通 REST API 和 Streamable HTTP MCP，用于查询已发布的去敏竞价快照。
生产采集、评分和冻结仍在原机器运行；这里不会重新评分或重新判断资格，也不提供交易、
采集控制、回测或模型训练接口。

导入 Vercel、构建完成与网站实际可用是不同的状态。还需要配置私有对象存储、身份认证、
Redis，并先发布快照，才能完成线上查询验证。本机测试不能证明云端服务已上线。

## 一、从 GitHub 导入 Vercel

1. 在 Vercel 新建项目，选择私有仓库 `Linertia/auction-web`。
2. 项目名使用 `auction-web`；Root Directory 选择仓库根目录 `.`，不要选择 `src`。
3. Framework Preset 选择 FastAPI。Build Command、Install Command 和 Output Directory
   保持框架默认值，不填写本机目录或自定义静态输出目录。
4. 配置下表中的服务端环境变量，再执行部署。预览环境需要单独确认变量和允许访问的域名。
5. 绑定稳定的 HTTPS 域名，并让 `AUCTION_PUBLIC_URL` 与实际访问域名一致。

`index.py` 导出 FastAPI ASGI 实例 `app`；`pyproject.toml` 声明 Python 3.12、依赖和
`index:app` 入口，`requirements.txt` 也列出相同依赖。函数最长运行时间为 30 秒，
查询内部超时为 8 秒。

网页通过服务端返回 HTML，不将 Python 源码、公式、策略阈值或环境变量作为浏览器资源发布。

## 二、服务端环境变量

变量只在 Vercel 的环境变量设置中配置。不得加入 `NEXT_PUBLIC_`、`VITE_` 等客户端前缀，
不得把真实值写进本仓库、README、聊天、日志或命令行参数。

| 变量名 | 配置要求 |
| --- | --- |
| `AUCTION_PUBLIC_URL` | 实际固定 HTTPS 网站地址，不含末尾斜杠或 `/mcp` |
| `AUCTION_SNAPSHOT_BACKEND` | `supabase` |
| `AUCTION_SERVER_IDS` | 授权服务器 ID 的 JSON 列表，例如 `["local"]` |
| `SUPABASE_URL` | 托管项目 HTTPS 地址，格式 https://项目引用.supabase.co |
| `SUPABASE_SECRET_KEY` | 当前推荐的服务端 sb_secret_ 密钥，禁止发给浏览器 |
| `SUPABASE_STORAGE_BUCKET` | `auction-snapshots`，必须私有 |
| `SUPABASE_STORAGE_PREFIX` | `auction` |
| `AUCTION_AUTH_USERS_JSON` | 私人模式的用户凭证哈希及服务器授权白名单 |
| `UPSTASH_REDIS_REST_URL` | Redis REST 服务的 HTTPS 地址 |
| `UPSTASH_REDIS_REST_TOKEN` | 限流、审计所用的 Redis 控制凭证 |

Supabase 项目级 secret 会绕过 RLS，不是原 S3 的 GetObject-only IAM。
Vercel 与本机发布器建议使用分别命名、可独立撤销的 secret，但不能声称严格读写隔离。
网站应用只提供读取；密钥泄露仍可能写入。严格权限隔离需要另行配置 Auth/JWT+Storage RLS，
本次未实施。S3 后端仍兼容，可显式设置 s3 并使用原 AWS 环境变量。
Redis 控制身份需要限流用的 EVAL（其中调用 INCR/EXPIRE）及审计用的 XADD 权限。

### 私人访问认证

`AUCTION_AUTH_USERS_JSON` 是 JSON 对象：每个用户 ID 对应 `sha256`（64 位十六进制哈希）、
`servers`（授权服务器 ID 列表），以及可选的 `expires_at`（Unix 秒级时间戳）。
每个用户应有独立的强随机 Bearer 凭证，服务器仅配置其 SHA-256，不保存明文。

凭证不能放入 URL、提交记录或浏览器持久存储。网页提交查询时通过 Authorization 请求头
使用凭证，随后清空输入；网站不读取个人持仓，也不连接券商。

### 可选外部 OAuth

需要外部 OAuth 时，配置 `AUCTION_OAUTH_ISSUER`、`AUCTION_OAUTH_JWKS_URL` 和
`AUCTION_OAUTH_GRANTS_JSON`，启用后替代私人 Bearer 白名单验证。
grants 将身份提供方的 subject ID 映射到授权服务器 ID 列表。

外部身份提供方需签发 RS256 JWT，scope 包含 `auction:read`，audience 为
`<AUCTION_PUBLIC_URL>/mcp`。本仓库只是资源服务器，不负责登录、发放 token、PKCE 或客户端注册。
ChatGPT/Claude 的 OAuth 登录流程仍需配置身份提供方并实际联调，不能只填写地址就视为已接通。

## 三、快照发布与存储

Vercel 无法读取原机器的磁盘。它只读取私有桶中已发布的去敏快照：

```text
auction/objects/<SHA256>.json
auction/<server_id>/<trade_date>/<stage>/latest.json
```

`latest.json` 只包含 `{"sha256":"<内容哈希>"}`；不可变对象包含已有的去敏 metadata/CSV。
上传器必须先上传并校验内容对象，再更新指针。`final` 和 `corrected` 分开存放，
更正不能覆盖原始冻结。生产采集和独立发布工具不属于本仓库。

在 Supabase Storage 新建私有 `auction-snapshots` bucket，Public关闭，
允许 MIME 为 application/json，建议单对象上限16 MiB；不要授予浏览器或 anon 开放访问。
查询先验证桶私有，再读指针和对象，SHA-256 不一致立即拒绝，禁止用最新行情补算。
官方 SDK 固定 supabase==2.32.0，依赖闭包见 requirements-supabase.lock.txt。
只支持托管项目和新式 sb_secret_ 服务端 key，不支持旧 service_role JWT或自托管地址。
本机发布器只接受 date/stage/server-id/dry-run，复用已有去敏导出，不重新计算策略。
上传内容（不覆盖）→回读校验长度/哈希→更新阶段指针→回读确认；内容失败不改指针。
指针失联可能已被接受，需重试确认；同日期同stage发布需串行，非跨对象事务。

免费额度和暂停政策以[官方定价](https://supabase.com/pricing)为准；本次查阅为1 GB Storage、
5 GB egress与5 GB cached egress、50 MB平台文件上限、闲置一周可暂停。
SDK 会完整缓冲下载后再检查大小，请勿向此桶写入非发布工具的大文件。
本次仅完成代码与离线测试，未完成真实 Supabase/Vercel 联调。
密钥权限说明见[官方 API key 文档](https://supabase.com/docs/guides/api/api-keys)。

没有快照时查询应返回明确错误，不得用盘中或收盘累计行情补算 09:25 数据。
快照中的历史误判不会被访问层自动修正。本网站当前查询阶段快照，不是完整实时采集监视器。

## 四、访问地址与参数

| 接口 | 地址 |
| --- | --- |
| 网页 | `https://<域名>/` |
| REST 查询 | `POST https://<域名>/api/auction/rank` |
| Streamable HTTP MCP | `https://<域名>/mcp` |
| OAuth 资源元数据 | `https://<域名>/.well-known/oauth-protected-resource/mcp` |

REST 与 MCP 参数相同，MCP 只注册 `rank_auction_candidates`：

| 参数 | 类型及默认值 |
| --- | --- |
| `trade_date` | 必填字符串，`YYYYMMDD`，不能是未来日期 |
| `server_id` | 字符串，默认 `local`，必须在授权白名单 |
| `stage` | `final`（默认）、`corrected`、`0915`、`0920` 或 `0924` |
| `limit` | 整数，默认 30，范围 1–100 |
| `offset` | 整数，默认 0，范围 0–10000 |
| `codes` | 可选的六位股票代码列表，默认空值，最多 100 项 |
| `qualified_only` | 布尔值，默认 `false` |

不接受任意路径、URL、SQL、shell 命令或凭证参数。返回保存的公开数值、原资格和排名，
不代表新的买入建议。

## 五、首次部署验收

1. 查看构建及运行状态。Vercel 显示 READY 仍不能代替真实查询验收；配置缺失应拒绝启动，
   不能自动开放匿名查询或退回本机数据源。
2. 用另一台电脑打开网页，检查查询表单正常，页面和网络响应没有凭证、源码或策略公式。
3. 不带认证请求 REST，预期 401；带独立授权的 Bearer 请求头，查询已发布日期。
   例如请求体可用 `{"trade_date":"20260930","server_id":"local","stage":"final","limit":3}`。
   只有该日期快照已上传并且访问控制正常时，才能预期成功。
4. 将股票代码、排名、分数及资格与本机原查询逐项比较。
5. MCP 客户端依次执行 initialize、tools/list、tools/call。工具列表只有
   `rank_auction_candidates`，返回数据与 REST 一致；每次的 request_id 可以不同。
6. 检查错误日期、代码、越权服务器和限流是否被拒绝；确认 Redis 有去敏审计记录。
   审计或控制服务故障应拒绝返回数据。

预览域名可能需要独立的 `AUCTION_PUBLIC_URL`。Vercel 平台的部署保护与应用认证是两层控制，
不能为了绕过平台登录而关闭应用认证。排错时不要打印环境变量或完整请求头。

## 六、代码与安全边界

REST 和 MCP 都调用 `AuctionRankingReader.rank_auction_candidates()`，不重新实现业务流程。
部署副本的 `auction_monitor.py` 只保留原 `AuctionFeature` 数据结构和上海时区；
`auction_monitor_v2.py` 只保留原排序函数，两处定义按原文保留。
采集器、数据提供方客户端、评分及资格计算模块已移除；原生产仓库与本机 stdio MCP 未修改。

请求体上限 32 KiB，接收最多 5 秒；每用户每分钟最多 60 次请求，REST 和 MCP 共用额度。
查询缓存最多 1 秒，每个实例最多两个查询工作线程。审计不保存明文凭证或原始结果；
Redis 审计保留是有界的，长期归档需另行配置，不能视为永久防篡改日志。

`.gitignore` 默认拒绝未列入白名单的文件；数据库、原始快照、日志、环境文件、凭证、
虚拟环境及测试生成物都不应提交。新增发布文件前应复查整个待提交 diff。

参考：[Vercel FastAPI 官方说明](https://vercel.com/docs/frameworks/backend/fastapi)。
