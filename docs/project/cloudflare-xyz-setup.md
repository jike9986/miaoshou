# aohuang888.xyz 接入 Cloudflare —— 下载专用域名

> 负责人 2026-11-20 买了 `aohuang888.xyz`，专门给安装包下载走 CDN。
>
> **为什么不用主域**：`aohuang888.com` 同一台 nginx 上跑着 newapi（:3000）、
> aohuang-ai（:3106）、我们自己的服务（:8790/:8812/:8813）、`aohuang888.cn`、
> `paytest.aohuang888.com` —— 全是负责人在做别的东西。改主域 NS 会同时
> 影响这 5 个业务，任何一个记录漏了都会断服务。`.xyz` 只做下载，挂了也不影响线上。

---

## 零、先说：客户端已经改好了

我这边已经完成，不用等你配完再改代码：

| 改动 | 位置 | 说明 |
|---|---|---|
| 客户端白名单加 `.xyz` | `app/src/updateDownload.ts` | 精确匹配，`evilaohuang888.xyz` 这类会被拒 |
| 服务端白名单加 `.xyz` | `server/core/constants.py` | `UPDATE_HOST_WHITELIST` |
| 分段并发下载 | `app/src/updateInstaller.ts` | 6 路并发，实测 3.6x |
| 16 条单元测试 | `app/src/updateDownload.test.mjs` | 已接入 `pnpm check` |

**注意**：这些改动要发下一个版本才会到用户机器上。当前 0.1.30 的客户端
还不认识 `.xyz`，所以**现在 `version.json` 里的 url 必须继续用主域**，
等新版本发出去、用户更新过之后，才能切到 `.xyz`。

---

## 一、在 Cloudflare 添加域名

🔵 **操作**

```
1. dash.cloudflare.com  →  右上「添加域名」/「Add a site」

2. 输入：aohuang888.xyz  →  继续

3. 选计划：Free（免费版够用，CDN 不限流量）

4. Cloudflare 扫描现有 DNS 记录
   → 因为新域名还没配记录，列表可能是空的
   → 手动加一条：

       类型   : A
       名称   : @            （就是 aohuang888.xyz 本身）
       内容   : 38.95.75.47  （你的服务器）
       代理   : 🟠 已代理     ← 必须橙色，这是 CDN 开关

5. Cloudflare 给你两个 NS 地址，形如：
       xxxx.ns.cloudflare.com
       yyyy.ns.cloudflare.com
   → 记下来
```

---

## 二、去购买处改 NS

🔵 **操作**

你的 `.xyz` 当前 NS 是 `ns1.dyna-ns.net` / `ns2.dyna-ns.net`，
说明是在某个 Cloudflare 经销商（可能是 Hostinger / Namecheap 之类）买的。

**去那个注册商后台**：

```
找到 aohuang888.xyz → DNS 设置 / Nameservers
把原来的 ns1.dyna-ns.net / ns2.dyna-ns.net
改成 Cloudflare 给的那两个
```

**生效时间**：几分钟到 24 小时，通常 10~30 分钟。

### 验证

```bash
nslookup -type=NS aohuang888.xyz 8.8.8.8
# 看到 *.ns.cloudflare.com → 生效了

nslookup aohuang888.xyz 8.8.8.8
# 看到 104.x.x.x 或 172.6x.x.x → 代理也生效了
```

---

## 三、🔵 必做：version.json 绕过缓存

如果 `.xyz` 也提供 version.json（建议提供，作为主域的备用），这条必须配：

```
Caching → Cache Rules → Create rule

  Rule name: Bypass version manifest
  When:
      Field    : URI Path
      Operator : equals
      Value    : /miaoshou-downloads/version.json
  Then:
      Cache eligibility : Bypass cache
```

**原因**：`version.json` 是「有没有新版本」的唯一判据。被缓存 = 发新版用户
永远收不到，而且后台看着一切正常，极难发现。

---

## 四、🔵 给安装包开长缓存

```
Caching → Cache Rules → Create rule

  Rule name: Cache installers
  When:
      Field    : URI Path
      Operator : starts with
      Value    : /miaoshou-downloads/
  Then:
      Cache eligibility : Cache everything
      Edge TTL          : 1 year
```

**为什么安全**：安装包文件名带版本号（`AohuangShangpin-0.1.30-Setup.exe`），
内容永不变。新版是新文件名 → 新 URL → CDN 自然去取新文件。

⚠️ 这条规则要**排在 version.json 那条后面**（Cloudflare 按顺序匹配，
先命中先返回）。或者干脆给 version.json 那条设更高的优先级。

