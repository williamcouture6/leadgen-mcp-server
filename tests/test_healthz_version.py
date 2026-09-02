"""`/healthz` dit AUSSI quelle version tourne.

Sans ça, rien ne permet de savoir si un déploiement a bien pris : le service
répondait `{"status": "ok"}` avec l'ancien code comme avec le nouveau, et la seule
façon de trancher était d'aller lire le SHA dans le tableau de bord Railway. Un
`curl` doit suffire — surtout au moment d'activer un workflow n8n en se fiant à
du code qu'on croit déployé.

Railway injecte `RAILWAY_GIT_COMMIT_SHA` tout seul ; en local la variable n'existe
pas, et l'endpoint doit quand même répondre.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AGENTS_HTTP_TOKEN", "t")
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    from src import http_api
    return http_api, TestClient(http_api.app)


def test_healthz_annonce_le_commit_deploye(client, monkeypatch) -> None:
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "1cfd437aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    http_api, tc = client
    body = tc.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["commit"] == "1cfd437"


def test_healthz_repond_meme_sans_la_variable(client, monkeypatch) -> None:
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    http_api, tc = client
    body = tc.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["commit"] == "unknown"
