import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTS_HTTP_TOKEN", "t")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    from src import http_api
    return http_api, TestClient(http_api.app)


def test_brandkit_endpoint_schedules_build(client, monkeypatch):
    http_api, tc = client
    called = {}
    async def fake_build(company_id, model="claude-sonnet-4-6"):
        called["company_id"] = company_id
        return {"company_id": company_id, "status": "ok", "fields_filled": [], "confidence": {}}
    monkeypatch.setattr(http_api.brand_kit_tools, "build_brand_kit", fake_build)

    resp = tc.post("/research/brand-kit", json={"company_id": "c1"},
                   headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    # TestClient exécute les BackgroundTasks après la réponse → build appelé.
    assert called.get("company_id") == "c1"


def test_brandkit_endpoint_requires_auth(client):
    _, tc = client
    resp = tc.post("/research/brand-kit", json={"company_id": "c1"})
    assert resp.status_code in (401, 403)
