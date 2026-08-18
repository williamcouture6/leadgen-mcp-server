"""Le statut de recherche doit dire la vérité sur ce qu'on a trouvé.

Avant : status='enriched' quoi qu'il arrive, même sans un seul courriel — d'où
145 entreprises annonçant des contacts qu'elles n'ont pas.

⚠️ Nommage : le plan parle de `save_research_result` ; la fonction s'appelle en
réalité `update_company_research`. `save_research` est le nom de l'outil Anthropic
côté agent de recherche, pas la fonction DB.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


def _capture(monkeypatch: pytest.MonkeyPatch, n_contacts: int) -> list[dict]:
    """Mock la couche Supabase et renvoie la liste des patches envoyés.

    ⚠️ On patche `dbt.db` (= `src.supabase_client`, importé sous l'alias `db`),
    pas `src.tools.db` : c'est `db.select` / `db.update` que le module appelle.
    """
    from src.tools import db as dbt

    patches: list[dict] = []

    async def _select(table: str, *, params: Any = None, schema: Any = None) -> list[dict]:
        if table == "contacts":
            return [{"id": f"ct-{i}"} for i in range(n_contacts)]
        return []

    async def _update(table: str, patch: dict, **kw: Any) -> list[dict]:
        patches.append(patch)
        return [{}]

    monkeypatch.setattr(dbt.db, "select", _select)
    monkeypatch.setattr(dbt.db, "update", _update)
    return patches


async def test_avec_contact_le_statut_reste_enriched(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import db as dbt

    patches = _capture(monkeypatch, n_contacts=2)
    await dbt.update_company_research("co-1", {"a": 1})
    assert patches[0]["status"] == "enriched"


async def test_sans_contact_le_statut_dit_la_verite(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import db as dbt

    patches = _capture(monkeypatch, n_contacts=0)
    await dbt.update_company_research("co-1", {"a": 1})
    assert patches[0]["status"] == "researched_no_contact"


async def test_premiere_passe_les_courriels_trouves_comptent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Piège d'ordre d'appel : `http_api` appelle `update_company_research` AVANT
    d'insérer les contacts scrapés. À cet instant la table `contacts` est vide même
    quand le scraping a ramené trois adresses. Ne compter que la table ferait donc
    passer 100 % des premières passes en 'researched_no_contact' — l'inverse exact
    du bug qu'on corrige. Les courriels trouvés valent donc preuve : `insert_contact`
    ne rejette qu'une adresse vide, tout le reste devient contact (inserted|duplicate).
    """
    from src.tools import db as dbt

    patches = _capture(monkeypatch, n_contacts=0)
    await dbt.update_company_research(
        "co-1", {"a": 1}, emails_found=[{"email": "info@exemple.ca", "local": "info", "kind": "generic"}]
    )
    assert patches[0]["status"] == "enriched"


async def test_un_courriel_vide_ne_compte_pas(monkeypatch: pytest.MonkeyPatch) -> None:
    """`insert_contact` renvoie skipped_no_email : rien n'entrera en base."""
    from src.tools import db as dbt

    patches = _capture(monkeypatch, n_contacts=0)
    await dbt.update_company_research("co-1", {"a": 1}, emails_found=[{"email": "", "local": ""}])
    assert patches[0]["status"] == "researched_no_contact"


async def test_last_enriched_at_est_pose_dans_les_deux_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C'est lui qui portera la ré-éligibilité à 90 jours."""
    from src.tools import db as dbt

    for n in (0, 2):
        patches = _capture(monkeypatch, n_contacts=n)
        await dbt.update_company_research("co-1", {"a": 1})
        assert patches[0].get("last_enriched_at")


# ----------------------------------------------------------------------
# Ré-éligibilité à 90 jours (Task C3)
# ----------------------------------------------------------------------


def _params_backlog(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from src.tools import db as dbt

    vus: list[dict] = []

    async def _select(table: str, *, params: Any = None, schema: Any = None) -> list[dict]:
        vus.append(params or {})
        return []

    monkeypatch.setattr(dbt.db, "select", _select)
    return vus


async def test_researched_no_contact_revient_apres_90_jours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une entreprise sans courriel aujourd'hui peut en publier un dans trois
    mois. Ne jamais repasser figerait 145 fiches ; repasser à chaque cron
    coûterait cher pour rien."""
    from src.tools import db as dbt

    vus = _params_backlog(monkeypatch)
    await dbt.list_companies_to_research(limit=10, track="agence-ia")

    params = vus[0]
    assert "researched_no_contact" in str(params), "le statut doit être éligible"
    assert "last_enriched_at" in str(params)


async def test_les_deux_portes_du_backlog_sont_dans_une_seule_clause_or(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`research_json is null` ne peut PAS rester un filtre de premier niveau.

    Toute company 'researched_no_contact' a par construction un research_json (c'est
    la passe de recherche qui pose ce statut). Un `research_json=is.null` en ET
    global annulerait donc la seconde porte en silence : la clause 90 jours serait
    posée, les tests passeraient, et pas une seule fiche ne reviendrait jamais.
    La condition sur research_json appartient à la porte 'sourced'.
    """
    from src.tools import db as dbt

    vus = _params_backlog(monkeypatch)
    await dbt.list_companies_to_research(limit=10, track="agence-ia")

    params = vus[0]
    assert "research_json" not in params, "annulerait la porte researched_no_contact"
    clause = params["or"]
    assert clause.startswith("(and(status.eq.sourced,research_json.is.null),")
    assert "and(status.eq.researched_no_contact,last_enriched_at.lt." in clause
    assert clause.endswith("))")


async def test_le_seuil_de_reprise_vaut_bien_90_jours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.tools import db as dbt

    vus = _params_backlog(monkeypatch)
    await dbt.list_companies_to_research(limit=10, track="agence-ia")

    m = re.search(r"last_enriched_at\.lt\.([^,)]+)", vus[0]["or"])
    assert m, "pas de seuil daté dans la clause or"
    seuil = datetime.fromisoformat(m.group(1))
    ecart = datetime.now(timezone.utc) - seuil
    assert timedelta(days=89) < ecart < timedelta(days=91), ecart


async def test_la_file_tourne_jamais_recherchees_dabord(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Même rotation que la file d'envoi (P4.10) : jamais tenté d'abord.

    Les 145 'researched_no_contact' ont été créées AVANT la plupart des 'sourced'
    (2026-05-28→06-12 contre 2026-06-12→07-08). En `created_at.asc` pur, elles
    front-runneraient donc 225 entreprises jamais recherchées à chaque passe : une
    file qui sert les échecs connus avant les pistes neuves.
    """
    from src.tools import db as dbt

    vus = _params_backlog(monkeypatch)
    await dbt.list_companies_to_research(limit=10, track="agence-ia")

    assert vus[0]["order"] == "last_enriched_at.asc.nullsfirst,created_at.asc"


async def test_les_statuts_terminaux_restent_hors_du_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La clause `or` ne doit pas rouvrir une porte que le filtre terminal ferme."""
    from src.tools import db as dbt

    vus = _params_backlog(monkeypatch)
    await dbt.list_companies_to_research(limit=10, track="agence-ia")

    assert vus[0]["status"] == "not.in.(disqualified,no_web_presence)"
