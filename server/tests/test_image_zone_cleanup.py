"""临时图床清理的两道保险：时间 + 引用。

负责人 2026-11-09 明确问过两件事：
  1. 刚入仓库的图片刚好遇到清理，会不会被清理？
  2. 已经从临时区搬到成品仓的，会不会被清理？
两道保险分别回答这两个问题：
  · mtime 必须早于 cutoff —— 刚上传的图一定比 cutoff 新，不会删；
  · 数据库仍在引用的文件名一律跳过 —— 入库搬运失败时商品仍指向临时区，
    那种图必须留着，否则商品变挂图。
成品区（products）整个目录不参与清理。
"""
from __future__ import annotations

import os
import time

import pytest

from services import image_upload


@pytest.fixture
def zones(tmp_path, monkeypatch):
    auto = tmp_path / "auto-temp"
    product = tmp_path / "products"
    auto.mkdir()
    product.mkdir()
    monkeypatch.setenv("AOHUANG_IMAGE_LOCAL_DIR", str(auto))
    monkeypatch.setenv("AOHUANG_IMAGE_PRODUCT_DIR", str(product))
    return auto, product


def _touch(path, *, age_days: float) -> None:
    path.write_bytes(b"x" * 32)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))


def test_fresh_image_is_never_deleted(zones):
    """刚上传的图（0 天）不能删 —— 回答「刚入仓库刚好遇到清理」。"""
    auto, _ = zones
    fresh = auto / "fresh.jpg"
    _touch(fresh, age_days=0)
    report = image_upload.cleanup_auto_temp(days=14)
    assert fresh.exists()
    assert report["deleted"] == 0
    assert report["kept"] == 1


def test_expired_unreferenced_image_is_deleted(zones):
    auto, _ = zones
    old = auto / "old.jpg"
    _touch(old, age_days=20)
    report = image_upload.cleanup_auto_temp(days=14)
    assert not old.exists()
    assert report["deleted"] == 1


def test_expired_but_still_referenced_image_is_kept(zones):
    """过期但仍有商品引用（入库搬运失败的情况）必须保留。"""
    auto, _ = zones
    old = auto / "old.jpg"
    _touch(old, age_days=20)
    report = image_upload.cleanup_auto_temp(days=14, referenced={"old.jpg"})
    assert old.exists()
    assert report["deleted"] == 0
    assert report["skippedReferenced"] == 1


def test_product_zone_is_never_touched(zones):
    """成品区的图再老也不删 —— 回答「已搬到成品仓的会不会被清理」。"""
    _, product = zones
    old = product / "sold.jpg"
    _touch(old, age_days=999)
    report = image_upload.cleanup_auto_temp(days=14)
    assert old.exists()
    assert report["scanned"] == 0


def test_copying_zone_skips_in_progress_upload_files(zones):
    """上传中的隐藏临时文件（.xxx.uploading）不参与清理。"""
    auto, _ = zones
    partial = auto / ".half.jpg.deadbeef.uploading"
    _touch(partial, age_days=30)
    report = image_upload.cleanup_auto_temp(days=14)
    assert partial.exists()
    assert report["scanned"] == 0


def test_dry_run_reports_without_deleting(zones):
    auto, _ = zones
    old = auto / "old.jpg"
    _touch(old, age_days=20)
    report = image_upload.cleanup_auto_temp(days=14, dry_run=True)
    assert old.exists()
    assert report["deleted"] == 1
    assert report["dryRun"] is True
