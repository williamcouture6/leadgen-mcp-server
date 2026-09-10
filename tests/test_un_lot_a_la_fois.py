"""Un lot à la fois, par route et par piste.

🔴 CE FICHIER EXISTE À CAUSE D'UN INCIDENT OBSERVÉ EN DIRECT, le 2026-09-08.

William lance `[REACTI] WF-4` **une** fois depuis l'éditeur n8n. Résultat :
**57 brouillons au lieu de 20**, écrits par trois lots concurrents, et le
compteur montait encore après qu'il ait annulé l'exécution.

Trois faits se combinent, chacun anodin pris seul :

1. Le nœud HTTP de n8n porte `retryOnFail: true, maxTries: 3`. Quand la requête
   paraît échouer — trop longue, connexion coupée, exécution annulée — n8n la
   **relance**.
2. Une relance n'est pas un rejeu : elle démarre un lot **neuf** côté serveur,
   qui lit les 20 contacts suivants.
3. **Annuler dans n8n n'arrête rien.** n8n ferme sa connexion ; la coroutine
   FastAPI continue jusqu'au bout sur Railway, en brûlant des jetons Anthropic.

Le plafond quotidien de WF-6 ne suffisait pas à couvrir ça : il borne le TOTAL
du jour, mais deux lots lancés à la même seconde lisent tous les deux
`deja_pousses = 0` et poussent 20 chacun. C'est cette fenêtre-là que le verrou
ferme.

⚠️ Un verrou en mémoire suffit **parce que le service tourne en un seul
processus** (`uvicorn` sans `--workers`, cf. Procfile et railway.json). Le jour
où quelqu'un ajoute des workers, il devient décoratif — il faudra un verrou
consultatif Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

import src.http_api as http_api
import src.tools.send as send_tools


@pytest.fixture(autouse=True)
def _verrous_propres():
    """Chaque test part de verrous neufs — sinon un test qui laisse un verrou
    pris ferait échouer le suivant pour la mauvaise raison."""
    http_api._VERROUS_DE_LOT.clear()
    yield
    http_api._VERROUS_DE_LOT.clear()


def _sortie_wf4(n: int) -> http_api.RunWf4Out:
    return http_api.RunWf4Out(
        processed=n, drafts_created=n, skipped=0, failed=0,
        slots_available=0, repli_lexique=0, items=[],
    )


def _sortie_wf6(n: int) -> send_tools.RunWf6Out:
    return send_tools.RunWf6Out(
        processed=n, pushed=n, skipped_cap=0, skipped_warmup=0,
        skipped_suppressed=0, skipped_platform_domain=0, skipped_other=0,
        errors=0, daily_cap=20, already_pushed_today=0, items=[],
    )


# ====================================================== WF-4 : LA RÉDACTION ==


@pytest.mark.asyncio
async def test_trois_tentatives_ne_redigent_qu_un_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 LE SCÉNARIO EXACT DE L'INCIDENT : les 3 tentatives de n8n, en parallèle.

    Sans le verrou, chacune démarre un lot et on obtient 60 brouillons pour un
    clic. Avec, la première travaille et les deux autres rendent la main tout
    de suite.
    """
    appels = 0

    async def faux_lot(payload):
        nonlocal appels
        appels += 1
        await asyncio.sleep(0.05)  # le temps que les autres arrivent
        return _sortie_wf4(20)

    monkeypatch.setattr(http_api, "_run_wf4", faux_lot)
    p = http_api.RunWf4In(limit=20, track="agence-ia")

    r = await asyncio.gather(http_api.run_wf4(p), http_api.run_wf4(p), http_api.run_wf4(p))

    assert appels == 1, f"{appels} lots ont démarré au lieu d'un seul"
    assert sum(x.drafts_created for x in r) == 20
    assert sorted(x.drafts_created for x in r) == [0, 0, 20]


