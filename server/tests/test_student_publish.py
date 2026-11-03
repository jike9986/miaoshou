from __future__ import annotations

import uuid

import pytest

from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from models.account_binding import AccountBinding
from models.automation_run import AutomationRun, AutomationRunItem
from models.image_job import ImageJob
from models.operator import Operator
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from services import (
    account_service,
    collect_box_service,
    operator_service,
    publish_service,
    setting_service,
    warehouse_service,
)
from services.price_template import DEFAULT_TEMPLATE


@pytest.fixture
def publish_db():
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _identity(db, suffix: str, *, role: str = operator_service.ROLE_MEMBER):
    operator, _token = operator_service.create(db, f"student-{suffix}", role=role)
    account = Account(name=f"account-{suffix}", shops="[]",
                      owner_operator_id=operator.id)
    db.add(account)
    db.flush()
    return operator, account


def _product(db, suffix: str, account: Account, *, detail_id: str = "12345",
             shop_id: str = "123") -> Product:
    row = Product(
        warehouse_key=f"{suffix}:source-shop", account_id=account.id,
        detail_id=detail_id, shop_id=shop_id, shop_name="Target shop",
        title="Finished title", status="ready", claimed=False,
        claimed_at=None, claimed_shop_id=None, collected={},
        price_min=10, price_max=10, payload={
            "weight": "0.5", "price": "10", "originPrice": "10",
            "skuMap": {"sku-1": {"price": "10", "originPrice": "10", "weight": "0.5"}},
        },
    )
    db.add(row)
    db.flush()
    return row


def _automation_run(db, suffix: str, operator, account: Account,
                    product: Product, *, publish_status: str) -> AutomationRunItem:
    run = AutomationRun(
        id=f"arun_{suffix}", owner_operator_id=operator.id,
        account_id=account.id, shop_id="123", status="running",
        settings={"publishEnabled": True},
    )
    db.add(run)
    db.flush()
    item = AutomationRunItem(
        run_id=run.id, key=f"{product.detail_id}:{product.shop_id}",
        detail_id=product.detail_id, source_shop_id=product.shop_id,
        product_id=product.id, process_status="succeeded",
        publish_status=publish_status,
    )
    db.add(item)
    db.flush()
    return item


def _publish_payload(product: Product, account: Account, **overrides):
    return {
        "confirm": True,
        "confirmUnverifiedSpecifications": True,
        "accountId": str(account.id),
        "shopId": "123",
        "template": "Student pricing",
        "items": [{"warehouseId": str(product.id)}],
        **overrides,
    }


def test_collect_box_claim_uses_stable_account_id_for_warehouse_status(
    publish_db, monkeypatch
):
    """A collect-box claim must be visible to the matching account filter.

    The collect-box route used to key ``Product.collected`` by the editable
    account name, while warehouse filters read the canonical ``account:{id}``
    key.  That made a successful claim still appear as unclaimed in the
    warehouse.  Keep the regression at the service boundary where both paths
    meet, without requiring a live Miaoshou request.
    """
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    account = Account(name=f"claim-status-{suffix}", shops="[]")
    db.add(account)
    db.flush()
    product = Product(
        warehouse_key="12345:123", account_id=account.id,
        detail_id="12345", shop_id="123", shop_name="Source shop",
        title="Finished title", status="ready", claimed=False,
        collected={}, payload={},
    )
    db.add(product)
    db.flush()

    class FakeClient:
        def claim_to_shop(self, detail_ids, shop_ids):
            assert detail_ids == [12345]
            assert shop_ids == [123]
            return {"ok": True}

        def close(self):
            return None

    monkeypatch.setattr(collect_box_service, "account_client",
                        lambda _account: FakeClient())
    monkeypatch.setattr(collect_box_service, "require_shop",
                        lambda _db, _account, shop_id: {"name": f"Shop {shop_id}"})

    result = collect_box_service.claim(
        db, account, detail_id="12345", shop_id="123", target_shop_id="123")
    db.commit()

    assert result["claimed"] is True
    assert product.collected == {
        f"account:{account.id}": {"at": product.collected[f"account:{account.id}"]["at"],
                                   "detailId": "12345"}
    }
    visible = warehouse_service.list_products(
        db, account_id=account.id, claim_account_id=account.id, claimed=True)
    assert {row["id"] for row in visible["items"]} == {str(product.id)}


