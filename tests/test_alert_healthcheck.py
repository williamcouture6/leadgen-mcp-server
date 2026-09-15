"""Le canal d'alerte se vérifie sans provoquer de panne.

🔴 CE FICHIER EXISTE À CAUSE DU 2026-09-14. Ce jour-là, la vérification des
workflows a trouvé le chemin d'alerte MORT : `[OPS] Error Handler -> Slack`
était `active = false`, et n8n 2.36.8 refuse un workflow d'erreur inactif —

    Calling Error Workflow for "19SsXZXvOz8raG5V".
    Workflow "Zp68S5Kjc2boLCAh" is not active and cannot be executed

Une vraie panne (502 sur `/wf4/run`, le 2026-09-09) n'avait donc produit AUCUN
ping. Le trou avait tenu parce qu'il n'existait aucun moyen de vérifier le
chemin d'alerte **autrement qu'en cassant quelque chose pour voir** : les autres
healthchecks disent `slack_leads_configured` et `slack_bookings_configured`,
jamais `errors`.

⚠️ Ce healthcheck couvre la MOITIÉ SERVEUR du chemin (la variable d'env est-elle
posée). La moitié n8n — le workflow d'erreur est-il ACTIF — ne se voit que sur
le VPS ; `n8n/workflows/README.md` porte la commande.
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
    return TestClient(http_api.app)


AUTH = {"Authorization": "Bearer t"}


def test_le_canal_errors_dedie_suffit(client, monkeypatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_ERRORS", "https://hooks.slack.test/errors")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    body = client.get("/alert/healthcheck", headers=AUTH).json()
    assert body["ok"] is True
    assert body["slack_errors_configured"] is True
    assert body["via"] == "SLACK_WEBHOOK_ERRORS"


def test_le_fallback_compte_mais_se_dit(client, monkeypatch) -> None:
    """Sans var dédiée, `notify` retombe sur SLACK_WEBHOOK_URL : l'alerte PART.

    Le healthcheck doit donc rester vert — mentir en rouge ferait chercher une
    panne inexistante — mais nommer le chemin, parce que le repli poste dans le
    canal fourre-tout et non dans #alertes."""
    monkeypatch.delenv("SLACK_WEBHOOK_ERRORS", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/fallback")
    body = client.get("/alert/healthcheck", headers=AUTH).json()
    assert body["ok"] is True
    assert body["slack_errors_configured"] is True
    assert body["via"] == "SLACK_WEBHOOK_URL"


def test_rien_de_configure_est_rouge(client, monkeypatch) -> None:
    """Le cas qui doit crier : `/alert` répondrait `ok: true` en jetant le
    message, parce que `notify` sans URL ne lève pas. Seul ce healthcheck
    distingue « posté » de « avalé »."""
    monkeypatch.delenv("SLACK_WEBHOOK_ERRORS", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    body = client.get("/alert/healthcheck", headers=AUTH).json()
    assert body["ok"] is False
    assert body["slack_errors_configured"] is False
    assert body["via"] is None


def test_le_healthcheck_est_derriere_le_bearer(client, monkeypatch) -> None:
    """Il nomme des variables d'environnement : pas d'accès anonyme."""
    monkeypatch.setenv("SLACK_WEBHOOK_ERRORS", "https://hooks.slack.test/errors")
    assert client.get("/alert/healthcheck").status_code == 401
