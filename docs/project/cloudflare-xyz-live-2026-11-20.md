# aohuang888.xyz CDN 上线记录（2026-11-20）

> 负责人买了 `aohuang888.xyz` 专做安装包下载 CDN。
> **全部配置已完成并实测通过，不需要在 Cloudflare Dashboard 做任何操作。**

---

## 一、最终实测结果

### 下载速度（12 MB 样本，本机到服务器）

| 路径 | 单连接 | 6 并发 |
|---|---|---|
| 主域 `aohuang888.com`（直连） | 1164 KB/s | 1000 KB/s |
| **CDN `aohuang888.xyz`** | **1428 KB/s** | **3208 KB/s** |

**6 并发 + CDN = 3.2 倍。** 按 74 MB 安装包折算：

```
主域直连    约 74 秒
CDN + 6并发 约 24 秒
```

### 缓存行为（**无需建任何 Cache Rule**）

```
安装包       cf-cache-status: HIT        ← 已缓存
version.json cf-cache-status: DYNAMIC    ← 不缓存
             Cache-Control: no-store
```

**为什么不用建规则**：源站 nginx 已经给了正确的响应头，Cloudflare 默认
就遵循源站的 `Cache-Control`。我在文档里写过的那些 Cache Rule 是「保险」，
实测发现不必需。

### 最关键的一条验证：用户能不能收到更新

```
1. 改 /var/www/miaoshou-downloads/version.json 的 notes 字段
2. 等 3 秒
3. 通过 https://aohuang888.xyz/.../version.json 读
4. → 立刻看到新内容  ✅
5. 还原，确认无残留  ✅
```

**这就是「CDN 会不会导致用户收不到更新」这个问题的直接答案：不会。**

---

## 二、踩到并修掉的三个坑

### 坑 1：443 端口没有 acme 验证路径（既有隐患）

**现象**：`https://域名/.well-known/acme-challenge/<file>` 返回 **404**。

**原因**：nginx 的 443 server 块里没有 `location /.well-known/acme-challenge/`，
只有 80 端口有。所以 HTTPS 的验证请求落到 `location /` 反代到后端 → 404。

**为什么以前没暴露**：Let's Encrypt 续期走 HTTP（80 有 location → 正常）。
但切 Cloudflare 后，CF 默认回源到 **443**，验证请求就走 HTTPS 了。

**不修的后果**：60 天后证书续期失败 → HTTPS 打不开 → 客户端全部连不上。

**已修**：

```nginx
location /.well-known/acme-challenge/ {
    root /var/www/letsencrypt;
    default_type text/plain;
    try_files $uri =404;
}
```

验证：`https://aohuang888.com/.well-known/acme-challenge/<file>` → **200**

### 坑 2：Cloudflare 的 Browser Integrity Check 拦脚本 UA

**现象**：用 Python 请求 CDN 得到 **403 error code 1010**。

```
Python-urllib/3.13   → 403  error code: 1010
Electron/31.7.7      → 206  ✅
curl/8.x             → 206  ✅
浏览器 UA             → 206  ✅
空 UA                → 206  ✅
```

**风险**：Electron 的默认 UA 目前能过，但那是**依赖 Cloudflare 的托管规则
恰好没拦**。哪天规则更新把 `Electron/` 加进去，全量用户的自动更新就集体
失效，而症状只是「下载失败」，极难联想到 CDN 规则。

**已修**：客户端显式声明浏览器形态 UA（`updateDownload.ts` 的
`DOWNLOAD_USER_AGENT`），不依赖默认值。

### 坑 3：`www.aohuang888.xyz` 没有 DNS 记录导致扩证书失败

**现象**：

```
Domain: www.aohuang888.xyz
Type:   dns
Detail: NXDOMAIN looking up A for www.aohuang888.xyz
```

**处理**：证书里不要 `www.aohuang888.xyz`。下载域名用不到 www，
少一条 DNS 记录少一个维护点。

---

## 三、最终证书

```
subject = CN=aohuang888.com
覆盖    = aohuang888.com, www.aohuang888.com,
          aohuang888.cn, www.aohuang888.cn,
          paytest.aohuang888.com,
          aohuang888.xyz          ← 新增
有效期  = 2026-11-20 → 2027-02-18
续期    = webroot，certbot.timer 自动跑
```

---

## 四、其他业务完全未受影响（已逐个验证）

| 服务 | 状态 |
|---|---|
| `aohuang888.com/` | 200 |
| `aohuang888.com/miaoshou-downloads/version.json` | 200 |
| `aohuang888.cn/` | 302（原本行为） |
| `paytest.aohuang888.com/` | 200 |
| `newapi.aohuang888.com/` | 200 |

