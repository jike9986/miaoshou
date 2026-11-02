from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from services import batch_service, claim_job_service


def test_large_job_is_scoped_and_stops_on_unknown(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="job-test", app_key="claim-job-test")
        other = Account(name="job-other", app_key="claim-job-other")
        db.add_all([account, other])
        db.commit()
        body = {"confirm": True, "claimOnly": True, "shopId": "1",
                "accountId": str(account.id),
                "items": [{"warehouseId": str(i)} for i in range(1, 355)]}
        batch = claim_job_service.create(db, body, account.id, None)
        db.commit()
        assert len(batch_service.items_of(db, batch.id)) == 354
        assert claim_job_service.list_jobs(db, other.id, 50)["total"] == 0
        try:
            claim_job_service.get(db, batch.id, other.id)
            assert False, "cross-account access must fail"
        except errors.AppError as exc:
            assert exc.code == errors.NOT_FOUND
        calls = []

        def publish(_db, payload, **_kwargs):
            calls.append(payload["items"][0]["warehouseId"])
            if len(calls) == 2:
                raise RuntimeError("timeout")
            return {"claimed": ["99"], "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)
        assert calls == ["1", "2"]
        assert batch.status == "failed"
        assert [item.status for item in items[:3]] == ["ok", "failed", "pending"]
        assert batch.current_index == 2
        assert batch_service.list_batches(db, account.id)["total"] == 0
    finally:
        db.close()


def test_recovery_is_opt_in_and_never_retries(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="recovery-test", app_key="claim-recovery-test")
        db.add(account)
        db.commit()
        batch = claim_job_service.create(db, {
            "confirm": True, "shopId": "1", "items": [
                {"warehouseId": "1"}, {"warehouseId": "2"}, {"warehouseId": "3"},
            ],
        }, account.id, None)
        items = batch_service.items_of(db, batch.id)
        batch.status = "running"
        batch.current_index = 1
        batch.results = [{"detailId": "1", "status": "ok"}]
        items[0].status = "ok"
        items[1].status = "running"
        db.commit()
        monkeypatch.delenv("AOHUANG_CLAIM_RECOVER_SINGLE_WORKER", raising=False)
        assert claim_job_service.recover_interrupted_on_startup() == 0
        monkeypatch.setenv("AOHUANG_CLAIM_RECOVER_SINGLE_WORKER", "1")
        assert claim_job_service.recover_interrupted_on_startup() == 1
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)
        assert batch.status == "failed"
        assert batch.current_index == 2
        assert [item.status for item in items] == ["ok", "failed", "pending"]
        assert "结果未知" in items[1].message
        assert claim_job_service.recover_interrupted_on_startup() == 0
    finally:
        db.close()
