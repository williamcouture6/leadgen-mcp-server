"""Le malus « déjà outillé » est calculé par le code, jamais par le modèle.

Pourquoi ce déplacement (2026-09-01) : le prompt disait « retire exactement 30
points » et le modèle rendait un `score` déjà ajusté. Sauf qu'un LLM ne tient
pas de registre entre plusieurs ajustements — il produit un seul nombre d'un
coup, puis reconstruit la soustraction dans sa justification. Mesuré sur les
283 scores de prod : **27 valeurs distinctes seulement**, dont 72 pour un quart
de la base. C'est un classificateur à quelques archétypes déguisé en échelle
de 0 à 100, pas une calculatrice.

Le modèle rend donc maintenant ce qu'il sait faire — une base (`score_base`) et
un constat (`outil_en_place`) — et le code fait l'arithmétique. Effet de bord
utile : `score_base` reste dans `research_json`, donc re-régler le malus plus
tard est un UPDATE SQL, pas un rescoring complet à repayer.
"""
from __future__ import annotations

from src.tools import db
from src.tools.research import _RESEARCH_TOOL


def _lp(**kwargs: object) -> dict[str, object]:
    return {"lead_potential": kwargs}


# --- le calcul --------------------------------------------------------------

def test_outil_en_place_retire_exactement_30() -> None:
    patch = db.extract_lead_potential_patch(_lp(score_base=72, outil_en_place=True))
    assert patch["lead_potential_score"] == 42


def test_sans_outil_la_base_passe_telle_quelle() -> None:
    patch = db.extract_lead_potential_patch(_lp(score_base=72, outil_en_place=False))
    assert patch["lead_potential_score"] == 72


def test_constat_absent_vaut_pas_de_malus() -> None:
    patch = db.extract_lead_potential_patch(_lp(score_base=72))
    assert patch["lead_potential_score"] == 72


def test_le_plancher_est_zero_et_la_colonne_est_bien_ecrite() -> None:
    # Sans plancher, une base de 20 donnerait -10, la borne 0-100 rejetterait
    # le patch, et la colonne garderait silencieusement sa vieille valeur.
    patch = db.extract_lead_potential_patch(_lp(score_base=20, outil_en_place=True))
    assert patch["lead_potential_score"] == 0


def test_seul_le_vrai_booleen_declenche_le_malus() -> None:
    # Un modèle qui répond "oui" ou 1 ne doit pas faire chuter un score de 30
    # points par accident : on exige `True`.
    for valeur in ("oui", "true", 1, [1]):
        patch = db.extract_lead_potential_patch(
            _lp(score_base=72, outil_en_place=valeur)
        )
        assert patch["lead_potential_score"] == 72, valeur


def test_le_malus_est_reglable_sans_rescoring(monkeypatch) -> None:
    # La preuve que le nombre vit dans le code : on le change, le calcul suit.
    # En prod, le même changement se fait en SQL depuis `score_base`, sans
    # repayer un seul appel LLM.
    monkeypatch.setattr(db, "MALUS_OUTIL_EN_PLACE", 20)
    patch = db.extract_lead_potential_patch(_lp(score_base=72, outil_en_place=True))
    assert patch["lead_potential_score"] == 52


# --- rétrocompatibilité -----------------------------------------------------

def test_l_ancienne_forme_score_reste_lue() -> None:
    # Les 283 lignes déjà en base portent `score` (déjà ajusté par le modèle),
    # pas `score_base`. Le backfill doit continuer de les copier telles quelles.
    patch = db.extract_lead_potential_patch(_lp(score=65, reasoning="ancienne passe"))
    assert patch["lead_potential_score"] == 65
    assert patch["lead_potential_reason"] == "ancienne passe"


def test_score_base_prime_sur_l_ancien_champ() -> None:
    patch = db.extract_lead_potential_patch(_lp(score_base=80, score=10))
    assert patch["lead_potential_score"] == 80


# --- garde-fous inchangés ---------------------------------------------------

def test_base_non_entiere_ne_touche_rien() -> None:
    assert db.extract_lead_potential_patch(_lp(score_base="72", outil_en_place=True)) == {}
    assert db.extract_lead_potential_patch(_lp(score_base=True)) == {}


def test_base_hors_borne_ne_touche_rien() -> None:
    assert db.extract_lead_potential_patch(_lp(score_base=150)) == {}
    assert db.extract_lead_potential_patch(_lp(score_base=-1)) == {}


# --- ce que le modèle a le droit de rendre ----------------------------------

def test_le_schema_demande_la_base_et_le_constat() -> None:
    champs = _RESEARCH_TOOL["input_schema"]["properties"]["lead_potential"]["properties"]
    assert set(champs) == {"score_base", "outil_en_place", "reasoning"}
    assert champs["outil_en_place"]["type"] == ["boolean", "null"]


def test_le_prompt_interdit_au_modele_de_soustraire() -> None:
    from src.tools import research

    p = research._PROMPT_PATH.read_text(encoding="utf-8")
    assert "Ne retire RIEN toi-même" in p
    assert "score_base" in p
    # L'ancienne consigne arithmétique ne doit pas revenir par un copier-coller.
    assert "retire exactement 30 points" not in p
