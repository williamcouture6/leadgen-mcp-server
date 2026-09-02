"""La file d'envoi WF-4 sort dans l'ordre du potentiel, plus dans l'ordre d'arrivée.

Constat du 2026-09-01 : `lead_potential_score` était **écrit et jamais lu**.
Les trois sélections du pipeline triaient sur `created_at`, donc le funnel
était strictement premier-arrivé-premier-servi et tout le barème ne changeait
l'ordre de rien.

Deux règles, dans cet ordre :

1. **Tête de file** — un avis Google dit qu'on n'arrive pas à joindre
   l'entreprise. Le score du lead n'est PAS gonflé pour autant (décision
   William) : c'est la marque dans `lead_potential_reason` qui le fait passer
   devant, et son 8 reste un 8.
2. **Potentiel décroissant**, puis l'ordre d'arrivée à égalité.

🔴 Et le marqueur ne doit JAMAIS descendre jusqu'à l'agent de personnalisation :
écrire « j'ai vu que tes clients disent que tu ne rappelles pas » citerait un
tiers au prospect à son sujet et ruinerait le courriel. Il ordonne, il ne parle
pas — c'est la règle de la spec 2026-08-27, et le dernier test la verrouille.
"""
from __future__ import annotations

import pytest

import src.tools.db as dbt
from src import supabase_client as real_db
from src.lib.lead_scoring import MARQUEUR_TETE_DE_FILE


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _monde(monkeypatch: pytest.MonkeyPatch, companies: list[dict]) -> None:
    """Un contact par company, tous éligibles, dans l'ordre donné."""
    contacts = [
        {"id": f"ct-{c['id']}", "company_id": c["id"], "email": f"{c['id']}@ex.ca",
         "status": "new"}
        for c in companies
    ]

    async def fake_select(table, params=None):
        if table == "contacts":
            return contacts
        if table == "companies":
            return [{"research_json": {"x": 1}, "track": "agence-ia", **c} for c in companies]
        if table == "messages":
            return []
        return []

    monkeypatch.setattr(real_db, "select", fake_select)


async def test_la_tete_de_file_passe_devant_un_meilleur_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cVert Québec : score 8, mais un avis dit « jamais eu de retour d'appel ».
    # Elle doit sortir avant une boîte à 82 arrivée bien avant elle.
    _monde(monkeypatch, [
        {"id": "co-forte", "name": "Forte", "lead_potential_score": 82,
         "lead_potential_reason": "ferme le soir, 300 avis"},
        {"id": "co-cvert", "name": "cVert", "lead_potential_score": 8,
         "lead_potential_reason": f"{MARQUEUR_TETE_DE_FILE} jamais eu de retour d'appel"},
    ])
    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert [x["company"]["name"] for x in lot] == ["cVert", "Forte"]


async def test_a_defaut_de_marque_le_potentiel_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _monde(monkeypatch, [
        {"id": "co-faible", "name": "Faible", "lead_potential_score": 25},
        {"id": "co-haute", "name": "Haute", "lead_potential_score": 78},
        {"id": "co-moyenne", "name": "Moyenne", "lead_potential_score": 52},
    ])
    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert [x["company"]["name"] for x in lot] == ["Haute", "Moyenne", "Faible"]


async def test_un_lead_jamais_score_passe_en_dernier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Un score absent ne doit pas se faufiler devant un lead mesuré — sinon
    # les 116 boîtes recherchées sans score doubleraient toute la file.
    _monde(monkeypatch, [
        {"id": "co-nulle", "name": "SansScore", "lead_potential_score": None},
        {"id": "co-basse", "name": "Basse", "lead_potential_score": 12},
    ])
    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert [x["company"]["name"] for x in lot] == ["Basse", "SansScore"]


async def test_a_score_egal_l_ordre_d_arrivee_est_preserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tri stable : le comportement historique (created_at.asc) survit partout
    # où le potentiel ne tranche pas.
    _monde(monkeypatch, [
        {"id": "co-1", "name": "Premiere", "lead_potential_score": 50},
        {"id": "co-2", "name": "Deuxieme", "lead_potential_score": 50},
        {"id": "co-3", "name": "Troisieme", "lead_potential_score": 50},
    ])
    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")
    assert [x["company"]["name"] for x in lot] == ["Premiere", "Deuxieme", "Troisieme"]


async def test_le_marqueur_ne_descend_jamais_vers_la_copie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 🔴 Le signal est interne. S'il atteignait l'agent de personnalisation,
    # rien n'empêcherait le courriel de citer l'avis d'un client mécontent au
    # prospect lui-même.
    _monde(monkeypatch, [
        {"id": "co-1", "name": "cVert", "lead_potential_score": 8,
         "lead_potential_reason": f"{MARQUEUR_TETE_DE_FILE} jamais eu de retour d'appel"},
    ])
    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")
    company = lot[0]["company"]
    assert "lead_potential_reason" not in company
    assert "lead_potential_score" not in company
    assert MARQUEUR_TETE_DE_FILE not in str(company)
    # Le reste de la fiche est intact.
    assert company["name"] == "cVert" and company["research_json"] == {"x": 1}
