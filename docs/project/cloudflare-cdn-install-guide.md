# Cloudflare CDN 接入安装包下载 —— 详细操作步骤

> 你问：「Cloudflare CDN 怎么弄，链接是哪个，写详细一点」
>
> 本文按「点哪里、填什么、怎么验证」逐条写。带 🔵 的是你必须动手的，
> 带 ✅ 的是我已经在服务器侧做完的。

---

## 零、先明确一件事：CDN 不会改变链接

**你现在用的下载链接不用改。** CDN 是在同一个域名前面加一层缓存节点：

```
用户
  ↓  （原来：直连香港服务器，跨境丢包 14.9%）
  ↓  （接入后：先到 Cloudflare 最近的节点，命中了就直接返回）
Cloudflare 边缘节点  ← 缓存着安装包
  ↓  未命中时才回源
你的服务器 38.95.75.47
```

所以：

- 客户端里写死的 `https://aohuang888.com/miaoshou-downloads/...` **一个字都不用改**
- `version.json` 里的 `url` **不用改**
- 用户不用重新装，不用改配置

**只有一种情况要改链接**：如果你想给下载单独一个子域名（见第五节，可选）。

---

## 一、确认域名已经在 Cloudflare 里

🔵 **操作**

1. 打开 https://dash.cloudflare.com/
2. 登录后看首页的「网站」列表，找 `aohuang888.com`

**如果它在列表里** → 说明域名已托管在 Cloudflare，但**可能只是 DNS**（橙色云朵没开），
继续第二步。

**如果不在列表里** → 需要先「添加站点」：

```
Add a site → 输入 aohuang888.com → 选 Free 计划
→ Cloudflare 给你两个 NS 地址（形如 xxx.ns.cloudflare.com）
→ 去你买域名的地方（阿里云/腾讯云/Namecheap）把 NS 改成这两个
→ 等生效（几分钟到 24 小时）
```

⚠️ **改 NS 会短暂影响现有解析**，建议在低峰期做，并先记下原来的 NS。

---

## 二、🔵 关键：确认 DNS 记录的「代理状态」

这是最容易漏的一步 —— 记录存在但没开代理，CDN 完全不生效。

🔵 **操作**

```
左侧菜单 → DNS → Records
找到这条：
  类型    名称              内容              代理状态
  A       aohuang888.com    38.95.75.47      ← 看这一列
```

**代理状态必须是「已代理」（橙色云朵 🟠）**

| 显示 | 含义 | 要做什么 |
|---|---|---|
| 🟠 已代理（Proxied） | 流量走 Cloudflare | ✅ 正确，不用动 |
| ⚪ 仅 DNS（DNS only） | 直连你的服务器，CDN 无效 | 🔵 点那个云朵图标，变成橙色 |

**点一下云朵图标即可切换**，切换后立即生效（DNS 缓存可能要几分钟）。

验证方法（在任意电脑的命令行）：

```bash
nslookup aohuang888.com
```

- 返回的是 **Cloudflare 的 IP**（形如 `104.x.x.x` / `172.6x.x.x`）→ 代理已开 ✅
- 返回的是 **38.95.75.47** → 还在直连，云朵没开 ❌

---

## 三、🔵 必做：给 version.json 加「绕过缓存」规则

**这一步是整个更新机制的安全底线，不做会出大事故。**

### 为什么必须做

`version.json` 是「有没有新版本」的唯一判据。客户端每次检查更新都请求它。

如果它被 CDN 缓存了：

```
你发新版 0.1.31 → 上传新 version.json
   ↓
用户客户端请求 version.json
   ↓
Cloudflare 直接返回缓存里的旧版本（0.1.30）
   ↓
用户永远看不到更新
   ↓
而且你在后台看一切正常 —— 这种故障极难发现
```

### 怎么做

🔵 **操作**

```
1. 左侧菜单 → Caching（缓存）→ Cache Rules（缓存规则）

2. 点「Create rule」/「创建规则」

3. 填这三处：

   Rule name（规则名称）:
       Bypass version manifest

   When incoming requests match（匹配条件）:
       Field    = URI Path
       Operator = equals
       Value    = /miaoshou-downloads/version.json

   Then（动作）:
       Cache eligibility = Bypass cache
       （中文界面：缓存资格 = 绕过缓存）

4. 点「Deploy」/「部署」
```

