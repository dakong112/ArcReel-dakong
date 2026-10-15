# 按客户部署 ArcReel：运维手册

ArcReel 是单租户应用：一个实例里只有一个用户空间，所有项目、供应商配置、Agent 凭据共用。因此给每个客户（个人或团队）单独部署一个实例，用 new-api 统一账号登录，模型调用计入客户自己的 new-api 钱包。

```text
浏览器 ── alice.arcreel.example.com ──► 反向代理 ──► 127.0.0.1:12401 ──► arcreel-alice 容器 ──► ./projects（数据卷）
   │                                                                          │
   └──── 登录跳转 ────► new-api /sso/authorize          服务端兑换登录码 ◄────┘
                         （SSO 客户端 arcreel-alice，allowed_user_ids 控制谁能进）
```

每个客户对应：

| 资源 | 示例 | 说明 |
|---|---|---|
| 目录 | `/srv/arcreel/alice/` | 复制本目录得到，内含 `docker-compose.yml`、`.env` 与数据 |
| 容器 | `arcreel-alice` | Compose 项目名取 `CUSTOMER_SLUG` |
| 数据卷 | `/srv/arcreel/alice/projects/` | 项目、生成资产、SQLite 数据库、日志 |
| 子域名 | `alice.arcreel.example.com` | 反向代理按域名转发到该客户的回环端口 |
| SSO 客户端 | `arcreel-alice` | new-api「SSOClients」里的一条，`allowed_user_ids` 列出能登录的 new-api 用户 |

## 1. 开通一个客户

以客户短名 `alice`、端口 `12401` 为例。

### 1.1 生成 SSO 客户端密钥

```bash
SECRET=$(openssl rand -hex 32)
echo "client_secret: $SECRET"                      # 写进该客户的 .env
printf %s "$SECRET" | sha256sum | cut -d' ' -f1    # secret_hash，写进 new-api
```

### 1.2 在 new-api 登记 SSO 客户端

用 root 账号打开 new-api 后台的系统设置，在 `SSOClients`（JSON 数组）里追加一条：

```json
{
  "client_id": "arcreel-alice",
  "name": "ArcReel 短剧（alice）",
  "secret_hash": "<上一步的 sha256>",
  "redirect_uris": ["https://alice.arcreel.example.com/sso/callback"],
  "launch_url": "https://alice.arcreel.example.com/login?sso=1",
  "allowed_user_ids": [42]
}
```

- `client_id` 只能用小写字母、数字、`_`、`-`，建议统一为 `arcreel-<客户短名>`。
- `allowed_user_ids` 填客户在 new-api 的用户 id；团队客户填每个成员的 id。**不填等于所有 new-api 用户都能进这个实例**，按客户部署时必须填写。
- `launch_url` 带 `?sso=1`：用户从 new-api 的应用列表点进来时直接走统一登录，不停在登录页。
- 名单外的用户在 new-api 授权页就被拒绝，应用列表里也看不到这个实例。

### 1.3 启动实例

```bash
mkdir -p /srv/arcreel && cp -r deploy/customer /srv/arcreel/alice
cd /srv/arcreel/alice
cp .env.example .env
# 编辑 .env：CUSTOMER_SLUG、ARCREEL_PORT、AUTH_PASSWORD、AUTH_TOKEN_SECRET、NEWAPI_*
docker compose up -d
docker compose ps        # 等到 healthy
```

### 1.4 反向代理与证书

Nginx 示例（每个客户一个 `server` 块，证书可用泛域名证书）：

```nginx
server {
    listen 443 ssl;
    server_name alice.arcreel.example.com;
    ssl_certificate     /etc/ssl/arcreel/fullchain.pem;
    ssl_certificate_key /etc/ssl/arcreel/privkey.pem;
    client_max_body_size 512m;

    location / {
        proxy_pass http://127.0.0.1:12401;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Agent 对话与项目事件走 SSE 长连接
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

### 1.5 验收

1. 打开 `https://alice.arcreel.example.com/`，登录页出现「用 new-api 登录」按钮。若没有，检查 `.env` 中 `NEWAPI_BASE_URL`、`NEWAPI_CLIENT_ID`、`NEWAPI_CLIENT_SECRET`、`NEWAPI_REDIRECT_URI` 四项是否都已填写，改完执行 `docker compose up -d`。
2. 用客户的 new-api 账号登录，回到 ArcReel 项目列表。
3. 用名单外的 new-api 账号尝试，应在 new-api 授权页被拒绝。
4. 打开设置页「供应商」，开启了 `NEWAPI_AUTO_PROVIDER` 时应出现自定义供应商「new-api」（见第 3 节）。

