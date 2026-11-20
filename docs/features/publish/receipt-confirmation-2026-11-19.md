# 发布回执确认修复（2026-11-19）

> 现象：发布记录里 `unknown` 状态堆积到 **702 条**，且每天翻倍：
> `11-16: 45 → 11-17: 65 → 11-18: 108 → 11-19: 201`。
>
> 实测查明：**这批记录在妙手平台侧全部是 `success`** —— 商品早就发到店铺了，
> 本地台账却写着「超时未收到平台回执」。用户看到失败会重复发布同一件商品。

---

## 一、三个缺陷

### BUG-P12 翻页深度不足（69 条 / 119 条）

`_refresh_processing` 用 `REFRESH_MAX_PAGES = 5` 页 × 20 行 = **最多看 100 行**。

实测账号 9 的妙手回执总量 **389 行**（20 页）。119 条被标 unknown 的记录里，
**69 条落在第 6~10 页** —— 当前代码根本翻不到，每轮扫描都查不到。

页码分布：
```
page 1: 14   page 2: 20   page 3: 16
page 6: 15   page 7: 20   page 8: 20   page 9: 10   page 10: 4
                ↑ 这 69 条是 5 页上限的盲区
```

**修复**：`REFRESH_MAX_PAGES` 5 → **40**（800 行）。
代价可控 —— 循环里有「待确认明细全都出现过就 break」，正常情况仍只打 1~2 次上游。

### BUG-P13 释放前不回源（50 条 / 119 条）

`release_stale_publish_logs` 旧实现：记录超过 `max_age_seconds=600` 秒，
**直接标 unknown，不再尝试回源确认**。

`run_due` 虽然有「先 refresh 再 release」的顺序，但那一轮 refresh 受 5 页限制
覆盖不到，紧接着 release 就把它判死。

实测时序（log 843~892，批次 `pub_30efa798`）：
```
requested_at = 11-19 16:54:00
marked_unknown = 11-19 17:04:13   ← 612 秒后，刚过 600 阈值
平台真实状态 = success
```

**修复**：释放前对这一批做一次**深度回源**（`_refresh_processing(deep=True)`），
确认不到的才标 unknown。

### BUG-P14 认领记录混进 unknown（422 条）

`_refresh_processing` 的 `pending` 会过滤掉 `_is_claim_only_log` 的行 ——
这是**正确设计**：认领的「成功」标志是商品进了采集箱，不是平台发布结果，
用发布回执去确认它本身就错了。

问题是这些记录最终被 `release_stale_publish_logs` 统统一标成 `unknown`，
看起来像发布失败。

实测验证（log 513，账号 10）：
```
detail.claimOnly = True
平台确实有 success 回执（gmtCreate 2026-11-16 18:02:52）
但 _current_attempt_receipts 认得，_refresh_processing 却因 claim-only 跳过
```

**修复**：释放时区分类型 —— 认领记录归档为 `claimed`，发布记录才是 `unknown`。

---

## 二、历史数据修正

新增 `server/scripts/repair_unknown_publish_logs.py`（默认 dry-run，`--apply` 才写库）。

流程：把 unknown 临时重置为 processing → 深度回源 → 平台确认的走真实状态 →
仍无回执的退回 unknown。

实测结果（数据取自修复后的数据库实际状态）：

| 账号 | 修复前 unknown | 回源转 success | 认领归档 claimed | fail | 平台总量 |
|---|---|---|---|---|---|
| 9 | 119 | **119** | — | — | 389 |
| 8 | 70 | **70** | — | — | — |
| 12 | 52 | **52** | — | — | 162 |
| 6 | 82 | 21 | 61 | — | 261 |
| 10 | 61 | 0 | 61 | — | 307 |
| 5 | 281 | 9 | 276 | 1 | 556 |
| 3 | 5 | 3 | 4 | 1 | 526 |
| 4 | 2 | 2 | — | — | 499 |
| 11 | 20 | — | 20 | — | — |
| 7 | 10 | **10** | — | — | — |

**最终状态**：
```
success: 292    claimed: 422    fail: 2    unknown: 3
```

剩下 3 条是 2026-11-02 的旧记录（批次 `pub_78a616596a9a` / `pub_5cb54e9506be`），
平台 30 页内已查不到，属真正的历史遗留，保持 unknown 是如实状态。

---

## 三、代码位置

| 文件 | 改动 |
|---|---|
| `server/api/publish.py` | `REFRESH_MAX_PAGES` 5→40；`_refresh_processing` 新增 `deep` 参数（不设页数上限，仅防死循环的硬上限 1000 页）|
| `server/services/schedule_service.py` | `release_stale_publish_logs` 释放前先按账号深度回源；认领记录归档为 `claimed` |
| `server/scripts/repair_unknown_publish_logs.py` | 新增，历史数据修正工具 |
| `server/tests/test_publish_receipt_refresh.py` | 新增，9 条测试 |

---

## 四、测试

`tests/test_publish_receipt_refresh.py` 覆盖：
- 翻页上限必须 ≥ 800 行（够覆盖 389 行这种体量）
- 回执真 ID 字段是 `collectBoxDetailId`，`detailId` 恒为 `None`（不能删那条兜底分支）
- `deep=True` 时实际会翻超过 5 页
- 释放前必须调用回源，且必须是 `deep=True`
- 平台说 success 的记录不能被标 unknown
- 认领记录归档为 `claimed` 而非 `unknown`
- 未超时的记录不碰、不触发多余上游调用

回归：全套本地测试 **11 条基线失败不变，零新增失败**。

---

## 五、为什么这个 bug 隐蔽

1. **不报错**。回源查不到就静默 `updated=0`，日志里只有一行 warning。
2. **平台侧一切正常**。笨手后台能看到商品在架上，只有本软件台账说失败了。
3. **有延迟**。发出后 600 秒才判死，人工排查时已经过了现场。
4. **自我掩盖**。unknown 不阻塞重新发布（`_find_in_flight` 只查 processing），
   所以用户「重发一次就好了」，看不出底层一直有问题。

---

## 六、遗留观察点

`release_stale_publish_logs` 现在会在释放前回源，最坏情况每轮扫描
（60 秒一次）对每个有超时记录的账号打满一次翻页。当前 `processing` 残留为 0
（修复后不再堆积），所以这条路径基本不会触发。

若将来发现上游调用量异常上涨，优先怀疑这里 —— 可加「同一账号 N 秒内
只深度回源一次」的内存节流。