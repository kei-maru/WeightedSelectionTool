# Google form抽選ツール部署文档

本文档介绍如何将本项目部署到 Oracle Cloud Infrastructure（OCI）的 Ubuntu Compute 实例，并使用 Nginx、HTTPS 和 X OAuth 登录。

## 1. 部署架构

```text
Internet
   |
   |  HTTPS :443
   v
Oracle Cloud Security List / NSG
   |
   v
Nginx :80 / :443
   |
   |  http://127.0.0.1:8765
   v
Docker Compose + FastAPI
   |
   v
./data/vrc_raffle.db
```

建议配置：

- Oracle Cloud Compute：Ubuntu 22.04 或 24.04
- 域名：例如 `raffle.example.com`
- Nginx：安装在宿主机
- FastAPI：运行在 Docker 容器
- SQLite 数据：保存在宿主机 `./data`
- HTTPS：Let's Encrypt + Certbot

本文中的 `raffle.example.com`、Git 仓库地址和用户名都需要替换为你的实际值。

## 2. Oracle Cloud 网络配置

实例需要具有公网 IP，并位于可以访问 Internet Gateway 的公共子网中。建议为实例绑定保留公网 IP，避免实例重建后 DNS 失效。

在实例使用的 Network Security Group 或子网 Security List 中添加有状态 Ingress 规则：

| 来源 | 协议 | 目标端口 | 用途 |
| --- | --- | --- | --- |
| 你的固定 IP `/32` | TCP | 22 | SSH |
| `0.0.0.0/0` | TCP | 80 | HTTP / 证书签发 |
| `0.0.0.0/0` | TCP | 443 | HTTPS |

如果使用 IPv6，再为 80 和 443 添加 `::/0`。

不要向公网开放 `8765`。项目的 Compose 配置已经将它限制为：

```yaml
127.0.0.1:8765:8765
```

OCI 的 Security List/NSG 和实例操作系统防火墙都必须允许请求通过。Oracle 官方说明这两层规则都会影响实例网络访问：[OCI Security Rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm)、[Ways to Secure a Network](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/waystosecure.htm)。

## 3. 配置域名

在 DNS 服务商添加 A 记录：

```text
raffle.example.com -> Oracle Cloud 实例公网 IPv4
```

确认解析：

```bash
dig +short raffle.example.com
```

返回值应当是 Oracle Cloud 实例的公网 IP。

## 4. 初始化 Ubuntu

登录服务器：

```bash
ssh ubuntu@SERVER_IP
```

更新系统并安装基础软件：

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl git nginx snapd
sudo systemctl enable --now nginx
```

如果使用 UFW：

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

SSH 端口最好只允许你的固定 IP，不要长期向 `0.0.0.0/0` 开放。

## 5. 安装 Docker

使用 Docker 官方 Apt 仓库，不建议在正式服务器使用便捷安装脚本。

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

```bash
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

退出 SSH 并重新登录，让 Docker 用户组生效，然后检查：

```bash
docker version
docker compose version
```

