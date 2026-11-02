"""Per-operator processing preferences must not leak between learners."""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from db.session import SessionLocal, init_db
from main import app
from services import operator_service, setting_service, warehouse_service

API = "/api/v1"


def test_member_workflow_preferences_are_private_and_used_for_ingest():
    init_db()
    db = SessionLocal()
    try:
        first, first_token = operator_service.create(
            db, f"workflow-first-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        second, second_token = operator_service.create(
            db, f"workflow-second-{uuid.uuid4().hex[:8]}",
            role=operator_service.ROLE_MEMBER)
        first_id = first.id
        shared_defaults = setting_service.workflow_defaults(db)
        db.commit()
    finally:
        db.close()

    client = TestClient(app, raise_server_exceptions=False)
    first_headers = {"Authorization": f"Bearer {first_token}"}
    second_headers = {"Authorization": f"Bearer {second_token}"}
    custom_weight = 1.731 if shared_defaults.get("weightIncrement") != 1.731 else 1.732
    personal = {
        "weightIncrement": custom_weight,
        "packageDimensions": {
            "packageLength": 41, "packageWidth": 22, "packageHeight": 13,
        },
        "sizeChartUrl": "https://example.test/first-size-chart.png",
    }
    try:
        saved = client.post(
            API + "/system/settings", json={"workflowDefaults": personal},
            headers=first_headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["workflowDefaults"]["weightIncrement"] == custom_weight

        first_settings = client.get(API + "/system/settings", headers=first_headers)
        second_settings = client.get(API + "/system/settings", headers=second_headers)
        assert first_settings.status_code == second_settings.status_code == 200
        assert first_settings.json()["workflowDefaults"]["packageDimensions"] == personal[
            "packageDimensions"]
        assert second_settings.json()["workflowDefaults"] == shared_defaults

        first_charts = client.get(API + "/size-charts", headers=first_headers)
        second_charts = client.get(API + "/size-charts", headers=second_headers)
        assert first_charts.status_code == second_charts.status_code == 200
        assert first_charts.json()["defaultUrl"] == personal["sizeChartUrl"]
        assert second_charts.json()["defaultUrl"] == shared_defaults["sizeChartUrl"]

        db = SessionLocal()
        try:
            assert warehouse_service._workflow_setting(
                db, "weightIncrement", None, first_id) == custom_weight
        finally:
            db.close()

        forbidden = client.post(
            API + "/system/settings", json={"publishEnabled": True},
            headers=first_headers)
        assert forbidden.status_code == 403, forbidden.text
    finally:
        client.close()