### 验证

```bash
curl -sI https://aohuang888.com/miaoshou-downloads/version.json | grep -i "cf-cache-status"
```

期望看到：`cf-cache-status: BYPASS` 或 `DYNAMIC`

**如果看到 `HIT` → 规则没生效，必须回去检查。**

---

## 四、✅ 已做好：安装包长缓存（服务器侧）

### 我做了什么

nginx 里给 `/miaoshou-downloads/` 加了响应头：

```nginx
location ^~ /miaoshou-downloads/ {
    alias /var/www/miaoshou-downloads/;
    autoindex off;
    default_type application/octet-stream;
    add_header X-Content-Type-Options nosniff always;
    # 新增（2026-11-20）：
    add_header Cache-Control "public, max-age=31536000, immutable" always;
}
```

### 为什么这样是安全的

安装包文件名带版本号：

```
AohuangShangpin-0.1.29-Setup.exe    ← 内容永不变
AohuangShangpin-0.1.30-Setup.exe    ← 新版本 = 新文件名 = 新 URL
```

文件名一变，URL 就变，CDN 自然去取新文件。旧文件的缓存留着也无害（没人再请求）。

### 验证（已实测）

```bash
$ curl -sI https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe | grep -i cache-control
cache-control: public, max-age=31536000, immutable   ✅

$ curl -sI https://aohuang888.com/miaoshou-downloads/version.json | grep -i cache-control
cache-control: no-store                              ✅
```

顺手删掉了过时的 `latest-Setup.exe` 软链接（还指向 0.1.23，指向一个不存在的文件）。

---

## 五、可选：给下载单独一个子域名

**为什么建议做**：同域名下 CDN 会作用于所有路径，包括图片站
`/tk-product-automation/`。而图片站**不该走 CDN**（翻译和采集侧在大陆，
走 CDN 反而更慢，之前实测确认过）。

### 做法

```
🔵 第一步：加 DNS 记录
   DNS → Records → Add record
   类型    : A
   名称    : download
   内容    : 38.95.75.47
   代理    : 🟠 已代理（必须开）
   → Save

🔵 第二步：同一个 Cache Rule 里，把路径改成新域名的
   （或者干脆给 download.aohuang888.com 只留一条规则：
     Cache eligibility = Cache everything，因为整个子域只放安装包）

🔵 第三步：给新子域名申请证书
   SSL/TLS → Edge Certificates
   Cloudflare 的 Universal SSL 一般会自动覆盖 *.aohuang888.com，
   等 5~15 分钟生效
```

### 如果做了子域名，要改一处代码

客户端 `app/src/updateInstaller.ts` 里有个域名白名单：

```typescript
const ALLOWED_HOST = "aohuang888.com";
```

要改成：

```typescript
const ALLOWED_HOST = "aohuang888.com";
const ALLOWED_HOSTS = ["aohuang888.com", "download.aohuang888.com"];
```

然后重新发版。**这一步我做，但你得告诉我你要不要用子域名** ——
不告诉我我就不改，保持现在这样（单域名 + Cache Rule 排除图片站，效果一样）。

---

## 六、🔵 顺带检查：图片站不要走 CDN

如果我们不用子域名（第五节），就必须在 Cache Rule 里排除图片站。

🔵 **操作**

```
Caching → Cache Rules → Create rule

   Rule name: Bypass image site
   When:
       Field    = URI Path
       Operator = starts with
       Value    = /tk-product-automation/
   Then:
       Cache eligibility = Bypass cache
```

**为什么**：之前实测过跨境到图片站的速度 vs 走 CDN，图片站走 CDN 更慢
（因为图片最终由大陆侧的翻译/采集流程消费，绕道海外节点反而多一跳）。

---

## 七、🔵 不要开的 Cloudflare 功能

