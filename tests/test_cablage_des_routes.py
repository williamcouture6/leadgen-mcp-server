"""Chaque route HTTP pointe bien sur la fonction qu'elle annonce.

🔴 POURQUOI CE FICHIER EXISTE, et il vaut la peine d'être lu en entier.

Le 2026-09-10, une fonction a été insérée ENTRE un décorateur et la fonction
qu'il décorait :

    @app.post("/wf4/run", ...)
    async def _bras_deja_servis_aujourdhui(track: str) -> int:   # <- avalée
        ...
    async def run_wf4(payload: RunWf4In) -> RunWf4Out:           # <- orpheline

Python n'y voit rien : un décorateur s'applique à ce qui le suit, quoi que ce
soit. FastAPI non plus : il a publié une route qui attend `track` en QUERY et
rend un `int` — donc 422 sur le POST JSON de n8n, trois fois par cron, tous les
jours, zéro brouillon.

ET LES 1734 TESTS PASSAIENT. Les six fichiers de test de WF-4 appellent
`http_api.run_wf4(...)` PAR SON NOM PYTHON. La fonction, elle, était intacte —
c'est son BRANCHEMENT qui ne l'était plus. Aucun test ne regardait la table de
routage.

Le pire : `run_wf4` porte le verrou de lot, et `_run_wf4` porte l'alerte de
famine. Les deux gardes qui existent précisément pour crier quand le pipeline
ne produit plus rien étaient EN AVAL du point de rupture. La panne se serait
donc installée en silence, exactement comme la panne Google Places restée
invisible cinq semaines.

⚠️ Ces tests ne vérifient pas ce que font les routes — d'autres fichiers s'en
chargent. Ils vérifient qu'elles sont BRANCHÉES sur la bonne fonction, ce
qu'aucune suite ne faisait.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _table_de_routage() -> dict[str, str]:
    from src.http_api import app

    return {
        r.path: r.endpoint.__name__
        for r in app.routes
        if hasattr(r, "endpoint") and hasattr(r, "path")
    }


# Les routes dont le débranchement arrête une partie du pipeline sans bruit.
# Le nom attendu est celui de la fonction publique, jamais d'un privé.
_ROUTES_CRITIQUES = {
    "/wf4/run": "run_wf4",
    "/wf5/run": "run_wf5",
    "/wf6/run": "run_wf6",
    "/summary/daily": "summary_daily",
    "/personalize/contact": "personalize_contact",
    "/wf7/instantly-webhook": "wf7_instantly_webhook",
}


@pytest.mark.parametrize("chemin,fonction", sorted(_ROUTES_CRITIQUES.items()))
def test_une_route_critique_pointe_sur_sa_fonction(chemin, fonction) -> None:
    table = _table_de_routage()
    assert chemin in table, f"{chemin} a disparu de la table de routage"
    assert table[chemin] == fonction, (
        f"{chemin} pointe sur {table[chemin]!r} au lieu de {fonction!r} — "
        "une fonction s'est probablement glissée entre le décorateur et la sienne"
    )


def test_aucune_route_ne_pointe_sur_une_fonction_privee() -> None:
    """Le garde-fou GÉNÉRIQUE, celui qui attrape le cas qu'on n'a pas prévu.

    Une fonction préfixée d'un underscore n'est pas destinée à être exposée :
    si elle se retrouve au bout d'une route, c'est qu'un décorateur a glissé.
    """
    fautives = {
        chemin: nom
        for chemin, nom in _table_de_routage().items()
        if nom.startswith("_")
    }
    assert fautives == {}, (
        f"routes branchees sur une fonction privee : {fautives} — "
        "un decorateur a ete separe de sa fonction"
    )


def test_wf4_accepte_un_corps_json_et_rend_un_lot(monkeypatch) -> None:
    """Le contrôle de bout en bout, par HTTP : c'est ce qu'aucun test WF-4 ne
    faisait, et c'est ce qui aurait montré le défaut tout de suite.

    n8n poste un CORPS JSON. Une route qui attendrait `track` en query rendrait
    422 sans que rien d'autre ne bouge."""
    from fastapi.testclient import TestClient

    from src import http_api

    monkeypatch.setenv("AGENTS_HTTP_TOKEN", "jeton-de-test")

    async def _lot_vide(payload):
        return http_api.RunWf4Out(
            processed=0, drafts_created=0, skipped=0, failed=0,
            slots_available=0, repli_lexique=0, items=[],
        )

    monkeypatch.setattr(http_api, "_run_wf4", _lot_vide)

    reponse = TestClient(http_api.app).post(
        "/wf4/run",
        json={"limit": 10, "template_choice": "ABCD", "track": "agence-ia", "persist": True},
        headers={"Authorization": "Bearer jeton-de-test"},
    )

    assert reponse.status_code == 200, (
        f"POST /wf4/run rend {reponse.status_code} : {reponse.text[:300]}"
    )
    assert "drafts_created" in reponse.json()
