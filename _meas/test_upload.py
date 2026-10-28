"""在本机验证 SFTP 图床上传链路（不打印任何凭据）。

跑法：cd server && python ../_meas/test_upload.py
成功的判据只有一个：返回一个 HTTPS 地址，且该地址能 200 拿到图片。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from services import image_upload  # noqa: E402

# 造一张最小的合法 PNG（1x1 红点），避免依赖外部素材
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63f8cfc000000301010018dd8db0"
    "0000000049454e44ae426082"
)

tmp = Path(__file__).with_name("_probe_chart.png")
tmp.write_bytes(PNG_1PX)

try:
    mime, ext = image_upload.image_type(PNG_1PX)
    print(f"[1] 类型识别: {mime} .{ext}")
except Exception as exc:
    print(f"[1] 类型识别失败: {exc}")
    raise SystemExit(1)

try:
    url = image_upload.upload_image(tmp)
    print(f"[2] 上传成功: {url}")
except image_upload.ImageUploadError as exc:
    print(f"[2] 上传失败: {exc}")
    raise SystemExit(2)
except Exception as exc:  # 凭据/网络异常
    print(f"[2] 上传异常: {type(exc).__name__}: {exc}")
    raise SystemExit(2)
finally:
    tmp.unlink(missing_ok=True)

# 验证回源可达
import urllib.request  # noqa: E402

try:
    with urllib.request.urlopen(url, timeout=20) as resp:
        size = len(resp.read())
        print(f"[3] 回源验证: HTTP {resp.status}, {size} 字节")
except Exception as exc:
    print(f"[3] 回源失败: {exc}")
    raise SystemExit(3)
