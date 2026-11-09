"""图床分区运维：查看两个分区的占用，按保留天数清理临时区。

图床分两个区（负责人 2026-11-09 定）：
  · auto-temp  自动化处理产生的临时图，14 天后自动清理（服务里每 6 小时跑一次）
  · products   已入库成品仓的商品图，长期保留，本脚本永不清理

用法（服务器上）：
    python scripts/image_zones.py                # 只报告
    python scripts/image_zones.py --clean        # 真删临时区过期文件
    python scripts/image_zones.py --days 30      # 改保留天数（配合 --clean）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from services import image_upload  # noqa: E402


def _zone_size(path: Path) -> tuple[int, int]:
    if not path.is_dir():
        return 0, 0
    files = 0
    total = 0
    for entry in path.iterdir():
        if entry.is_file() and not entry.name.startswith("."):
            files += 1
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return files, total


def main() -> int:
    parser = argparse.ArgumentParser(description="图床分区运维")
    parser.add_argument("--clean", action="store_true", help="真删临时区过期文件")
    parser.add_argument("--days", type=int, default=image_upload.AUTO_TEMP_RETENTION_DAYS,
                        help="临时区保留天数（默认 14）")
    args = parser.parse_args()

    import os

    auto_dir = Path(os.environ.get("AOHUANG_IMAGE_LOCAL_DIR",
                                   image_upload.DEFAULT_SFTP_REMOTE_DIR))
    product_dir = Path(image_upload.PRODUCT_REMOTE_DIR)
    for label, path in (("auto-temp（临时，%d 天清理）" % args.days, auto_dir),
                        ("products（成品，永久保留）", product_dir)):
        files, size = _zone_size(path)
        print("%-32s %6d 个文件  %8.1f MB  %s"
              % (label, files, size / 1048576, path))

    report = image_upload.cleanup_auto_temp(days=args.days, dry_run=not args.clean)
    action = "删除" if args.clean else "将删除"
    print()
    print("临时区扫描 %d 个文件，%s %d 个（%.1f MB），保留 %d 个"
          % (report.get("scanned", 0), action, report.get("deleted", 0),
             report.get("freedBytes", 0) / 1048576, report.get("kept", 0)))
    if report.get("error"):
        print("跳过原因：%s" % report["error"])
    if not args.clean:
        print("（只报告模式，加 --clean 才会真删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