安装方式参考 [Docker Engine for Ubuntu](https://docs.docker.com/engine/install/ubuntu/)。

## 6. 上传项目

推荐目录：

```bash
sudo mkdir -p /opt/google-form-raffle
sudo chown "$USER":"$USER" /opt/google-form-raffle
```

使用 Git：

```bash
git clone YOUR_REPOSITORY_URL /opt/google-form-raffle
cd /opt/google-form-raffle
```

也可以在本地通过 `rsync` 上传：

```bash
rsync -av --exclude '.git' --exclude '.venv' --exclude '.env' \
  ./ ubuntu@SERVER_IP:/opt/google-form-raffle/
```

创建数据目录：

```bash
cd /opt/google-form-raffle
mkdir -p data backups
```

如果需要迁移本地历史，将本机的 `data/vrc_raffle.db` 上传到服务器的同一路径。

## 7. 配置生产环境变量

复制配置文件：

```bash
cp .env.example .env
chmod 600 .env
```

生成 Session 密钥：

```bash
openssl rand -hex 32
```

编辑 `.env`：

```dotenv
AUTH_REQUIRED=1
X_CLIENT_ID=YOUR_X_CLIENT_ID
X_CLIENT_SECRET=YOUR_X_CLIENT_SECRET
X_REDIRECT_URI=https://raffle.example.com/auth/callback
SESSION_SECRET=PASTE_THE_RANDOM_VALUE_HERE

COOKIE_SECURE=1

# 可选：只允许指定 X 用户。公开服务可以留空。
ALLOWED_X_USER_IDS=
ALLOWED_X_USERNAMES=
```

注意：

- 正式域名使用 HTTPS 时，`COOKIE_SECURE` 必须是 `1`。
- `X_REDIRECT_URI` 必须是完整的 HTTPS 地址。
- `.env` 已加入 `.gitignore`，不要提交密钥。
- 更换 `SESSION_SECRET` 会让现有登录 Cookie 全部失效。
- 如果 X 应用是 confidential client，则填写 `X_CLIENT_SECRET`；否则按 X 应用配置留空。

## 8. 配置 X Developer Console

在 X Developer Console 的应用设置中启用 OAuth 2.0，并设置：

```text
Callback URI / Redirect URL:
https://raffle.example.com/auth/callback
```

需要的 scope：

```text
tweet.read users.read
```

Callback URI 必须与 `.env` 中的 `X_REDIRECT_URI` 完全一致，包括：

- `https://`
- 域名
- 路径 `/auth/callback`
- 是否带 `www`
- 末尾是否有 `/`

本项目使用 X OAuth 2.0 Authorization Code Flow + PKCE，参考 [X OAuth 2.0 官方文档](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code)。

## 9. 启动应用容器

```bash
cd /opt/google-form-raffle
docker compose up -d --build
```

检查：

```bash
docker compose ps
docker compose logs --tail=100 raffle
curl -I http://127.0.0.1:8765/
```

容器配置了 `restart: unless-stopped`，服务器或 Docker 重启后会自动恢复。

此时应用只监听服务器本机的 `127.0.0.1:8765`，公网还不能直接访问。

## 10. 配置 Nginx

创建 `/etc/nginx/sites-available/google-form-raffle`：

```nginx
server {
    listen 80;
    listen [::]:80;

    server_name raffle.example.com;

    client_max_body_size 30m;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;

        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }
}
```

启用站点：

```bash
sudo ln -s /etc/nginx/sites-available/google-form-raffle \
  /etc/nginx/sites-enabled/google-form-raffle
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

检查 HTTP：

```bash
curl -I http://raffle.example.com/
```

Nginx 默认不会原样传递原始 Host，因此这里显式设置 `Host` 和转发请求信息。相关指令参考 [Nginx ngx_http_proxy_module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)。

## 11. 配置 HTTPS

Certbot 官方推荐使用 Snap：

```bash
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/local/bin/certbot
```

申请证书并让 Certbot 修改 Nginx：

```bash
sudo certbot --nginx -d raffle.example.com
```

测试自动续期：

```bash
sudo certbot renew --dry-run
```

参考 [Certbot 官方 Nginx 指南](https://certbot.eff.org/instructions?ws=nginx&os=snap)。

最终检查：

```bash
curl -I https://raffle.example.com/
```

## 12. 上线验收

依次检查：

1. 访问 `https://raffle.example.com/`，直接进入主界面。
2. 未登录时显示 `単発抽選モード（未登録）`。
3. 未登录抽选固定为均等概率，可以设置特别条件。
4. 未登录抽选不出现在历史记录和用户一览中。
5. 点击 `Xでログイン`，可以进入 X 授权页面。
6. X 登录后右上角显示头像和名字。
7. 登录后可以使用 Event、抽选结果和用户一览。
8. 不同 X 账号看不到彼此的数据。
9. Session 编号在每个 X 账号内从 `#1` 开始。
10. 上传 CSV、抽选、删除 Session、历史同步、Excel 导出均正常。

## 13. 日常更新

更新前先备份数据库：

```bash
cd /opt/google-form-raffle
mkdir -p data/backups
docker compose exec -T raffle python -c "import datetime, sqlite3; src=sqlite3.connect('/app/data/vrc_raffle.db'); name='/app/data/backups/backup_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'.db'; dst=sqlite3.connect(name); src.backup(dst); dst.close(); src.close(); print(name)"
```

拉取并重建：

```bash
git pull --ff-only
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 raffle
```

数据库迁移会在应用启动时自动执行。

## 14. 数据备份与恢复

需要备份：

```text
/opt/google-form-raffle/data/vrc_raffle.db
/opt/google-form-raffle/.env
```

`.env` 含有密钥，备份时必须加密并限制访问权限。

恢复数据库：

```bash
cd /opt/google-form-raffle
docker compose stop raffle
cp data/backups/YOUR_BACKUP.db data/vrc_raffle.db
docker compose start raffle
docker compose logs --tail=100 raffle
```

恢复前建议先保留当前数据库副本。

## 15. 日志与维护

应用日志：

```bash
docker compose logs -f raffle
```

Nginx 日志：

```bash
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

检查服务：

```bash
systemctl status docker
systemctl status nginx
docker compose ps
```

检查证书计时器：

```bash
systemctl list-timers | grep certbot
```

## 16. 常见问题

### 域名无法访问

依次检查：

1. DNS A 记录是否指向实例公网 IP。
2. OCI Security List/NSG 是否开放 80 和 443。
3. UFW 或其他系统防火墙是否允许 Nginx。
4. Nginx 是否正在运行。

### `502 Bad Gateway`

```bash
docker compose ps
docker compose logs --tail=100 raffle
curl -I http://127.0.0.1:8765/
sudo nginx -t
```

### 上传时出现 `413 Request Entity Too Large`

确认 Nginx 配置包含：

```nginx
client_max_body_size 30m;
```

修改后执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

### X 登录后回到登录前状态或提示 state 错误

检查：

- 网站是否通过 HTTPS 访问。
- `COOKIE_SECURE=1` 是否已经传入容器。
- `X_REDIRECT_URI` 是否与 X Developer Console 完全一致。
- Nginx 是否传递 `X-Forwarded-Proto $scheme`。
- 浏览器是否禁止 Cookie。
- `SESSION_SECRET` 是否在容器重启时被修改。

查看容器环境时，不要输出密钥本身：

```bash
docker compose exec -T raffle python -c "import os; print('AUTH_REQUIRED', os.getenv('AUTH_REQUIRED')); print('COOKIE_SECURE', os.getenv('COOKIE_SECURE')); print('CLIENT_ID_SET', bool(os.getenv('X_CLIENT_ID'))); print('SESSION_SECRET_SET', bool(os.getenv('SESSION_SECRET'))); print('REDIRECT', os.getenv('X_REDIRECT_URI'))"
```

### 登录后没有旧数据

这是账号隔离的正常行为。历史记录按 X 用户 ID 保存；本地 `local` 数据或其他 X 账号的数据不会显示。

### 修改 `.env` 后没有生效

重新创建容器：

```bash
docker compose up -d --force-recreate
```

然后检查实际环境变量状态。

## 17. 安全检查清单

- [ ] OCI 只向公网开放 80 和 443。
- [ ] SSH 22 只允许管理员固定 IP。
- [ ] 8765 只绑定 `127.0.0.1`。
- [ ] 全站使用 HTTPS。
- [ ] `COOKIE_SECURE=1`。
- [ ] `.env` 权限为 `600`，且没有提交到 Git。
- [ ] `SESSION_SECRET` 足够随机。
- [ ] 私有服务配置 X 用户白名单。
- [ ] 定期更新 Ubuntu、Docker 和 Nginx。
- [ ] 定期备份 SQLite 数据库并测试恢复。
- [ ] 监控 Nginx 和容器日志。

如果服务允许任何人进行未登录抽选，应额外考虑 Nginx 限流、上传大小限制和日志监控，防止公开接口被滥用。
