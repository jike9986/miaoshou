# 0.1.31 发布记录 —— CDN 上线 + 分段下载策略修正

> 2026-11-20 发布。负责人要求：安装包下载要快、流量不走服务器、直接弄好 CDN。

---

## 一、最终结果

### 安装包已在 CDN 上，下载流量不再经过源站

```
https://aohuang888.xyz/miaoshou-downloads/AohuangShangpin-0.1.31-e99209b6-Setup.exe
  cf-cache-status: HIT
  Content-Length: 74831449
  实测: 71.4 MB / 52 秒 / 1400 KB/s
```

对比改造前：

```
主域直连源站   71.4 MB / 156 秒 /  470 KB/s
CDN           71.4 MB /  52 秒 / 1400 KB/s   ← 3 倍
```

**流量全部由 Cloudflare 承担，源站只在 CDN 未命中时被请求一次。**

### 主域 302 到 CDN（老客户端也能受益）

```nginx
location ^~ /miaoshou-downloads/ {
    if ($host ~* "^(www\.)?aohuang888\.com$") {
        return 302 https://aohuang888.xyz$request_uri;
    }
    ...
}
```

`if ($host ...)` 不能省：同一个 server 块也服务 `.xyz`，无条件重定向
会让 `.xyz` 跳到自己 → 无限循环。

`version.json` 有独立的精确匹配 location，不受 302 影响 ——
**刻意让「有没有新版本」这个信息不依赖 CDN**。

---

## 二、最重要的一个发现：分段下载在 CDN 上是负优化

我原来做的「6 路并发分段下载」在直连源站时确实有效：

```
直连 aohuang888.com（12MB 样本）
  单连接   65.4s   188 KB/s
  6 并发   17.9s   685 KB/s   ← 3.6x
```

瓶颈是单条 TCP 在 14.9% 重传链路上的拥塞窗口，多开连接有效。

**但走 Cloudflare 之后结论完全反过来了**（同一台机器、同一个文件、完整 74MB）：

```
经 CDN aohuang888.xyz（完整 74MB）
  单连接   156s   470 KB/s
  6 并发   995s    73 KB/s   ← 慢 6 倍
```

CDN 对单 IP 的并发连接会排队/限速，多开只互相拖慢。

**如果没实测就直接发版，所有用户下载会从 156 秒变成 995 秒。**

### 修正方式：按响应头判断，不写死

```typescript
// app/src/updateDownload.ts
export function isCdnHeaders(headers: Record<string, unknown>): boolean {
  // server: cloudflare / cf-ray / cf-cache-status / via: cloudfront / ...
}
```

看到任何 CDN 迹象 → 单连接；否则 → 6 路并发。

### 为什么用响应头而不是测速

第一版写的是「先测一下并发是否更快」。**实测证明这条路走不通** ——
这条跨境链路的抖动比信号本身还大。固定 8 MB 总下载量反复测：

```
1 并发   1513 /  265 KB/s
4 并发    424 /  368 KB/s
6 并发   1808 / 2144 KB/s
8 并发   1882 / 1884 KB/s
```

同一并发数两次测量能差 5 倍。基于计时的判断是在赌运气，
赌错的代价是「所有用户慢 6 倍」。

响应头是确定性信号，与网速无关。

---

## 三、踩到的坑（都是实测才发现的）

### 坑 1：CDN 缓存了同名安装包，导致清单与文件不一致

**这是最危险的一个，会直接让所有用户下载失败。**

改完 `isBehindCdn` 重新打包后，我上传了新的
`AohuangShangpin-0.1.31-Setup.exe`（同名覆盖），然后：

```
清单声明:  size 74831449, sha256 e99209b6...
CDN 返回:  size 74832728, sha256 b505e072...   ← 旧文件！
```

原因：nginx 给安装包设了 `Cache-Control: public, max-age=31536000, immutable`，
Cloudflare 缓存了一年。**同名文件覆盖后 CDN 不知道内容变了。**

**修复**：文件名带内容 hash：

```
AohuangShangpin-0.1.31-e99209b6-Setup.exe
```

新文件名 = 新 URL = CDN 必然重新取。这也是 CDN 的标准做法。

