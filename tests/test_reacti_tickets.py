"""Tests REACTI — résolution verticale, grille ticket/commission, gate OPT,
et injection de la ligne commission dans le brief booké Slack (WF-8)."""
from __future__ import annotations

import pytest

from src.lib import reacti_tickets as rt
from src.lib.slack import build_booked_blocks


# ----------------------------------------------------------------------
# resolve_vertical / ticket_for_company
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "industry,expected",
    [
        ("Déneigement résidentiel", "deneigement"),
        ("Snow removal", "deneigement"),
        ("Service de tonte de pelouse", "tonte"),
        ("Lawn mowing", "tonte"),
        ("Entretien de piscine", "piscine"),
        ("Pool & spa maintenance", "piscine"),
        ("Exterminateur / gestion parasitaire", "extermination"),
        ("Pest control", "extermination"),
        ("Nettoyage de vitres résidentiel", "vitres"),
        ("Window cleaning", "vitres"),
    ],
)
def test_resolve_vertical_par_industry(industry: str, expected: str) -> None:
    assert rt.resolve_vertical(industry=industry) == expected


def test_paysagiste_4_saisons_quand_ete_et_hiver() -> None:
    """Réalité QC : une boîte qui tond ET déneige => contrat annuel 4-saisons."""
    assert rt.resolve_vertical(industry="Paysagement, tonte de gazon et déneigement") == "paysagiste_4_saisons"
    assert rt.resolve_vertical(
        industry="Paysagiste", google_types=["lawn_care", "snow_removal"]
    ) == "paysagiste_4_saisons"


def test_gate_opt_retourne_none() -> None:
    """Prospect OPT (santé/pro) => aucune verticale REACTI => None (no-op brief)."""
    assert rt.resolve_vertical(industry="Clinique de physiothérapie") is None
    assert rt.resolve_vertical(industry="Cabinet dentaire", google_types=["dentist"]) is None
    assert rt.resolve_vertical(industry=None, google_types=None) is None
    assert rt.ticket_for_company(industry="Clinique dentaire") is None


def test_explicit_prioritaire_sur_inference() -> None:
    """Un champ verticale explicite (sourcing REACTI à venir) prime sur les mots-clés."""
    assert rt.resolve_vertical(industry="Déneigement", explicit="vitres") == "vitres"


def test_explicit_invalide_retombe_sur_inference() -> None:
    assert rt.resolve_vertical(industry="Déneigement", explicit="inexistant") == "deneigement"


def test_ticket_for_company_valeurs_grille() -> None:
    t = rt.ticket_for_company(industry="Déneigement")
    assert t is not None
    assert t.vertical == "deneigement"
    assert t.ticket == 700
    assert t.commission == 105  # 700 * 0.15
    assert t.rate_pct == 15
    assert "Déneigement" in t.label


def test_commission_for_arrondi() -> None:
    assert rt.commission_for(700) == 105
    assert rt.commission_for(800) == 120
    assert rt.commission_for(1500) == 225
    assert rt.commission_for(250) == 38  # 37.5 -> 38


# ----------------------------------------------------------------------
# Injection dans le brief booké (WF-8)
# ----------------------------------------------------------------------

def test_brief_inclut_ligne_commission_si_reacti() -> None:
    t = rt.ticket_for_company(industry="Déneigement")
    _, blocks = build_booked_blocks(
        contact_name="Marc",
        company_name="Déneigement Tremblay",
        contact_email="marc@tremblay.ca",
        meeting_start_iso="2026-08-15T18:00:00Z",
        reacti_ticket=t,
    )
    body = " ".join(str(b) for b in blocks)
    assert "REACTI" in body
    assert "Déneigement" in body
    assert "700" in body
    assert "105" in body


def test_brief_sans_ligne_commission_pour_opt() -> None:
    """reacti_ticket=None (prospect OPT) => brief strictement inchangé."""
    _, blocks = build_booked_blocks(
        contact_name="Anne",
        company_name="Clinique X",
        contact_email="anne@x.com",
        meeting_start_iso="2026-05-28T18:00:00Z",
        reacti_ticket=None,
    )
    body = " ".join(str(b) for b in blocks)
    assert "REACTI" not in body
    assert "économie commission" not in body