## 2. 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `CUSTOMER_SLUG` | 是 | Compose 项目名后缀，区分同一宿主机上的各实例 |
| `ARCREEL_PORT` | 是 | 宿主机回环端口，每个客户不同 |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | 是 | 本地账号，只给运维兜底用，不交给客户 |
| `AUTH_TOKEN_SECRET` | 是 | 登录令牌签名密钥，长期固定；更换后所有已登录会话失效 |
| `NEWAPI_BASE_URL` | 是 | ArcReel 服务端访问 new-api 的地址，可以是内网地址 |
| `NEWAPI_PUBLIC_URL` | 否 | 浏览器访问 new-api 的地址；留空时与 `NEWAPI_BASE_URL` 相同 |
| `NEWAPI_CLIENT_ID` | 是 | SSO 客户端的 `client_id` |
| `NEWAPI_CLIENT_SECRET` | 是 | SSO 客户端密钥明文 |
| `NEWAPI_REDIRECT_URI` | 是 | `https://<子域名>/sso/callback`，必须与 `redirect_uris` 中的一项完全一致 |
| `NEWAPI_ALLOWED_USER_IDS` | 否 | 实例侧第二道名单，逗号分隔；写错（非数字）时 new-api 登录整体不可用 |
| `NEWAPI_AUTO_PROVIDER` | 否 | `true` 时首次统一登录自动配置供应商，见第 3 节 |

`NEWAPI_BASE_URL`、`NEWAPI_CLIENT_ID`、`NEWAPI_CLIENT_SECRET`、`NEWAPI_REDIRECT_URI` 缺任何一项，实例只提供本地密码登录。

## 3. 计费如何流转

1. 用户在 new-api 授权后，ArcReel 服务端用登录码向 new-api 兑换身份。new-api 同时为该用户签发（或复用）一把名为 `sso:arcreel-<客户短名>` 的 API 密钥，这把密钥的用量计入该用户的 new-api 钱包。
2. `NEWAPI_AUTO_PROVIDER=true` 且实例里还没有任何供应商（自定义供应商与预置供应商凭证都没有）时，ArcReel 把这把密钥保存为自定义供应商「new-api」，接口地址 `NEWAPI_BASE_URL/v1`，格式 OpenAI 兼容。之后的登录不再改动供应商。
3. 供应商建好后没有模型：在设置页「供应商 → new-api」点「获取模型列表」，勾选要用的模型并设为默认。此后图片、视频、文本生成都经 new-api 中转并扣该用户的钱包。
4. 创作 Agent 使用 Anthropic 协议，不读自定义供应商，需要手工配置一次：设置页「ArcReel Agent」新增自定义 Agent 供应商，接口地址填 `NEWAPI_BASE_URL`，密钥填同一把 `sso:` 密钥（或客户的团队 Key）。

注意：

- **谁第一个用统一账号登录，供应商就用谁的密钥**。开通后请让实例所有者先登录；团队客户应在 new-api 创建团队 Key，再到「供应商 → new-api」把密钥换成团队 Key，费用才计入团队钱包。
- 团队钱包账户本身不能登录应用，团队成员以本人身份登录。
- 在 new-api 删除或禁用这把 `sso:` 密钥，实例的模型调用随即失败；用户下次统一登录会重新签发密钥，但已保存的供应商不会自动更新，需要在设置页替换。
- ArcReel 部分媒体端点与 new-api 中转格式可能不完全一致，给客户承诺按量计费前，先逐个端到端验证实际要用的模型。

## 4. 访问控制与回收

- 增减成员：修改 new-api 中该 SSO 客户端的 `allowed_user_ids`；如设置了 `NEWAPI_ALLOWED_USER_IDS`，同步修改并 `docker compose up -d`。
- 移出名单只阻止新的登录。已登录的浏览器持有的令牌最长 7 天有效，需要立即踢出时更换 `AUTH_TOKEN_SECRET` 后重启实例（所有人需重新登录）。
- 本地密码登录始终可用，供运维在 new-api 不可用时进入实例。不要把 `AUTH_PASSWORD` 交给客户。
- 停用客户：`docker compose down`，从 new-api 删除 SSO 客户端条目，移除反向代理配置；数据目录按合同保留或归档后删除。

## 5. 日常运维

- 备份：归档整个客户目录（`.env` 与 `projects/`），方式同 `website/docs/ops/deployment.md`「6.1 SQLite 部署备份」。
- 升级：逐个实例 `docker compose pull && docker compose up -d`，先在一个实例上验证。
- 日志：`docker compose logs -f arcreel`；统一登录失败时日志里有 `new-api SSO 交换` 开头的记录，含 new-api 返回的状态码与原因。

## 6. 后续：多租户

当前一客户一实例的代价是每个客户一个容器的内存和磁盘开销，客户多了以后运维成本线性增长。往多租户演进的路径：

1. 用户表与会话：令牌主体已经是 `newapi:<id>`，下一步按它建用户记录，替换目前所有请求都落到同一个默认用户的做法。
2. 数据隔离：项目目录、全局资产库、Agent 记忆按用户分目录；数据库各表按用户 id 过滤（仓储层已预留查询范围钩子）。
3. 供应商按用户：每个用户使用自己登录换得的 `sso:` 密钥调用模型，不再共用实例级供应商。
4. Agent 沙箱、任务队列、并发限额按用户隔离与计量。

在这些完成之前，不要让不同客户共用一个实例。
