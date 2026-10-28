"""ImageJob 公网 URL 字段 + 上传接线的验证脚本（临时，绝不碰真实 app.db）。

跑法：
    pwsh> python _meas/probe_public_url.py
脚本自己在导入 db 之前就把 AOHUANG_DATA_DIR 指到临时目录，双保险。
图床一律 monkeypatch 掉，绝不真的上传。

结果写 _meas/probe_public_url.json（中文在 PowerShell 控制台会乱码）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
sys.path.insert(0, str(SERVER))

DATA_DIR = Path(tempfile.mkdtemp(prefix="aohuang-probe-puburl-"))
os.environ["AOHUANG_DATA_DIR"] = str(DATA_DIR)
os.environ.pop("TK_AUTOMATION_DATA_DIR", None)
# 图床地址不给：本脚本一律注入假 upload_image，绝不真的连 SFTP / HTTP
os.environ.pop("IMAGE_UPLOAD_URL", None)

import models  # noqa: E402,F401  - 注册所有表
from core.config import get_settings  # noqa: E402
from db.session import Base, SessionLocal, engine  # noqa: E402
from models.image_job import ImageJob  # noqa: E402

from services import image_service, image_upload  # noqa: E402

Base.metadata.create_all(engine)
settings = get_settings()
settings.ensure_dirs()

CHECKS: list[dict] = []


def check(name: str, passed: bool, detail: object = "") -> None:
    CHECKS.append({"name": name, "result": "PASS" if passed else "FAIL",
                   "detail": detail if not passed else ""})


# --- 老库升级：把三列删掉再跑 init_db 的补列逻辑 ---------------------------
# 真实用户升级时库是旧结构（没有这三列），只能靠 db/session._add_missing_columns
# 补。这里把新库退回旧结构再补一次，等价于一次真实升级。
from sqlalchemy import text  # noqa: E402

from db.session import _add_missing_columns  # noqa: E402


def _columns() -> set[str]:
    with engine.begin() as conn:
        rows = conn.execute(text('PRAGMA table_info("image_jobs")')).fetchall()
    return {row[1] for row in rows}


for _col in ("public_url", "uploaded_at", "upload_error"):
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE image_jobs DROP COLUMN "{_col}"'))
check("退回旧结构后三列确实不存在",
      not ({"public_url", "uploaded_at", "upload_error"} & _columns()),
      sorted(_columns()))
_add_missing_columns()
check("_add_missing_columns 把 public_url 补回来了", "public_url" in _columns(),
      sorted(_columns()))
check("_add_missing_columns 把 uploaded_at 补回来了", "uploaded_at" in _columns(),
      sorted(_columns()))
check("_add_missing_columns 把 upload_error 补回来了", "upload_error" in _columns(),
      sorted(_columns()))


PUBLIC = "https://example.com/x.png"


class Uploader:
    """记录调用次数，便于断言「第二次没被调用」。"""

    def __init__(self, behaviour: str = "ok") -> None:
        self.calls = 0
        self.behaviour = behaviour

    def __call__(self, path, **kwargs):
        self.calls += 1
        if self.behaviour == "boom":
            raise RuntimeError("sftp down")
        return PUBLIC


def install(uploader) -> None:
    image_upload.upload_image = uploader


ORIGINAL_UPLOAD = image_upload.upload_image


def write_file(name: str) -> None:
    (settings.image_results_dir / name).write_bytes(b"\x89PNG\r\n\x1a\nprobe")


db = SessionLocal()


def new_job(job_id: str, *, status: str = "ready", filename: str = "x.png") -> ImageJob:
    job = ImageJob(id=job_id, batch_id="probe", source="main",
                   source_url="https://cbu01.alicdn.com/img/a.jpg",
                   status=status, filename=filename,
                   result_url=f"/api/v1/images/jobs/{job_id}/file" if filename else "")
    db.add(job)
    db.flush()
    return job


# --- 0. 模型真的多了三列 ----------------------------------------------------
cols = {c.name for c in ImageJob.__table__.columns}
check("模型有 public_url 列", "public_url" in cols, sorted(cols))
check("模型有 uploaded_at 列", "uploaded_at" in cols, sorted(cols))
check("模型有 upload_error 列", "upload_error" in cols, sorted(cols))
# 不写死总数：这个模型后来还会加列（`operator_id` 就加过一次），
# 硬编码 26 会让探针因为「别人正常加了一列」而假红，那是最没价值的红。
# 这里只断言三列都在、且列数至少到过 26（23 + 这三列），
# 靠下面 `_add_missing_columns` 那几条验证老库补列是真的补上了。
check("列数至少 26（原有 23 + 这三列）", len(cols) >= 26, len(cols))

# --- 1. 成功路径 -----------------------------------------------------------
write_file("x.png")
job = new_job("job-ok")
up = Uploader("ok")
install(up)
ok = image_service._upload_result(db, job)
check("成功时返回 True", ok is True, ok)
check("成功时写入 public_url", job.public_url == PUBLIC, job.public_url)
check("成功时 uploaded_at 非空", job.uploaded_at is not None, job.uploaded_at)
check("成功时 upload_error 为空", job.upload_error == "", job.upload_error)
check("成功时 status 仍是 ready", job.status == "ready", job.status)
check("成功时只调用了一次 upload_image", up.calls == 1, up.calls)

# --- 2. 已有 public_url 时跳过（幂等） --------------------------------------
up2 = Uploader("ok")
install(up2)
ok = image_service._upload_result(db, job)
check("已有 public_url 时返回 True", ok is True, ok)
check("已有 public_url 时不再调用 upload_image", up2.calls == 0, up2.calls)
check("已有 public_url 时地址不变", job.public_url == PUBLIC, job.public_url)

# --- 3. 上传抛异常：不改 status --------------------------------------------
job_fail = new_job("job-fail")
up3 = Uploader("boom")
install(up3)
ok = image_service._upload_result(db, job_fail)
check("上传失败返回 False", ok is False, ok)
check("上传失败 public_url 仍为空", job_fail.public_url in ("", None), job_fail.public_url)
check("上传失败 upload_error 有内容", bool(job_fail.upload_error), job_fail.upload_error)
check("上传失败 upload_error 截断到 500 内",
      len(job_fail.upload_error) <= 500, len(job_fail.upload_error))
check("上传失败 uploaded_at 仍为空", job_fail.uploaded_at is None, job_fail.uploaded_at)
check("上传失败绝不把 status 改成 failed", job_fail.status == "ready", job_fail.status)

# 重试真的能成功（幂等判据是 public_url，失败时它还是空）
up4 = Uploader("ok")
install(up4)
ok = image_service._upload_result(db, job_fail)
check("失败后再传一次成功", ok is True and job_fail.public_url == PUBLIC, job_fail.public_url)
check("失败后再传一次清空 upload_error", job_fail.upload_error == "", job_fail.upload_error)

# --- 4. 结果文件不存在 -----------------------------------------------------
job_missing = new_job("job-missing", filename="nope.png")
up5 = Uploader("ok")
install(up5)
ok = image_service._upload_result(db, job_missing)
check("文件不存在返回 False", ok is False, ok)
check("文件不存在 upload_error 说明原因", job_missing.upload_error == "结果文件不存在",
      job_missing.upload_error)
check("文件不存在时不上传", up5.calls == 0, up5.calls)
check("文件不存在时 status 仍是 ready", job_missing.status == "ready", job_missing.status)

# filename 为空
job_nofile = new_job("job-nofile", filename="")
ok = image_service._upload_result(db, job_nofile)
check("没有 filename 返回 False", ok is False, ok)
check("没有 filename upload_error 有说明", bool(job_nofile.upload_error),
      job_nofile.upload_error)

# --- 5. serialize 暴露三个字段 ---------------------------------------------
image_service._upload_result(db, job)  # job 已有值，走幂等分支
payload = image_service.serialize(job)
check("serialize 有 publicUrl", payload.get("publicUrl") == PUBLIC, payload.get("publicUrl"))
check("serialize 有 uploadedAt", payload.get("uploadedAt") is not None,
      payload.get("uploadedAt"))
check("serialize 有 uploadError", payload.get("uploadError") == "",
      payload.get("uploadError"))
check("serialize 的 previewUrl 仍是本地相对路径",
      str(payload.get("previewUrl", "")).startswith("/api/v1/images/jobs/"),
      payload.get("previewUrl"))
missing_payload = image_service.serialize(job_missing)
check("没上传过时 publicUrl 是 None", missing_payload.get("publicUrl") is None,
      missing_payload.get("publicUrl"))
check("没上传过时 uploadError 仍能读出来",
      missing_payload.get("uploadError") == "结果文件不存在",
      missing_payload.get("uploadError"))

# --- 6. serialize 输出里三个字段都在（真实契约，不靠 response_model）------
# 原来这里校验 `schemas.responses.ImageJobOut`，但那个模块已删除：它 14 个类
# 零引用、零 `response_model=`，且 `ProductOut` 比真实 serialize 少 5 个字段
# （revision / updatedBy / updatedAt / detailImageCount / skuProperties）——
# 接上去会静默丢掉乐观锁版本号。留着比删掉危险，所以删了。
# 契约的真实来源是 `image_service.serialize` 的返回 dict，这里直接验它。
fields = set(payload)
check("serialize 输出有 publicUrl", "publicUrl" in fields, sorted(fields))
check("serialize 输出有 uploadedAt", "uploadedAt" in fields, sorted(fields))
check("serialize 输出有 uploadError", "uploadError" in fields, sorted(fields))
check("serialize 输出 publicUrl 类型是 str 或 None",
      payload.get("publicUrl") is None or isinstance(payload.get("publicUrl"), str),
      type(payload.get("publicUrl")).__name__)

# --- 7. 三处 status="ready" 接线真的存在 ----------------------------------
import inspect  # noqa: E402

for func_name in ("translate", "refresh", "reconcile"):
    body = inspect.getsource(getattr(image_service, func_name))
    check(f"{func_name} 里调用了 _upload_result",
          "_upload_result(" in body, func_name)

# --- 7b. 功能验证：reconcile 拿到结果后真的把公网地址传上去了 ---------------
orig_query_task = image_service.query_task
try:
    image_service.query_task = lambda task_id: {
        "status": "ready", "image": b"\x89PNG\r\n\x1a\nprobe",
        "upstreamStatus": "succeeded"}
    job_rec = ImageJob(id="job-reconcile", batch_id="probe", source="main",
                       source_url="https://cbu01.alicdn.com/img/r.jpg",
                       status="submitted", task_id="up-1")
    db.add(job_rec)
    db.flush()
    up_rec = Uploader("ok")
    install(up_rec)
    image_service.reconcile(db, ["job-reconcile"])
    db.flush()
    check("reconcile 后 status 变 ready", job_rec.status == "ready", job_rec.status)
    check("reconcile 后 public_url 被写入", job_rec.public_url == PUBLIC,
          job_rec.public_url)
    check("reconcile 后 uploaded_at 非空", job_rec.uploaded_at is not None,
          job_rec.uploaded_at)
    check("reconcile 触发了一次上传", up_rec.calls == 1, up_rec.calls)

    # 上游传图床失败时 reconcile 仍把任务标 ready
    job_rec2 = ImageJob(id="job-reconcile-boom", batch_id="probe", source="main",
                        source_url="https://cbu01.alicdn.com/img/r2.jpg",
                        status="submitted", task_id="up-2")
    db.add(job_rec2)
    db.flush()
    install(Uploader("boom"))
    image_service.reconcile(db, ["job-reconcile-boom"])
    db.flush()
    check("上传失败时 reconcile 仍标 ready", job_rec2.status == "ready",
          job_rec2.status)
    check("上传失败时 reconcile 记下 upload_error", bool(job_rec2.upload_error),
          job_rec2.upload_error)
finally:
    image_service.query_task = orig_query_task

# --- 8. HTTP 接口 ----------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from api import API_PREFIX  # noqa: E402
from main import app  # noqa: E402

db.commit()

token = settings.local_secret()
headers = {"Authorization": f"Bearer {token}"}
base = f"{API_PREFIX}/images/jobs"

with TestClient(app) as client:
    # 8.1 成功 200
    write_file("http-ok.png")
    h_ok = new_job("job-http-ok", filename="http-ok.png")
    db.commit()
    up_h1 = Uploader("ok")
    install(up_h1)
    resp = client.post(f"{base}/{h_ok.id}/upload", headers=headers)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    check("POST upload 成功返回 200", resp.status_code == 200, resp.status_code)
    check("POST upload 返回 job.publicUrl",
          body.get("job", {}).get("publicUrl") == PUBLIC, str(body)[:300])
    check("POST upload 成功时调用了一次上传", up_h1.calls == 1, up_h1.calls)

    # 8.2 已有值幂等 200，且不再上传
    up_h2 = Uploader("boom")  # 若被调用就会失败，正好证明没调用
    install(up_h2)
    resp2 = client.post(f"{base}/{h_ok.id}/upload", headers=headers)
    body2 = resp2.json() if resp2.headers.get("content-type", "").startswith("application/json") else {}
    check("已有 public_url 时再次 POST 返回 200", resp2.status_code == 200, resp2.status_code)
    check("已有 public_url 时返回现有值",
          body2.get("job", {}).get("publicUrl") == PUBLIC, str(body2)[:300])
    check("已有 public_url 时第二次没调用上传", up_h2.calls == 0, up_h2.calls)

    # 8.3 非 ready -> 409
    h_pending = new_job("job-http-pending", status="submitted", filename="")
    db.commit()
    resp3 = client.post(f"{base}/{h_pending.id}/upload", headers=headers)
    err3 = resp3.json().get("error", {}) if resp3.headers.get("content-type", "").startswith("application/json") else {}
    check("非 ready 返回 409", resp3.status_code == 409, resp3.status_code)
    check("非 ready 的 message 说明不能上传",
          "不能上传" in str(err3.get("message", "")), str(err3)[:300])

    # 8.4 不存在 -> 404
    resp4 = client.post(f"{base}/job-does-not-exist/upload", headers=headers)
    err4 = resp4.json().get("error", {}) if resp4.headers.get("content-type", "").startswith("application/json") else {}
    check("任务不存在返回 404", resp4.status_code == 404, resp4.status_code)
    check("404 用错误信封", err4.get("code") == "NOT_FOUND", str(err4)[:300])

    # 8.5 上传失败 -> 502 + message 用 upload_error
    write_file("http-boom.png")
    h_boom = new_job("job-http-boom", filename="http-boom.png")
    db.commit()
    up_h3 = Uploader("boom")
    install(up_h3)
    resp5 = client.post(f"{base}/{h_boom.id}/upload", headers=headers)
    err5 = resp5.json().get("error", {}) if resp5.headers.get("content-type", "").startswith("application/json") else {}
    check("上传失败返回 502", resp5.status_code == 502, resp5.status_code)
    check("502 的 message 用 upload_error",
          err5.get("message") == "sftp down", str(err5)[:300])
    db.expire_all()
    after = db.query(ImageJob).filter(ImageJob.id == h_boom.id).one()
    check("502 之后 status 仍是 ready（库里也是）", after.status == "ready", after.status)

    # 8.6 GET 单个任务里能看到 publicUrl
    got = client.get(f"{base}/{h_ok.id}", headers=headers)
    gbody = got.json() if got.headers.get("content-type", "").startswith("application/json") else {}
    check("GET job 里带 publicUrl",
          gbody.get("job", {}).get("publicUrl") == PUBLIC, str(gbody)[:300])

image_upload.upload_image = ORIGINAL_UPLOAD

db.rollback()
db.close()
shutil.rmtree(DATA_DIR, ignore_errors=True)

passed = sum(1 for c in CHECKS if c["result"] == "PASS")
out = {"passed": passed, "failed": len(CHECKS) - passed, "total": len(CHECKS),
       "checks": CHECKS}
target = HERE / "probe_public_url.json"
target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"PASS {passed}/{len(CHECKS)} -> {target}")
sys.exit(0 if passed == len(CHECKS) else 1)
