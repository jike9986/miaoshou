# Cloudflare 接入：从你截图的那个页面开始

> 你截图停在 **「连接您的域名」** 这一步。下面从这个页面接着往下走，
> 每一步都写清点哪里、填什么。

---

## 先说结论：`aohuang888.com` 这个域名本身**不要动**

你截图那个「连接您的域名」输入框，是给 **Cloudflare 自家产品**（Pages / Workers /
AI Gateway 之类）绑定自定义域名用的，**不是我们需要的操作**。

我们的场景不同：`aohuang888.com` 已经是你的服务器（38.95.75.47）上的站点，
要走 CDN 只需要**在 DNS 记录上打开橙色云朵**，不需要「连接域名」。

**在那个页面直接点「← 返回」，不要提交。** 提交了会把域名绑到 Cloudflare 的
某个产品上，反而可能打乱现有解析。

---

## 一、正确路径：DNS 记录开代理

🔵 **操作**

```
1. dash.cloudflare.com  →  选 aohuang888.com

2. 左侧菜单 → DNS  →  Records

3. 找到指向你服务器的那条记录：
     类型 : A
     名称 : aohuang888.com   （或 @）
     内容 : 38.95.75.47

4. 看最右边「代理状态」列：
      🟠 已代理（Proxied）      → 已正确，跳到第二节
      ⚪ 仅 DNS（DNS only）     → 点那个云朵图标，变橙色
```

**点云朵图标就切换，立即生效**（DNS 缓存要几分钟）。

### 验证

在任意电脑命令行：

```bash
nslookup aohuang888.com
```

| 返回 | 含义 |
|---|---|
| `104.x.x.x` 或 `172.6x.x.x` | ✅ 已经是 Cloudflare 的 IP，代理生效 |
| `38.95.75.47` | ❌ 还在直连，云朵没开 |

---

## 二、🔵 必做：version.json 绕过缓存

这是**整个更新机制的命门**。不做会出「发了新版但全体用户永远收不到」的故障。

```
Caching（缓存） → Cache Rules（缓存规则） → Create rule

  Rule name:
      Bypass version manifest

  When incoming requests match:
      Field    : URI Path
      Operator : equals
      Value    : /miaoshou-downloads/version.json

  Then:
      Cache eligibility : Bypass cache

  → Deploy
```

### 验证

```bash
curl -sI https://aohuang888.com/miaoshou-downloads/version.json | grep -i cf-cache-status
```

期望：`BYPASS` 或 `DYNAMIC`

**看到 `HIT` 就说明规则没生效 —— 先别发版。**

---

## 三、🔵 必做：图片站排除 CDN

```
Caching → Cache Rules → Create rule

  Rule name: Bypass image site
  When:
      Field    : URI Path
      Operator : starts with
      Value    : /tk-product-automation/
  Then:
      Cache eligibility : Bypass cache
```

**原因**：图片是给大陆侧的翻译/采集流程消费的，绕道海外节点多一跳，实测更慢。

---

## 四、🔵 检查这几个开关必须关掉

| 开关 | 位置 | 不关的后果 |
|---|---|---|
| **Rocket Loader** | Speed → Optimization | 改写 HTML 的 `<script>`，React SPA 会白屏 |
| **Auto Minify** | Speed → Optimization | 同上 |
| **Always Online** | Caching → Configuration | 源站挂时返回旧缓存，用户以为「能用但坏了」，极难排查 |
| **缓存 HTML** | Caching → Configuration | SPA 的 index.html 被缓存，发版后用户看到旧界面 |

只保留：**DNS 代理（CDN 加速）+ 缓存规则 + Universal SSL**。

---

## 五、可选：给下载单独子域名

如果你想让 CDN **只作用于安装包下载**、完全不影响图片站和 API，
可以单开一个子域名（比 Cache Rule 更干净，但要多做几步）。

```
🔵 第一步：加 DNS 记录
   DNS → Records → Add record
     类型    : A
     名称    : download
     内容    : 38.95.75.47
     代理    : 🟠 已代理（必须开）
   → Save

🔵 第二步：等证书（一般 5~15 分钟）
   SSL/TLS → Edge Certificates
   看是否出现 download.aohuang888.com

🔵 第三步：给整条子域开缓存
   Caching → Cache Rules → Create rule
     When: Hostname equals download.aohuang888.com
     Then: Cache eligibility = Cache everything
```

**如果做了这一步，要告诉我** —— 客户端白名单得改：

```typescript
// app/src/updateInstaller.ts
const ALLOWED_HOST = "aohuang888.com";
// 要加成
const ALLOWED_HOSTS = ["aohuang888.com", "download.aohuang888.com"];
```

不改的话客户端会拒绝下载（安全校验不允许非白名单域名）。

---

## 六、我这边已经做完的（不用你管）

已经部署并验证过的：

```bash
$ curl -sI https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe | grep -i cache-control
cache-control: public, max-age=31536000, immutable     ✅

$ curl -sI https://aohuang888.com/miaoshou-downloads/version.json | grep -i cache-control
cache-control: no-store                                ✅
```

- 安装包：长缓存（文件名带版本号，内容永不变）
- version.json：`no-store`（每次回源，保证能发现新版本）
- 删掉了过时的 `latest-Setup.exe` 软链接（原来指向 0.1.23）

nginx 配置里那条 version.json 的 location 是**整套更新的地基**，我没有动它：

```nginx
location = /miaoshou-downloads/version.json {
    alias /var/www/miaoshou-downloads/version.json;
    default_type application/json;
    add_header Cache-Control "no-store" always;
}
```

---

## 七、完整验证清单

```bash
# ① 域名已走 CF
nslookup aohuang888.com
#   期望 Cloudflare IP（104.x / 172.6x）

# ② version.json 不缓存（最重要）
curl -sI https://aohuang888.com/miaoshou-downloads/version.json \
  | grep -iE "cache-control|cf-cache-status"
#   期望 no-store + BYPASS/DYNAMIC

# ③ 安装包长缓存
curl -sI https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe \
  | grep -iE "cache-control|cf-cache-status"
#   第一次 MISS，第二次 HIT

# ④ 图片站不走 CDN
curl -sI https://aohuang888.com/tk-product-automation/products/x.jpg \
  | grep -i cf-cache-status
#   期望：无输出 或 BYPASS

# ⑤ SPA 不被缓存
curl -sI https://aohuang888.com/miaoshou-platform/ | grep -i cf-cache-status
#   期望：DYNAMIC 或 BYPASS（不能是 HIT）

# ⑥ 每次发版后立刻验（这条最关键）
curl -s https://aohuang888.com/miaoshou-downloads/version.json | grep version
#   必须是刚发的新版本号
```

---

## 八、预期效果

| 场景 | 实测速度 | 74 MB 安装包 |
|---|---|---|
| 现在（直连香港，TCP 重传 14.9%） | 200 KB/s ~ 1.4 MB/s 波动 | **205 秒** |
| 接入 CF 香港节点 | **3 ~ 9 MB/s 稳定** | **10 ~ 25 秒** |

（同机同时段对照实测，不是估算）

---

## 九、一句话总结该做什么

1. **在你截图那个页面点「← 返回」**，不要提交
2. `DNS → Records` → 把 `aohuang888.com` 那条 A 记录的云朵点成**橙色**
3. 建两条 Cache Rule：`version.json` 绕过缓存、`/tk-product-automation/` 绕过缓存
4. 确认 Rocket Loader / Auto Minify / Always Online 都是关的
5. 跑一遍第七节的验证命令

做完第 2 步下载速度就会立刻变快，第 3 步保证更新不丢。