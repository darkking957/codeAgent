# 部署（生产 / 信任好友）

「拉下来就能部署」：机密不入库，每台机器只需填一次 `.env`。

## 一次性准备（每台新服务器）

```bash
git clone <repo-url> && cd codeAgent

# 1. 机密（唯一需手填的文件）
cp .env.example .env
#   编辑 .env：填 ANTHROPIC_API_KEY、POSTGRES_PASSWORD
#   生成强 DB 口令：python3 -c "import secrets; print(secrets.token_hex(24))"

# 2. 运行配置（无机密，默认即可用；换模型/端点在此改）
cp config.yaml.example config.yaml
```

3. **域名解析**：把 `Caddyfile` 第一行的域名（默认 `codelabrory.com`）的 A 记录指向本机公网 IP。换域名就改 `Caddyfile` 第一行。
4. **防火墙**：`sudo ufw allow 80/tcp && sudo ufw allow 443/tcp`（app 不直接对外，入口只有 caddy 的 80/443）。

## 起栈

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

访问 `https://<域名>` → 输入**门口令**（basic_auth 前门）→ 好友自助注册（验证码自动填）。

## 常用运维

```bash
# 看日志（确认证书签发 / 排障）
docker compose -f docker-compose.prod.yml logs -f caddy
docker compose -f docker-compose.prod.yml logs -f app

# 改门口令：重算 bcrypt 哈希贴到 Caddyfile 的 `friend ` 后面，再重启 caddy
docker run --rm caddy:2 caddy hash-password --plaintext '新门口令'
docker compose -f docker-compose.prod.yml up -d caddy

# 停 / 清理（-v 连数据卷一起删，慎用）
docker compose -f docker-compose.prod.yml down
```

## 数据与迁移

持久化在命名卷：`pgdata`（用户/会话/用量）、`wsdata`（每用户 workspace）、`caddy_data`（证书）。
迁移到新机：备份这些卷（或 `pg_dump`）后在新机恢复；其余照「一次性准备」走一遍即可。

## 安全前提（务必确认）

- **门口令是访问边界**：`COREAGENT_WEB_DEV=1`（验证码自助回带）只因被 caddy basic_auth 挡在门后才安全。别去掉前门。
- **沙箱**：code 模式跑命令靠容器内 bubblewrap；宿主不支持无特权 user namespace 时子进程工具 fail-closed（拒绝执行）。需真能跑命令则给 app 服务加 `privileged: true`。
- **Caddyfile** 含门口令的 bcrypt 哈希；仓库若**公开**，建议把哈希外置或用私有仓库。