@pytest.mark.asyncio
async def test_le_refus_n_est_pas_une_erreur(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le lot refusé rend un compte à ZÉRO, pas une exception.

    C'est voulu : le nœud IF de n8n lit `drafts_created` et part sur « Log OK ».
    Lever une erreur ferait exactement le contraire de ce qu'on cherche — n8n
    verrait un échec et **relancerait**.
    """
    async def lent(payload):
        await asyncio.sleep(0.1)
        return _sortie_wf4(20)

    monkeypatch.setattr(http_api, "_run_wf4", lent)
    p = http_api.RunWf4In(limit=20, track="agence-ia")

    premier = asyncio.create_task(http_api.run_wf4(p))
    await asyncio.sleep(0.02)
    refuse = await http_api.run_wf4(p)          # ne lève pas
    assert refuse.drafts_created == 0
    assert refuse.processed == 0
    assert refuse.items == []
    assert (await premier).drafts_created == 20


@pytest.mark.asyncio
async def test_le_verrou_se_relache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contrôle négatif : un verrou qui ne se relâche pas bloquerait le cron du
    lendemain. Le test précédent passerait quand même — d'où celui-ci."""
    async def rapide(payload):
        return _sortie_wf4(20)

    monkeypatch.setattr(http_api, "_run_wf4", rapide)
    p = http_api.RunWf4In(limit=20, track="agence-ia")

    for tour in range(3):
        r = await http_api.run_wf4(p)
        assert r.drafts_created == 20, f"tour {tour + 1} bloqué : le verrou colle"


@pytest.mark.asyncio
async def test_le_verrou_meme_si_le_lot_leve(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 Le piège classique du verrou : une exception qui le laisse pris.

    `async with` le relâche même en cas d'erreur — ce test l'exige plutôt que
    de l'espérer. Sans ça, une seule panne de lot gèlerait le cron pour
    toujours, en silence.
    """
    async def qui_plante(payload):
        raise RuntimeError("Anthropic 529")

    monkeypatch.setattr(http_api, "_run_wf4", qui_plante)
    p = http_api.RunWf4In(limit=20, track="agence-ia")

    with pytest.raises(RuntimeError):
        await http_api.run_wf4(p)
    assert not http_api._verrou_de_lot("wf4:agence-ia").locked(), (
        "le verrou est resté pris après une erreur : le cron ne repartira jamais"
    )


@pytest.mark.asyncio
async def test_les_pistes_sont_independantes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le verrou porte sur (route, piste). OPT et agence-ia ne se bloquent pas —
    un verrou global les ferait s'attendre sans raison."""
    async def lent(payload):
        await asyncio.sleep(0.05)
        return _sortie_wf4(20)

    monkeypatch.setattr(http_api, "_run_wf4", lent)
    a, b = await asyncio.gather(
        http_api.run_wf4(http_api.RunWf4In(limit=20, track="agence-ia")),
        http_api.run_wf4(http_api.RunWf4In(limit=20, track="OPT")),
    )
    assert a.drafts_created == b.drafts_created == 20


# ========================================================= WF-6 : L'ENVOI ====


@pytest.mark.asyncio
async def test_trois_tentatives_n_envoient_qu_un_lot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 LE CAS QUI COÛTE LE PLUS CHER — ici ce sont de VRAIS courriels.

    Le plafond quotidien ne suffit pas : deux lots lancés à la même seconde
    lisent tous les deux `deja_pousses = 0` et poussent 20 chacun. Le verrou
    est ce qui ferme cette fenêtre.
    """
    appels = 0

    async def faux_envoi(payload):
        nonlocal appels
        appels += 1
        await asyncio.sleep(0.05)
        return _sortie_wf6(20)

    monkeypatch.setattr(send_tools, "run_wf6", faux_envoi)
    p = send_tools.RunWf6In(limit=20, track="agence-ia")

    r = await asyncio.gather(http_api.run_wf6(p), http_api.run_wf6(p), http_api.run_wf6(p))

    assert appels == 1, f"{appels} lots d'ENVOI ont démarré — des courriels en double"
    assert sum(x.pushed for x in r) == 20


# =================================== TOUTES LES ROUTES QUI ÉCRIVENT ==========


@pytest.mark.parametrize(
    "fonction,interne",
    [("run_wf1", "_run_wf1"), ("run_wf3", "_run_wf3"), ("run_wf5", "_run_wf5")],
)
def test_chaque_route_qui_ecrit_a_son_verrou(fonction: str, interne: str) -> None:
    """La liste est explicite plutôt que devinée : une route qui écrit et qui
    n'a pas de verrou est exactement le trou de l'incident.

    On lit le CODE de la fonction publique — elle doit déléguer à son interne
    après avoir pris le verrou.
    """
    import inspect

    assert hasattr(http_api, interne), f"{fonction} n'a pas été dédoublée"
    source = inspect.getsource(getattr(http_api, fonction))
    assert "_lot_deja_en_cours" in source, f"{fonction} ne consulte pas le verrou"
    assert "_verrou_de_lot" in source, f"{fonction} ne prend pas le verrou"
    assert f"await {interne}(payload)" in source
