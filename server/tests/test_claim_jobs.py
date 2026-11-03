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
            raise AssertionError("cross-account access must fail")
        except errors.AppError as exc:
            assert exc.code == errors.NOT_FOUND
        calls = []

        def publish(_db, payload, **_kwargs):
            calls.append([item["warehouseId"] for item in payload["items"]])
            assert payload["forceRebuild"] is True
            if len(calls) == 2:
                raise RuntimeError("timeout")
            return {"claimedWarehouseIds": calls[-1], "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)
        assert [len(call) for call in calls] == [100, 100]
        assert batch.status == "failed"
        assert [item.status for item in items] == (
            ["ok"] * 100 + ["failed"] * 100 + ["pending"] * 154)
        assert batch.current_index == 200
        assert batch_service.list_batches(db, account.id)["total"] == 0
    finally:
        db.close()


def test_known_item_failures_map_by_warehouse_id_and_source_index_then_continue(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="claim-partial-test", app_key="claim-partial-test")
        db.add(account)
        db.commit()
        body = {"confirm": True, "claimOnly": True, "shopId": "1",
                "accountId": str(account.id),
                "items": [{"warehouseId": str(i)} for i in range(1, 102)]}
        batch = claim_job_service.create(db, body, account.id, None)
        db.commit()
        calls = []

        def publish(_db, payload, **_kwargs):
            chunk = payload["items"]
            calls.append([item["warehouseId"] for item in chunk])
            if len(calls) == 1:
                succeeded = [value for value in calls[-1] if value not in {"3", "5"}]
                return {
                    "claimedWarehouseIds": succeeded,
                    "failures": [
                        {"warehouseId": "3", "reason": "详情图超过限制"},
                        {"sourceIndex": 4, "reason": "SKU规格重复"},
                    ],
                }
            return {"claimedWarehouseIds": calls[-1], "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)

        assert [len(call) for call in calls] == [100, 1]
        assert batch.status == "failed"
        assert batch.current_index == 101
        assert items[2].status == "failed"
        assert items[2].message == "详情图超过限制"
        assert items[4].status == "failed"
        assert items[4].message == "SKU规格重复"
        assert items[100].status == "ok"
        assert sum(item.status == "ok" for item in items) == 99
        assert sum(item.status == "failed" for item in items) == 2
    finally:
        db.close()


def test_legacy_aggregate_failure_receipt_keeps_later_chunks_running(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="claim-legacy-partial-test",
                          app_key="claim-legacy-partial-test")
        db.add(account)
        db.commit()
        body = {"confirm": True, "claimOnly": True, "shopId": "1",
                "accountId": str(account.id),
                "items": [{"warehouseId": str(i)} for i in range(1, 102)]}
        batch = claim_job_service.create(db, body, account.id, None)
        db.commit()
        calls = []

        def publish(_db, payload, **_kwargs):
            chunk_ids = [item["warehouseId"] for item in payload["items"]]
            calls.append(chunk_ids)
            if len(calls) == 1:
                return {"claimedWarehouseIds": chunk_ids[:-1],
                        "failures": ["商品保存失败"]}
            return {"claimedWarehouseIds": chunk_ids, "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)

        assert [len(call) for call in calls] == [100, 1]
        assert batch.status == "failed"
        assert batch.current_index == 101
        assert items[99].status == "failed"
        assert items[99].message == "商品保存失败"
        assert items[100].status == "ok"
    finally:
        db.close()


def test_unconfirmed_collect_box_result_stops_without_running_next_chunk(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="claim-unknown-result-test",
                          app_key="claim-unknown-result-test")
        db.add(account)
        db.commit()
        body = {"confirm": True, "claimOnly": True, "shopId": "1",
                "accountId": str(account.id),
                "items": [{"warehouseId": str(i)} for i in range(1, 102)]}
        batch = claim_job_service.create(db, body, account.id, None)
        db.commit()
        calls = []

        def publish(_db, payload, **_kwargs):
            chunk_ids = [item["warehouseId"] for item in payload["items"]]
            calls.append(chunk_ids)
            if len(calls) == 1:
                return {
                    "claimedWarehouseIds": chunk_ids[:-1],
                    "failures": [{"stage": "collect_box_verify",
                                  "detailId": "new-draft",
                                  "reason": "已提交，但采集箱尚未确认到该商品"}],
                }
            return {"claimedWarehouseIds": chunk_ids, "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)

        assert [len(call) for call in calls] == [100]
        assert batch.status == "failed"
        assert batch.current_index == 100
        assert [item.status for item in items[:99]] == ["ok"] * 99
        assert items[99].status == "failed"
        assert "结果未知" in items[99].message
        assert items[100].status == "pending"
    finally:
        db.close()


def test_mapped_but_uncertain_failure_stops_without_running_next_chunk(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="claim-unknown-item-test",
                          app_key="claim-unknown-item-test")
        db.add(account)
        db.commit()
        batch = claim_job_service.create(db, {
            "confirm": True, "claimOnly": True, "shopId": "1",
            "accountId": str(account.id),
            "items": [{"warehouseId": str(i)} for i in range(1, 102)],
        }, account.id, None)
        db.commit()
        calls = []

        def publish(_db, payload, **_kwargs):
            calls.append(payload["items"])
            chunk = [item["warehouseId"] for item in payload["items"]]
            return {"claimedWarehouseIds": chunk[:-1], "failures": [{
                "warehouseId": chunk[-1], "certain": False,
                "reason": "妙手接口超时",
            }]}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)

        assert len(calls) == 1
        assert batch.status == "failed"
        assert batch.current_index == 100
        assert items[99].status == "failed"
        assert "结果未知" in items[99].message
        assert items[100].status == "pending"
    finally:
        db.close()


def test_legacy_failure_with_multiple_unmapped_items_stops_job(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="claim-ambiguous-legacy-test",
                          app_key="claim-ambiguous-legacy-test")
        db.add(account)
        db.commit()
        body = {"confirm": True, "claimOnly": True, "shopId": "1",
                "accountId": str(account.id),
                "items": [{"warehouseId": str(i)} for i in range(1, 102)]}
        batch = claim_job_service.create(db, body, account.id, None)
        db.commit()
        calls = []

        def publish(_db, payload, **_kwargs):
            chunk_ids = [item["warehouseId"] for item in payload["items"]]
            calls.append(chunk_ids)
            if len(calls) == 1:
                return {"claimedWarehouseIds": chunk_ids[:-2],
                        "failures": ["商品保存失败"]}
            return {"claimedWarehouseIds": chunk_ids, "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        batch = claim_job_service.get(db, batch.id, account.id)
        items = batch_service.items_of(db, batch.id)

        assert [len(call) for call in calls] == [100]
        assert batch.status == "failed"
        assert batch.current_index == 100
        assert [item.status for item in items[:98]] == ["ok"] * 98
        assert [item.status for item in items[98:100]] == ["failed"] * 2
        assert all("结果未知" in item.message for item in items[98:100])
        assert items[100].status == "pending"
    finally:
        db.close()


def test_failure_identity_mismatch_is_unknown():
    chunk = [{"warehouseId": str(value)} for value in range(1, 4)]
    result = {
        "claimedWarehouseIds": ["1", "2"],
        "failures": [{"warehouseId": "3", "sourceIndex": 1,
                      "reason": "详情图超过限制"}],
    }

    succeeded, messages, known = claim_job_service._claim_chunk_outcome(result, chunk)

    assert succeeded == {"1", "2"}
    assert messages == {}
    assert known is False


def test_mixed_failure_receipt_preserves_mapped_reason_and_stops_job(monkeypatch):
    init_db()
    db = SessionLocal()
    try:
        account = Account(name="claim-mixed-failure-test",
                          app_key="claim-mixed-failure-test")
        db.add(account)
        db.commit()
        batch = claim_job_service.create(db, {
            "confirm": True, "claimOnly": True, "shopId": "1",
            "accountId": str(account.id),
            "items": [{"warehouseId": str(i)} for i in range(1, 102)],
        }, account.id, None)
        db.commit()
        calls = []

        def publish(_db, payload, **_kwargs):
            chunk_ids = [item["warehouseId"] for item in payload["items"]]
            calls.append(chunk_ids)
            if len(calls) == 1:
                return {
                    "claimedWarehouseIds": chunk_ids[:-2],
                    "failures": [
                        {"warehouseId": chunk_ids[-2], "reason": "详情图超过限制"},
                        "上游返回了无法匹配到商品的错误",
                    ],
                }
            return {"claimedWarehouseIds": chunk_ids, "failures": []}

        monkeypatch.setattr(claim_job_service.publish_service, "publish", publish)
        claim_job_service.run(batch.id)
        db.expire_all()
        items = batch_service.items_of(db, batch.id)

        assert [len(call) for call in calls] == [100]
        assert items[98].status == "failed"
        assert items[98].message == "详情图超过限制"
        assert items[99].status == "failed"
        assert "结果未知" in items[99].message
        assert items[100].status == "pending"
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
