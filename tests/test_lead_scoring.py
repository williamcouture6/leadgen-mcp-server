"""Pondération du potentiel : le modèle observe, le code additionne.

Décisions William du 2026-09-01 verrouillées ici :

- le score sort d'une pondération déterministe, plus de la tête du modèle ;
- **seuls** quatre motifs disqualifient (entité publique, annuaire, coopérative,
  plus de 50 employés) — et une disqualification met le score à 0 ;
- un **service de réponse humain 24/7** ne disqualifie pas : la boîte reste
  joignable et reste dans la liste, seul son score descend ;
- un outil déjà en place fait descendre le score, sans le faire s'effondrer ;
- des avis récents **sans réponse du propriétaire** font monter le score (le
  levier existe ; sa source de données reste à brancher, voir plus bas).

⚠️ Le signal `avis_30j_sans_reponse` n'est alimenté par RIEN aujourd'hui :
l'API Places (New) ne rend aucun champ de réponse du propriétaire — le Review
n'a que name, text, rating, publishTime, authorAttribution, visitDate — et
plafonne à 5 avis. Le poids est en place et testé ; il attend une source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.lib.lead_scoring import POIDS, calculer_score
from src.tools import db
from src.tools.research import _ferme_soir_ou_weekend, signaux_mesures

MAINTENANT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _il_y_a(jours: int) -> str:
    return (MAINTENANT - timedelta(days=jours)).isoformat().replace("+00:00", "Z")


# --- le calcul --------------------------------------------------------------

def test_sans_aucun_signal_on_reste_a_la_base() -> None:
    # Tout inconnu : ni bonus ni malus. Une fiche vide ne vaut pas zéro, elle
    # vaut « on ne sait pas », et le tri la place au milieu.
    score, trace = calculer_score({})
    assert score == POIDS["base"]
    assert trace == [f"base +{POIDS['base']}"]


def test_le_profil_cible_marque_haut() -> None:
    score, trace = calculer_score({
        "ferme_soir_ou_weekend": True,
        "promet_urgence_24_7": True,
        "avis_total": 138,
        "avis_30j": 4,
        "villes_desservies": 4,
        "metiers_offerts": 3,
        "rdv_en_ligne": False,
        "saisonnier": True,
        "outil_en_place": False,
    })
    # 25 +14 +10 +8 +6 +4 +3 +8 +4
    assert score == 82
    assert "contradiction_urgence +10" in trace


def test_les_avis_recents_sans_reponse_font_monter() -> None:
    socle = {"avis_total": 138, "avis_30j": 3}
    sans = calculer_score(socle)[0]
    avec_un = calculer_score({**socle, "avis_30j_sans_reponse": 1})[0]
    avec_deux = calculer_score({**socle, "avis_30j_sans_reponse": 4})[0]
    assert avec_un - sans == POIDS["avis_30j_sans_reponse_un"]
    assert avec_deux - sans == POIDS["avis_30j_sans_reponse_deux"]


def test_la_contradiction_exige_les_deux_faits() -> None:
    # Promettre l'urgence 24/7 en étant réellement ouvert 24 h n'est pas une
    # contradiction : c'est une boîte qui tient sa promesse.
    ouvert = calculer_score({"promet_urgence_24_7": True, "ferme_soir_ou_weekend": False})
    assert ouvert[0] == POIDS["base"]


def test_outil_en_place_fait_descendre_sans_disqualifier() -> None:
    socle = {"avis_total": 138, "ferme_soir_ou_weekend": True}
    avant = calculer_score(socle)[0]
    apres = calculer_score({**socle, "outil_en_place": True})[0]
    assert apres - avant == POIDS["outil_en_place"] < 0
    assert apres > 0, "la boîte doit rester dans la liste"


def test_service_humain_24_7_descend_fort_mais_reste_joignable() -> None:
    socle = {"avis_total": 500, "ferme_soir_ou_weekend": True}
    avant = calculer_score(socle)[0]
    apres = calculer_score({**socle, "service_reponse_humain_24_7": True})[0]
    assert apres - avant == POIDS["service_reponse_humain_24_7"] == -25
    # Décision William : ça descend, ça ne disqualifie pas — la boîte reste
    # joignable, donc elle reste dans la liste avec un score non nul.
    assert apres > 0


def test_le_plancher_est_zero() -> None:
    score, trace = calculer_score({
        "outil_en_place": True, "service_reponse_humain_24_7": True,
    })
    assert score == 0
    assert any("borne 0-100" in ligne for ligne in trace)


def test_une_disqualification_met_le_score_a_zero() -> None:
    parfait = {"ferme_soir_ou_weekend": True, "avis_total": 900, "avis_30j": 5}
    assert calculer_score(parfait)[0] > 50
    assert calculer_score(parfait, disqualifie=True)[0] == 0


def test_inconnu_n_est_pas_absent() -> None:
    # `rdv_en_ligne: None` (site illisible) ne doit pas rapporter les points de
    # « aucune prise de RDV en ligne » — sinon un site en panne devient un
    # excellent prospect.
    assert calculer_score({"rdv_en_ligne": None})[0] == POIDS["base"]
    assert calculer_score({"rdv_en_ligne": False})[0] == POIDS["base"] + POIDS["aucun_rdv_en_ligne"]


def test_un_booleen_n_est_pas_un_compte() -> None:
    # `True` vaut 1 en Python : sans garde, avis_total=True passerait pour 1 avis.
    assert calculer_score({"avis_total": True, "villes_desservies": True})[0] == POIDS["base"]


# --- les signaux mesurés ----------------------------------------------------

def test_les_horaires_de_bureau_declenchent_l_ancre() -> None:
    # Lundi-vendredi 8 h-17 h : rien le soir, rien la fin de semaine.
    place = {"regularOpeningHours": {"periods": [
        {"open": {"day": j, "hour": 8}, "close": {"day": j, "hour": 17}}
        for j in range(1, 6)
    ]}}
    assert _ferme_soir_ou_weekend(place) is True


def test_ouvert_le_samedi_et_le_soir_ne_declenche_pas() -> None:
    place = {"regularOpeningHours": {"periods": [
        {"open": {"day": 1, "hour": 8}, "close": {"day": 1, "hour": 20}},
        {"open": {"day": 6, "hour": 9}, "close": {"day": 6, "hour": 16}},
    ]}}
    assert _ferme_soir_ou_weekend(place) is False


def test_ouvert_en_continu_ne_declenche_pas() -> None:
    # Une période sans `close` = ouvert 24 h sur 24 dans l'API Places.
    place = {"regularOpeningHours": {"periods": [{"open": {"day": 0, "hour": 0}}]}}
    assert _ferme_soir_ou_weekend(place) is False


def test_horaires_absents_restent_inconnus() -> None:
    assert _ferme_soir_ou_weekend({}) is None
    assert _ferme_soir_ou_weekend({"regularOpeningHours": {"periods": []}}) is None


def test_les_avis_du_mois_sont_comptes_sur_publish_time() -> None:
    place = {
        "userRatingCount": 138,
        "reviews": [
            {"publishTime": _il_y_a(3)},
            {"publishTime": _il_y_a(11)},
            {"publishTime": _il_y_a(400)},
            {"publishTime": "pas une date"},
        ],
    }
    from src.tools.research import _avis_recents
    assert _avis_recents(place, maintenant=MAINTENANT) == 2


def test_sans_avis_le_compte_reste_inconnu() -> None:
    from src.tools.research import _avis_recents
    assert _avis_recents({}, maintenant=MAINTENANT) is None


def test_les_outils_alimentent_deux_signaux_distincts() -> None:
    # Un Calendly est une prise de RDV ; un chat n'en est pas une.
    place = {"userRatingCount": 40}
    avec_rdv = signaux_mesures(place, {"status": "http_200", "outils_detectes": ["Calendly"]})
    assert avec_rdv["outil_en_place"] is True and avec_rdv["rdv_en_ligne"] is True

    chat_seul = signaux_mesures(place, {"status": "http_200", "outils_detectes": ["Tawk.to"]})
    assert chat_seul["outil_en_place"] is True and chat_seul["rdv_en_ligne"] is False


def test_un_site_illisible_ne_conclut_rien() -> None:
    muet = signaux_mesures({"userRatingCount": 40}, {"status": "error: ConnectError"})
    assert muet["outil_en_place"] is None
    assert muet["rdv_en_ligne"] is None
    assert muet["avis_total"] == 40


# --- la copie en base -------------------------------------------------------

def test_le_patch_calcule_depuis_les_signaux() -> None:
    patch = db.extract_lead_potential_patch({
        "lead_potential": {
            "signaux": {"ferme_soir_ou_weekend": True, "avis_total": 138},
            "reasoning": "ferme le soir, 138 avis",
        }
    })
    assert patch["lead_potential_score"] == 25 + 14 + 8
    assert patch["lead_potential_reason"] == "ferme le soir, 138 avis"


def test_une_disqualification_du_research_ecrase_le_score() -> None:
    patch = db.extract_lead_potential_patch({
        "disqualifications": ["installation municipale"],
        "lead_potential": {"signaux": {"ferme_soir_ou_weekend": True, "avis_total": 900}},
    })
    assert patch["lead_potential_score"] == 0


def test_les_lignes_d_avant_le_2026_09_01_gardent_leur_score() -> None:
    # 283 lignes en base portent un `score` rendu par le modèle. Les signaux
    # n'ont jamais été relevés : impossible de recalculer, on recopie.
    patch = db.extract_lead_potential_patch({"lead_potential": {"score": 65}})
    assert patch["lead_potential_score"] == 65


def test_forme_heritee_invalide_ne_touche_rien() -> None:
    assert db.extract_lead_potential_patch({"lead_potential": {"score": "65"}}) == {}
    assert db.extract_lead_potential_patch({"lead_potential": {}}) == {}
