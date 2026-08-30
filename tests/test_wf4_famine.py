"""WF-4 : un lot qui ne rédige rien crie au lieu de se taire.

Le défaut : `/wf4/run` peut repartir les mains vides — zéro draft — alors que
des centaines de contacts attendent encore. Rien ne le disait. Ce projet a déjà
payé cinq semaines de silence sur un défaut de cette famille (la clé Google
Places désactivée pour facturation : tout le WF-1 en échec, aucun cri, parce
que tout est fail-soft).

⚠️ Ces tests portent sur l'ALERTE, pas sur la famine elle-même. Le correctif de
fond (la sélection sur-lit les N plus vieux contacts) appartient à un autre
lot ; l'alerte reste utile après lui et n'en dépend pas.
"""
from __future__ import annotations

import pytest

from src.http_api import _doit_alerter_famine


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# =====================================================================
# B1 — la fonction pure
# =====================================================================

def test_alerte_famine_quand_zero_traite_mais_leads_restants():
    assert _doit_alerter_famine(processed=0, envoyables_restants=312)


def test_pas_d_alerte_famine_quand_la_liste_est_vide():
    assert not _doit_alerter_famine(processed=0, envoyables_restants=0)


def test_pas_d_alerte_famine_quand_ca_tourne():
    assert not _doit_alerter_famine(processed=10, envoyables_restants=300)


# =====================================================================
# B2 — le câblage dans /wf4/run
# =====================================================================

def _socle(monkeypatch, *, backlog=None, file_active=0, servis=0,
           comptage_leve=False, slack_ok=True, comptes=None, envois=None):
    """Rend le module http_api avec /wf4/run isolé de tout le reste.

    `file_active` / `servis` alimentent les deux `count()` du calcul des
    restants ; `restants = file_active - servis`.
    """
    from src import http_api
    from src import supabase_client as sb
    from src.lib import calcom as calcom_mod
    from src.lib import slack as slack_mod

    async def fake_backlog(limit=10, max_per_company=1, track="OPT", **kw):
        return list(backlog or [])

    async def fake_count(table, params=None, schema=None):
        p = params or {}
        if comptes is not None:
            comptes.append({"table": table, "params": p})
        if comptage_leve:
            raise RuntimeError("boom PostgREST")
        return {"contacts": file_active, "messages": servis}[table]

    async def fake_notify(**kw):
        if envois is not None:
            envois.append(kw)
        return slack_ok

    monkeypatch.setattr(http_api.db_tools, "list_contacts_to_personalize", fake_backlog)
    monkeypatch.setattr(calcom_mod, "get_available_slots", lambda **kw: [])
    monkeypatch.setattr(http_api, "_load_client_references", lambda: [])
    monkeypatch.setattr(sb, "count", fake_count)
    monkeypatch.setattr(slack_mod, "notify", fake_notify)
    return http_api


async def _run(http_api, **kw):
    from src.http_api import RunWf4In

    return await http_api.run_wf4(RunWf4In(track="agence-ia", **kw))


async def test_zero_draft_sur_une_file_pleine_crie(monkeypatch):
    envois: list[dict] = []
    http_api = _socle(monkeypatch, backlog=[], file_active=812, servis=500, envois=envois)

    out = await _run(http_api)

    assert out.processed == 0
    assert out.alerte_famine_envoyee is True
    assert len(envois) == 1
    assert envois[0]["category"] == "alerts"
    # Le message NOMME le nombre restant : sans lui, « 0 draft » ne distingue
    # pas une panne d'une fin de liste.
    assert "312" in envois[0]["text"]
    assert "famine" in envois[0]["text"].lower()


async def test_zero_draft_sur_une_file_vide_se_tait(monkeypatch):
    """Fin de liste : il n'y a rien à annoncer, et une alerte quotidienne sans
    objet finirait par être ignorée le jour où elle compte."""
    envois: list[dict] = []
    http_api = _socle(monkeypatch, backlog=[], file_active=800, servis=800, envois=envois)

    out = await _run(http_api)

    assert out.processed == 0
    assert out.alerte_famine_envoyee is None
    assert envois == []


async def test_un_lot_qui_tourne_ne_paie_meme_pas_le_comptage(monkeypatch):
    """Le comptage n'est payé que dans le cas suspect : un lot qui produit n'a
    rien de plus à demander à la base."""
    comptes: list[dict] = []
    envois: list[dict] = []
    http_api = _socle(
        monkeypatch,
        backlog=[{"contact": {"id": "c1", "email": "a@b.ca"}, "company": {"id": "co1"}}],
        file_active=800, servis=1, comptes=comptes, envois=envois,
    )

    async def fake_perso(contact, company, **kw):
        from src.http_api import PersonalizeContactOut

        return PersonalizeContactOut(contact_id=contact["id"], status="ok", message_id="m1")

    monkeypatch.setattr(http_api, "_personalize_one", fake_perso)

    out = await _run(http_api)

    assert out.processed == 1
    assert out.alerte_famine_envoyee is None
    assert comptes == [], "aucun count() quand le lot produit"
    assert envois == []


async def test_le_comptage_passe_par_count_exact_et_par_track(monkeypatch):
    """Même règle que la ligne de conformité du résumé : PostgREST plafonne à
    1000 lignes en silence et les agrégats serveur sont désactivés (PGRST123).
    Compter en ramenant les lignes dirait « il en reste » pour toujours."""
    comptes: list[dict] = []
    http_api = _socle(
        monkeypatch, backlog=[], file_active=812, servis=500, comptes=comptes,
    )
    await _run(http_api)

    assert [c["table"] for c in comptes] == ["contacts", "messages"]
    assert all(c["params"]["track"] == "eq.agence-ia" for c in comptes)
    # Les contacts joignables et pas encore écartés d'un côté…
    assert comptes[0]["params"]["email"] == "not.is.null"
    assert comptes[0]["params"]["status"] == "in.(new,ready)"
    # …les messages ENCORE VIVANTS de l'autre, la même définition que celle qui
    # sert à WF-4 pour décider qu'un contact est déjà pris.
    assert comptes[1]["params"]["status"] == "not.in.(failed)"
    assert comptes[1]["params"]["direction"] == "eq.outbound"


async def test_un_comptage_illisible_crie_quand_meme(monkeypatch):
    """Sans ça, une panne de lecture rendrait 0 restants, donc « fin de liste »,
    donc silence : l'alerte se saborderait au moment exact où quelque chose ne
    va pas."""
    envois: list[dict] = []
    http_api = _socle(monkeypatch, backlog=[], comptage_leve=True, envois=envois)

    out = await _run(http_api)

    assert out.alerte_famine_envoyee is True
    assert "ILLISIBLE" in envois[0]["text"]
    assert "traiter comme une panne" in envois[0]["text"]


async def test_le_retour_de_slack_est_lu(monkeypatch):
    """Une alerte perdue qui se croit partie est le mode d'échec que WF-5 a déjà
    refermé de son côté."""
    http_api = _socle(
        monkeypatch, backlog=[], file_active=812, servis=500, slack_ok=False,
    )
    out = await _run(http_api)
    assert out.alerte_famine_envoyee is False


async def test_les_restants_ne_descendent_jamais_sous_zero(monkeypatch):
    """L'estimation peut se retourner (plus de messages vivants que de contacts
    joignables : des désabonnés en gardent un). Un nombre négatif dans une
    alerte détruirait la confiance qu'on lui accorde."""
    http_api = _socle(monkeypatch, file_active=10, servis=99)
    assert await http_api._compter_envoyables_restants("agence-ia") == (0, True)
