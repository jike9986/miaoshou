"""孤儿图反查必须覆盖全库所有字符串列。

**这里记录一次差点删掉 942 张生产商品图的事故。**

第一版审计脚本只扫了 `products.payload`，报出 **1016 个孤儿 / 288 MB**。
实际扫全库后只有 **74 个 / 21.9 MB**：

```
只扫 payload:  引用 6342 条  → 孤儿 1016
扫全部表:      引用 8051 条  → 孤儿 74
被其它表救回（绝不能删）:     942
```

漏掉的 942 张在 `products` 表的**其他列**里：

| 列 | 说明 |
|---|---|
| `products.images` | 主图列表（独立列，不在 payload 里） |
| `products.detail_images` | 详情图列表 |
| `products.automation_images` | 自动化图片任务结果 |
| `image_jobs.public_url` | 图片任务输出地址 |

如果按 1016 的结论执行删除，942 张正在被商品使用的图会被真删掉 ——
商品详情页大面积挂图，且不可恢复。

## 现在的规则

1. **宁滥勿缺**：扫所有表的**所有字符串/JSON 列**，不猜「哪个列名像图片」。
   多扫几列只是慢一点，漏扫会删掉生产数据。
2. **扫描表数闸门**：扫到的表少于 10 张就中止，说明反查不完整。
3. **孤儿占比闸门**：孤儿超过总文件 60% 就中止，那个比例通常是漏扫信号。
4. **内容哈希核对**：孤儿若与某个在用文件内容完全相同，删了才真正无损。
"""
from __future__ import annotations

URL_JPG = "https://aohuang888.com/tk-product-automation/products/store-abc123.jpg"
URL_PNG = "https://aohuang888.com/tk-product-automation/products/store-def456.png"
AUTO = "https://aohuang888.com/tk-product-automation/auto-temp/store-xyz789.jpg"


def test_url_regex_extracts_all_zone_paths():
    """正则要认出 products / auto-temp / mid / thumbs 四种路径。"""
    from scripts.audit_orphan_images import _URL_RE

    text = (
        f'<img src="{URL_JPG}">'
        f'{{"imgUrl":"{URL_PNG}","big":"{AUTO}"}}'
        'https://aohuang888.com/tk-product-automation/products/mid/store-abc123.jpg'
        'https://aohuang888.com/tk-product-automation/products/thumbs/store-def456.jpg'
    )
    found = set(_URL_RE.findall(text))
    assert "store-abc123.jpg" in found
    assert "store-def456.png" in found
    assert "store-xyz789.jpg" in found


def test_orphan_ratio_gate_blocks_mass_deletion():
    """孤儿占比过闸门时必须中止 —— 这是拦住那次 1016 事故的闸门。"""
    from scripts.audit_orphan_images import MAX_ORPHAN_RATIO

    assert MAX_ORPHAN_RATIO <= 0.60, "闸门太松，拦不住漏扫"
    # 那次事故的比例：1016 / 4351 = 23.4% —— 单看比例拦不住，
    # 所以还需要「扫描表数」闸门（见下一个测试）。
    assert 1016 / 4351 < MAX_ORPHAN_RATIO


def test_scan_table_count_gate():
    """扫到的表太少必须判为反查不完整。"""
    import inspect as py_inspect

    from scripts import audit_orphan_images

    source = py_inspect.getsource(audit_orphan_images.main)
    assert "scanned_tables < 10" in source, "扫描表数闸门被删了"
    assert "反查覆盖不足" in source


def test_collect_references_scans_all_text_columns():
    """`_collect_references` 不能跳过 products 表的非 payload 列。"""
    import inspect as py_inspect

    from scripts import audit_orphan_images

    source = py_inspect.getsource(audit_orphan_images._collect_references)
    # 不能有「跳过 products 表」这种写法
    assert 'if table_name == "products":' not in source, \
        "又把 products 表跳过了 —— 942 张图的事故就是这么来的"
    # 必须按列类型筛选并扫所有表
    assert "get_table_names" in source
    assert "TEXT" in source and "JSON" in source


def test_orphan_is_not_reused_content_rule():
    """内容哈希相同 = 删了无损；内容独一 = 需要人确认。

    这条是「敢不敢删」的判据，不是代码断言，而是把规则写进测试提醒后来人。
    """
    import hashlib

    same_a = hashlib.sha256(b"same-bytes").hexdigest()
    same_b = hashlib.sha256(b"same-bytes").hexdigest()
    other = hashlib.sha256(b"other-bytes").hexdigest()

    assert same_a == same_b, "内容相同 → 删了无损"
    assert same_a != other, "内容独一 → 删了就没了"