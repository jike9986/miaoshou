# 部署手册

## 生产服务器

| 项目 | 值 |
|------|---|
| 服务器 IP | 38.95.75.47 |
| 域名 | aohuang888.com |
| 服务名 | aohuang-listing-v2 |
| 后端端口 | 8813（仅绑 127.0.0.1） |
| 后端目录 | /opt/miaoshou-listing-v2/server |
| 虚拟环境 | /opt/miaoshou-listing-v2/venv |
| 环境变量 | /etc/aohuang-listing-v2.env |
| 数据目录 | /var/lib/miaoshou-listing-v2 |
| 客户端安装包 | /var/www/miaoshou-downloads/ |
| 更新清单 | /var/www/miaoshou-downloads/version.json |

## 架构

```
用户 Electron 客户端
    │  HTTPS (443)
    ▼
nginx (aohuang888.com)
    │  反向代理到 127.0.0.1:8813
    ▼
FastAPI (aohuang-listing-v2)
    │
    ├── PostgreSQL 14  127.0.0.1:5432/miaoshou_listing_v2
    └── 妙手 ERP API  https://openapi-erp.91miaoshou.com
```

客户端不直接连数据库，所有操作走 HTTPS API。

## 常用运维命令

```bash
# 重启服务
systemctl restart aohuang-listing-v2

# 查看服务状态
systemctl status aohuang-listing-v2

# 实时查看日志
journalctl -u aohuang-listing-v2 -f

# 查看应用日志文件
tail -f /var/lib/miaoshou-listing-v2/logs/server.log
```

## 更新后端代码

```bash
# 本地改完后，上传指定文件（以 collect_box_service.py 为例）
scp server/services/collect_box_service.py root@38.95.75.47:/opt/miaoshou-listing-v2/server/services/

# 重启服务生效
ssh root@38.95.75.47 'systemctl restart aohuang-listing-v2'
```

## 更新客户端安装包

1. 本地构建：`npm run build`，再 `npx electron-builder build --win --publish never`
2. 生成文件在 `release/` 目录：`奥皇上品-x.y.z-Setup.exe` + `.blockmap`
3. 计算新安装包的 sha256（用于 version.json）
4. 上传到服务器：
   ```bash
   scp release/奥皇上品-x.y.z-Setup.exe root@38.95.75.47:/var/www/miaoshou-downloads/AohuangShangpin-x.y.z-Setup.exe
   scp release/奥皇上品-x.y.z-Setup.exe.blockmap root@38.95.75.47:/var/www/miaoshou-downloads/AohuangShangpin-x.y.z-Setup.exe.blockmap
   ```
5. 更新 `/var/www/miaoshou-downloads/version.json`：
   ```json
   {
     "version": "x.y.z",
     "url": "https://aohuang888.com/miaoshou-downloads/AohuangShangpin-x.y.z-Setup.exe",
     "sha256": "<64位十六进制>",
     "sizeBytes": <字节数>,
     "notes": "更新说明",
     "publishedAt": "2026-xx-xxTxx:xx:xxZ",
     "channel": "stable"
   }
   ```
6. `latest.yml` 是客户端下载链接的软链接辅助文件，同步更新
7. 客户端打开后会自动检测到新版本，提示更新

## 本地快速安装（开发机）

桌面快捷方式指向 `D:\软件\奥皇上品\`，不走 NSIS 安装器。

更新方式：
```powershell
# 停止旧版
Stop-Process -Name "AohuangShangpin" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# 同步新 build（win-unpacked 目录）到运行目录
robocopy "release\win-unpacked" "D:\软件\奥皇上品" /E /IS /IT /NFL /NDL /NJH /NJS
```

## 环境变量说明

关键变量（完整列表见 `/etc/aohuang-listing-v2.env`）：

| 变量 | 说明 |
|------|------|
| AOHUANG_DATABASE_URL | PostgreSQL 连接串 |
| AOHUANG_CREDENTIAL_KEY | 妙手 AppSecret 加密密钥（Fernet） |
| AOHUANG_DATA_DIR | 数据目录（secret.key、日志、更新包等） |
| AOHUANG_SHARED_MODE | 1=共享部署（多用户），0=单机 |
| AOHUANG_BIND_HOST | 监听地址，生产填 127.0.0.1 |
| AOHUANG_API_PORT | 监听端口，生产填 8813 |
| AOHUANG_CLIENT_TOKEN | 客户端内置令牌（所有 Electron 客户端共享） |
| AOHUANG_DEEPSEEK_API_KEY | DeepSeek 颜色翻译密钥 |
| AOHUANG_IMAGE_LOCAL_DIR | 图片本地存储目录 |
| AOHUANG_IMAGE_LOCAL_BASE | 图片公网访问基地址 |

**不要把这些变量写进代码或提交到 Git。**

## nginx 配置

配置文件：`/etc/nginx/sites-enabled/aohuang-ai.conf`

关键规则：
- HTTP 自动 301 跳转 HTTPS
- TLS 证书：Let's Encrypt（`/etc/letsencrypt/live/aohuang888.com/`）
- 静态文件：`/var/www/miaoshou-downloads/`（安装包下载）
- 图片目录：`/var/www/tk-product-automation/`

## 数据库

```bash
# 连接数据库
psql postgresql://miaoshou_listing_v2:<密码>@127.0.0.1:5432/miaoshou_listing_v2

# 常用查询
-- 查看账号
SELECT id, name, app_id_hint FROM accounts;

-- 查看操作员
SELECT id, name, role, enabled FROM operators;

-- 查看定时任务
SELECT id, name, enabled, frequency, last_run_date FROM schedules;
```

## TLS 证书续期

证书由 Let's Encrypt 自动续期（certbot）。如果续期失败：
```bash
certbot renew --nginx
systemctl reload nginx
```
