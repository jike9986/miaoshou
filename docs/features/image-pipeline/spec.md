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

---

## 十一、怎么判断「哪些图没用了」

**不能只扫 `products` 表。** `image_jobs`（翻译任务表）也存图片地址，
2026-11-19 第一次统计时只扫了 products 表，把 848 个仍被翻译任务引用的文件
误判成孤儿；扫全库（161 个文本/JSON 列）后真实孤儿是 **38 个文件 / 7.0 MB**。

正确口径：

```python
# 1. 遍历 information_schema.columns，取出所有 text/varchar/character/json/jsonb 列
# 2. 每列 where col::text like '%products/%' 取值，正则抠出 products/<文件名>
# 3. 该集合减去 os.listdir(products/) 就是孤儿
```

**「引用了但文件不存在」也要分开看**：2026-11-19 实测
在用商品 9,744 处引用、失效 0 处；已软删除商品 7,169 处引用、失效 6,680 处
（那些图在转换之前就不存在了，属于历史脏数据）。

## 十二、转换后必须复查「引用是否跟着改」

`convert` 模式每张图都做三步校验：写文件 → 改数据库 → **复查旧名残留**
（还有引用就回滚并删掉刚写的 jpg）→ 才 commit + 删原文件。

但「改数据库」这一步曾漏列（`products.automation_images`），代价是 31 个
在用商品挂图。现在的列清单是动态发现的，加字段不会再漏。
批量转换后建议固定跑一遍：

```bash
P=/opt/miaoshou-listing-v2/venv/bin/python
$P fix_converted_image_refs.py --dry-run   # 应为「改动 0 行」
```

## 十三、EdgeOne 接入要不要备案（2026-11-19 核实）

腾讯云官方口径：

> 接入 EO 的站点域名，加速区域选择为**中国大陆境内**或**全球可用区**时，
> 需要先完成 ICP 备案。若当前站点域名未完成 ICP 备案，
> **仅可选择使用「全球可用区（不含中国大陆）」**区域加速。

结论：源站在香港（38.95.75.47 属 AS979 NetLab，香港），本来也备案不了
（ICP 备案要求服务器在中国大陆）。选「全球可用区（不含中国大陆）」
**不需要备案**。

免费版套餐：0 美元/月，安全加速流量**不限量**、请求数不限量、1 个站点、
200 个子域名；但属「限量内测」，需要到 EdgeOne 独立站拿**兑换码**。

---

## 十四、三档尺寸与各自用在哪（2026-11-19 补）

负责人明确「我都是给国内用户用的」，源站在香港，**跨境带宽是瓶颈**，
所以尺寸要按「显示多大就传多大」分开做。

| 目录 | 最长边 | 质量 | 平均体积 | 用在哪 | 谁生成 |
| --- | --- | --- | --- | --- | --- |
| `products/<stem>.jpg` | 原图 | 92 | 216 KB | 发布到妙手、大图查看器、精修 | 入库转码 |
| `products/mid/<stem>.jpg` | 900 | 80 | **88 KB** | 详情页画廊、SKU 图、抽屉主图 | 入库派生 |
| `products/thumbs/<stem>.jpg` | 200 | 82 | **8.3 KB** | 仓库列表卡片 | 入库派生 |

派生图命名一律 `<stem>.jpg`，地址由原图推导（前后端同一套规则）：
`products/abc.png` → `products/mid/abc.jpg`、`products/thumbs/abc.jpg`。

**前端兜底**：`RemoteImage` 支持 `fallbackSrc`，派生图 404 时自动退回原图，
用户不会看到裂图。所以派生图缺失只是「多花点流量」，不会坏页面。

**大图查看器必须用原图**：那是用户主动放大看细节，给 900px 等于降质。

### 实测（2026-11-19）

```
最近 20 个在用商品，详情页全部图片：
  原图     66.7 MB
  中等图   28.4 MB   ← 降 57%
  单商品平均 3.3 MB → 1.4 MB
```

列表页一页 20 件：≈ 0.2 MB（缩略图）。
二次访问：`Cache-Control: immutable`，浏览器一年内不再请求。

## 十五、为什么最后没上 EdgeOne

结论：**给国内用户用，EdgeOne 免费版帮不上，先不上。**

1. 免费版的「开放中国访问」仍需备案才能吃大陆节点；未备案只能选
   「全球可用区（不含中国大陆）」，大陆用户依旧跨境，跟现在的香港源站差不多，
   还多一跳。
2. 要真吃大陆节点必须 ICP 备案，而备案要求大陆接入商 + 大陆服务器；
   现在的源站在香港（38.95.75.47 属 AS979 NetLab，香港），备案走不通。
3. 免费版是「限量内测」，要参加活动拿兑换码，属额外运维负担。

**替代方案（已落地，零成本）**：把图按尺寸拆成三档 + 长缓存。
详情页 3.3 MB → 1.4 MB，列表页 → 0.2 MB，二次访问 0 请求。

以后真要做大陆加速，唯一干净的路是：
买一台大陆 ECS 做备案接入 → 域名 ICP 备案（7~20 工作日）→
接入腾讯云 CDN/EO 中国大陆区域，回源仍可指向香港。
