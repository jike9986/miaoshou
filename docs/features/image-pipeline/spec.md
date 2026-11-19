# 图片链路规范（image-pipeline）

> 负责人 2026-11-18 提出：「从仓库导入的图片加载要几秒，后面换流量的服务器可能一下子流量就没了」。
> 本文是这个问题的完整方案：两个分区、一次转换、一份缩略图、三层缓存。
> 落地代码：`server/services/image_upload.py`、`server/services/image_service.py`、`scripts/optimize_product_images.py`。

---

## 一、两个分区

图床根目录 `/var/www/tk-product-automation/`，nginx 直接静态服务（`location ^~ /tk-product-automation/`）。

| 分区 | 本地目录 | 公开前缀 | 保留策略 |
| --- | --- | --- | --- |
| 临时区 | `auto-temp/` | `https://aohuang888.com/tk-product-automation/auto-temp/` | 14 天自动清理 |
| 成品区 | `products/` | `https://aohuang888.com/tk-product-automation/products/` | 永久 |

环境变量对应：`AOHUANG_IMAGE_LOCAL_DIR/_BASE`（临时区）、`AOHUANG_IMAGE_PRODUCT_DIR/_BASE`（成品区）。

**为什么必须真搬文件**：临时区是按目录整批删的。只改数据库标记不搬文件，14 天后成品商品的主图会跟着一起消失。

**清理保护**：`image_upload.referenced_temp_names()` 扫 `products` 表里仍然引用临时区地址的商品（主图、详情图、SKU 图、尺码图），命中的文件不删。时间到了但商品还在用，就留着。

---

## 二、转换时机：只在「入库」转一次

```
1688/妙手原图
   ↓ 自动化翻译（要走翻译 API）
auto-temp/          ← 存原格式，不转
   ↓ 入库成品仓 warehouse_service → promote_to_product_zone()
products/           ← 这里统一转 JPEG + 生成缩略图
```

**为什么不在翻译前转**：翻译 API 返回的格式不确定，前面转了可能白转一遍（PNG → JPG → API 又返回 PNG）。

**为什么不在前端转**：原图必须保真。发布到妙手用的就是成品区的原图，前端压缩等于降低上架素材质量。

**格式判断只看魔数**：`image_upload.image_type()` 按文件头判断，不信扩展名。

---

## 三、入库转换做什么

`promote_to_product_zone(url)`（`server/services/image_upload.py`）：

1. 读原文件字节
2. `optimize_product_image(raw)` → `(字节, 扩展名)`
   - 已经是 JPEG：原样返回，**不重复压缩**（二次压缩只掉画质）
   - PNG/WebP：转 JPEG `PRODUCT_JPEG_QUALITY = 92`，视觉无损
   - 带 alpha（RGBA/LA/P+transparency）：先 `_flatten_alpha()` 合成到白底，**不能直接 `convert("RGB")`** —— 那会把透明区填成黑色，白底商品图变黑底
   - **永不失败**：Pillow 没装、图损坏、转完反而更大 → 返回原字节，入库继续
3. 落盘 `products/<stem>.jpg`（内容相同则不重写）
4. `make_thumbnail()` → `products/thumbs/<stem>.jpg`（最长边 200，质量 82）
5. 删掉临时区原文件（删不掉就留着，14 天清理会收）
6. 返回成品区新地址

远程 SFTP 模式走同一套逻辑（`_zone_paths` → `upload_image`），缩略图单独 `putfo` 到 `products/thumbs/`。

---

## 四、缩略图命名与目录

```
products/
├── <stem>.jpg          原图（发布、详情页用）
└── thumbs/
    └── <stem>.jpg      缩略图（列表卡片用，200×200）
```

**统一 `.jpg` 后缀**，与原图扩展名无关。原因：nginx 带 `X-Content-Type-Options: nosniff`，
扩展名和实际格式不一致会被浏览器拒绝渲染。

地址推导在 `image_upload.thumb_url_of(url)`：

```
https://.../products/abc.png  →  https://.../products/thumbs/abc.jpg
https://.../auto-temp/x.jpg   →  ""（临时区没有缩略图）
https://cbu01.alicdn.com/x.jpg →  ""（外链没有缩略图）
```

**为什么要子目录而不是另建顶级目录**：
- 同一个公开前缀，`AOHUANG_IMAGE_PRODUCT_BASE` 不用改两套
- 备份/清理脚本路径统一
- CDN 可以按 `*/thumbs/*` 单独设规则

**前端回退**：`warehouse_service` 序列化时加 `imageThumb`，前端 `ProductCard` 用
`product.imageThumb || product.image`。取不到缩略图（老商品、外链）自动回退原图，不会裂图。

---

## 五、翻译阶段降流量

`server/services/image_service.py`：

```
SHRINK_LIMIT    = 500 * 1024   # 超过 500 KB 才压
SHRINK_MAX_SIDE = 1200         # 最长边
SHRINK_QUALITY  = 85
```

单张图翻译要往返 4 次（下载 / 上传 API / API 返回 / 上传图床），原来按 2 MB 阈值，
一张 3.8 MB 的 PNG 全程占 15 MB 带宽。降到 500 KB 阈值后单张 ≈ 2 MB，一件 7 张 = 14 MB。

