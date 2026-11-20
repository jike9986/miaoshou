# 每件商品独立货源链接（2026-11-20 架构改造）

> 负责人这一轮的两个追问：
> 1. 「就是这个覆盖每个产品还要多 1.5 秒那不是又变久了」—— **1.5 秒这个数是错的**
> 2. 「复制粘贴这种不是很快吗为什么这么慢」—— **问得对，慢的不是复制**
>
> 而且这一轮发现上一版的实现**根本没解决问题**：一批 N 件会共用同一条链接。

---

## 一、先纠正我上一轮说错的数字

| 我说过 | 实测 |
|---|---|
| 每次覆盖约 1.5 秒 | **1.05 秒**（链式 edit 平均） |
| read + edit + verify ≈ 1.5s | read 0.47s；链式 edit 0.88~1.01s |

而且第一次之后**不需要再 read** —— 这是这一轮最重要的发现，见第三节。

---

## 二、「复制粘贴为什么不那么快」——因为慢的不是复制

实测拆解单次改链接的 1.05 秒：

```
单次改链接: 1.05s
  ├─ 0.90s  强制节流等待（Pacer）
  └─ 0.15s  真实上游处理时间
```

**0.90 秒里一点活都没干，是在等。**

等的原因是妙手开放平台的 QPS 限制。`server/services/miaoshou.py` 的 `Pacer`：

```python
class Pacer:
    """同账号节流：保证两次上游请求之间至少间隔 interval 秒。"""
    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.interval:
                time.sleep(self.interval - delta)   # ← 就是这里在等
            self._last = time.monotonic()
```

`UPSTREAM_PACE_SECONDS = 0.90`。

### 为什么不干脆把节流关掉

关掉会撞妙手的 `accountApiQpsRateLimit`。撞了会怎样：

- 当时那个请求直接失败
- `Pacer` 会把间隔往上抬到 1.5s 上限（`penalize()`）
- 连续成功 50 次才降 0.05 秒（`reward()`）

**所以「省 0.9 秒」的代价是「后面所有请求都变慢」，不划算。**

### 「复制粘贴」这个类比哪里不对

你的直觉是对的 —— 在本地复制粘贴确实是纳秒级。但这里每次「改链接」不是
本地操作，是**一次跨境 HTTP 请求到妙手的服务器**：

```
你的服务器 38.95.75.47（香港）
   ↓  ~0.10s  网络往返
妙手 API 服务器（国内）
   ↓  ~0.05s  它内部读写数据库
   ↓  ~0.10s  返回
```

纯网络+处理约 0.15 秒，这部分省不掉（地理距离决定）。
真正能省的是那 0.9 秒节流，而它省不得。

---

## 三、关键发现：链式 edit，不用每次重读

### 上一版的流程（每件 1.5s）

```
读详情（0.47s）→ 改（0.9s）→ 回读校验（0.5s）= 1.87s/件
```

### 现在（每件 1.05s，首次读一次之后）

实测：`edit` 的返回里**每次都带新的 `ossMd5`**，可以直接用于下一次 edit。

```
read 一次（0.47s，整批只一次）
   ↓
edit #1 → 返回 ossMd5_1   （1.01s）
edit #2 用 ossMd5_1 → 返回 ossMd5_2   （0.94s）
edit #3 用 ossMd5_2 → 返回 ossMd5_3   （0.88s）
```

代码里就是 `source_link.override_common_box_source(client, cid, url, cache=cache)`
—— 传同一个 `cache` dict 进来，自动复用 ossMd5 和编辑模板（含已夹住 stock 的 skuMap）。

**一批 50 件的额外耗时：约 53 秒**（50 × 1.05s，含 1 次 read）。

对比：
- 我上一轮说的 75 秒 → 现在实测 53 秒
- 如果每件都重读详情：1 次读 × 50 = 24 秒额外，总共 77 秒

---

## 四、最重要的发现：上一版根本没解决问题

### 问题是什么

`collect_carriers` 的复制循环：

```python
while pending_copies > 0:
    for _ in range(pending_copies):
        client.claim_common_to_platform([(common_id, "tiktok", 1)])
                                   #  ^^^^^^^^^ 一直是同一个载体
```

一批 50 件，**50 份副本全是同一个载体的拷贝**。

而 `claim_common_to_platform` 是「复制当时快照」：

```
复制前把公共箱链接改成 X → 复制 → 副本持有 X
复制后再改公共箱成 Y     → 已复制的副本仍然是 X（不会跟着变）
```

所以我上一版「在复制循环外改一次链接」的结果是：

**这一批 50 件全部指向同一条链接。**

那等于什么都没修 —— 负责人当初的问题就是「一批 50 个不同商品，
采购时分不清哪件对哪条」。

### 现在改成什么样

在复制循环**内部**，每件复制前改一次：

```python
source_cursor = 0
retry_targets: list[str] = []          # 失败待重试的件，记住它自己的链接
while pending_copies > 0:
    for target in round_targets:       # 每件一个目标链接
        if common_id and target:
            source_link.override_common_box_source(
                client, common_id, target, cache=source_cache)
        client.claim_common_to_platform([(common_id, "tiktok", 1)])
```

