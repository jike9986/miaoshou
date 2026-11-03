"""Final translated images must be saved and read back before automation can publish."""
from __future__ import annotations

import uuid
from copy import deepcopy

import pytest

from api import collect_box
from core import errors
from core.utils import now_ts
from db.session import SessionLocal, init_db
from models.account import Account
from models.automation_run import AutomationRunItem
from models.image_job import ImageJob
from models.product import Product
from services import (
    account_service,
    automation_run_service,
    image_service,
    operator_service,
    warehouse_service,
)

SOURCE = "https://images.example.test/source.jpg"
TRANSLATED = "https://images.example.test/translated.jpg"


@pytest.fixture()
def case(monkeypatch):
    init_db()
    db = SessionLocal()
    owner, _ = operator_service.create(
        db, f"image-life-{uuid.uuid4().hex[:12]}", role=operator_service.ROLE_MEMBER)
    account = Account(name=f"image-life-{uuid.uuid4().hex[:12]}",
                      app_key=uuid.uuid4().hex, app_secret_enc="",
                      owner_operator_id=owner.id, status="active")
    db.add(account)
    db.commit()
    detail_id = str(uuid.uuid4().int % 10**12)
    run = automation_run_service.create(
        db, owner, account_id=account.id, shop_id="22",
        settings={"textEnabled": True, "imagesEnabled": True, "publishEnabled": True,
                  "priceTemplateEnabled": True, "template": "Test template"},
        items=[{"key": f"{detail_id}:11", "detailId": detail_id, "shopId": "11"}])
    item = db.query(AutomationRunItem).filter_by(run_id=run.id).one()
    automation_run_service.start_item(db, run, item.id)
    payload = {"title": "Translated title", "imgUrls": [SOURCE], "skuMap": {},
               "weight": "250", "length": "10", "width": "10", "height": "10"}
    product = Product(
        warehouse_key=f"{detail_id}:11", account_id=account.id, detail_id=detail_id,
        shop_id="11", operator_id=owner.id, automation_result=True,
        payload=deepcopy(payload), title=payload["title"], revision=1,
        draft_verified_revision=None,
        quality_review={"verifiedRevision": 1, "evidence": "test"},
        automation_images={"queued": True, "completed": False, "requested": 0,
                           "submitted": 0, "failed": 0, "jobs": [],
                           "generation": uuid.uuid4().hex, "revision": 1,
                           "startedAt": now_ts()})
    db.add(product)
    db.commit()
    job = {"productId": product.id, "detailId": detail_id, "operatorId": owner.id,
           "generation": product.automation_images["generation"],
           "limits": {"main": 1, "sku": 0, "detail": 0}}

    class DraftClient:
        def __init__(self):
            self.payload = deepcopy(payload)
            self.saves = []
            self.reads = 0
            self.corrupt_readback = False
            self.closed = False

        def shop_detail(self, detail_id, shop_id):
            self.reads += 1
            data = deepcopy(self.payload)
            if self.corrupt_readback and self.saves:
                data["imgUrls"] = [SOURCE]
            return {"data": {"shopCollectItemInfo": data, "ossMd5": "fresh-md5"}}

        def save_shop(self, detail_id, shop_id, oss_md5, data):
            assert oss_md5 == "fresh-md5"
            self.saves.append(deepcopy(data))
            self.payload = deepcopy(data)

        def close(self):
            self.closed = True

    upstream = DraftClient()
    monkeypatch.setattr(account_service, "account_client", lambda _account: upstream)
    monkeypatch.setattr(warehouse_service, "select_translatable_images",
                        lambda _payload, **_kwargs: {"main": [SOURCE], "sku": [], "detail": []})

    def attach(session, _product, **kwargs):
        image = ImageJob(
            id=uuid.uuid4().hex, product_id=product.id, operator_id=owner.id,
            source="main", source_url=SOURCE, status="ready", result_url=TRANSLATED,
            public_url=TRANSLATED)
        session.add(image)
        session.flush()
        assert kwargs["operator_id"] == owner.id
        return {"requested": 1, "submitted": 1, "failed": 0,
                "jobs": [image_service.serialize(image)]}

    monkeypatch.setattr(warehouse_service, "attach_image_translation", attach)
    yield db, run, item, product, job, upstream
    db.close()


