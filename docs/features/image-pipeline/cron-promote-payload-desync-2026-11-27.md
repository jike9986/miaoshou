# 定时任务搬图不同步 payload，商品图全部 404（2026-11-27）

> 现象：妙手商品详情里 `<p><img><img><img>` 全是空白框，
> 主图、SKU 颜色图、详情图一并不显示。负责人截图：
> 「为什么这些处理的图片全部没有了」。
>
> 实测：`#594`、`#582`、`#595`、`#596` 等一大批全部中招。

---

## 一、根因

`/etc/cron.d/aohuang-img-migrate` 每天 02:00 跑 `migrate_autotemp.py`：

```sql
UPDATE products SET detail_images = ..., images = ...
```

它只改 `detail_images` / `images` 两个**遗留列**，
**完全没有碰 `payload`**。

而妙手实际读的是 `payload`：

| 位置 | 对应 payload 字段 |
|---|---|
| 详情图 | `notes` 里 `<img src="...">` |
| SKU 颜色图 | `skuPropertyList[].attrValueList[].imgUrl` |
| 主图 | `imgUrls` |
| 尺码图 | `sizeChart` |

`image_upload.promote_to_product_zone()` 搬文件时会 `unlink()`
临时区原件。于是：

```
搬之前：文件在 auto-temp/ ，payload 写 auto-temp/    ✓ 一致
搬之后：文件到 products/  ，payload 还写 auto-temp/   ✗ 全 404
```

**旧列看起来「迁移完成」（残留 0 条），真正在用的 payload 一条都没改。**
这是这个 bug 藏了这么久的原因 —— 查遗留列永远正常。

### 实测时间线

```
auto-temp 最新文件: Nov 26 22:32   ← 之后没有新文件进临时区
products 最新文件:  Nov 27 02:01   ← 正是 02:00 这次 cron
payload 里还有 auto-temp 地址: 84 条
旧列 detail_images/images 里还有: 0 条
```

---

## 二、附带问题：连「待确认」的自动化结果也搬走

`warehouse_service.save_from_collect_box()` 里搬图有明确条件：

```python
# 搬运挪到 `admit_automation_results`（用户确认「加入成品仓库」那一刻）。
if not automation_result:
    promote_report = image_upload.promote_payload_images(payload)
```

设计意图：**自动化结果图留在临时区，等用户点「加入成品仓库」才搬**。

负责人 2026-11-21 原话：

> 「为什么这些自动化处理好的在自动化结果里面，我还没有从自动化结果里面
> 把这些东西加入仓库，你就把图片从临时仓库加入永久仓库了，要是其中有哪些
> 我不需要没加入成品仓库呢」

这个 cron 不看 `automation_result`，把待确认商品的图也搬了 ——
直接绕过那个设计，用户还在挑要不要入库，图已经被挪走了。

---

## 三、修法

`server/scripts/migrate_autotemp.py` 重写：

### 1. 同步 payload（核心）

递归遍历 payload 所有字符串叶子值，把 `auto-temp` 地址换成新地址。

**必须处理 HTML 内嵌地址。** `notes` 是整段 HTML 字符串，
只判 `url.startswith(auto_base)` 对 HTML 永远为假：

```python
# 错：notes 永远走不进这个分支
if node.startswith(auto_base): ...

# 对：在字符串里找所有 auto-temp 地址
while True:
    i = out.find(auto_base, start)
    ...截到分隔符...
```

2026-11-27 实测 `#639`：按纯地址修完 11 处后，`notes` 里还剩
**9 个** auto-temp 地址没改，妙手详情图继续 404。

### 2. 跳过待确认的自动化结果

```
automation_result = true 且文件仍在临时区  → 跳过，图原地不动
automation_result = true 但文件已被搬走    → 只改地址，不搬文件
其余商品                                  → 搬文件 + 同步 payload
```

第二条不能省：54 件待确认商品里 45 件的文件已被历史版本搬走，
一律跳过等于让它们永远 404。

### 3. 操作方式

```bash
python scripts/migrate_autotemp.py              # dry-run，只统计
python scripts/migrate_autotemp.py --apply      # 真写
python scripts/migrate_autotemp.py --id 639     # 只处理单件
```

---

## 四、修复结果

真机全量 330 件商品、5110 个图床地址：

| 步骤 | 处理量 |
|---|---|
| `migrate_autotemp --apply` | 30 件搬文件 626 张；53 件只改地址 |
| 扩展名不匹配修正 | 155 件 1551 处 |

### 扩展名不匹配（另一类 404）

文件是 `.jpg`，payload 写 `.png`，内容哈希完全相同：

```
payload: .../auto-temp/009a93fc....png
磁盘:    .../products/009a93fc....jpg
```

转 JPEG 优化后扩展名没跟上。按「目录 + 同名不同扩展名」修地址，
不动文件。

### 复验

```
5110 个图床地址 → 5110 个 HTTP 200，0 失败
```

另 158 件 payload 仍含 `auto-temp` 地址 —— 逐件核对文件确实都在
临时区，属**正常待确认状态**（入库那刻才搬），不是问题。

单件抽查 `#594`：修前 3 个 404，修后 15/15 全 200。

---

## 五、教训

1. **检查数据必须查实际读取的那个字段。** 遗留列干净不代表 payload 干净。
   这个 bug 从 cron 上线起就存在，因为查旧列永远是 0 残留。
2. **定时任务不能绕过业务条件。** `save_from_collect_box` 有
   `if not automation_result` 保护，cron 直接干，保护形同虚设。
3. **HTML 里的 URL 要单独处理。** `notes` 不是 URL 列表，是字符串。
4. **同一类 bug 会复发。** 2026-11-19 修过一次
   （见 `image-zone-promotion-2026-11-19.md`，95 件 2121 处），
   当时只修了入库路径，没检查 cron，于是从另一边又漏了一遍。
