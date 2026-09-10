"""Les alertes de WF-6 sont-elles VRAIMENT appelées ?

🔴 LE TROU QUE CE FICHIER FERME, et c'est exactement le défaut chassé toute la
soirée du 2026-09-01 : un test qui vérifie une fonction sans vérifier qu'on
l'appelle.

`test_wf6_file_bloquee.py` exerce `_alerter_file_bloquee` en profondeur — six
tests, filtre, message, gardes sur la garde. Aucun ne vérifie que `run_wf6`
l'appelle. On pouvait donc supprimer la ligne d'appel dans un refactor et voir
les six rester verts, pendant que le silence revenait.

C'est la même famille que le motif `[A-ZÀ-Ü]` routé par un `_find_matches` qui
baisse la casse, et que l'ancre `attend (\\d+) minutes` devenue muette : la
fonction est bonne, le chemin ne passe plus par elle.

Ce fichier teste donc les DEUX SITES D'APPEL de `run_wf6`, avec pour chacun un
contrôle négatif — l'alerte ne doit pas partir quand tout va bien, sinon on
apprend à l'ignorer.

⚠️ Les deux alertes sont DISTINCTES à dessein. « Aucune campagne configurée »
et « la file attend un verdict » sont deux pannes différentes : la première
laisse une file pleine de brouillons APPROUVÉS, que `_alerter_file_bloquee`
(qui cherche des `is.null`) ne verrait pas. Une alerte qui se trompe de cause
coûte plus qu'une alerte absente, parce qu'on la suit.
"""
from __future__ import annotations

import pytest

from src.tools import send as send_tools


@pytest.fixture
def wf6(monkeypatch):
    """Monte `run_wf6` avec toutes ses dépendances externes neutralisées."""
    appels: dict[str, int] = {"campagne_absente": 0, "file_bloquee": 0}
    etat: dict[str, object] = {"drafts": []}

    async def _campagne_absente(track):
        appels["campagne_absente"] += 1
        return True

    async def _file_bloquee(track):
        appels["file_bloquee"] += 1
        return True

    async def _select(table, params=None):
        return list(etat["drafts"])

    async def _compte(*a, **k):
        return 0

    monkeypatch.setattr(send_tools, "_alerter_campagne_absente", _campagne_absente)
    monkeypatch.setattr(send_tools, "_alerter_file_bloquee", _file_bloquee)
    monkeypatch.setattr(send_tools.db, "select", _select)
    monkeypatch.setattr(send_tools, "count_pushed_today", _compte)
    return appels, etat


@pytest.mark.asyncio
async def test_run_wf6_alerte_quand_la_campagne_manque(wf6) -> None:
    """Le site d'appel nº1. Sans lui, le refus est muet tous les jours."""
    appels, _ = wf6
    out = await send_tools.run_wf6(
        send_tools.RunWf6In(limit=10, track="agence-ia", campaign_id=None)
    )
    assert appels["campagne_absente"] == 1, "run_wf6 n'appelle pas l'alerte de campagne"
    assert out.processed == 0 and out.errors == 0, (
        "le retour reste silencieux — c'est bien pourquoi l'alerte est nécessaire"
    )


@pytest.mark.asyncio
async def test_pas_d_alerte_de_campagne_quand_elle_est_configuree(wf6) -> None:
    """Contrôle négatif : une alerte qui part toujours n'apprend rien."""
    appels, _ = wf6
    await send_tools.run_wf6(
        send_tools.RunWf6In(limit=10, track="agence-ia", campaign_id="camp-123")
    )
    assert appels["campagne_absente"] == 0


@pytest.mark.asyncio
async def test_run_wf6_alerte_quand_le_lot_revient_vide(wf6) -> None:
    """Le site d'appel nº2, celui qu'aucun test ne couvrait."""
    appels, etat = wf6
    etat["drafts"] = []
    await send_tools.run_wf6(
        send_tools.RunWf6In(limit=10, track="agence-ia", campaign_id="camp-123")
    )
    assert appels["file_bloquee"] == 1, (
        "run_wf6 n'appelle pas l'alerte de file bloquée — la ligne a pu être "
        "supprimée sans faire rougir les six tests de la fonction"
    )


@pytest.mark.asyncio
async def test_pas_d_alerte_de_file_quand_le_lot_est_plein(wf6) -> None:
    """Contrôle négatif : un lot non vide est le cas NORMAL."""
    appels, etat = wf6
    etat["drafts"] = [{"id": "1", "to_email": "a@b.co", "track": "agence-ia"}]
    await send_tools.run_wf6(
        send_tools.RunWf6In(limit=10, track="agence-ia", campaign_id="camp-123")
    )
    assert appels["file_bloquee"] == 0


@pytest.mark.asyncio
async def test_les_deux_alertes_ne_se_confondent_pas(wf6) -> None:
    """Campagne absente ne doit PAS déclencher l'alerte de file, et l'inverse.

    Les deux pannes laissent des files très différentes : sans campagne, la
    file peut être pleine de brouillons APPROUVÉS, que l'alerte de file (qui
    cherche des `is.null`) ne verrait pas. Confondre les deux donnerait un
    diagnostic faux — et on suit un diagnostic faux.
    """
    appels, etat = wf6
    await send_tools.run_wf6(
        send_tools.RunWf6In(limit=10, track="agence-ia", campaign_id=None)
    )
    assert (appels["campagne_absente"], appels["file_bloquee"]) == (1, 0)

    appels["campagne_absente"] = appels["file_bloquee"] = 0
    etat["drafts"] = []
    await send_tools.run_wf6(
        send_tools.RunWf6In(limit=10, track="agence-ia", campaign_id="camp-123")
    )
    assert (appels["campagne_absente"], appels["file_bloquee"]) == (0, 1)