**主域 NS 一个字没动**，所以这些全部照常。

---

## 五、当前版本清单里还是主域地址

```json
{
  "version": "0.1.29",
  "url": "https://aohuang888.com/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe"
}
```

**暂时不要改。** 0.1.30 及更早的客户端白名单里只有 `aohuang888.com`，
改成 `.xyz` 会让那些客户端报「更新地址不合法」，等于所有人都收不到更新。

### 推荐的切换方式：主域 302 到 CDN

```nginx
location ^~ /miaoshou-downloads/ {
    return 302 https://aohuang888.xyz$request_uri;
}
```

这样：
- 老客户端请求主域 → 被 302 到 CDN → **立刻吃到 3.2 倍速度**
- 不用改 `version.json`、不用等任何人升级
- 主域本身的下载也走 CDN，带宽压力转移

**需要我做这个改动吗？** 说一声我就配，2 分钟，服务不中断。

---

## 六、客户端代码已就绪（等发版）

| 改动 | 文件 |
|---|---|
| 白名单含 `.xyz` | `app/src/updateDownload.ts` |
| 显式浏览器 UA | 同上（`DOWNLOAD_USER_AGENT`） |
| 6 路并发分段下载 | `app/src/updateInstaller.ts` |
| 16 条测试 | `app/src/updateDownload.test.mjs`（已接入 `pnpm check`） |
| 服务端白名单含 `.xyz` | `server/core/constants.py` |

**这些要发下一个版本才会到用户机器上。** 在当前版本上，分段下载和 UA
都不会生效 —— 但 CDN 本身已经生效了（通过 302 或者以后改清单地址）。

---

## 七、**不需要**在 Cloudflare 做任何操作（原判断有误，已实测纠正）

我一开始写「必须建一条 acme-challenge Bypass 规则」，**那是错的**。
实测证明不需要。

### 实测证据一：这个路径默认就不缓存

```
通过 CDN 连续请求 3 次 /.well-known/acme-challenge/probe
  第 1 次: cf-cache-status: DYNAMIC
  第 2 次: cf-cache-status: DYNAMIC
  第 3 次: cf-cache-status: DYNAMIC
```

`DYNAMIC` = Cloudflare 没有缓存、每次都回源。
因为 nginx 对这个 location 没设 `Cache-Control`，CF 的默认行为就是不缓存。

### 实测证据二：完整续期演练通过

```
$ certbot renew --cert-name aohuang888.com --dry-run --non-interactive

Simulating renewal of an existing certificate for aohuang888.com and 5 more domains
Congratulations, all simulated renewals succeeded:
  /etc/letsencrypt/live/aohuang888.com/fullchain.pem (success)
EXIT=0
```

**6 个域名全部通过，包括经过 Cloudflare 的 `aohuang888.xyz`。**

`--dry-run` 走的是真实 ACME 流程（真实的 HTTP-01 验证），只是不真正签发。
它通过就说明 60 天后的自动续期会成功。

### 结论

**你在 Cloudflare 什么都不用做。** 所有配置已经完成并验证。

---

## 八、完整验证清单（全部已跑过）

```bash
# ① NS 已切到 Cloudflare
nslookup -type=NS aohuang888.xyz 8.8.8.8
#   → osmar.ns.cloudflare.com / teresa.ns.cloudflare.com  ✅

# ② 解析到 Cloudflare IP
nslookup aohuang888.xyz 1.1.1.1
#   → 172.67.144.56 / 104.21.28.42  ✅

# ③ CDN 生效
curl -sI https://aohuang888.xyz/miaoshou-downloads/version.json | grep -i "server\|cf-ray"
#   → Server: cloudflare, CF-RAY: ...-NRT（东京节点）  ✅

# ④ 安装包被缓存
curl -sI https://aohuang888.xyz/miaoshou-downloads/AohuangShangpin-0.1.29-Setup.exe | grep -i cf-cache-status
#   → HIT  ✅

# ⑤ 清单不缓存
curl -sI https://aohuang888.xyz/miaoshou-downloads/version.json | grep -iE "cf-cache-status|cache-control"
#   → DYNAMIC + no-store  ✅

# ⑥ 更新能立刻传播（最关键）
#   改 version.json → 3 秒后通过 CDN 读 → 看到新内容  ✅

# ⑦ 其他业务未受影响
curl -s -o /dev/null -w "%{http_code}\n" https://aohuang888.cn/
#   → 302（原本行为）  ✅
```