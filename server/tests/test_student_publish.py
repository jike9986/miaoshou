from __future__ import annotations

import uuid

import pytest

from core import errors
from db.session import SessionLocal, init_db
from models.account import Account
from models.automation_run import AutomationRun, AutomationRunItem
from models.image_job import ImageJob
from models.product import Product
from models.product_claim import ProductClaim
from models.publish_log import PublishLog
from services import operator_service, publish_service, setting_service, warehouse_service
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
