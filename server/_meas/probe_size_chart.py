"""尺寸表三个补充行为的本地验证脚本（不联网、不部署）。

输出全部用 ASCII，避免 Windows 控制台把中文吃成乱码。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 用临时数据目录，绝不能碰真实 app.db
_TMP_DIR = Path(tempfile.mkdtemp(prefix="sizechart-probe-"))
os.environ["AOHUANG_DATA_DIR"] = str(_TMP_DIR)
os.environ["TK_AUTOMATION_DATA_DIR"] = str(_TMP_DIR)

from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX  # noqa: E402
from core import errors  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from services import size_chart_service as svc  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64

results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))


# --- 纯函数层 -------------------------------------------------------------
def test_sniff() -> None:
    check("sniff png", svc.sniff_image_type(PNG) == "image/png")
    check("sniff jpeg", svc.sniff_image_type(JPEG) == "image/jpeg")
    check("sniff webp", svc.sniff_image_type(WEBP) == "image/webp")
    check("sniff garbage", svc.sniff_image_type(b"not an image") == "")


def test_content_type() -> None:
    check("ct match", svc.check_content_type(PNG, "image/png") == "image/png")
    check("ct match with charset",
          svc.check_content_type(JPEG, "image/jpeg; charset=binary") == "image/jpeg")
    check("ct alias jpg", svc.check_content_type(JPEG, "image/jpg") == "image/jpeg")
    check("ct empty passes", svc.check_content_type(PNG, "") == "image/png")

    try:
        svc.check_content_type(PNG, "image/webp")
    except errors.AppError as exc:
        check("ct mismatch status", exc.status == 422, f"status={exc.status}")
        check("ct mismatch message",
              exc.message == "文件类型与声明不一致：声明 WebP，实际 PNG", exc.message)
    else:
        check("ct mismatch raises", False, "no error")

    try:
        svc.check_content_type(b"garbage-bytes", "image/png")
    except errors.AppError as exc:
        check("ct unknown status", exc.status == 422, f"status={exc.status}")
        check("ct unknown message", "未知格式" in exc.message, exc.message)
    else:
        check("ct unknown raises", False, "no error")


def test_url_validation() -> None:
    good = "https://aohuang888.com/x/size-chart.png"
    check("url ok", svc._validate_remote_url(good) == good)
    for bad in ("http://127.0.0.1:8000/a.png", "http://localhost/a.png",
                "http://192.168.1.9/a.png", "http://10.0.0.5/a.png",
                "http://172.20.3.4/a.png", "http://169.254.1.1/a.png",
                "http://[::1]/a.png", "http://metadata.google.internal/a.png",
                "http://0.0.0.0/a.png", "http://[fe80::1]/a.png"):
        try:
            svc._validate_remote_url(bad)
        except errors.AppError as exc:
            check(f"url reject {bad}", exc.status == 400, exc.message)
        else:
            check(f"url reject {bad}", False, "accepted")
    for bad in ("ftp://x.com/a.png", "file:///c:/a.png", "not-a-url"):
        try:
            svc._validate_remote_url(bad)
        except errors.AppError:
            check(f"url scheme reject {bad}", True)
        else:
            check(f"url scheme reject {bad}", False, "accepted")


# --- 服务层 / 路由层 -------------------------------------------------------
def test_db() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # 第 1 条：defaultUrl
        url = svc.default_url(db)
        check("default_url non-empty", bool(url), url)
        listing = svc.list_charts(db)
        check("list has defaultUrl", listing.get("defaultUrl") == url, str(listing.keys()))
        check("list defaultUrl camel", "defaultUrl" in listing)

        # 第 1 条：remember_url 入库 + 内网拒绝
        saved = svc.remember_url(db, url="https://cdn.example.com/a/chart.png")
        check("remember creates", saved["chart"]["url"].endswith("chart.png"))
        check("remember name from url", saved["chart"]["name"] == "chart", saved["chart"]["name"])
        again = svc.remember_url(db, url="https://cdn.example.com/a/chart.png",
                                 name="chart")
        check("remember idempotent", again.get("replaced") is True)
        try:
            svc.remember_url(db, url="http://127.0.0.1/x.png")
        except errors.AppError as exc:
            check("remember blocks internal", exc.status == 400, exc.message)
        else:
            check("remember blocks internal", False, "accepted")

        # 第 2 条：按 URL 删除
        svc.remember_url(db, url="https://cdn.example.com/b/other.png", name="other")
        deleted = svc.delete_by_url(db, "https://cdn.example.com/b/other.png")
        check("delete by url", deleted["deleted"] is True and deleted["count"] == 1)
        check("delete by url refcount",
              all(i["url"] != "https://cdn.example.com/b/other.png"
                  for i in deleted["items"]))
        try:
            svc.delete_by_url(db, "https://cdn.example.com/b/other.png")
        except errors.AppError as exc:
            check("delete by url missing -> 404", exc.status == 404, exc.message)
        else:
            check("delete by url missing -> 404", False, "no error")
        default = svc.default_url(db)
        try:
            svc.delete_by_url(db, default)
        except errors.AppError as exc:
            check("delete default refused", exc.status == 400 and "默认" in exc.message,
                  exc.message)
        else:
            check("delete default refused", False, "no error")
        db.commit()
    finally:
        db.close()


def test_http() -> None:
    from _probe_auth import teacher_headers

    headers = teacher_headers()
    with TestClient(app) as client:
        base = f"{API_PREFIX}/size-charts"

        # 第 3 条：声明与内容不一致 -> 422
        bad = client.post(f"{base}/upload?name=mismatch", content=PNG,
                          headers={**headers, "Content-Type": "image/webp"})
        body = bad.json()
        check("http mismatch 422", bad.status_code == 422, str(bad.status_code))
        check("http mismatch message",
              body.get("error", {}).get("message")
              == "文件类型与声明不一致：声明 WebP，实际 PNG",
              str(body)[:200])

        # 第 1 条：上传接口收 JSON URL
        ok_url = client.post(f"{base}/upload?name=via-url",
                             json={"url": "https://cdn.example.com/c/url-chart.png"},
                             headers=headers)
        check("http url upload 201", ok_url.status_code == 201, str(ok_url.status_code))
        check("http url upload name",
              ok_url.json().get("chart", {}).get("name") == "via-url",
              str(ok_url.json())[:200])

        # 内网 URL 被拒
        internal = client.post(f"{base}/upload?name=internal",
                               json={"url": "http://10.1.2.3/x.png"}, headers=headers)
        check("http url internal rejected", internal.status_code == 400,
              str(internal.status_code))

        # 第 2 条：DELETE 按 url（body）
        target = ok_url.json()["chart"]["url"]
        dele = client.request("DELETE", base, json={"url": target}, headers=headers)
        check("http delete by url", dele.status_code == 200, str(dele.status_code))
        check("http delete by url id", dele.json().get("url") == target,
              str(dele.json())[:200])

        # DELETE 按 url（query）
        reg = client.post(base, json={"name": "q-del", "url": "https://cdn.example.com/d/q.png"},
                          headers=headers)
        check("http register", reg.status_code == 201, str(reg.status_code))
        dele2 = client.request("DELETE", f"{base}?url=https%3A%2F%2Fcdn.example.com%2Fd%2Fq.png",
                               headers=headers)
        check("http delete by url query", dele2.status_code == 200, str(dele2.status_code))

        # 列表字段
        listing = client.get(base, headers=headers).json()
        check("http list defaultUrl", bool(listing.get("defaultUrl")),
              str(listing.keys()))

        # 按 id 删除仍可用
        reg2 = client.post(base, json={"name": "by-id",
                                       "url": "https://cdn.example.com/e/byid.png"},
                           headers=headers)
        cid = reg2.json()["chart"]["id"]
        dele3 = client.request("DELETE", f"{base}/{cid}", headers=headers)
        check("http delete by id", dele3.status_code == 200, str(dele3.status_code))


def main() -> int:
    test_sniff()
    test_content_type()
    test_url_validation()
    test_db()
    test_http()
    failed = [r for r in results if not r[1]]
    for label, ok, detail in results:
        print(("PASS " if ok else "FAIL ") + label + ("  [" + detail + "]" if detail and not ok else ""))
    print("TOTAL %d  PASS %d  FAIL %d" % (len(results), len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
