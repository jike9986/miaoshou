# 下载速度调查（2026-11-23）

## 别人下载慢，查出两个独立问题

### 问题 1：客户端主动放弃了并发下载（主因，已修）

`app/src/updateInstaller.ts` 里有一段 2026-11-20 留下的判断：

```ts
if (segments.length > 1 && !(await isBehindCdn(safeUrl))) {
  // 分段并发
} else {
  // 单连接
}
```

`isBehindCdn()` 检查响应头有没有 `cf-ray`，只要有就走**单连接**。依据是当时测到的：

```
经 Cloudflare aohuang888.xyz，完整 74MB
  单连接   156s   470 KB/s
  6 并发   995s    73 KB/s   ← 慢 6 倍
```

**这个结论现在不成立了。** 2026-11-23 用生产清单里的真实 URL 重测（`.com` → 302 → `.xyz`，用户实际走的路径）：

```
单连接
  17 MB/s
  16 MB/s

6 并发 Range（同一 URL、同一条 302）
  96 MB/s (704ms)
  27 MB/s (2466ms)
```

并发快 1.6~5.7 倍。Cloudflare 现在不但不压制并发，反而对单连接限得更狠——
单连接稳定在 15~23 MB/s，而 6 条并发各能跑 5~7 MB/s。

**修法**：删掉 `isBehindCdn()` 整个函数，`planSegments()` 无条件优先并发。
`downloadBySegments()` 本来就有完整降级链（任何一片失败 → 清 .part → 回退单连接），
所以并发走不通不会卡住用户。

教训：「在 CDN 后面」和「并发没用」是两件无关的事，之前把它们混为一谈了。

### 问题 2：服务器内核发送缓冲区偏小（未修，影响有限）

```
net.core.wmem_max = 212992      # 208 KB
net.core.rmem_max = 212992      # 208 KB
net.ipv4.tcp_wmem = 4096 16384 4194304
net.ipv4.tcp_rmem = 4096 131072 6291456
```

`net.core.wmem_max` 只有 208 KB，高延迟链路上 BDP（带宽时延积）会限制单连接吞吐：

```
吞吐上限 ≈ wmem_max / RTT
200ms RTT 时 ≈ 208KB / 0.2s ≈ 1 MB/s
```

这解释了为什么跨省/跨境用户单连接只有 1 MB/s 左右。但**并发下载绕过了这个限制**
（每条连接各自有独立的缓冲区），所以修了问题 1 之后影响大幅降低。

真要修的话（可选）：

```bash
cat >> /etc/sysctl.d/99-aohuang-net.conf <<'EOF'
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
EOF
sysctl --system
```

## 排查过程中确认「不是问题」的项

| 怀疑对象 | 实测 | 结论 |
|---|---|---|
| nginx 限速 | `grep limit_rate /etc/nginx/` 无结果 | 没限速 |
| nginx 版本旧 | 1.18.0 | 够用，不是瓶颈 |
| 磁盘 IO | `iostat` 全 0，`sda` 空闲 | 不是瓶颈 |
| 服务器总带宽 | 4 线程并发聚合 20 MB/s | 总带宽充足 |
| 丢包 | ping 0% loss | 无丢包 |
| Cloudflare 缓存未命中 | `cf-cache-status: HIT` | 已命中 |
| 文件本身 | 69 MB 正常大小 | 正常 |

## 验证命令

```bash
# 单连接基线
curl -o /dev/null -sL -w "speed=%{speed_download}B/s\n" \
  "$(curl -s https://aohuang888.com/miaoshou-downloads/version.json | python -X utf8 -c 'import sys,json;print(json.load(sys.stdin)["url"])')"

# 6 并发（模拟客户端改造后的行为）
URL=$(curl -s https://aohuang888.com/miaoshou-downloads/version.json | python -X utf8 -c 'import sys,json;print(json.load(sys.stdin)["url"])')
SIZE=$(curl -s https://aohuang888.com/miaoshou-downloads/version.json | python -X utf8 -c 'import sys,json;print(json.load(sys.stdin)["sizeBytes"])')
SEG=$(( (SIZE + 5) / 6 ))
start=$(date +%s%N)
for i in 0 1 2 3 4 5; do
  s=$(( i*SEG )); e=$(( s+SEG-1 )); [ $e -ge $SIZE ] && e=$((SIZE-1))
  curl -s -o /dev/null -r ${s}-${e} "$URL" &
done
wait
end=$(date +%s%N)
echo "$(( SIZE * 1000 / ((end-start)/1000000) / 1024 / 1024 )) MB/s"
```