**验证方法（必须做）**：下载 CDN 上的实际字节算 hash，跟清单比。

```python
# 不是看 Content-Length 就完事，要真的算 sha256
with urllib.request.urlopen(url) as r:
    while chunk := r.read(262144):
        h.update(chunk)
assert h.hexdigest() == manifest["sha256"]
```

我这次就是靠这一步发现了不一致。

### 坑 2：Cloudflare 的 Browser Integrity Check 拦脚本 UA

```
Python-urllib/3.13   → 403  error code 1010
Electron/31.7.7      → 206
curl/8.x             → 206
浏览器 UA / 空 UA     → 206
```

Electron 默认 UA 现在能过是运气。已让客户端显式声明浏览器形态 UA
（`DOWNLOAD_USER_AGENT`），不依赖默认值。

### 坑 3：老客户端白名单不含 `.xyz`

已部署的 0.1.30 客户端里：

```
asar 里出现的域名: ['aohuang888.com']
.xyz 是否出现: False
```

`downloadInstaller` 第一件事就是 `validateUpdateUrl`，不在白名单直接
返回「更新地址不合法」。所以**清单必须给主域地址**：

```json
"url": "https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.31-e99209b6-Setup.exe"
```

主域 302 会把请求送到 CDN。这样新老客户端都能下载。

**演进路径**：等绝大多数用户升到 0.1.31（认识 `.xyz`）之后，
再把清单改成 `.xyz` 直连，省掉 302 的那一跳。

### 坑 4：443 没有 acme 验证路径（既有隐患）

只有 80 端口有 `/.well-known/acme-challenge/`，443 没有。
切 Cloudflare 后 CF 默认回源 443，验证请求会落到 `location /` → 404。

已补上并验证 `https://aohuang888.com/.well-known/acme-challenge/<file>` → 200。

---

## 四、发布内容清单

| 项 | 值 |
|---|---|
| 版本 | 0.1.31 |
| 安装包 | `AohuangShangpin-0.1.31-e99209b6-Setup.exe` |
| 大小 | 74,831,449 字节 |
| sha256 | `e99209b6948e12aee679126b0f629a0b16cf4dc26116a79ef24dedfc1201b081` |
| 清单 | `/var/www/miaoshou-downloads/version.json`（no-store） |
| CDN | `aohuang888.xyz`（Cloudflare，free 计划） |

### 客户端改动

- 白名单加 `aohuang888.xyz`（精确匹配，`evilaohuang888.xyz` 被拒）
- 显式浏览器 UA
- 分段下载：直连用 6 路，CDN 用单连接（响应头判断）
- 每片核对字节数（防止 truncate 后留零字节空洞）

### 测试

`app/src/updateDownload.test.mjs` 23 条，已接入 `pnpm check`：

- 白名单：2 个官方域通过、5 个仿冒域拒绝、http/端口/凭据/非exe 拒绝
- 切片：真实 74831449 字节无缝覆盖、不重叠、末段到 total-1、不完并发上限
- **CDN 判定**：7 种迹象识别、大小写不敏感（回归测试钉住那个真实 bug）、
  数组值、null/undefined 不抛错

---

## 五、以后发版的正确流程

```bash
# 1. 五处版本号同步
python -X utf8 scripts/check_version.py

# 2. 构建 + 打包
pnpm check && pnpm build
cd app && pnpm exec electron-builder --win --dir
cd .. && node scripts/verify-desktop-package.mjs
"/c/Program Files (x86)/Inno Setup 6/ISCC.exe" installer/setup.iss

# 3. 算 hash，用带 hash 的文件名上传（避免 CDN 缓存同名旧文件）
H=$(sha256sum "release/奥皇上品-0.1.31-安装程序.exe" | cut -d' ' -f1)
# 上传到 /var/www/miaoshou-downloads/AohuangShangpin-<版本>-${H:0:8}-Setup.exe

# 4. 写清单（url 用主域，兼容老客户端）

# 5. **必须**下载 CDN 上的实际文件验证 sha256 与清单一致

# 6. 部署服务端 + 桌面端
python scripts/deploy_server.py
```