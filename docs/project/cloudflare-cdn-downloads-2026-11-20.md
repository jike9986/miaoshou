# Cloudflare CDN 处理安装包（2026-11-20）

> 负责人两问：
> 1. 「Cloudflare CDN 处理安装包后续更新也会同步更新吗」
> 2. 「用户能收到这个更新吗」
>
> **答案：会同步，用户能收到 —— 前提是 `version.json` 必须保持不缓存。**
> 这一条现在已经是对的（`cache-control: no-store`），下面解释为什么不能动它。

---

## 一、先看更新链路长什么样

```
客户端启动 / 点「检查更新」
   ↓ GET https://aohuang888.com/miaoshou-downloads/version.json
服务端返回清单 { version, url, sha256, sizeBytes, notes }
   ↓ 客户端比较 version > 本地 APP_VERSION
下载 url 指向的 exe
   ↓ 边下边算 sha256
校验通过 → 存 pending.json → 静默安装 → 重启
```

**清单（version.json）决定「有没有新版本」，exe 只是内容。**
所以两者的缓存策略必须相反：

| 文件 | 缓存策略 | 理由 |
|---|---|---|
| `version.json` | **`no-store`（绝不缓存）** | 一缓存就等于「新版本发布了但没人知道」，必须每次回源 |
| `*.exe` | **长缓存 `immutable`** | 文件名带版本号（`-0.1.29-`），内容永不变，缓存越久越省流量 |

这条区分是关键：**CDN 缓存 exe 不会影响更新发现，因为发现走的是不缓存的那条。**

---

## 二、当前已经正确的部分

实测线上响应头：

```
=== version.json ===
cache-control: no-store          ← 已经对了
etag: "6affba1c-207"

=== AohuangShangpin-0.1.29-Setup.exe ===
content-length: 74819729
last-modified: Fri, 20 Nov 2026 03:10:17 GMT
etag: "6affba19-475a891"
（没有 cache-control —— 靠浏览器/CF 默认行为，不够明确）
```

nginx 里已经有一条专门为 version.json 写的 location（`/etc/nginx/sites-enabled/aohuang-ai.conf:95`）：

```nginx
location = /miaoshou-downloads/version.json {
    alias /var/www/miaoshou-downloads/version.json;
    default_type application/json;
    add_header Cache-Control "no-store" always;    # ← 这一句是整个更新机制的地基
}
```

**不要动这个 location。** 换 CDN 时它依然是回源路径。

---

## 三、要补的那一步：给 exe 加长缓存头

现在 exe 没有显式 `Cache-Control`，CDN 会按默认 TTL 缓存（可能很短），
每次用户下载都可能回源 —— 那就白挂 CDN 了。

```nginx
location ^~ /miaoshou-downloads/ {
    alias /var/www/miaoshou-downloads/;
    autoindex off;
    default_type application/octet-stream;
    add_header X-Content-Type-Options nosniff always;
    # ↓ 新增：安装包文件名带版本号，内容永不变，可以放心长缓存
    add_header Cache-Control "public, max-age=31536000, immutable" always;
}
```

**为什么安全**：升级时新版本是**新文件名**（`AohuangShangpin-0.1.30-Setup.exe`），
URL 变了，CDN 自然去取新文件。旧文件仍被缓存也无害（没人再请求它）。

`latest-Setup.exe` 那个软链接如果还在用，它**不能**长缓存（内容会变）——
所以要么删掉它，要么单独给它 `no-cache`。实测现在指向 0.1.23，已经过时，
建议直接删。

---

## 四、Cloudflare 侧的配置

### 4.1 必须做：给 version.json 加 Cache Rule 绕过

即使源站给了 `no-store`，为保险起见在 CF 再加一条规则：

```
Dashboard → Caching → Cache Rules → Create rule
  规则名: Bypass version manifest
  When incoming requests match:
      URI Path equals "/miaoshou-downloads/version.json"
  Then:
      Cache eligibility → Bypass cache
```

**这一步是整个方案的安全底线。** 万一 CF 因为某些默认行为把 JSON 缓存了，
用户就会「永远检查不到新版本」—— 而这种故障极难被发现（后台看着一切正常）。

