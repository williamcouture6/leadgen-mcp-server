"""La ROUTE de conformité, et non la fonction.

🔴 C'est le test qui manquait, et son absence est ce qui a laissé passer le
bloquant du conseil final. Les quatre tests de `test_config_lcap.py` appellent
`comp.compliance_check(...)` directement — la fonction. L'écriture en base, elle,
se produit dans la ROUTE, juste après. Le docstring de ces tests affirmait
« rien n'ayant été écrit en base » sans jamais le vérifier, et c'était faux.

Ce fichier monte la vraie route avec un `db.update` qui capture, et regarde ce
qui touche réellement `messages`.
"""
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.http_api import ComplianceCheckIn
from src.tools import compliance as compliance_tools


def _projeter(row: dict[str, Any], params: dict[str, str] | None) -> dict[str, Any]:
    """Imite PostgREST : une colonne hors du `select` n'existe pas dans la ligne.

    ⚠️ Sans cette projection, le test serait faux-vert sur toute la famille des
    colonnes oubliées dans un `select` — exactement la classe de défaut que le
    câblage des avis a déjà produite deux fois.
    """
    demandees = [c.strip() for c in ((params or {}).get("select") or "*").split(",")]
    if "*" in demandees:
        return dict(row)
    return {k: v for k, v in row.items() if k in demandees}


def _socle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    followups: dict[str, str] | None = None,
    google_rating: float | None = 4.8,
    google_reviews_count: int | None = 47,
) -> dict[str, Any]:
    from src import supabase_client as sb
    from src.lib import calcom as calcom_mod

    capture: dict[str, Any] = {"juge": {}, "maj": []}

    message = {
        "id": "msg-1", "subject": "s", "body_text": "Corps",
        "contact_id": "c-1", "generated_by_agent_run": None,
        "compliance_check_passed": None, "compliance_tentatives": 0,
        "followups": followups,
    }
    contact = {
        "id": "c-1", "company_id": "co-1", "first_name": "Marc",
        "last_name": "T", "title": "Proprio",
        "email_verification_source": "website_scrape",
    }
    company = {
        "id": "co-1", "research_json": {"x": 1}, "track": "agence-ia",
        "google_rating": google_rating, "google_reviews_count": google_reviews_count,
    }

    async def faux_select(table, *, params=None, schema=None, **kw):
        if table == "messages":
            return [_projeter(message, params)]
        if table == "contacts":
            return [_projeter(contact, params)]
        if table == "companies":
            return [_projeter(company, params)]
        return []

    async def faux_update(table, patch, *, filters=None, schema=None, **kw):
        capture["maj"].append({"table": table, "patch": patch})
        return [{"id": "msg-1"}]

    # ⚠️ On capture la VRAIE fonction AVANT de la remplacer.
    #
    # La première version de ce socle appelait `compliance_tools.compliance_check`
    # depuis le faux — donc lui-même après le monkeypatch. La récursion infinie
    # était avalée par le `except` de la route, qui rendait `error` sans rien
    # persister : le test « aucune écriture » passait pour une raison qui n'avait
    # rien à voir avec le correctif. Un faux-vert de plus, trouvé en écrivant le
    # contrôle négatif — c'est précisément à ça qu'il sert.
    vraie_fonction = compliance_tools.compliance_check

    async def faux_juge(**kw):
        capture["juge"] = kw
        return await vraie_fonction(**kw)

    monkeypatch.setattr(sb, "select", faux_select)
    monkeypatch.setattr(sb, "update", faux_update)
    monkeypatch.setattr(compliance_tools, "compliance_check", faux_juge)
    monkeypatch.setattr(calcom_mod, "get_available_slots", lambda **kw: [])
    return capture


# ---------------- Le bloquant : une config en faute n'ecrit RIEN ----------------

@pytest.mark.asyncio
async def test_une_config_incomplete_nECRIT_rien_sur_le_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """🔴 Le test qui aurait attrapé le bloquant du conseil final.

    Avec `LCAP_MENTIONS_REDUITES=true` et `INSTANTLY_CAMPAIGN_FOOTER` vide —
    l'état exact du go-live — la route rendait `error` ET écrivait
    `compliance_check_passed=false`. Le brouillon quittait le lot pour toujours,
    son contact gelait à vie, 20 par jour, 255 en deux semaines.
    """
    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", "")
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture IA")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")

    cap = _socle(monkeypatch)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))

    assert out.verdict == "error"
    ecritures = [m["patch"] for m in cap["maj"] if m["table"] == "messages"]
    for patch in ecritures:
        assert "compliance_check_passed" not in patch, (
            f"écrire {patch} sort le brouillon du lot POUR TOUJOURS, pour une "
            "variable d'environnement vide"
        )
        assert "compliance_tentatives" not in patch, (
            "une config absente n'est pas une tentative de jugement : "
            "l'incrémenter atteindrait le plafond anti-boucle en 3 passes"
        )


@pytest.mark.asyncio
async def test_une_config_complete_ecrit_normalement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrôle négatif : le correctif ne doit pas empêcher toute écriture."""
    from tests.fixtures.corps_ac1 import SIGNATURE_COMPTE_INSTANTLY

    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY)
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "William Couture")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")

    cap = _socle(monkeypatch)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))

    ecritures = [m["patch"] for m in cap["maj"] if m["table"] == "messages"]
    assert ecritures, "un verdict normal doit se persister"
    assert "compliance_check_passed" in ecritures[0]


# ---------------- Le cablage : ce que la route passe reellement au juge ----------------

@pytest.fixture(autouse=True)
def _env_vert(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.fixtures.corps_ac1 import SIGNATURE_COMPTE_INSTANTLY

    monkeypatch.setenv("WARMUP_DISABLED", "true")
    monkeypatch.setenv("LCAP_MENTIONS_REDUITES", "true")
    monkeypatch.setenv("INSTANTLY_CAMPAIGN_FOOTER", SIGNATURE_COMPTE_INSTANTLY)
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "William Couture")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")


@pytest.mark.asyncio
async def test_la_route_passe_les_relances_au_juge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans ce test, retirer `followups` du `select` ferait juger UN corps sur
    trois — et les deux relances partiraient sans avoir été inspectées."""
    triplet = {"relance_1": "r1", "relance_2": "r2"}
    cap = _socle(monkeypatch, followups=triplet)

    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))

    assert cap["juge"]["followups"] == triplet


@pytest.mark.asyncio
async def test_la_route_passe_les_avis_au_juge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans ce test, retirer les colonnes du `select` bloquerait 100 % des
    entreprises qui annoncent une note — `check_avis_conformes` refuse tout
    chiffre quand la colonne arrive à None."""
    cap = _socle(monkeypatch, google_rating=4.8, google_reviews_count=47)

    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))

    assert cap["juge"]["google_rating"] == 4.8
    assert cap["juge"]["google_reviews_count"] == 47


@pytest.mark.asyncio
async def test_sans_relances_le_juge_recoit_None(monkeypatch: pytest.MonkeyPatch) -> None:
    """La piste OPT et les messages antérieurs à AC1b n'en ont pas."""
    cap = _socle(monkeypatch, followups=None)

    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))

    assert cap["juge"]["followups"] is None