`_shrink` 自带保护：Pillow 没装、图坏了、压完更大 → 原样返回。

---

## 六、缓存三层

| 层 | 配置 | 位置 |
| --- | --- | --- |
| 源站 nginx | `Cache-Control: public, max-age=31536000, immutable` | `/etc/nginx/sites-enabled/aohuang-ai.conf` 的 `location ^~ /tk-product-automation/` |
| 浏览器 | 靠上面这行头，Chromium 自己缓存 | 客户端 |
| CDN（待接） | EdgeOne 缓存 365 天 | 见第七节 |

**为什么敢用 `immutable`**：文件名是内容哈希（`image_store` 用 `sha256(raw)` 命名），内容一变 URL 必变，不存在「同名不同内容」的情况。

实测（2026-11-19）：

```
$ curl -sI https://aohuang888.com/tk-product-automation/products/thumbs/<hash>.jpg
content-type: image/jpeg
cache-control: public, max-age=31536000, immutable
x-content-type-options: nosniff
```

---

## 七、CDN（EdgeOne）接入顺序

**CDN 是缓存层，不能替代服务器存储**：源文件必须留在 `products/`，CDN 只是让用户从就近节点拿副本。CDN 缓存会过期、会被清，源站删了图就真没了。

**必须先做完存量转换再接 CDN**，否则 CDN 计费按 654 KB 的原图算，钱全花在 PNG 上。

接入步骤：

```
1. 腾讯云控制台 → EdgeOne → 创建站点
2. 加速域名：img.aohuang888.com
3. 源站：38.95.75.47，回源 Host 必须是 aohuang888.com（否则 nginx 匹配不到 /tk-product-automation/）
4. DNS：加 CNAME 指向 EdgeOne 分配的地址
5. 缓存规则：*.jpg *.jpeg *.png *.webp → 365 天，忽略查询字符串
6. 改环境变量：
   AOHUANG_IMAGE_PRODUCT_BASE=https://img.aohuang888.com/tk-product-automation/products/
   AOHUANG_IMAGE_LOCAL_BASE=https://img.aohuang888.com/tk-product-automation/auto-temp/
   重启 aohuang-listing-v2
```

验证：`curl -I` 第一次 `x-cache: MISS`，第二次 `HIT`。

---

## 八、存量处理脚本

`scripts/optimize_product_images.py`（服务器上跑，venv 里有 Pillow）。

三种模式按风险递增，**别一把梭**：

```bash
cd /opt/miaoshou-listing-v2/server
set -a && . /etc/aohuang-listing-v2.env && set +a
P=/opt/miaoshou-listing-v2/venv/bin/python

$P optimize_product_images.py plan          # 只读报告
$P optimize_product_images.py thumbs --limit 20
$P optimize_product_images.py thumbs        # 只生成缩略图，不动原图不动库
$P optimize_product_images.py convert --dry-run
$P optimize_product_images.py convert --limit 20
$P optimize_product_images.py convert       # 转 JPEG + 改库 + 删原文件
```

**convert 模式的顺序与回滚**（每一步都有校验）：

```
1. 写 products/<stem>.jpg            → 校验：能打开、尺寸与原图一致、format == JPEG
2. 建 products/thumbs/<stem>.jpg     → 缺了才建
3. UPDATE products 里 6 个字段的引用   → cover / images / detail_images / sku_properties / payload / collected
4. 复查旧名残留                        → 还有引用就 rollback 并删掉刚写的 jpg
5. 都没问题才 commit + 删原 PNG
```

单张失败只记日志继续跑，不中断整批。

**幂等**：`thumbs` 里已存在的跳过；`convert` 时 `<stem>.jpg` 已存在且非空就跳过。

**只转被数据库引用的文件**：没被引用的（孤儿文件）不动，报告里单独列出来，属于后面单独清理的范围。

---

## 九、失败回退一览

| 场景 | 行为 |
| --- | --- |
| Pillow 没装 | `optimize_product_image` 返回原字节，入库继续，日志 warning |
| 图片损坏 | 同上 |
| 转完比原图大 | 保留原格式（小图/纯透明图会这样） |
| 缩略图失败 | 不生成，前端回退原图 |
| 写成品区失败 | 返回临时区地址，商品仍入库 |
| 删临时区文件失败 | 留着，14 天清理会收 |
| 数据库引用改写后仍有残留 | 回滚该文件的数据库改动 + 删掉新写的 jpg，保留原文件 |

**核心原则：图片优化不能把商品入库搞挂。** 所有 Pillow 调用都包在 try/except 里，最差情况就是「没优化」，不是「报错」。

---

## 十、实测数据（2026-11-19）

转换前 `products/`：

```
4,277 张  2,733 MB  平均 654 KB
JPG 3,192 / PNG 1,067 / WebP 18
> 1 MB：1,053 张（全是 PNG，最大 3.8 MB）
200 KB ~ 1 MB：0 张
```

`plan` 报告：待转 1,072 张，合计 2,145 MB；未被数据库引用 848 个。

缩略图实测：2 MB 的 PNG 生成 8~12 KB 缩略图，20 张耗时 2.7 秒。
列表页一页 20 件从 ≈ 13 MB 降到 ≈ 0.2 MB（**约 1/65**）。
