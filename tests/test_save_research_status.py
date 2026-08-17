"""Le statut de recherche doit dire la vérité sur ce qu'on a trouvé.

Avant : status='enriched' quoi qu'il arrive, même sans un seul courriel — d'où
145 entreprises annonçant des contacts qu'elles n'ont pas.

⚠️ Nommage : le plan parle de `save_research_result` ; la fonction s'appelle en
réalité `update_company_research`. `save_research` est le nom de l'outil Anthropic
côté agent de recherche, pas la fonction DB.
"""
from __future__ import annotations

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