def test_shared_miaoshou_account_claim_state_is_per_member(publish_db):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    first, _ = _identity(db, f"shared-claim-first-{suffix}")
    second, _ = _identity(db, f"shared-claim-second-{suffix}")
    teacher, _ = _identity(db, f"shared-claim-teacher-{suffix}",
                            role=operator_service.ROLE_TEACHER)

    shared_account = Account(
        name=f"shared-claim-account-{suffix}", owner_operator_id=first.id,
        shops="[]", is_default=False,
    )
    db.add(shared_account)
    db.flush()
    db.add_all([
        AccountBinding(account_id=shared_account.id, operator_id=first.id),
        AccountBinding(account_id=shared_account.id, operator_id=second.id),
    ])
    product = _product(db, f"shared-claim-product-{suffix}", shared_account)
    product.collected = {
        f"account:{shared_account.id}": {
            "at": 1_800_000_000, "detailId": product.detail_id,
        },
    }
    db.flush()
    warehouse_service.claim_product(db, product.id, first.id)
    db.flush()
    product_id = str(product.id)
    account_id = str(shared_account.id)

    first_list = warehouse_service.list_products(
        db, operator_id=first.id, claim_account_id=account_id,
        claimed=True, status="claimed")
    second_claimed = warehouse_service.list_products(
        db, operator_id=second.id, claim_account_id=account_id, claimed=True)
    second_unclaimed = warehouse_service.list_products(
        db, operator_id=second.id, claim_account_id=account_id,
        claimed=False, status="ready")

    assert product_id in {row["id"] for row in first_list["items"]}
    assert first_list["stats"]["totalClaimed"] == 1
    assert product_id not in {row["id"] for row in second_claimed["items"]}
    assert second_claimed["stats"]["totalClaimed"] == 0
    second_item = next(row for row in second_unclaimed["items"]
                       if row["id"] == product_id)
    assert second_item["claimed"] is False
    assert second_item["status"] == "ready"
    assert product_id in {row["id"] for row in second_unclaimed["items"]}

    # Teachers retain the existing account-level collected view and filters.
    teacher_list = warehouse_service.list_products(
        db, operator_id=teacher.id, claim_account_id=account_id,
        claimed=True, status="claimed", account_id=account_id)
    teacher_item = next(row for row in teacher_list["items"]
                        if row["id"] == product_id)
    assert teacher_item["claimed"] is True
    assert teacher_item["status"] == "ready"
    assert teacher_list["stats"]["totalClaimed"] == 1


