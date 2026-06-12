import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTS_HTTP_TOKEN", "t")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    from src import http_api
    return http_api, TestClient(http_api.app)


def test_brandkit_endpoint_calls_build(client, monkeypatch):
    http_api, tc = client

    async def fake_build(company_id, model="claude-sonnet-4-6"):
        return {"company_id": company_id, "status": "ok",
                "fields_filled": ["logo_url"], "confidence": {"logo_url": "medium"}}
    monkeypatch.setattr(http_api.brand_kit_tools, "build_brand_kit", fake_build)

    resp = tc.post("/research/brand-kit", json={"company_id": "c1"},
                   headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["fields_filled"] == ["logo_url"]


def test_brandkit_endpoint_requires_auth(client):
    _, tc = client
    resp = tc.post("/research/brand-kit", json={"company_id": "c1"})
    assert resp.status_code in (401, 403)