def finish(db, run, item, product):
    return automation_run_service.finish_process(
        db, run, item.id, status="succeeded", product_id=product.id,
        result={"saved": True, "draftVerified": False,
                "images": {"queued": True, "completed": False,
                           "requested": 0, "submitted": 0, "failed": 0}})


def assert_failed(case):
    db, run, item, product, _, _ = case
    db.expire_all()
    assert product.automation_images["completed"] is True
    assert product.automation_images["queued"] is False
    assert product.automation_images["failed"] >= 1
    assert product.draft_verified_revision is None
    assert item.process_status == "needs_review"
    assert item.publish_status == "needs_review"
    assert "_serverVerification" not in item.process_result
    with pytest.raises(errors.AppError):
        automation_run_service.start_publish(db, run, item.id)
    db.rollback()


def test_queued_without_image_jobs_cannot_publish(case):
    db, run, item, product, _, _ = case
    result = finish(db, run, item, product)
    assert result["status"] == "running"
    assert item.process_result["images"]["queued"] is True
    assert item.process_result["draftVerified"] is False
    assert "_serverVerification" not in item.process_result
    with pytest.raises(errors.AppError, match="草稿"):
        automation_run_service.start_publish(db, run, item.id)


@pytest.mark.parametrize("background_first", [False, True])
def test_final_images_save_and_readback_refresh_the_exact_run_proof(case, background_first):
    db, run, item, product, job, upstream = case
    db.add(ImageJob(
        id=uuid.uuid4().hex, product_id=product.id, operator_id=product.operator_id,
        source="main", source_url=SOURCE, status="failed"))
    db.commit()
    if not background_first:
        finish(db, run, item, product)
    collect_box._run_image_translation(job)
    if background_first:
        finish(db, run, item, product)
    db.expire_all()
    assert len(upstream.saves) == 1
    assert upstream.saves[0]["imgUrls"] == [TRANSLATED]
    assert upstream.reads == 2
    assert upstream.closed is True
    assert product.payload["imgUrls"] == [TRANSLATED]
    assert product.revision == product.draft_verified_revision == 2
    images = item.process_result["images"]
    assert images["completed"] is True and images["queued"] is False
    assert images["failed"] == 0
    assert item.process_result["_serverVerification"] == {
        "productId": str(product.id), "productUpdatedAt": product.updated_at,
        "productRevision": 2}
    assert "generation" not in images
    if background_first:
        resumed = automation_run_service.start_publish(
            db, run, item.id, confirmed_product_id=str(product.id), confirmed_revision=2)
    else:
        resumed = automation_run_service.resume_publish_after_images(
            db, run, item.id, confirmed_product_id=str(product.id), confirmed_revision=2)
    assert resumed["items"][0]["publishStatus"] == "running"


def test_zero_chinese_images_finishes_without_waiting_for_a_nonexistent_job(case, monkeypatch):
    db, run, item, product, job, upstream = case
    monkeypatch.setattr(warehouse_service, "select_translatable_images",
                        lambda *_args, **_kwargs: {"main": [], "sku": [], "detail": []})
    finish(db, run, item, product)
    collect_box._run_image_translation(job)
    db.expire_all()
    assert product.automation_images["completed"] is True
    assert product.automation_images["requested"] == 0
    assert product.automation_images["failed"] == 0
    assert product.revision == product.draft_verified_revision == 1
    assert upstream.reads == 1 and upstream.saves == []
    assert item.process_result["draftVerified"] is True
    assert item.publish_result["resumeKind"] == "images_pending"
    automation_run_service.resume_publish_after_images(db, run, item.id)


@pytest.mark.parametrize("background_first", [False, True])
def test_failure_before_or_after_finish_records_real_failure_and_blocks_publish(
        case, monkeypatch, background_first):
    db, run, item, product, job, upstream = case
    monkeypatch.setattr(warehouse_service, "attach_image_translation",
                        lambda *_args, **_kwargs: {"requested": 1, "submitted": 0,
                                                  "failed": 1, "jobs": []})
    if not background_first:
        finish(db, run, item, product)
    collect_box._run_image_translation(job)
    if background_first:
        finish(db, run, item, product)
    assert_failed(case)
    assert "提交" in item.process_reason
    assert upstream.saves == []