---

## 五、🔵 源站要接受这个域名的请求

现在 nginx 的 `server_name` 里没有 `aohuang888.xyz`，所以 Cloudflare
回源时带 `Host: aohuang888.xyz`，nginx 会落到默认 server 或报错。

**这一步我来做**，但需要你确认后我才改。要加的是：

```nginx
# /etc/nginx/sites-enabled/aohuang-ai.conf
server {
    listen 443 ssl http2;
    server_name aohuang888.com www.aohuang888.com
                paytest.aohuang888.com
                aohuang888.cn www.aohuang888.cn
                aohuang888.xyz www.aohuang888.xyz;   # ← 新增
    ...
}
```

同时证书也要覆盖 `.xyz`（否则 HTTPS 握手失败 → Cloudflare 报 525）。

两个选择：

| 方案 | 做法 | 优劣 |
|---|---|---|
| **A. 扩展现有证书** | `certbot --expand -d aohuang888.com,... -d aohuang888.xyz` | 一张证书管全部，简单。但 Let's Encrypt 有 100 域名上限（够用） |
| **B. Cloudflare 全代理 + 源站自签** | 源站用 15 年自签证书，CF 设 Full (strict) | 以后不用管续期。但 CF 要到 Origin Server 里上传那张自签证书 |

**我建议 A**：改动最小，且现有 certbot 自动续期已经在跑（`certbot.timer`），
加个域名不影响续期。

---

## 六、验证清单

```bash
# ① NS 已切到 Cloudflare
nslookup -type=NS aohuang888.xyz 8.8.8.8 | grep cloudflare

# ② A 记录解析到 CF IP
nslookup aohuang888.xyz 8.8.8.8
#   期望 104.x / 172.6x，不是 38.95.75.47

# ③ 下载能通（配好 nginx + 证书之后）
curl -sI https://aohuang888.xyz/miaoshou-downloads/version.json
#   期望 HTTP 200

# ④ version.json 不缓存
curl -sI https://aohuang888.xyz/miaoshou-downloads/version.json | grep -i cf-cache-status
#   期望 BYPASS 或 DYNAMIC

# ⑤ 安装包被缓存（第二次应该是 HIT）
curl -sI https://aohuang888.xyz/miaoshou-downloads/AohuangShangpin-0.1.30-Setup.exe | grep -i cf-cache-status
#   第一次 MISS，第二次 HIT

# ⑥ 主域完全不受影响
curl -sI https://aohuang888.com/miaoshou-downloads/version.json | head -3
#   期望 HTTP 200，行为跟以前一样
```

---

## 七、切换到新域名的时机（重要）

**不能现在就切。** 顺序必须是：

```
1. 你配好 Cloudflare + 我配好 nginx 证书  →  https://aohuang888.xyz 能下载
2. 我发 0.1.31（客户端认识 .xyz）
3. 等用户都更新到 0.1.31（或至少大部分）
4. 这时才可以把 version.json 的 url 改成 .xyz
```

**为什么**：0.1.30 及更早的客户端只认 `aohuang888.com`。如果现在就把
`version.json` 的 url 改成 `.xyz`，那些客户端会报「更新地址不合法」，
等于**所有人都收不到更新**。

更稳的做法：`version.json` 里继续用主域，**主域 nginx 反过来代理到 CDN**：

```nginx
# 主域的下载路径直接 302 到 CDN 域名
location ^~ /miaoshou-downloads/ {
    return 302 https://aohuang888.xyz$request_uri;
}
```

这样老客户端请求主域 → 被 302 到 `.xyz` → 由 CDN 提供，**不需要改清单、
不需要等用户升级**。这是最平滑的接法。

---

## 八、CDN 到位后的预期

| 场景 | 实测 |
|---|---|
| 现在单连接直连 | 188 KB/s，74MB 要 **205 秒** |
| 现在 6 并发（已实现） | 685 KB/s，约 **57 秒** |
| CDN + 6 并发 | 预计 **10~20 秒**（CF 香港实测 3~9 MB/s） |

分段下载是**在 CDN 之前就能拿到的收益**，CDN 到位后再叠加。

---

## 九、我需要你确认的三件事

1. **`.xyz` 在哪个注册商买的？** 改 NS 要去那个后台。
2. **要不要我配 nginx + 扩证书？** 要的话我现在就做（约 5 分钟，服务不中断）。
3. **主域用 302 转发到 CDN，还是等新版本直接切？** 我建议 302（老客户端也能受益）。