### 4.2 必须做：只给下载目录开 CDN，别把图片站也带进去

**同域不同路径，要分开处理**：

| 路径 | 是否走 CDN | 理由 |
|---|---|---|
| `/miaoshou-downloads/` | ✅ 走 | 纯静态安装包，跨境慢，CDN 收益最大 |
| `/tk-product-automation/` | ❌ **不走** | 翻译和采集侧在大陆，走 CDN 反而更慢（已实测确认） |
| `/miaoshou-platform/` | ❌ 不走 | API 要走源站，缓存了会出错 |

做法：Cloudflare 的 Cache Rule 里对 `tk-product-automation` 设 Bypass，
或者更简单的办法 —— **给下载单独一个子域名**：

```
download.aohuang888.com  →  CNAME 到 aohuang888.com，只服务 /miaoshou-downloads/
```

这样图片站完全不受影响，也不会有人误配。

### 4.3 别开的东西

| 功能 | 为什么不开 |
|---|---|
| Rocket Loader | 会改 HTML 里的 script，`miaoshou-platform` 是 SPA，容易坏 |
| Auto Minify | 同上，且对安装包没意义 |
| Polish / 图片优化 | 只对图片站有用，而图片站不走 CDN |
| Always Online | 源站挂了它会返回旧缓存，用户会以为「服务正常但功能异常」 |

只开 **CDN 加速 + 缓存规则**，其余保持关闭。

---

## 五、预期的速度改善（基于实测）

同机同时段对照（已在 `docs/project/rules.md` 记录）：

| 目标 | 实测速度 |
|---|---|
| 自家服务器（跨境直连） | 200 KB/s ~ 1.4 MB/s（波动剧烈） |
| Cloudflare 香港节点 | **3 ~ 9 MB/s（稳定）** |

74 MB 安装包：
- 现在：**205 秒**（363 KB/s）
- CDN 后：**约 10 ~ 25 秒**

---

## 六、验证清单（做完逐条打勾）

```bash
# 1. version.json 必须 no-store（不变）
curl -sI https://aohuang888.com/miaoshou-downloads/version.json | grep -i cache-control
#   期望: cache-control: no-store

# 2. exe 必须有长缓存
curl -sI https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe | grep -i cache-control
#   期望: cache-control: public, max-age=31536000, immutable

# 3. 走 CDN 的响应必须带 cf-cache-status
curl -sI https://download.aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe \
  | grep -iE 'cf-cache-status|cf-ray'
#   第一次: cf-cache-status: MISS
#   第二次: cf-cache-status: HIT     ← 说明缓存生效

# 4. 图片站必须仍然直连（不能有 cf-cache-status）
curl -sI https://aohuang888.com/tk-product-automation/products/thumbs/xxx.jpg | grep -i cf-cache-status
#   期望: 无输出

# 5. 发版后清单必须立刻变（最重要的一条）
#   发新版 → 立刻 curl version.json，必须是新版本号
curl -s https://aohuang888.com/miaoshou-downloads/version.json | grep version
```

第 5 条每次发版都要验 —— 它验证的是「用户能不能收到更新」这件事本身。

---

## 七、和「换服务器」的关系

这个 CDN 步骤**不需要换服务器**，是纯增量改善。

但如果以后要换，判断依据是 `docs/project/rules.md` 里记的那三条：
回程线路（CN2 GIA）、重传率（< 3%）、带宽是否独享。
**不要为了「更快」去加 CPU 和内存** —— 实测 3 并发下载时 CPU 98% 空闲，
瓶颈全在线路重传率 14.9%。

---

## 八、当前状态

- ✅ `version.json` 的 `no-store` 已在 nginx 里配好（不用改）
- ⬜ exe 的 `Cache-Control` 还没加（本文第三节，需要改 nginx）
- ⬜ Cloudflare 侧的 Cache Rule 还没建（本文第四节，需要你在 Dashboard 操作）
- ⬜ 子域名 `download.aohuang888.com` 还没建（可选，但推荐）

**改 nginx 是服务器侧、我能做；建 Cache Rule 和子域名在 Cloudflare Dashboard，
需要你操作（我没有你的 CF 账号权限）。**