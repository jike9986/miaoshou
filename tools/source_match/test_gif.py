"""验证 GIF 转 JPEG：两个真实样本都要能认出来。"""
import warnings

warnings.filterwarnings("ignore")

from services import image_service

URLS = [
    # 1x1 占位 GIF，43 字节
    "https://aohuang888.com/tk-product-automation/auto-temp/store-b1442e85b03bdcaf66dc58c7abb98745.jpg",
    # 1.9MB 的真 GIF
    "https://aohuang888.com/tk-product-automation/auto-temp/store-b663a8e4345e0dbcb35a840deb898a9b.jpg",
]

for u in URLS:
    raw = image_service._fetch_source(u)
    if not raw:
        print(f"取不到: {u[-40:]}")
        continue
    before = image_service._image_type(raw)
    after_raw = image_service._gif_to_jpeg(raw)
    after = image_service._image_type(after_raw)
    print("%-45s %7.0f KB  %-14s -> %7.0f KB  %-14s% s" % (
        u[-42:], len(raw) / 1024, str(before),
        len(after_raw) / 1024, str(after),
        "" if after and after[0] != "image/gif" else "   <<< 仍不是可提交格式 <<<"))

# 再造一个带透明通道的 GIF，确认白底合成没问题
import io
from PIL import Image

buf = io.BytesIO()
img = Image.new("P", (200, 200), 0)
img.putpalette([255, 255, 255] * 256)
img.info["transparency"] = 0
img.save(buf, format="GIF")
gif = buf.getvalue()
out = image_service._gif_to_jpeg(gif)
print("\n合成透明 GIF: %d -> %d 字节, 类型=%s" % (len(gif), len(out), image_service._image_type(out)))