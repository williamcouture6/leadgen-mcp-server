"""WF-5 : câblage du verdict `non_juge` et persistance du verdict.

`compliance_check` (le tool) sait rendre `non_juge` depuis la livraison
précédente, mais la route qui l'appelle ne lui passait ni `track` ni
`tentatives`, et écrivait `compliance_check_passed = (verdict == "approved")` —
donc `false` sur un `non_juge`. `false` FIGE le draft hors du lot pour toujours
(la requête du lot ne cherche que `is.null`) : la garde des 3 tentatives ne
serait jamais atteinte et une panne passagère du juge deviendrait un refus
définitif.

Les selects sont projetés ici comme PostgREST le fait vraiment — une colonne
absente du `select` est absente de la ligne rendue. C'est ce qui rend les tests
de câblage non-falsifiables : si l'implémentation retire `compliance_tentatives`
du select, la valeur n'arrive plus au juge et le test tombe.
"""
from __future__ import annotations

from typing import Any

import pytest

from src import http_api
from src.http_api import ComplianceCheckIn, _patch_verdict_conformite
from src.tools import compliance as compliance_tools


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test")


# =====================================================================
# A2 — la fonction pure du patch
# =====================================================================

def test_patch_laisse_passed_null_sur_non_juge():
    p = _patch_verdict_conformite("non_juge", tentatives_avant=0)
    assert p["compliance_verdict"] == "non_juge"
    assert "compliance_check_passed" not in p, "passed doit rester NULL"
    assert p["compliance_tentatives"] == 1


def test_patch_pose_passed_true_sur_approved():
    assert _patch_verdict_conformite("approved", tentatives_avant=0)["compliance_check_passed"] is True


def test_patch_pose_passed_false_sur_refus():
    assert _patch_verdict_conformite("needs_revision", tentatives_avant=1)["compliance_check_passed"] is False


def test_patch_incremente_toujours_les_tentatives():
    assert _patch_verdict_conformite("approved", tentatives_avant=2)["compliance_tentatives"] == 3


def test_patch_tolere_des_tentatives_nulles():
    """`compliance_tentatives` absent d'un SELECT rend None : `None + 1` lèverait."""
    assert _patch_verdict_conformite("approved", tentatives_avant=None)["compliance_tentatives"] == 1


# =====================================================================
# A3/A4 — la route /compliance/check
# =====================================================================

def _projeter(row: dict[str, Any], params: dict[str, str] | None) -> dict[str, Any]:
    """Imite PostgREST : une colonne hors du `select` n'existe pas dans la ligne."""
    demandees = [c.strip() for c in ((params or {}).get("select") or "*").split(",")]
    if "*" in demandees:
        return dict(row)
    return {k: v for k, v in row.items() if k in demandees}


def _socle_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict: str = "approved",
    tentatives_en_base: int = 0,
    track_en_base: str | None = "agence-ia",
    company_absente: bool = False,
    contact_absent: bool = False,
    update_leve: bool = False,
    update_rend_vide: bool = False,
) -> dict[str, Any]:
    """Rend un dict de capture : `appels` (kwargs reçus par le juge), `maj` (patches)."""
    from src import supabase_client as sb
    from src.lib import calcom as calcom_mod

    capture: dict[str, Any] = {"appels": {}, "maj": []}

    ligne_message = {
        "id": "msg-1", "subject": "Une question", "body_text": "Corps du courriel",
        "contact_id": None if contact_absent else "c-1",
        "generated_by_agent_run": None,
        "compliance_check_passed": None,
        "compliance_tentatives": tentatives_en_base,
    }
    ligne_contact = {
        "id": "c-1", "company_id": "co-1", "first_name": "Marc",
        "last_name": "Tremblay", "title": "Proprio",
        "email_verification_source": "website_scrape",
    }
    ligne_company = {"id": "co-1", "research_json": {"nom": "X"}, "track": track_en_base}

    async def fake_select(table, *, params=None, schema=None, **kw):
        if table == "messages":
            return [_projeter(ligne_message, params)]
        if table == "contacts":
            return [] if contact_absent else [_projeter(ligne_contact, params)]
        if table == "companies":
            return [] if company_absente else [_projeter(ligne_company, params)]
        return []

    async def fake_update(table, patch, *, filters=None, schema=None, **kw):
        capture["maj"].append({"table": table, "patch": patch, "filters": filters})
        if update_leve:
            raise RuntimeError("supabase injoignable")
        return [] if update_rend_vide else [{"id": "msg-1"}]

    async def fake_juge(**kw):
        capture["appels"] = kw
        return compliance_tools.ComplianceCheckOut(
            message_id=kw["message_id"], verdict=verdict,
            send_decision="SEND" if verdict == "approved" else "DO_NOT_SEND",
        )

    monkeypatch.setattr(sb, "select", fake_select)
    monkeypatch.setattr(sb, "update", fake_update)
    monkeypatch.setattr(compliance_tools, "compliance_check", fake_juge)
    monkeypatch.setattr(calcom_mod, "get_available_slots", lambda **kw: [])
    return capture