@pytest.mark.parametrize("failure", ["unknown", "upload", "readback", "upstream-change"])
def test_partial_or_unverified_image_results_never_enable_publish(case, monkeypatch, failure):
    db, run, item, product, job, upstream = case
    finish(db, run, item, product)
    if failure == "unknown":
        original_attach = warehouse_service.attach_image_translation

        def unknown(session, *args, **kwargs):
            result = original_attach(session, *args, **kwargs)
            image = session.get(ImageJob, result["jobs"][0]["id"])
            image.status = "unknown"
            return result

        monkeypatch.setattr(warehouse_service, "attach_image_translation", unknown)
    elif failure == "upload":
        monkeypatch.setattr(warehouse_service, "apply_image_results",
                            lambda *_args, **_kwargs: {"replaced": 0, "missing": [SOURCE]})
    elif failure == "readback":
        upstream.corrupt_readback = True
    else:
        upstream.payload["title"] = "Another edit"
    collect_box._run_image_translation(job)
    assert_failed(case)
    if failure != "readback":
        assert upstream.saves == []


@pytest.mark.parametrize("change", ["generation", "revision", "binding"])
def test_stale_worker_never_overwrites_new_work_or_an_unbound_account(case, change):
    db, run, item, product, job, upstream = case
    finish(db, run, item, product)
    if change == "generation":
        product.automation_images = {**product.automation_images, "generation": uuid.uuid4().hex}
    elif change == "revision":
        product.revision += 1
    else:
        account = db.get(Account, product.account_id)
        account.owner_operator_id = None
    db.commit()
    collect_box._run_image_translation(job)
    db.expire_all()
    assert upstream.saves == []
    assert product.payload["imgUrls"] == [SOURCE]
    if change == "generation":
        assert product.automation_images["queued"] is True
        assert product.automation_images["generation"] != job["generation"]
    else:
        assert_failed(case)


def test_recovery_expires_interrupted_work_without_resubmitting_billable_images(case):
    db, run, item, product, job, upstream = case
    finish(db, run, item, product)
    product.automation_images = {**product.automation_images, "startedAt": now_ts() - 700}
    db.commit()
    recovered = automation_run_service.recover(db, run)
    assert recovered["status"] == "needs_review"
    assert_failed(case)
    # A delayed worker from the expired generation must not re-authorize the run.
    collect_box._run_image_translation(job)
    db.expire_all()
    assert product.draft_verified_revision is None
    assert upstream.saves == []
    assert db.query(ImageJob).filter(ImageJob.product_id == product.id).count() == 0


@pytest.mark.parametrize("stage", ["ocr", "submit"])
@pytest.mark.parametrize("change", ["expired", "new-generation"])
def test_slow_work_cannot_restore_an_expired_or_replaced_generation(
        case, monkeypatch, stage, change):
    db, run, item, product, job, upstream = case
    finish(db, run, item, product)
    expected = {**product.automation_images, "queued": False, "completed": True,
                "failed": 1, "reason": "Interrupted"}
    if change == "new-generation":
        expected = {**product.automation_images, "generation": uuid.uuid4().hex}

    def transition(session):
        current = session.get(Product, product.id)
        current.automation_images = deepcopy(expected)
        session.commit()

    if stage == "ocr":
        def select(*_args, **_kwargs):
            transition(db)
            return {"main": [SOURCE], "sku": [], "detail": []}

        monkeypatch.setattr(warehouse_service, "select_translatable_images", select)
    else:
        original_attach = warehouse_service.attach_image_translation

        def attach(session, *args, **kwargs):
            result = original_attach(session, *args, **kwargs)
            # A commit inside a submitter releases session locks. Recheck its result.
            transition(session)
            return result

        monkeypatch.setattr(warehouse_service, "attach_image_translation", attach)

    collect_box._run_image_translation(job)
    db.expire_all()
    assert product.automation_images == expected
    assert upstream.saves == []
    assert product.draft_verified_revision is None
    image_count = db.query(ImageJob).filter(ImageJob.product_id == product.id).count()
    assert image_count == (stage == "submit")


def test_unbinding_during_ocr_prevents_billable_submission(case, monkeypatch):
    db, run, item, product, job, upstream = case
    finish(db, run, item, product)

    def select(*_args, **_kwargs):
        account = db.get(Account, product.account_id)
        account.owner_operator_id = None
        db.commit()
        return {"main": [SOURCE], "sku": [], "detail": []}

    monkeypatch.setattr(warehouse_service, "select_translatable_images", select)
    collect_box._run_image_translation(job)
    assert_failed(case)
    assert upstream.saves == []
    assert db.query(ImageJob).filter(ImageJob.product_id == product.id).count() == 0