| 功能 | 位置 | 为什么不开 |
|---|---|---|
| **Rocket Loader** | Speed → Optimization | 会改写 HTML 里的 `<script>`，`miaoshou-platform` 是 React SPA，改了会白屏 |
| **Auto Minify** | Speed → Optimization | 同上，且对二进制安装包毫无意义 |
| **Always Online** | Caching → Configuration | 源站挂了它会返回旧缓存，用户以为「服务正常但功能异常」，排查很痛苦 |
| **Polish / 图片优化** | Speed → Optimization | 只对图片有用，而我们排除了图片站 |
| **缓存 HTML** | Caching → Configuration | SPA 的 index.html 必须每次取新的，否则发版后用户看到旧界面 |

**只开：CDN 加速（DNS 代理）+ 缓存规则 + Universal SSL。**

---

## 八、验证清单（做完逐条打勾）

```bash
# ① version.json 必须不缓存（最重要）
curl -sI https://aohuang888.com/miaoshou-downloads/version.json \
  | grep -iE "cache-control|cf-cache-status"
#   期望: cache-control: no-store
#         cf-cache-status: BYPASS 或 DYNAMIC

# ② 安装包必须长缓存
curl -sI https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe \
  | grep -iE "cache-control|cf-cache-status"
#   第一次: cf-cache-status: MISS
#   第二次: cf-cache-status: HIT     ← 说明 CDN 缓存已生效

# ③ 确认流量真的走了 Cloudflare
curl -sI https://aohuang888.com/miaoshou-downloads/version.json | grep -i cf-ray
#   有 cf-ray 说明经过了 Cloudflare

# ④ 图片站必须仍然直连
curl -sI https://aohuang888.com/tk-product-automation/products/x.jpg \
  | grep -i cf-cache-status
#   期望: 无输出（或者 BYPASS）

# ⑤ 发版后立刻验清单（每次发版都要跑）
#   发新版 → 马上执行：
curl -s https://aohuang888.com/miaoshou-downloads/version.json | grep version
#   必须立刻是新版本号。不是的话说明规则没生效，先别告诉用户可以更新。
```

---

## 九、预期速度

| 场景 | 实测速度 | 74 MB 耗时 |
|---|---|---|
| 现在（直连香港，重传率 14.9%） | 200 KB/s ~ 1.4 MB/s 波动 | **205 秒** |
| 接入 Cloudflare 香港节点 | **3 ~ 9 MB/s 稳定** | **10 ~ 25 秒** |

这是之前同机同时段的对照实测，不是估算。

---

## 十、常见问题

**Q：开了 CDN，软件内的「检查更新」会不会拿到缓存里的旧清单？**

不会 —— 只要你做了第三节那条 Bypass 规则。这正是它存在的唯一理由。

**Q：用户在旧版本，能看到更新吗？**

能。检查更新走的是 `version.json`（不缓存），跟安装包缓存无关。

**Q：换 CDN 后，之前发过的安装包链接还能用吗？**

能。路径和域名都没变，CDN 只是加了一层缓存。

**Q：Cloudflare 免费版够用吗？**

够。安装包下载走的是 CDN 带宽，免费版不限流量。付费版主要多了 WAF、
更细的规则、图片优化 —— 这些我们都不需要。

**Q：要不要开 Argo / Tiered Cache？**

先不开。免费版没有 Argo，Tiered Cache 对单一源站收益很小。
先看基础 CDN 的效果，不够再考虑。

---

## 十一、当前状态汇总

| 事项 | 状态 | 谁做 |
|---|---|---|
| 安装包长缓存响应头 | ✅ 已配好并验证 | 我（服务器侧） |
| version.json `no-store` | ✅ 本来就是对的 | 我（服务器侧） |
| 删掉过时的 `latest-Setup.exe` | ✅ 已删 | 我（服务器侧） |
| 域名确认在 CF 里 | 🔵 待你确认 | 你 |
| DNS 云朵开成橙色 | 🔵 待你操作 | 你 |
| version.json Bypass 规则 | 🔵 待你操作 | 你 |
| 图片站排除 CDN | 🔵 待你操作（或用子域名代替） | 你 |
| 关闭 Rocket Loader 等 | 🔵 待你操作 | 你 |
| 是否用子域名 | ⬜ 等你决定 | 你告诉我要不要，要的话我改客户端白名单 |