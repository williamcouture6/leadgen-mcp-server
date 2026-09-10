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


def _monde(
    monkeypatch: pytest.MonkeyPatch,
    companies: list[dict],
    params_vus: dict[str, dict] | None = None,
) -> None:
    """Un contact par company, tous éligibles, dans l'ordre donné.

    ⚠️ `website` est obligatoire dans la fixture : `site_ou_fiche_exploitable`
    (AC1b) écarte une entreprise sans site ET sans fiche Google exploitable.
    Et `research_json` reste sans `services_offered`, donc aucun métier n'est
    reconnu — l'entreprise est alors joignable toute l'année et la fenêtre
    saisonnière ne filtre rien. Ces deux gardes tournent AVANT le tri : ces
    tests portent sur l'ORDRE, il faut donc que tout le monde les franchisse.
    """
    contacts = [
        {"id": f"ct-{c['id']}", "company_id": c["id"], "email": f"{c['id']}@ex.ca",
         "status": "new"}
        for c in companies
    ]

    async def fake_select(table, params=None):
        if params_vus is not None:
            params_vus[table] = dict(params or {})
        if table == "contacts":
            return contacts
        if table == "companies":
            return [
                {"research_json": {"x": 1}, "track": "agence-ia",
                 "website": f"https://{c['id']}.ca", **c}
                for c in companies
            ]
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
    """Tri stable : le comportement historique survit où le potentiel ne tranche pas.

    ⚠️ Deux moitiés, et le conseil du 2026-09-09 a montré qu'il manquait la
    seconde : vérifier l'ordre rendu ne prouve que la stabilité de `sort()`,
    puisque le faux `select` ignore `order`. On vérifie donc AUSSI que la
    requête demande bien `created_at.asc` — sans quoi « l'ordre d'arrivée »
    ne voudrait rien dire.
    """
    params_vus: dict[str, dict] = {}
    _monde(monkeypatch, [
        {"id": "co-1", "name": "Premiere", "lead_potential_score": 50},
        {"id": "co-2", "name": "Deuxieme", "lead_potential_score": 50},
        {"id": "co-3", "name": "Troisieme", "lead_potential_score": 50},
    ], params_vus=params_vus)

    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")

    assert [x["company"]["name"] for x in lot] == ["Premiere", "Deuxieme", "Troisieme"]
    assert params_vus["contacts"]["order"] == "created_at.asc"


async def test_le_rang_d_arrivee_ne_suit_pas_l_ordre_d_envoi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Le gabarit A/B se tire de `rang_arrivee`, jamais de la position triée.

    C'est le bloquant nº1 du conseil du 2026-09-09 : `bras_du_lot` alterne sur
    le rang, donc si ce rang suivait le tri par potentiel, le bras A prendrait
    toujours les meilleurs leads et `v_perf_par_bras` mesurerait leur qualité
    au lieu de la copie. La faible arrivée en premier doit garder le rang 0.
    """
    _monde(monkeypatch, [
        {"id": "co-faible", "name": "Faible", "lead_potential_score": 8},
        {"id": "co-haute", "name": "Haute", "lead_potential_score": 82},
    ])

    lot = await dbt.list_contacts_to_personalize(limit=10, track="agence-ia")

    # Ordre d'ENVOI : le potentiel décide.
    assert [x["company"]["name"] for x in lot] == ["Haute", "Faible"]
    # Ordre d'ARRIVÉE : inchangé, et c'est lui qui tire le gabarit.
    rangs = {x["company"]["name"]: x["rang_arrivee"] for x in lot}
    assert rangs == {"Faible": 0, "Haute": 1}


async def test_un_prioritaire_hors_saison_ne_mange_pas_une_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L'exclusion est souveraine ; le tri n'ordonne que les éligibles.

    C'est la couture entre les deux règles, et le seul endroit où elles
    pouvaient se nuire. Si l'exclusion saisonnière tournait APRÈS le tri et la
    coupe, un déneigeur prioritaire démarché en juillet occuperait une place du
    lot avant d'être jeté : le lot rendrait moins de brouillons que demandé et
    l'alerte de famine crierait sans raison. Ici l'ordre tient — exclure, puis
    trier, puis couper.

    On force la garde plutôt que de dépendre du mois courant : la logique
    saisonnière a ses propres tests, celui-ci porte sur l'enchaînement.
    """
    _monde(monkeypatch, [
        {"id": "co-neige", "name": "Deneigeur", "lead_potential_score": 8,
         "lead_potential_reason": f"{MARQUEUR_TETE_DE_FILE} jamais eu de retour d'appel"},
        {"id": "co-haute", "name": "Haute", "lead_potential_score": 78},
        {"id": "co-faible", "name": "Faible", "lead_potential_score": 25},
    ])
    monkeypatch.setattr(
        dbt, "fenetre_saisonniere_ouverte",
        lambda company, **kw: company["name"] != "Deneigeur",
    )

    lot = await dbt.list_contacts_to_personalize(limit=2, track="agence-ia")

    noms = [x["company"]["name"] for x in lot]
    assert "Deneigeur" not in noms, "hors saison : il ne devait pas entrer dans la file"
    assert noms == ["Haute", "Faible"], "le lot reste plein malgré l'exclusion"


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