### 为什么需要 `retry_targets`

重试那一件必须**重新把它自己的链接设回去**。

只靠 `source_cursor` 递增的话，第二轮会拿到 `preferred[-1]`，
于是那件就顶着**别的商品的链接**进店铺 —— 比不改更糟。

这个 bug 是测试抓出来的：

```
FAILED test_retry_round_redoes_link_for_that_item
  assert len(client.edit_calls) >= 4
  assert 3 >= 4     ← 只改了 3 次，说明重试那件没重设链接
```

### `preferred` 为空时一次都不改

存量 178 件商品全都没货源链接，发布时 `preferred` 为空。
这种情况必须**一次 edit 都不发**，不增加任何耗时。

用 `None` 表示「这件不改链接」：

```python
round_targets: list[str | None] = list(retry_targets)
if not preferred:
    round_targets.append(None)
```

这个也踩过一次：最初写成 `if preferred:` 才 append，导致 `preferred` 为空时
`round_targets` 是空列表 → 一件都不复制 → 报错「复制接口未返回新的商品 ID」。
测试抓出来了：

```
FAILED test_no_preferred_leaves_carrier_link_alone
  CarrierError: 复制接口未返回新的商品 ID
```

---

## 五、生产实测

```
preferred(3 条真实链接):
   https://detail.1688.com/offer/717267894593.html
   https://detail.1688.com/offer/1006677085598.html
   https://detail.1688.com/offer/980434711914.html

生成载体: [3548760248, 3548760308, 3548760347]  耗时 9.6s

=== source_override 事件 ===
  第1件 url=.../717267894593.html  changed=False   ← 本来就是这条，跳过
  第2件 url=.../1006677085598.html changed=True
  第3件 url=.../980434711914.html  changed=True

override 次数: 3   （应为 3）✓
每条链接都不同: True  ✓
```

---

## 六、测试

`tests/test_carrier_source_per_item.py` **8 条**：

| 测试 | 钉住什么 |
|---|---|
| `test_every_copy_gets_its_own_link` | 4 件 → 4 份副本，链接与 preferred 一一对应 |
| `test_does_not_reread_detail_for_every_item` | 10 件只读 **1 次**详情（链式复用生效） |
| `test_fewer_links_than_copies_keeps_last` | 链接不够时沿用最后一条，不退化成载体自带那条 |
| `test_no_preferred_leaves_carrier_link_alone` | preferred 为空 → **一次 edit 都不发** |
| `test_stock_is_capped_once_and_reused` | stock 623927 → 99999，且只夹一次 |
| `test_cache_reuses_oss_md5_across_calls` | 第二次用的 ossMd5 是第一次返回的 |
| `test_cache_invalidated_on_edit_failure` | edit 失败 → 丢缓存，下次重读 |
| `test_retry_round_redoes_link_for_that_item` | 重试那件重新设自己的链接 |

其中两条是**先失败、修好代码后才通过**的，说明测试确实在起作用。

`tests/test_source_link.py` 36 条仍全过；相关套件合计 **164 条全过**。

---

## 七、部署时踩的坑：deploy_server.py 没上传 carrier_service

改完代码跑 `python scripts/deploy_server.py` 后，端到端测试只触发了 1 次 override
（应该 3 次），且事件里 `index=None` —— 那是旧代码的字段。

查证：

```
远端 source_override 出现次数: 1     ← 旧代码
carrier sha 一致: True               ← 手动上传后才一致
远端 round_targets 次数: 7           ← 新代码特征
```

**结论：`scripts/deploy_server.py` 的文件清单不包含 `services/carrier_service.py`。**

这条要记进 `rules.md`：**改 `carrier_service.py` 之后必须单独确认它上传成功**
（比对 sha256），不能只看脚本打了 "Done"。

---

## 八、和「为什么价格不对」那个问题一样，这次的教训

上一轮我基于「复制会继承当前链接」的假设就写了代码，没验证「一批多件」这个场景。
如果当时真发版上线，负责人的问题**一点都没解决**，而且更难发现
（因为新入仓商品确实有了链接，只是全批共用一条）。

**教训：涉及「批量 × 逐件」的逻辑，必须用一个能表达「每件不同」的假 client
写测试跑一遍，再上生产。**

---

## 九、还剩什么

| 事项 | 状态 |
|---|---|
| 每件独立链接 | ✅ 架构已改，生产实测通过 |
| 链式复用 ossMd5（省 24 秒/批） | ✅ 已实现并测过 |
| 重试件重新设链接 | ✅ 已修，测试覆盖 |
| preferred 为空零开销 | ✅ 已修，测试覆盖 |
| `deploy_server.py` 漏传 carrier_service | ⬜ 已写入 rules.md，等下次改脚本时一并修 |
| 存量 178 件补录链接 | ⬜ 需要人工在界面补（补一件生效一件） |