@pytest.mark.asyncio
async def test_route_passe_le_track_reel_au_juge(monkeypatch):
    cap = _socle_check(monkeypatch, track_en_base="agence-ia")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["track"] == "agence-ia"


@pytest.mark.asyncio
async def test_route_ne_force_jamais_le_track_a_opt(monkeypatch):
    """`or "OPT"` ferait vouvoyer un corps tutoyé et bloquerait tout agence-ia."""
    cap = _socle_check(monkeypatch, track_en_base=None)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["track"] is None


@pytest.mark.asyncio
async def test_route_sans_company_ne_force_pas_de_track(monkeypatch):
    cap = _socle_check(monkeypatch, company_absente=True)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["track"] is None
    assert cap["appels"]["research_json"] == {}


@pytest.mark.asyncio
async def test_route_passe_les_tentatives_lues_en_base(monkeypatch):
    cap = _socle_check(monkeypatch, tentatives_en_base=2)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert cap["appels"]["tentatives"] == 2


@pytest.mark.asyncio
async def test_non_juge_laisse_passed_null_en_base(monkeypatch):
    cap = _socle_check(monkeypatch, verdict="non_juge", tentatives_en_base=1)
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    patch = cap["maj"][0]["patch"]
    assert "compliance_check_passed" not in patch
    assert patch["compliance_verdict"] == "non_juge"
    assert patch["compliance_tentatives"] == 2


@pytest.mark.asyncio
async def test_approved_ecrit_passed_true_en_base(monkeypatch):
    cap = _socle_check(monkeypatch, verdict="approved")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    patch = cap["maj"][0]["patch"]
    assert patch["compliance_check_passed"] is True
    assert patch["compliance_verdict"] == "approved"
    assert "compliance_notes" in patch


@pytest.mark.asyncio
async def test_dry_run_n_ecrit_rien(monkeypatch):
    cap = _socle_check(monkeypatch, verdict="blocked")
    await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1", persist=False))
    assert cap["maj"] == []


@pytest.mark.asyncio
async def test_echec_decriture_finit_dans_le_journal(monkeypatch):
    """Un verdict non persisté qui se croit persisté est le mode d'échec de 1bfb918."""
    _socle_check(monkeypatch, verdict="blocked", update_leve=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.verdict == "blocked", "le verdict rendu ne change pas"
    assert out.error_text and "persist" in out.error_text


@pytest.mark.asyncio
async def test_ecriture_sans_ligne_touchee_est_un_echec(monkeypatch):
    """PostgREST rend [] quand aucune ligne ne matche : silencieux, mais raté."""
    _socle_check(monkeypatch, verdict="non_juge", update_rend_vide=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.error_text and "persist" in out.error_text


@pytest.mark.asyncio
async def test_ecriture_reussie_ne_salit_pas_le_journal(monkeypatch):
    _socle_check(monkeypatch, verdict="approved")
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.error_text is None


@pytest.mark.asyncio
async def test_message_sans_contact_ne_juge_rien(monkeypatch):
    cap = _socle_check(monkeypatch, contact_absent=True)
    out = await http_api.compliance_check(ComplianceCheckIn(message_id="msg-1"))
    assert out.verdict == "error"
    assert out.error_text == "contact_not_found"
    assert cap["maj"] == [], "aucun verdict ne s'écrit sur une erreur de fetch"
