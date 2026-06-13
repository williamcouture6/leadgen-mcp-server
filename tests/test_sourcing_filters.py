"""Tests du filtre de junk au sourcing WF-1 (lib.sourcing_filters).

Contrat épinglé ici :
  - primaryType bien-être / hôtel / gym / OSBL → rejeté (raison primary_type:*).
  - Chaînes retail (Trévi, Club Piscine, Club Spa, Piscines Soucy) → rejetées
    par NOM peu importe le type (raison chain:*).
  - Installateurs piscine locaux taggés sporting_goods_store/store → PASSENT
    (on ne jette pas les vrais contracteurs avec les détaillants).
  - Types piscine légit (swimming_pool, general_contractor, service) → passent.
"""
from __future__ import annotations

from src.lib.sourcing_filters import sourcing_disqualify_reason


# ---------------- primaryType bloqués (bien-être / hors-cible) ----------------

def test_spa_detente_rejete() -> None:
    reason = sourcing_disqualify_reason("Spa Escale Santé à Rosemont", "spa")
    assert reason == "primary_type:spa"


def test_massage_spa_hotel_gym_osbl_rejetes() -> None:
    for name, ptype in [
        ("Centre de Santé La Source", "massage_spa"),
        ("Hôtel Le Bonne Entente", "hotel"),
        ("Club Sportif MAA", "gym"),
        ("SPA de Québec", "non_profit_organization"),
    ]:
        assert sourcing_disqualify_reason(name, ptype) == f"primary_type:{ptype}"


# ---------------- chaînes retail bloquées par nom ----------------

def test_chaines_rejetees_par_nom() -> None:
    """Trévi / Club Piscine / Club Spa / Soucy = franchises, peu importe le type
    Google (souvent sporting_goods_store, qu'on NE bloque pas en bloc)."""
    for name in [
        "Trévi Boucherville",
        "Trévi Montréal (Pointe-Claire)",
        "Club Piscine Super Fitness",
        "Club Piscine - Québec",
        "Club Spa Inc",
        "Piscines Soucy - Spas - Meubles - BBQ",
    ]:
        reason = sourcing_disqualify_reason(name, "sporting_goods_store")
        assert reason is not None and reason.startswith("chain:"), name


def test_chaine_match_insensible_casse_et_accents_trevi() -> None:
    assert sourcing_disqualify_reason("TREVI Québec", "store") == "chain:trevi"
    assert sourcing_disqualify_reason("Trévi Lévis", "store") == "chain:trévi"


# ---------------- installateurs locaux légit : PASSENT ----------------

def test_installateurs_piscine_locaux_passent() -> None:
    """Régression : on ne doit PAS jeter les vrais contracteurs piscine taggés
    sporting_goods_store par Google."""
    for name in [
        "Piscines Gratton",
        "Concept Piscine Design",
        "Aqua Fibre Innovation Inc",
        "Piscine Hippocampe",
        "Piscines & Spas Perrin",
    ]:
        assert sourcing_disqualify_reason(name, "sporting_goods_store") is None, name


def test_types_piscine_legit_passent() -> None:
    for ptype in ["swimming_pool", "general_contractor", "service", "roofing_contractor"]:
        assert sourcing_disqualify_reason("Piscines Élégance Québec", ptype) is None


# ---------------- robustesse entrées vides ----------------

def test_name_et_type_vides_passent() -> None:
    assert sourcing_disqualify_reason(None, None) is None
    assert sourcing_disqualify_reason("", "") is None
    assert sourcing_disqualify_reason("Déneigement ABC", None) is None
