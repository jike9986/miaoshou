# 固定下载链接下发了旧包（2026-11-26）

> 结论：主域固定链接已修复并被校验闸门守住。`.xyz` 上残留一条 Cloudflare 缓存，
> 不影响用户，需要一次 purge 才彻底干净。

## 一、现场

| 入口 | 字节 | sha256 | 判定 |
|---|---|---|---|
| `version.json` 里的带哈希地址（.com / .xyz） | 71459602 | `daa2c7bf…` | ✅ 0.1.54 |
| `aohuang888.com/miaoshou-downloads/latest-Setup.exe` | **71460377** | **`f5295be4…`** | ❌ **0.1.44** |
| `aohuang888.xyz/miaoshou-downloads/latest-Setup.exe` | **71460377** | **`f5295be4…`** | ❌ 同上，`cf-cache-status: HIT` `Age: 235408` |
| 服务器上的软链接 `latest-Setup.exe` | — | 指向 `0.1.54-daa2c7bf` | 源站本来就是对的 |

差 **6 个版本**。用户点「下载安装包」，拿到的是 0.1.44。

注意这里有个反直觉的事实：**软件内自动更新一直是好的**。客户端走的是
`version.json` → 带哈希文件名那条链路，从头到尾没坏。坏的只有「给人贴的那个固定链接」。

## 二、根因

`latest-Setup.exe` 是**内容会变**的软链接，却落在目录级长缓存里：

```nginx
location ^~ /miaoshou-downloads/ {
    add_header Cache-Control "public, max-age=31536000, immutable" always;
}
```

`immutable` 的语义是「一年内别再来问我，内容不会变」。对带哈希的文件名这是正确
配置；对可变的软链接是灾难 —— Cloudflare 忠实地把 0.1.44 缓存了一整年，
源站改对了它也不回源。

`docs/project/cloudflare-cdn-downloads-2026-11-20.md:85` 早就写过这个隐患
（「要么删掉它，要么单独给它 `no-cache`」），一直没有落地。

## 三、为什么六个版本都没发现

`verify_release.py` 只校验 `version.json` 里那个带哈希的地址。那条链路从来没坏，
所以每次发布校验都是满绿。

> **校验没覆盖到的入口，等于没校验。**

固定链接是用户唯一真正被广告出去的入口，却不在校验范围里。

## 四、修复

### 4.1 源站 nginx

`/etc/nginx/sites-enabled/aohuang-ai.conf`：

```nginx
location = /miaoshou-downloads/latest-Setup.exe {
    default_type text/plain;
    add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
    add_header Pragma "no-cache" always;
    include /etc/nginx/snippets/miaoshou-latest-redirect.conf;
}
```

要点：

- **精确匹配 `=`** 优先级高于目录级 `^~`，所以它不再落进长缓存
- `no-store` 让 302 本身也不许缓存
- 跳转目标是**带内容哈希的文件名**，每次发版 URL 都变，缓存天然失效

### 4.2 跳转目标由发版脚本维护

`/etc/nginx/snippets/miaoshou-latest-redirect.conf` 的内容只有一行：

```nginx
return 302 https://aohuang888.xyz/miaoshou-downloads/AohuangShangpin-0.1.54-daa2c7bf-Setup.exe;
```

它由 `scripts/publish_release.py` 每次发版覆写，然后 `nginx -t` + `reload`。
**只上传安装包不算发完**，必须跑完这个脚本，否则固定链接还指着上一个版本。

`snippet` 里 `return 302` 前的那几行注释解释为什么不能指回 `latest-Setup.exe`
本身 —— 那是下一个改这里的人最需要的上下文。

### 4.3 校验闸门

`scripts/verify_release.py` 新增第 4 步，校验**固定链接最终落地的字节**：

```
固定链接(主域)    : OK      ← 阻塞项，主域直连源站，就是被广告出去的链接
固定链接(.xyz CDN): STALE   ← 警告项，缓存清不掉时不阻塞发布
```

主域是硬闸门；`.xyz` 默认只警告（加 `--strict-cdn` 变闸门）。理由：CF 缓存
在本脚本里清不掉，如果一律阻塞，下一次发版会被这条永久堵死。

### 4.4 清缓存工具

`scripts/purge_cdn.py` 调 Cloudflare API 按 URL 清缓存。

token 来源（优先级）：`CF_API_TOKEN` / `CLOUDFLARE_API_TOKEN` 环境变量
→ `~/.dsh/aohuang-cf-token` → 报错并给获取步骤。

```bash
python -X utf8 scripts/purge_cdn.py --dry-run    # 只打印请求
python -X utf8 scripts/purge_cdn.py              # 清固定链接
python -X utf8 scripts/purge_cdn.py --everything # 整 zone 全清（慎用）
```

没有 token 时手动：
Dashboard → Caching → Configuration → Purge Cache → Custom Purge →
`https://aohuang888.xyz/miaoshou-downloads/latest-Setup.exe`

## 五、验证结果

```
=== 结果 ===
  清单一致          : OK
  清单地址合规      : OK
  清单字节一致      : OK
  固定链接(主域)    : OK
  固定链接(.xyz CDN): STALE（见下方处置）
```

主域固定链接完整下载 71459602 bytes / `daa2c7bf…` = 0.1.54。

主机名逐个实测（全部直连源站，302 到带哈希文件）：

```
aohuang888.com      → 302 → AohuangShangpin-0.1.54-daa2c7bf-Setup.exe
www.aohuang888.com  → 302 → 同上
aohuang888.cn       → 302 → 同上
www.aohuang888.cn   → 302 → 同上
aohuang888.xyz      → 200（CF 缓存的旧字节，待 purge）
```

## 六、新增的防回归测试

`server/tests/test_release_fixed_link.py`（17 项全绿）：

- 文件名必须带 sha256 前 8 位
- 跳转目标不能是 `latest-Setup.exe` 自身（自跳转 + 重新落进长缓存）
- 拒绝 `http://`、拒绝不带哈希的手写文件名
- snippet 写出后能被读回（publish 后的 verify 依赖这一点）
- 清单 url 不合规时发布闸门必须拦住
- 事故现场的两个包（0.1.44 / 0.1.54）文件名单调性固化

## 七、一句话记住

> 带哈希的文件名可以 `immutable` 缓存一年；
> 任何**内容会变**的入口（软链接、固定链接、汇总页）必须 `no-store`。

## 八、遗留

`.xyz/latest-Setup.exe` 的 Cloudflare 缓存条目需要一次 purge 才会消失（TTL 一年）。
在那之前：任何走 `.xyz` 直连固定链接的人仍会拿到 0.1.44。
**官方给的下载链接是主域 `.com`，它已经正确。**

要彻底根治，建议在 Cloudflare 上给这条路径加一条 Cache Rule（Bypass cache），
这样即使源站配置被人改错，CDN 也不会再缓存可变入口。