def test_client_claim_tracks_successful_carriers_per_miaoshou_account(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    # Shared client tokens are transport credentials only; use an explicit
    # teacher identity for this service-level carrier bookkeeping test.
    operator = Operator(name=f"carrier-teacher-{suffix}", token=f"test-{suffix}",
                        role=operator_service.ROLE_TEACHER, enabled=True, is_admin=False)
    db.add(operator)
    db.flush()
    _, first_account = _identity(db, suffix + "a")
    _, second_account = _identity(db, suffix + "b")
    first = _product(db, suffix + "1", first_account, detail_id="10001")
    second = _product(db, suffix + "2", first_account, detail_id="10002")
    third = _product(db, suffix + "3", first_account, detail_id="10003")

    class FakeClient:
        def claim_to_shop(self, *_args):
            from services.miaoshou import MiaoshouError
            raise MiaoshouError("missing")

        def common_box_list(self, **_kwargs):
            return {"data": {"detailList": []}}

    monkeypatch.setattr(publish_service, "account_client_for",
                        lambda *_args: (first_account, FakeClient()))
    monkeypatch.setattr(publish_service, "require_shop",
                        lambda *_args: {"name": "shop", "site": "MY"})
    monkeypatch.setattr(publish_service, "hold_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service, "_in_flight_blocker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service, "_load_carrier_payloads", lambda *_args: [{}, {}, {}])
    monkeypatch.setattr(publish_service.carrier_service, "harvest_from_payloads",
                        lambda *_args: None)
    monkeypatch.setattr(publish_service.carrier_service, "collect_carriers",
                        lambda *_args, **_kwargs: [91, 92, 93])
    monkeypatch.setattr(publish_service.carrier_service, "fill_carriers",
                        lambda *_args, **_kwargs: {
                            "saved": [91, 93], "savedSources": [0, 2],
                            "failures": ["92 failed"],
                        })
    result = publish_service.publish(db, {"confirm": True, "claimOnly": True, "shopId": "123",
                                          "items": [{"warehouseId": str(p.id)}
                                                    for p in (first, second, third)]},
                                     _log_operator_id=operator.id)
    assert result["claimedWarehouseIds"] == [str(first.id), str(third.id)]
    key = f"account:{first_account.id}"
    assert first.collected[key]["detailId"] == "91"
    assert key not in second.collected
    assert third.collected[key]["detailId"] == "93"
    for account, expected in ((first_account, {str(first.id), str(third.id)}),
                              (second_account, set())):
        result = warehouse_service.list_products(db, operator_id=operator.id,
                                                  claim_account_id=account.id)
        visible = {item["id"] for item in result["items"] if item["claimed"]}
        assert visible == expected
        filtered = warehouse_service.list_products(db, operator_id=operator.id,
                                                    claim_account_id=account.id, claimed=True)
        assert {item["id"] for item in filtered["items"]} == expected
        assert result["stats"]["totalClaimed"] == len(expected)


def test_connecting_existing_account_switches_current_account(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    first = Account(name=f"first-{suffix}", app_key=f"key-first-{suffix}",
                    app_secret_enc=account_service.crypto.encrypt("secret-first"),
                    is_default=True, shops="[]")
    second = Account(name=f"second-{suffix}", app_key=f"key-second-{suffix}",
                     app_secret_enc=account_service.crypto.encrypt("secret-second"),
                     is_default=False, shops="[]")
    db.add_all([first, second])
    db.flush()

    class FakeClient:
        def __init__(self, *_args):
            pass

        def shop_list(self, **_kwargs):
            return {"data": {"shopList": []}}

        def close(self):
            pass

    monkeypatch.setattr(account_service, "MiaoshouClient", FakeClient)
    result = account_service.connect(db, account_id=str(second.id),
                                     app_id=None, app_secret=None,
                                     auto_scan=False)
    db.flush()

    assert result["accountId"] == str(second.id)
    assert account_service.current_account(db).id == second.id
    db.refresh(first)
    db.refresh(second)
    assert first.is_default is False
    assert second.is_default is True


def test_member_must_explicitly_acknowledge_unverified_specifications(
        publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account)

    payload = _publish_payload(product, account)
    payload.pop("confirmUnverifiedSpecifications")
    with pytest.raises(errors.AppError) as exc:
        publish_service.publish(db, payload, operator_id=operator.id)

    assert exc.value.code == "QUALITY_REVIEW_REQUIRED"
    assert exc.value.status == 409
    assert exc.value.details["items"] == [{
        "warehouseId": str(product.id),
        "reasons": ["missing_dimensions"],
    }]


def test_member_can_publish_verified_product_without_quality_override(
        publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    product.quality_review = {
        "verifiedRevision": product.revision,
        "source": "source inspection",
        "evidence": "confirmed package measurements",
    }
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account)

    payload = _publish_payload(product, account)
    payload.pop("confirmUnverifiedSpecifications")
    result = publish_service.publish(db, payload, operator_id=operator.id)

    assert result["published"] == ["98765"]


def _stub_member_publish(monkeypatch, db, account, *, received_payloads: list | None = None,
                         target_shop_id: str = "123"):
    class FakeClient:
        def publish_items(self, detail_ids, shop_ids):
            self.published = (detail_ids, shop_ids)

    client = FakeClient()
    monkeypatch.setattr(publish_service, "account_client_for", lambda *_args: (account, client))
    monkeypatch.setattr(publish_service, "require_shop", lambda *_args: {
        "shopId": target_shop_id, "name": "Target shop", "site": "MY",
    })
    monkeypatch.setattr(publish_service.setting_service, "publish_enabled", lambda *_args: True)
    monkeypatch.setattr(publish_service, "_in_flight_blocker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service, "hold_for_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service.carrier_service, "harvest_from_payloads",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(publish_service.carrier_service, "collect_carriers",
                        lambda *_args, **_kwargs: [90001])

    def fill_carriers(_client, _carriers, payloads, *, shop_id):
        if received_payloads is not None:
            received_payloads.extend(payloads)
        assert shop_id == int(target_shop_id)
        return {"saved": [98765], "savedSources": [0], "failures": []}

    monkeypatch.setattr(publish_service.carrier_service, "fill_carriers", fill_carriers)
    template = dict(DEFAULT_TEMPLATE)
    template.update({"name": "Student pricing", "site": "MY", "profitPercent": 10})
    setting_service.save_template(db, template)
    return client


def _stub_guarded_publish(monkeypatch, db, account, *, target_shop_id="123"):
    blocker = publish_service._in_flight_blocker
    hold = publish_service.hold_for_session
    client = _stub_member_publish(monkeypatch, db, account, target_shop_id=target_shop_id)
    monkeypatch.setattr(publish_service, "_in_flight_blocker", blocker)
    monkeypatch.setattr(publish_service, "hold_for_session", hold)
    return client


@pytest.mark.parametrize("role", [operator_service.ROLE_MEMBER, operator_service.ROLE_TEACHER])
@pytest.mark.parametrize("failure", ["timeout", "crash", "caller_rollback"])
@pytest.mark.parametrize("source_shop_id", ["123", "456"])
def test_publish_intent_survives_unknown_result_and_caller_rollback(
        publish_db, monkeypatch, role, failure, source_shop_id):
    from services.miaoshou import MiaoshouError

    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix, role=role)
    product = _product(db, suffix, account, shop_id=source_shop_id)
    if role == operator_service.ROLE_MEMBER:
        db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
        db.flush()
    client = _stub_guarded_publish(monkeypatch, db, account)
    product_id, account_id, operator_id = product.id, account.id, operator.id
    expected_detail_id = (product.detail_id
                          if role == operator_service.ROLE_TEACHER and source_shop_id == "123"
                          else "98765")
    calls = []

    def submit(detail_ids, shop_ids):
        calls.append((detail_ids, shop_ids))
        # A separate transaction must see the exact intent before the HTTP call.
        with SessionLocal() as other:
            log = other.query(PublishLog).filter_by(
                account_id=account_id, operator_id=operator_id, product_id=product_id).one()
            assert log.status == "processing"
            assert log.detail_id == expected_detail_id
            assert log.shop_id == "123"
            assert log.detail["claimOnly"] is False
        if failure == "timeout":
            raise MiaoshouError("read timeout after submission", kind="timeout")
        if failure == "crash":
            raise RuntimeError("process interrupted after submission")

    client.publish_items = submit
    payload = _publish_payload(product, account)
    if failure == "timeout":
        with pytest.raises(errors.AppError) as exc:
            publish_service.publish(db, payload, operator_id=operator_id)
        assert exc.value.code == errors.UPSTREAM_UNKNOWN_RESULT
    elif failure == "crash":
        with pytest.raises(RuntimeError, match="interrupted"):
            publish_service.publish(db, payload, operator_id=operator_id)
    else:
        result = publish_service.publish(db, payload, operator_id=operator_id)
        assert result["records"]["items"][0]["status"] == "processing"
    db.rollback()

    with pytest.raises(errors.AppError) as retry:
        publish_service.publish(db, payload, operator_id=operator_id)
    assert retry.value.code == "CONFLICT_BUSY"
    assert len(calls) == 1
    assert db.query(PublishLog).filter_by(product_id=product_id).one().status == "processing"


def test_publish_checkpoint_failure_never_submits_to_platform(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    teacher, account = _identity(db, suffix, role=operator_service.ROLE_TEACHER)
    product = _product(db, suffix, account)
    client = _stub_guarded_publish(monkeypatch, db, account)
    calls = []
    client.publish_items = lambda *_args: calls.append(True)

    def fail_commit():
        raise RuntimeError("cannot persist submission")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="cannot persist submission"):
        publish_service.publish(db, _publish_payload(product, account), operator_id=teacher.id)
    assert calls == []


@pytest.mark.parametrize("role", [operator_service.ROLE_MEMBER, operator_service.ROLE_TEACHER])
@pytest.mark.parametrize("remote_status", ["processing", "success", "fail", "cancel"])
def test_warehouse_published_state_requires_final_receipt_and_stays_owner_scoped(
        publish_db, monkeypatch, role, remote_status):
    from api import publish as publish_api

    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix, role=role)
    product = _product(db, suffix, account)
    if role == operator_service.ROLE_MEMBER:
        db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
        db.flush()
    client = _stub_guarded_publish(monkeypatch, db, account)
    revision = product.revision
    result = publish_service.publish(
        db, _publish_payload(product, account), operator_id=operator.id)
    assert result["records"]["items"][0]["status"] == "processing"
    assert product.status == "ready"
    assert product.claimed is False
    assert product.revision == revision

    client.publish_records = lambda **_kwargs: {"data": {"detailList": [{
        "detailId": result["published"][0], "shopId": "123", "status": remote_status,
    }]}}
    client.close = lambda: None
    refreshed = publish_api._refresh_processing(db, str(account.id), operator_id=operator.id)
    assert refreshed["updated"] == 1
    assert db.query(PublishLog).filter_by(product_id=product.id).one().status == remote_status
    globally_published = role == operator_service.ROLE_TEACHER and remote_status == "success"
    assert product.status == ("published" if globally_published else "ready")
    assert product.claimed is globally_published
    assert product.revision == revision + int(globally_published)
    if role == operator_service.ROLE_MEMBER:
        visible = warehouse_service.detail(db, product.id, operator_id=operator.id)["product"]
        assert visible["publishStatus"] == remote_status
        assert visible["status"] == ("published" if remote_status == "success" else "claimed")


@pytest.mark.parametrize("currently_verified", [False, True])
def test_final_receipt_keeps_current_content_verification_without_reviving_stale_checks(
        publish_db, currently_verified):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    teacher, account = _identity(db, suffix, role=operator_service.ROLE_TEACHER)
    product = _product(db, suffix, account)
    product.automation_result = True
    verified_revision = product.revision if currently_verified else product.revision - 1
    product.draft_verified_revision = verified_revision
    product.quality_review = {
        "verifiedRevision": verified_revision, "source": "source inspection",
    }
    _automation_run(db, suffix, teacher, account, product, publish_status="running")
    log = PublishLog(account_id=account.id, operator_id=teacher.id, product_id=product.id,
                     detail_id=product.detail_id, shop_id="123", status="success")
    db.add(log)
    db.flush()

    publish_service.reconcile_product_publish(db, log)

    assert product.status == "published"
    expected_verified_revision = product.revision if currently_verified else verified_revision
    assert product.draft_verified_revision == expected_verified_revision
    assert product.quality_review["verifiedRevision"] == expected_verified_revision
    result_ids = {row[0] for row in warehouse_service._verified_automation_product_ids(db).all()}
    assert (product.id in result_ids) is currently_verified


def test_legacy_carrier_processing_log_matches_source_product_and_target_shop(publish_db):
    from core.utils import product_key

    db = publish_db
    operator, account = _identity(db, uuid.uuid4().hex[:8])
    product = _product(db, uuid.uuid4().hex[:8], account, shop_id="456")
    db.add(PublishLog(account_id=account.id, operator_id=operator.id, product_id=product.id,
                      detail_id="98765", shop_id="123", warehouse_key=product.warehouse_key,
                      status="processing"))
    db.flush()
    with pytest.raises(errors.AppError) as exc:
        publish_service._in_flight_blocker(
            db, account=account, keys=[product_key(product.detail_id, "123")],
            operator_id=operator.id)
    assert exc.value.code == "CONFLICT_BUSY"
    publish_service._in_flight_blocker(
        db, account=account, keys=[product_key(product.detail_id, "789")],
        operator_id=operator.id)


@pytest.mark.parametrize("recent_retry", [False, True])
def test_unresolved_publish_does_not_expire_or_unlock_after_recent_retry(
        publish_db, recent_retry):
    from core.utils import now_ts, product_key

    db = publish_db
    operator, account = _identity(db, uuid.uuid4().hex[:8])
    row = PublishLog(account_id=account.id, operator_id=operator.id,
                     detail_id="98765", shop_id="123", warehouse_key="12345:123",
                     status="processing", requested_at=now_ts() - 86400,
                     last_requested_at=now_ts() if recent_retry else None)
    db.add(row)
    db.flush()

    with pytest.raises(errors.AppError) as exc:
        publish_service._in_flight_blocker(
            db, account=account, keys=[product_key("12345", "123")], operator_id=operator.id)
    assert exc.value.code == "CONFLICT_BUSY"


def test_definite_upstream_rejection_is_durable_and_can_be_retried(publish_db, monkeypatch):
    from services.miaoshou import MiaoshouError

    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.flush()
    client = _stub_guarded_publish(monkeypatch, db, account)
    payload = _publish_payload(product, account)

    def reject(*_args):
        raise MiaoshouError("invalid listing", code="INVALID_LISTING",
                            payload={"code": "INVALID_LISTING", "result": "fail"})

    client.publish_items = reject
    with pytest.raises(errors.AppError):
        publish_service.publish(db, payload, operator_id=operator.id)
    db.rollback()
    log = db.query(PublishLog).filter_by(product_id=product.id).one()
    assert log.status == "fail"
    assert log.finished_at is not None
    client.publish_items = lambda *_args: None
    publish_service.publish(db, payload, operator_id=operator.id)
    assert log.status == "processing"
    assert log.attempts == 2


@pytest.mark.parametrize("marker", [True, None, False])
def test_claim_only_logs_do_not_block_formal_publish_but_explicit_publish_does(
        publish_db, marker):
    from models.batch import Batch

    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    batch = Batch(id=f"clm_{suffix}", settings={"kind": "warehouse_claim"},
                  account_id=account.id, operator_id=operator.id)
    db.add(batch)
    db.flush()
    db.add(PublishLog(account_id=account.id, operator_id=operator.id,
                      detail_id="12345", shop_id="123", warehouse_key="12345:123",
                      batch_id=batch.id, status="processing",
                      detail={} if marker is None else {"claimOnly": marker}))
    db.flush()
    if marker is False:
        with pytest.raises(errors.AppError):
            publish_service._in_flight_blocker(
                db, account=account, keys=["12345:123"], operator_id=operator.id)
    else:
        publish_service._in_flight_blocker(
            db, account=account, keys=["12345:123"], operator_id=operator.id)


def test_member_can_publish_claimed_product_with_template_without_mutating_shared_product(
    publish_db, monkeypatch,
):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.flush()
    original_payload = dict(product.payload)
    received_payloads: list[dict] = []
    _stub_member_publish(monkeypatch, db, account, received_payloads=received_payloads)

    result = publish_service.publish(
        db, _publish_payload(product, account), operator_id=operator.id)

    assert result["mode"] == "carrier"
    assert result["repriced"]
    assert received_payloads[0]["skuMap"]["sku-1"]["price"] != "10"
    assert product.payload == original_payload
    assert product.status == "ready"
    assert product.claimed is False
    assert product.claimed_at is None
    assert product.claimed_shop_id is None
    log = db.query(PublishLog).filter(PublishLog.product_id == product.id).one()
    assert log.operator_id == operator.id


def test_shared_account_publish_ledgers_and_duplicate_guards_are_owner_scoped(
        publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    first, account = _identity(db, suffix)
    second, _ = _identity(db, f"second-{suffix}")
    product = _product(db, suffix, account)
    db.add_all([
        ProductClaim(product_id=product.id, owner_operator_id=first.id),
        ProductClaim(product_id=product.id, owner_operator_id=second.id),
    ])
    db.flush()
    _stub_member_publish(monkeypatch, db, account)

    publish_service.publish(db, _publish_payload(product, account),
                            operator_id=first.id)
    publish_service.publish(db, _publish_payload(product, account),
                            operator_id=second.id)

    rows = db.query(PublishLog).filter(
        PublishLog.account_id == account.id,
        PublishLog.product_id == product.id,
    ).all()
    assert {row.operator_id for row in rows} == {first.id, second.id}
    assert publish_service.list_logs(db, account_id=account.id,
                                     operator_id=first.id)["total"] == 1
    assert publish_service.list_logs(db, account_id=account.id,
                                     operator_id=second.id)["total"] == 1

    # The publish response is asynchronous and leaves each ledger row in
    # processing until the upstream reconciliation confirms success. Duplicate
    # publish protection applies only after that confirmation.
    for row in rows:
        row.status = "success"
    db.flush()

    with pytest.raises(errors.AppError) as duplicate:
        publish_service.publish(db, _publish_payload(product, account),
                                operator_id=first.id)
    assert duplicate.value.code == "ALREADY_PUBLISHED"


def test_automation_result_cannot_publish_before_run_item_is_started(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    teacher, account = _identity(db, suffix, role=operator_service.ROLE_TEACHER)
    product = _product(db, suffix, account)
    product.automation_result = True
    product.draft_verified_revision = product.revision
    _automation_run(db, suffix, teacher, account, product, publish_status="queued")
    _stub_member_publish(monkeypatch, db, account)

    with pytest.raises(errors.AppError) as exc:
        publish_service.publish(
            db, _publish_payload(product, account), operator_id=teacher.id)

    assert exc.value.status == 409


def test_automation_result_can_publish_after_run_item_is_started(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    teacher, account = _identity(db, suffix, role=operator_service.ROLE_TEACHER)
    product = _product(db, suffix, account)
    product.automation_result = True
    product.draft_verified_revision = product.revision
    item = _automation_run(db, suffix, teacher, account, product,
                           publish_status="running")
    run = db.get(AutomationRun, item.run_id)
    run.settings = {**run.settings,
                    "specificationConfirmations": {
                        str(product.id): int(product.revision or 0),
                    }}
    _stub_member_publish(monkeypatch, db, account)

    result = publish_service.publish(
        db, _publish_payload(product, account), operator_id=teacher.id)

    assert result["published"] == [product.detail_id]


def test_member_cannot_republish_successful_product_to_same_shop(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account)

    publish_service.publish(db, _publish_payload(product, account), operator_id=operator.id)
    log = db.query(PublishLog).filter(PublishLog.product_id == product.id).one()
    log.status = "success"
    db.flush()

    with pytest.raises(errors.AppError) as exc:
        publish_service.publish(db, _publish_payload(product, account), operator_id=operator.id)
    assert exc.value.code == "ALREADY_PUBLISHED"
    assert exc.value.status == 409


def test_member_can_publish_same_product_to_a_different_shop(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.add(PublishLog(account_id=account.id, product_id=product.id,
                      detail_id="98765", shop_id="123", title=product.title,
                      status="success", operator_id=operator.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account, target_shop_id="456")
    payload = _publish_payload(product, account, shopId="456")

    result = publish_service.publish(db, payload, operator_id=operator.id)

    assert result["targetShopId"] == "456"
    assert db.query(PublishLog).filter(
        PublishLog.product_id == product.id, PublishLog.shop_id == "456"
    ).one().status == "processing"


@pytest.mark.parametrize("status", ["fail", "cancel"])
def test_member_can_retry_failed_or_cancelled_product(publish_db, monkeypatch, status):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=operator.id))
    db.add(PublishLog(account_id=account.id, product_id=product.id,
                      detail_id="98765", shop_id="123", title=product.title,
                      status=status, operator_id=operator.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account)

    result = publish_service.publish(
        db, _publish_payload(product, account), operator_id=operator.id)

    assert result["published"] == ["98765"]


@pytest.mark.parametrize("case", ["unclaimed", "owned-by-other", "detail-id-bypass"])
def test_member_cannot_publish_unclaimed_foreign_or_direct_detail_item(
    publish_db, monkeypatch, case,
):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    if case == "owned-by-other":
        other, _ = _identity(db, f"other-{suffix}")
        db.add(ProductClaim(product_id=product.id, owner_operator_id=other.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, account)
    payload = _publish_payload(product, account)
    if case == "detail-id-bypass":
        payload["items"] = [{"detailId": "12345", "shopId": "123"}]

    with pytest.raises(errors.AppError) as exc:
        publish_service.publish(db, payload, operator_id=operator.id)
    assert exc.value.status == 403


def test_member_cannot_use_direct_claim_only_publish_request(publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    operator, account = _identity(db, suffix)
    product = _product(db, suffix, account)
    _stub_member_publish(monkeypatch, db, account)

    with pytest.raises(errors.AppError) as exc:
        publish_service.publish(
            db,
            {"confirm": True, "claimOnly": True, "accountId": str(account.id),
             "shopId": "123", "items": [{"warehouseId": str(product.id)}]},
            operator_id=operator.id,
        )

    assert exc.value.status == 403


def test_member_publish_status_and_history_are_owner_scoped(publish_db):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    first, account = _identity(db, suffix)
    second, second_account = _identity(db, f"second-{suffix}")
    product = _product(db, suffix, account)
    second_product = _product(db, f"second-{suffix}", second_account)
    db.add_all([
        ProductClaim(product_id=product.id, owner_operator_id=first.id),
        ProductClaim(product_id=second_product.id, owner_operator_id=second.id),
        # Retain a legacy cross-account log to verify it stays hidden.
        ProductClaim(product_id=product.id, owner_operator_id=second.id),
        PublishLog(account_id=account.id, product_id=product.id,
                   detail_id="12345", shop_id="456", title="first",
                   status="success", operator_id=first.id),
        PublishLog(account_id=account.id, product_id=product.id,
                   detail_id="12345", shop_id="789", title="other",
                   status="success", operator_id=second.id),
    ])
    product.status = "published"  # 模拟旧版/老师的共享全局状态
    db.flush()

    first_list = warehouse_service.list_products(db, operator_id=first.id)["items"]
    second_list = warehouse_service.list_products(db, operator_id=second.id)["items"]
    first_item = next(item for item in first_list if item["id"] == str(product.id))
    second_item = next(item for item in second_list
                       if item["id"] == str(second_product.id))
    assert first_item["status"] == "published"
    assert first_item["publishStatus"] == "success"
    assert first_item["publishedShopIds"] == ["456"]
    assert second_item["status"] == "claimed"
    assert second_item["publishStatus"] is None
    assert second_item["publishedShopIds"] == []
    second_shared_item = next(item for item in second_list
                              if item["id"] == str(product.id))
    assert second_shared_item["status"] == "claimed"
    assert second_shared_item["publishStatus"] is None
    assert warehouse_service.stats(db, operator_id=first.id)["totalPublished"] == 1
    assert warehouse_service.stats(db, operator_id=second.id)["totalPublished"] == 0

    first_detail = warehouse_service.detail(db, product.id, operator_id=first.id)
    assert [row["shopId"] for row in first_detail["publishHistory"]] == ["456"]
    assert first_detail["publishHistory"][0]["status"] == "success"
    assert warehouse_service.detail(db, second_product.id,
                                    operator_id=second.id)["publishHistory"] == []
    assert warehouse_service.detail(db, product.id,
                                    operator_id=second.id)["publishHistory"] == []
    assert warehouse_service.detail(db, product.id,
                                    operator_id=first.id)["product"]["status"] == "published"


def test_shared_finished_goods_are_visible_and_unadmitted_results_are_isolated(publish_db):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    member, own_account = _identity(db, suffix)
    other, other_account = _identity(db, f"other-{suffix}")
    teacher, _ = _identity(db, f"teacher-{suffix}", role=operator_service.ROLE_TEACHER)
    own_product = _product(db, f"own-{suffix}", own_account)
    other_product = _product(db, f"other-{suffix}", other_account)
    other_product.claimed = True
    other_product.collected = {
        other_account.name: {"at": 1_800_000_000, "detailId": other_product.detail_id},
    }
    hidden_result = _product(db, f"hidden-result-{suffix}", other_account,
                             detail_id=f"hidden-result-{suffix}")
    hidden_result.automation_result = True
    own_result = _product(db, f"own-result-{suffix}", own_account,
                          detail_id=f"own-result-{suffix}")
    own_result.automation_result = True
    _automation_run(db, f"own-result-{suffix}", member, own_account, own_result,
                    publish_status="not_requested")

    visible = warehouse_service.list_products(db, operator_id=member.id, page_size=100)
    visible_ids = {row["id"] for row in visible["items"]}
    assert str(own_product.id) in visible_ids
    assert str(other_product.id) in visible_ids
    assert str(hidden_result.id) not in visible_ids
    assert str(own_result.id) not in visible_ids
    shared_item = next(row for row in visible["items"]
                       if row["id"] == str(other_product.id))
    assert shared_item["accountId"] is None
    assert shared_item["account"] == ""
    assert shared_item["collected"] == {}
    assert shared_item["legacyClaimed"] is True
    assert visible["facets"]["accounts"] == []

    # The account filter is validated as the member's selected publish account,
    # but it must not remove shared finished goods from the common catalogue.
    filtered = warehouse_service.list_products(
        db, operator_id=member.id, account_id=str(own_account.id), page_size=100)
    assert str(other_product.id) in {row["id"] for row in filtered["items"]}
    assert filtered["stats"]["totalProducts"] >= 2

    shared_detail = warehouse_service.detail(db, other_product.id, operator_id=member.id)
    assert shared_detail["product"]["accountId"] is None
    assert shared_detail["product"]["collected"] == {}
    teacher_detail = warehouse_service.detail(db, other_product.id, operator_id=teacher.id)
    assert teacher_detail["product"]["collected"] == other_product.collected
    assert teacher_detail["product"]["legacyClaimed"] is True

    with pytest.raises(errors.AppError) as exc:
        warehouse_service.detail(db, hidden_result.id, operator_id=member.id)
    assert exc.value.status == 403

    assert warehouse_service.detail(
        db, own_result.id, operator_id=member.id)["product"]["id"] == str(own_result.id)

    with pytest.raises(errors.AppError) as exc:
        warehouse_service.claim_product(db, hidden_result.id, member.id)
    assert exc.value.status == 403

    with pytest.raises(errors.AppError) as exc:
        warehouse_service.claim_product(db, own_result.id, member.id)
    assert exc.value.status == 400

    first_claim = warehouse_service.claim_product(db, other_product.id, member.id)
    second_claim = warehouse_service.claim_product(db, other_product.id, other.id)
    assert first_claim.owner_operator_id == member.id
    assert second_claim.owner_operator_id == other.id

    teacher_rows = warehouse_service.list_products(
        db, operator_id=teacher.id, account_id=str(other_account.id))
    assert str(other_product.id) in {row["id"] for row in teacher_rows["items"]}
    teacher_detail = warehouse_service.detail(
        db, other_product.id, operator_id=teacher.id)
    assert teacher_detail["product"]["id"] == str(other_product.id)
    claim = warehouse_service.claim_product(db, other_product.id, teacher.id)
    assert claim.owner_operator_id == teacher.id


def test_product_payload_redacts_credentials_for_members_and_teachers(publish_db):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    member, _member_account = _identity(db, suffix)
    source_member, source_account = _identity(db, f"source-{suffix}")
    teacher, _ = _identity(db, f"teacher-{suffix}", role=operator_service.ROLE_TEACHER)
    product = _product(db, f"payload-{suffix}", source_account)
    product.payload = {
        "weight": "0.5",
        "appKey": "upstream-app-key",
        "app_secret": "upstream-app-secret",
        "nested": {
            "access_token": "access-token",
            "accountId": str(source_account.id),
            "ownerOperatorId": source_member.id,
            "skuMap": {"sku-1": {"price": "10", "weight": "0.5"}},
        },
    }
    db.flush()

    for operator in (member, teacher):
        body = warehouse_service.detail(
            db, product.id, include_payload=True, operator_id=operator.id)
        payload = body["product"]["payload"]
        assert payload["weight"] == "0.5"
        assert payload["nested"]["skuMap"]["sku-1"]["price"] == "10"
        assert "appKey" not in payload
        assert "app_secret" not in payload
        assert "access_token" not in payload["nested"]
        if operator is member:
            assert "accountId" not in payload["nested"]
            assert "ownerOperatorId" not in payload["nested"]
        else:
            assert payload["nested"]["accountId"] == str(source_account.id)
            assert payload["nested"]["ownerOperatorId"] == source_member.id


def test_members_can_publish_shared_finished_good_with_their_own_accounts(
        publish_db, monkeypatch):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    member, own_account = _identity(db, suffix)
    other, other_account = _identity(db, f"other-{suffix}")
    product = _product(db, f"foreign-{suffix}", other_account)
    db.add(ProductClaim(product_id=product.id, owner_operator_id=member.id))
    db.add(ProductClaim(product_id=product.id, owner_operator_id=other.id))
    db.flush()
    _stub_member_publish(monkeypatch, db, own_account)
    result = publish_service.publish(
        db, _publish_payload(product, own_account), operator_id=member.id)
    assert result["published"] == ["98765"]
    log = db.query(PublishLog).filter(PublishLog.product_id == product.id).one()
    assert log.account_id == own_account.id
    assert log.operator_id == member.id

    # Each claimant publishes through their separately bound account.
    _stub_member_publish(monkeypatch, db, other_account, target_shop_id="789")
    result = publish_service.publish(
        db, _publish_payload(product, other_account, shopId="789"),
        operator_id=other.id)
    assert result["published"] == ["98765"]
    assert db.query(PublishLog).filter(
        PublishLog.product_id == product.id,
        PublishLog.operator_id == other.id,
    ).one().account_id == other_account.id


def test_member_product_detail_only_exposes_own_image_jobs(publish_db):
    db = publish_db
    suffix = uuid.uuid4().hex[:8]
    first, account = _identity(db, suffix)
    second, second_account = _identity(db, f"second-{suffix}")
    teacher, _ = _identity(db, f"teacher-{suffix}", role=operator_service.ROLE_TEACHER)
    product = _product(db, suffix, account)
    second_product = _product(db, f"second-{suffix}", second_account)
    own_id = f"own-{suffix}"
    foreign_id = f"foreign-{suffix}"
    legacy_id = f"legacy-{suffix}"
    db.add_all([
        ImageJob(id=own_id, product_id=product.id, operator_id=first.id,
                 filename="own.png", reserved_milliyuan=24),
        ImageJob(id=foreign_id, product_id=second_product.id, operator_id=second.id,
                 filename="foreign.png", reserved_milliyuan=240),
        ImageJob(id=legacy_id, product_id=product.id, operator_id=None,
                 filename="legacy.png", reserved_milliyuan=120),
    ])
    db.flush()

    first_detail = warehouse_service.detail(db, product.id, operator_id=first.id)
    second_detail = warehouse_service.detail(db, product.id, operator_id=second.id)
    teacher_detail = warehouse_service.detail(db, product.id, operator_id=teacher.id)
    teacher_second_detail = warehouse_service.detail(
        db, second_product.id, operator_id=teacher.id)

    assert {job["id"] for job in first_detail["imageJobs"]} == {own_id}
    assert first_detail["imageJobs"][0]["filename"] == "own.png"
    assert second_detail["imageJobs"] == []
    assert {job["id"] for job in teacher_detail["imageJobs"]} == {own_id, legacy_id}
    assert {job["id"] for job in teacher_second_detail["imageJobs"]} == {foreign_id}
