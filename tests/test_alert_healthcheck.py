"""Le canal d'alerte se vérifie sans provoquer de panne.

🔴 CE FICHIER EXISTE À CAUSE DU 2026-09-14. Ce jour-là, la vérification des
workflows a trouvé le chemin d'alerte MORT : `[OPS] Error Handler -> Slack`
était `active = false`, et n8n 2.36.8 refuse un workflow d'erreur inactif —

    Calling Error Workflow for "19SsXZXvOz8raG5V".
    Workflow "Zp68S5Kjc2boLCAh" is not active and cannot be executed

Une vraie panne (502 sur `/wf4/run`, le 2026-09-09) n'avait donc produit AUCUN
ping. Le trou avait tenu parce que vérifier le chemin d'alerte **laissait une
trace** : il fallait soit casser quelque chose pour voir, soit poster un vrai
message par `/alert`. Un contrôle qui salit le canal qu'il contrôle ne se fait
pas. Et les autres healthchecks disent `slack_leads_configured` et
`slack_bookings_configured`, jamais `errors`.

⚠️ `/alert` ne ment pas — `notify` ne rend `True` que sur un `200 ok` de Slack.
Ce qu'il ne fait pas, c'est se laisser interroger gratuitement, séparer les
causes d'un `ok: false`, ou nommer le canal atteint.

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
    """Le cas qui doit crier.

    `/alert` le verrait aussi — `notify` sans URL rend `False`, pas `True` —
    mais son `ok: false` confond « rien de configuré » avec « Slack a refusé »
    et « réseau coupé ». Seul `via: null` désigne la cause qui se répare dans
    les variables d'environnement. Et surtout : le constater par `/alert`
    coûte un message posté."""
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


# ============================================ LES TROUS TROUVÉS EN RELECTURE ==
# Trois conseils de relecture ont passé ce fichier le 2026-09-14. Les quatre
# tests ci-dessus tuaient chacun une régression réelle, mais laissaient passer
# les quatre scénarios suivants — dont deux rendent le healthcheck VERT sur un
# canal mort, c'est-à-dire exactement ce qu'il existe pour empêcher.


def test_les_deux_posees_la_dediee_gagne(client, monkeypatch) -> None:
    """🔴 L'ÉTAT DE PRODUCTION, et le seul où l'ORDRE est observable.

    Les tests plus haut posent une variable OU l'autre, jamais les deux. Or
    c'est seulement quand les deux existent que « la dédiée d'abord, le repli
    ensuite » se voit. Sans ce test, intervertir les deux blocs de
    `voie_du_canal` laisse la suite entièrement verte — et le healthcheck
    annonce alors `SLACK_WEBHOOK_URL` pendant que `notify` poste dans
    `SLACK_WEBHOOK_ERRORS`. Vert et faux, dans l'autre sens.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_ERRORS", "https://hooks.slack.test/errors")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/fallback")
    assert client.get("/alert/healthcheck", headers=AUTH).json()["via"] == (
        "SLACK_WEBHOOK_ERRORS"
    )


def test_une_variable_blanche_ne_compte_pas(client, monkeypatch) -> None:
    """Une variable à `"   "` est le résultat classique d'un copier-coller raté
    dans le tableau de bord Railway. `notify` la rejette (`.strip()` avant le
    test de vérité, `lib/slack.py`), donc le healthcheck doit la rejeter aussi
    — sinon il rend vert un canal par lequel rien ne part.

    Les deux blanches ⇒ rouge ; la dédiée blanche seule ⇒ le repli prend, et le
    healthcheck doit le DIRE plutôt que d'annoncer la dédiée.
    """
    monkeypatch.setenv("SLACK_WEBHOOK_ERRORS", "   ")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "   ")
    body = client.get("/alert/healthcheck", headers=AUTH).json()
    assert body["ok"] is False and body["via"] is None

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/fallback")
    assert client.get("/alert/healthcheck", headers=AUTH).json()["via"] == (
        "SLACK_WEBHOOK_URL"
    )


def test_le_controle_ne_poste_rien(client, monkeypatch) -> None:
    """🔴 LA PROMESSE CENTRALE DE CET ENDPOINT, qu'aucun test ne gardait.

    Tout ce fichier repose sur une propriété : contrôler le canal d'alerte ne
    doit LAISSER AUCUNE TRACE. C'est la seule chose qui le distingue de
    `/alert`, et c'est ce qui fait qu'on ose le lancer souvent.

    Quelqu'un qui « améliorerait » l'endpoint en envoyant un vrai ping de
    vérification — ou qui y glisserait un `notify` de journalisation — ferait
    disparaître son unique raison d'être sans casser un seul des autres tests.
    Ici toute sortie réseau explose.
    """
    import httpx

    async def interdit(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("le healthcheck a fait une requête réseau")

    monkeypatch.setattr(httpx.AsyncClient, "post", interdit)
    monkeypatch.setenv("SLACK_WEBHOOK_ERRORS", "https://hooks.slack.test/errors")
    assert client.get("/alert/healthcheck", headers=AUTH).json()["ok"] is True


def test_un_mauvais_jeton_ne_passe_pas_non_plus(client, monkeypatch) -> None:
    """L'autre test d'auth n'envoie AUCUN en-tête. Affaiblir `_require_auth` en
    un simple « l'en-tête est-il présent ? » le laisserait vert en acceptant
    n'importe quelle valeur."""
    monkeypatch.setenv("SLACK_WEBHOOK_ERRORS", "https://hooks.slack.test/errors")
    r = client.get("/alert/healthcheck", headers={"Authorization": "Bearer faux"})
    assert r.status_code == 401


def test_le_healthcheck_ne_peut_pas_mentir_sur_ce_que_fait_notify(monkeypatch) -> None:
    """🔴 LE GARDE QUI TIENT LES AUTRES : `voie_du_canal` et `_webhook_url`
    doivent rendre le MÊME verdict sur toute entrée.

    Un conseil de relecture a mesuré que la résolution du webhook existait en
    quatre copies dans ce dépôt, et que l'une d'elles avait DÉJÀ divergé
    (`/wf9/healthcheck` avait perdu le `.strip()`). La duplication de cette
    résolution-là n'est donc pas un risque théorique.

    Le healthcheck ne vaut que s'il nomme la voie que `notify` emprunte
    VRAIMENT. Ce test balaie les combinaisons et casse à la première divergence
    — y compris si quelqu'un ajoute un niveau de repli dans une fonction et
    l'oublie dans l'autre.
    """
    import os

    from src.lib import slack as slack_lib

    valeurs = [None, "", "   ", "https://hooks.slack.test/x", "  https://y  "]
    for categorie in (None, "", "errors", "alerts", "inconnue"):
        for dediee in valeurs:
            for repli in valeurs:
                for var, val in (
                    ("SLACK_WEBHOOK_ERRORS", dediee),
                    ("SLACK_WEBHOOK_ALERTS", dediee),
                    ("SLACK_WEBHOOK_URL", repli),
                ):
                    monkeypatch.delenv(var, raising=False)
                    if val is not None:
                        monkeypatch.setenv(var, val)
                url = slack_lib._webhook_url(categorie)
                nom = slack_lib.voie_du_canal(categorie)
                assert (url is None) == (nom is None), (
                    f"désaccord sur ({categorie!r}, {dediee!r}, {repli!r}) : "
                    f"url={url!r} nom={nom!r}"
                )
                if nom is not None:
                    assert os.environ.get(nom, "").strip() == url, (
                        f"{nom} ne porte pas l'URL que notify utiliserait"
                    )
