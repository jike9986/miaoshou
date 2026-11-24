# 商品图三套尺寸与派生图生成（2026-11-24）

> 起因：负责人反馈「成品仓库里点进商品详情，图片加载得特别慢」，
> 截图里详情图区和颜色图全是空白格子。

## 结论先说

成品区每张图存三份尺寸，详情页全部走最小的那套：

| 目录 | 尺寸 | 平均体积 | 用在哪 |
|---|---|---|---|
| `products/` 原图 | 1254×1254 | 230 KB | 点开放大查看、发布到妙手 |
| `products/mid/` | 900×900 | 92 KB | 大图查看器中图 |
| `products/thumbs/` | 200×200 | **8.3 KB** | 详情页画廊（132px）、颜色小图（80px）、列表卡片 |

尺码图另有专门两份（因为它在站点根目录、不在 `products/` 下）：

| 文件 | 尺寸 | 体积 |
|---|---|---|
| `size-chart.png`（默认共用） | 1254×1254 | **1.19 MB** ← 原图，仅发布用 |
| `size-chart.jpg` | 900×900 | 154 KB |
| `size-chart-thumb.jpg` | 200×200 | 13.7 KB |

## 慢的原因（实测）

出口带宽约 100 KB/s。改前单件商品详情页要拉：

```
尺码图      1.19 MB  (9.02 s)
详情图 x9   2.95 MB
主图   x8   1.74 MB
合计        4.69 MB  →  约 47 秒
```

改后：`20 张缩略图 × 8.3 KB + 尺码图 13.7 KB ≈ 180 KB` → **1.8 秒**（缓存后 0.18 秒）。

图片响应头是 `Cache-Control: public, max-age=31536000`，
**每张图只在第一次加载传一遍**，之后全走本机磁盘缓存。

## 前端派生地址规则

`web/src/lib/utils.ts` 两个函数，必须与后端 `image_upload.thumb_url_of()` /
`mid_url_of()` 保持一致：

```ts
thumbImageUrl("https://…/products/abc.jpg")       // → https://…/products/thumbs/abc.jpg
thumbImageUrl("https://…/products/abc.png")       // → https://…/products/thumbs/abc.jpg   ← 后缀统一成 .jpg
midImageUrl  ("https://…/products/abc.jpg")       // → https://…/products/mid/abc.jpg
thumbImageUrl("https://…/size-chart.png")         // → https://…/size-chart-thumb.jpg
midImageUrl  ("https://…/size-chart.png")         // → https://…/size-chart.jpg
```

取到空串时调用方必须回退原图（`RemoteImage` 的 `fallbackSrc` 兜最后一道）。

## 派生图什么时候生成

三个入口：

1. **入库搬运**（主路径）：`promote_to_product_zone()` 把临时区的图搬进成品区时，
   顺手生成原图 + thumbs + mid。
2. **入库兜底**（2026-11-24 新增）：`promote_payload_images()` 里的 `_swapped()`
   对「已经在成品区」的图调 `_ensure_derived()`，缺什么补什么。
   幂等，有就跳过。
3. **批量补齐脚本**：`tools/source_match/backfill_derived.py`

### 为什么原来会漏

派生图只在**图片首次搬进成品区**时生成一次。所以：

- 2026-11-11 那批先入库、派生图机制后上线的图 → 永远只有原图
- 尺码图走 `promote_payload_images` 的 `_swap`，但原图早在成品区了，
  `auto_temp_name()` 返回空 → 直接 return，派生图永远不生成

实测（222 件成品仓商品）：3060 处图片里 203 处缺 thumbs/mid，尺码图 221/222 全缺。

## 两个必须注意的坑

### 1. `_zone_paths()` 第 3 个返回值是「环境变量名」，不是路径

```python
# 错 ❌ 会拼出 "AOHUANG_IMAGE_PRODUCT_DIR/xxx.jpg"，is_file() 恒 False
_dir, base, local_product, _b = _zone_paths("product")

# 对 ✅
_dir, base, _local_env, _b = _zone_paths("product")
local_product = os.environ.get(_local_env, "").strip().rstrip("/")
```

`_zone_paths()` 返回的是 `(远程目录, 公开前缀, 本地目录环境变量名, 公开前缀环境变量名)`。
本地路径必须从环境变量读。这个坑 2026-11-24 踩过一次，`_ensure_derived()` 静默失效。

### 2. 缓存标记要在成功之后加

`_DERIVED_DONE` 是「这个 stem 已确认有派生图」的进程内缓存。
如果在函数开头就 `add(stem)`，一旦中途失败（比如路径算错），
这个 stem 就被永久标记「处理过」，之后永远不再补。必须写在写入成功之后。

## 外链图处理

成品仓 payload 里的图片可能指向外部图床。2026-11-24 实测有两类共 203 处：

| 域名 | 数量 | 处置 |
|---|---|---|
| `earth-rt.chengji-inc.com`（妙手图床） | 192 | 实测 **404 已失效** → 从 payload 移除 |
| `cbu01.alicdn.com`（1688 图床） | 11 | 实测 200 正常 → 下载转存进自家图床 |

处理脚本 `tools/source_match/fix_external_images.py`。
搬运用 URL 内容的 sha256 做文件名，同一张图重复搬运不会产生两份。

处理完 2868 处图片全部是自家域名，缩略图覆盖 100%。

## 相关脚本

| 脚本 | 用途 |
|---|---|
| `tools/source_match/backfill_derived.py` | 批量补齐缺失的 thumbs/mid |
| `tools/source_match/fix_external_images.py` | 外链图搬入 / 失效外链清理 |

## 排查口诀

详情页图慢 → 先看 `payload` 里地址是哪种：
- 指向 `products/xxx.jpg` 原图（不是 `thumbs/`）→ 前端用了错的 helper
- 指向外链域名 → 跑 `fix_external_images.py`
- `thumbs/` 下文件不存在 → 跑 `backfill_derived.py`
- 尺码图慢 → 看是不是 `size-chart.png`（1.19 MB），应该用 `size-chart-thumb.jpg`