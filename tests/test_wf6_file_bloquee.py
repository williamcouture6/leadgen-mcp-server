"""Un lot WF-6 vide n'est pas forcément une file vide.

🔴 LE SILENCE QUE CE FICHIER RÉPARE, ET IL RESSEMBLE À UN SUCCÈS.

On active WF-6 sans avoir activé WF-5. WF-4 écrit des brouillons avec
`compliance_check_passed = NULL` ; la requête de WF-6 exige `is.true` ; le lot
revient vide. `run_wf6` rend `processed=0, errors=0`, le nœud IF de n8n part
sur « Log OK », et la checklist go-live se coche entièrement pendant que RIEN
ne part.

Rien dans les journaux ne distingue « la campagne est finie » de « il manque un
maillon ». Trouvé par l'audit de bout en bout du 2026-09-01.

La distinction est faite en base : reste-t-il des brouillons en attente de
verdict ? Si oui, la file n'est pas vide et quelque chose en amont ne tourne
pas.
"""
from __future__ import annotations

import pytest

from src.tools import send as send_tools


class _Espion:
    """Remplace `db.select` et `slack.notify` sans toucher au réseau."""

    def __init__(self, en_attente: int) -> None:
        self.en_attente = en_attente
        self.params: dict | None = None
        self.texte: str | None = None
        self.echec_slack = False
        self.explose_db = False

    async def select(self, table: str, params: dict | None = None):
        if self.explose_db:
            raise RuntimeError("base injoignable")
        self.params = params
        return [{"id": str(i)} for i in range(self.en_attente)]

    async def notify(self, **kw):
        self.texte = kw.get("text")
        return not self.echec_slack


@pytest.fixture
def espion(monkeypatch):
    def _monte(en_attente: int) -> _Espion:
        e = _Espion(en_attente)
        from src.lib import slack as slack_lib

        monkeypatch.setattr(send_tools.db, "select", e.select)
        monkeypatch.setattr(slack_lib, "notify", e.notify)
        return e

    return _monte


@pytest.mark.asyncio
async def test_une_file_bloquee_declenche_l_alerte(espion) -> None:
    e = espion(7)
    assert await send_tools._alerter_file_bloquee("agence-ia") is True
    assert "7 brouillon(s)" in e.texte


@pytest.mark.asyncio
async def test_une_file_vraiment_vide_ne_crie_pas(espion) -> None:
    """Une fin de liste est normale. Alerter dessus apprendrait à ignorer
    l'alerte, et la vraie passerait avec."""
    e = espion(0)
    assert await send_tools._alerter_file_bloquee("agence-ia") is False
    assert e.texte is None


@pytest.mark.asyncio
async def test_l_alerte_cherche_les_brouillons_SANS_VERDICT(espion) -> None:
    """C'est le filtre qui porte tout le sens.

    `is.null` = écrit par WF-4, jamais jugé. Chercher `is.false` compterait les
    brouillons REFUSÉS, pour lesquels il n'y a précisément rien à attendre — et
    l'alerte crierait tous les jours sur une situation normale.
    """
    e = espion(3)
    await send_tools._alerter_file_bloquee("agence-ia")
    assert e.params["compliance_check_passed"] == "is.null"
    assert e.params["status"] == "eq.draft"
    assert e.params["track"] == "eq.agence-ia"


@pytest.mark.asyncio
async def test_l_alerte_nomme_la_piste_numero_un(espion) -> None:
    """« 0 push » ne distingue pas une panne d'une fin de liste.

    Une alerte qu'on ne peut pas interpréter finit ignorée : celle-ci doit
    nommer WF-5, qui est la cause de très loin la plus probable.
    """
    e = espion(4)
    await send_tools._alerter_file_bloquee("agence-ia")
    assert "WF-5" in e.texte
    assert "compliance_check_passed" in e.texte


@pytest.mark.asyncio
async def test_une_base_injoignable_ne_fait_pas_tomber_l_envoi(espion) -> None:
    """Un filet n'a pas le droit de casser ce qu'il surveille.

    Si la lecture échoue, on renonce à l'alerte — on ne propage pas
    l'exception dans `run_wf6`, qui est en train de pousser de vrais courriels.
    """
    e = espion(5)
    e.explose_db = True
    assert await send_tools._alerter_file_bloquee("agence-ia") is False


@pytest.mark.asyncio
async def test_une_alerte_perdue_est_rapportee_comme_perdue(espion) -> None:
    """Rend `False` quand Slack refuse.

    Même règle que `_alerter_famine_wf4` : une alerte perdue qui se croit
    partie est le pire des deux mondes.
    """
    e = espion(6)
    e.echec_slack = True
    assert await send_tools._alerter_file_bloquee("agence-ia") is False
    assert e.texte is not None, "le message a bien été composé, c'est l'envoi qui a échoué"
