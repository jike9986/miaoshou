"""成品图优化：转 JPEG + 生成缩略图。

负责人 2026-11-18：图床里 1,067 张商品图是 PNG（单张最大 3.8 MB），
列表页一张卡片显示 200×200 却加载 654 KB 原图。这里验证转换逻辑。
"""
import io

from services import image_upload


def _png_bytes(size=(800, 800), color=(200, 30, 30), alpha=False):
    """生成带噪点的 PNG，模拟真实商品照片。

    纯色图不能用：JPEG 对纯色压缩效率极低，转完反而更大，
    会被 optimize_product_image 的"转完更大就保留原样"保护逻辑挡掉。
    """
    import random

    from PIL import Image

    mode = "RGBA" if alpha else "RGB"
    image = Image.new(mode, size, (0, 0, 0, 0) if alpha else color)
    pixels = image.load()
    rng = random.Random(42)
    for y in range(size[1]):
        for x in range(size[0]):
            r = min(255, max(0, color[0] + rng.randint(-40, 40)))
            g = min(255, max(0, color[1] + rng.randint(-40, 40)))
            b = min(255, max(0, color[2] + rng.randint(-40, 40)))
            pixels[x, y] = (r, g, b, 128) if alpha else (r, g, b)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_png_converted_to_jpeg():
    """PNG 必须转成 JPEG，体积明显变小。"""
    raw = _png_bytes()
    out, ext = image_upload.optimize_product_image(raw)
    assert ext == ".jpg"
    assert out.startswith(b"\xff\xd8\xff"), "输出必须是 JPEG 魔数"
    assert len(out) < len(raw), "转完必须更小"


def test_jpeg_passed_through_untouched():
    """已经是 JPEG 的不重复压 —— 二次压缩只会掉画质。"""
    from PIL import Image

    image = Image.new("RGB", (400, 400), (10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    raw = buffer.getvalue()
    out, ext = image_upload.optimize_product_image(raw)
    assert ext == ".jpg"
    assert out == raw, "JPEG 原样返回，字节不变"


def _rgba_photo_with_transparent_corner(size=(800, 800), corner=150):
    """真实尺寸的 RGBA 照片：角落里一块全透明，其余是带噪点的实色。

    不能用 200×200 纯透明图做样本：那种 PNG 只有几百字节，
    转出的 JPEG 反而更大，会被"转完更大就保留原样"的保护逻辑挡掉，
    测不到合成分支。
    """
    import random

    from PIL import Image

    image = Image.new("RGBA", size, (0, 0, 0, 0))
    pixels = image.load()
    rng = random.Random(7)
    for y in range(size[1]):
        for x in range(size[0]):
            if x < corner and y < corner:
                pixels[x, y] = (0, 0, 0, 0)  # 全透明
            else:
                pixels[x, y] = (
                    rng.randint(0, 255), rng.randint(0, 255),
                    rng.randint(0, 255), 255)
    return image


def test_flatten_alpha_turns_transparency_white():
    """全透明区域合到白底，不能变黑。

    直接 convert("RGB") 会把 alpha=0 的区域填成黑色，白底商品图就变成黑底。
    注意只能验全透明（alpha=0）区域：半透明像素叠白底后是"颜色变浅"，
    那是正确的合成结果，不该断言成纯白。
    """
    image = _rgba_photo_with_transparent_corner()
    flat = image_upload._flatten_alpha(image)
    assert flat.mode == "RGB"
    corner = flat.getpixel((5, 5))
    assert corner == (255, 255, 255), f"全透明区应为纯白，实际 {corner}"
    assert flat.getpixel((600, 600)) != (255, 255, 255), "不透明区不能被冲掉"


def test_transparent_png_converted_and_white():
    """端到端：带透明区的 PNG 走 optimize_product_image 后，透明区变白。"""
    from PIL import Image

    image = _rgba_photo_with_transparent_corner()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()

    out, ext = image_upload.optimize_product_image(raw)
    assert ext == ".jpg", "带透明区的照片应当转成 JPEG"
    with Image.open(io.BytesIO(out)) as result:
        corner = result.getpixel((5, 5))
        assert all(channel > 240 for channel in corner[:3]), (
            f"全透明区应为白底，实际 {corner}")


def test_thumbnail_flattens_transparency():
    """缩略图同样要白底，不能黑底。"""
    from PIL import Image

    image = _rgba_photo_with_transparent_corner()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    thumb = image_upload.make_thumbnail(buffer.getvalue())
    assert thumb is not None
    with Image.open(io.BytesIO(thumb)) as result:
        corner = result.getpixel((2, 2))
        assert all(channel > 240 for channel in corner[:3]), corner


def test_tiny_png_kept_as_is_when_jpeg_would_be_bigger():
    """小图/纯透明图转完更大时保留原图，不能越转越大。"""
    from PIL import Image

    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw = buffer.getvalue()

    out, ext = image_upload.optimize_product_image(raw)
    assert out == raw, "转完更大时必须原样返回"
    assert ext == "", "原样返回时不带扩展名，调用方保留原文件名"


def test_thumbnail_is_200px():
    """缩略图最长边必须是 200。"""
    from PIL import Image

    raw = _png_bytes(size=(1200, 900))
    thumb = image_upload.make_thumbnail(raw)
    assert thumb is not None
    with Image.open(io.BytesIO(thumb)) as image:
        assert max(image.size) == image_upload.THUMB_MAX_SIDE, image.size
        assert image.size[0] > image.size[1], "横图应该保持横"


def test_thumbnail_smaller_than_original():
    """缩略图必须显著小于原图。"""
    raw = _png_bytes(size=(1200, 1200))
    thumb = image_upload.make_thumbnail(raw)
    assert thumb is not None
    assert len(thumb) < len(raw) / 5, "缩略图应远小于原图"


def test_broken_image_returns_original():
    """坏图不能抛错 —— 优化失败必须放行，否则整批入库挂掉。"""
    raw = b"not an image at all"
    out, ext = image_upload.optimize_product_image(raw)
    assert out == raw
    assert ext == ""
    assert image_upload.make_thumbnail(raw) is None


def test_thumb_url_of_product_path():
    """成品区地址 → thumbs 子目录地址。"""
    base = "https://aohuang888.com/tk-product-automation/products/"
    assert image_upload.thumb_url_of(base + "abc123.jpg") == (
        base + "thumbs/abc123.jpg")


def test_thumb_url_of_rejects_non_product():
    """临时区/外链没有缩略图，返回空串让前端回退。"""
    assert image_upload.thumb_url_of(
        "https://aohuang888.com/tk-product-automation/auto-temp/x.jpg") == ""
    assert image_upload.thumb_url_of("https://cbu01.alicdn.com/x.jpg") == ""
    assert image_upload.thumb_url_of("") == ""
