"""独立审查验证 part4：HTTP 层（临时数据目录副本，不改真实库）。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

REAL = Path(os.environ["LOCALAPPDATA"]) / "aohuang-desktop" / "app.db"
TMP = Path(tempfile.mkdtemp(prefix="audit4-"))
shutil.copy2(REAL, TMP / "app.db")
if (REAL.parent / "secret.key").is_file():
    shutil.copy2(REAL.parent / "secret.key", TMP / "secret.key")
os.environ["AOHUANG_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def show(label, response):
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = response.text[:200]
    code = ""
    if isinstance(body, dict) and "error" in body:
        code = body["error"].get("code", "")
        body = body["error"]
    print(f"  {label}: HTTP {response.status_code} {code} {str(body)[:180]}")
    return response


def main() -> int:
    from fastapi.testclient import TestClient
    import main as app_main
    from core.config import get_settings
    from db.session import SessionLocal
    from models.schedule import Schedule
    from models.product import Product
    from services import setting_service

    token = get_settings().local_secret()
    c = TestClient(app_main.app, raise_server_exceptions=False)
    H = {"Authorization": f"Bearer {token}"}
    print("data_dir =", os.environ["AOHUANG_DATA_DIR"])

    print("\n=== P0-1 采集箱 6 条接口不再 500 ===")
    show("GET /collect-box?pageSize=2", c.get("/api/v1/collect-box?pageSize=2", headers=H))
    show("GET /collect-box/1?shopId=x", c.get("/api/v1/collect-box/1?shopId=x", headers=H))
    show("POST /collect-box/1/claim", c.post("/api/v1/collect-box/1/claim?shopId=1",
                                             headers=H, json={"targetShopId": "18077147"}))
    show("POST /collect-box/claim", c.post("/api/v1/collect-box/claim", headers=H,
                                           json={"keys": ["1:1"], "targetShopId": "18077147"}))
    show("POST /collect-box/delete", c.post("/api/v1/collect-box/delete", headers=H,
                                            json={"detailIds": ["1"]}))
    show("POST /categories/sync", c.post("/api/v1/categories/sync", headers=H, json={}))

    print("\n=== P0-3 process 接口可达 ===")
    show("POST /collect-box/1/process", c.post("/api/v1/collect-box/1/process", headers=H,
                                               json={"shopId": "18077147", "save": False}))

    print("\n=== P1-10 PATCH /schedules 显式 null 清空 ===")
    db = SessionLocal()
    pid = db.query(Product).filter(Product.deleted_at.is_(None)).first()
    db.close()
    body = {"accountId": str(pid.account_id), "shopId": pid.shop_id,
            "template": "", "frequency": "range", "perRun": 2,
            "time": "09:00", "startDate": "2026-01-01", "endDate": "2026-12-31",
            "name": "__audit__"}
    r = show("POST /schedules (range)", c.post("/api/v1/schedules", headers=H, json=body))
    if r.status_code == 201:
        tid = r.json()["task"]["id"]
        print("    created:", tid, "start", r.json()["task"].get("startDate"),
              "end", r.json()["task"].get("endDate"))
        r2 = show("PATCH frequency=daily + startDate=null + endDate=null",
                  c.patch(f"/api/v1/schedules/{tid}", headers=H,
                          json={"frequency": "daily", "startDate": None, "endDate": None}))
        db = SessionLocal()
        row = db.get(Schedule, tid)
        print("    库里: frequency=%r start=%r end=%r" %
              (row.frequency, row.start_date, row.end_date))
        db.close()
        # 再试 days 清空（weekly -> daily）
        show("PATCH days=null", c.patch(f"/api/v1/schedules/{tid}", headers=H,
                                        json={"days": None}))
        # 再试 name=null（不可清空字段，应沿用）
        show("PATCH name=null", c.patch(f"/api/v1/schedules/{tid}", headers=H,
                                        json={"name": None}))
        # 空 body
        show("PATCH {} 空 body", c.patch(f"/api/v1/schedules/{tid}", headers=H, json={}))
        show("DELETE", c.delete(f"/api/v1/schedules/{tid}", headers=H))

    print("\n=== P1-9 剩余契约差异抽查 ===")
    show("PATCH /warehouse/{id} title='A' (1 char)", c.patch(
        f"/api/v1/warehouse/{pid.id}", headers=H, json={"title": "A"}))
    show("POST /publish 幂等头（同 key 两次）", c.post(
        "/api/v1/publish", headers={**H, "Idempotency-Key": "audit-1"}, json={}))
    show("GET /collect-box?pageSize=50 (契约1-20)", c.get(
        "/api/v1/collect-box?pageSize=50", headers=H))
    show("POST /titles/preview", c.post("/api/v1/titles/preview", headers=H,
                                        json={"titles": ["好鞋 包邮 秒杀"]}))
    show("GET /size-charts", c.get("/api/v1/size-charts", headers=H))
    show("GET /carrier/links", c.get("/api/v1/carrier/links", headers=H))
    show("POST /sync/price-templates", c.post("/api/v1/sync/price-templates", headers=H,
                                              json={"pageSize": 5}))
    show("POST /images/analyze(空)", c.post("/api/v1/images/analyze", headers=H,
                                            json={"images": []}))
    show("POST /warehouse/price-preview", c.post("/api/v1/warehouse/price-preview", headers=H,
                                                 json={"items": [{"warehouseId": str(pid.id)}],
                                                       "template": "默认模板"}))
    show("PATCH /violation-words/enabled", c.patch("/api/v1/violation-words/enabled",
                                                    headers=H, json={"enabled": False,
                                                                     "words": ["zzz-none"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
