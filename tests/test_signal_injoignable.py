"""« Je n'arrive pas à les joindre » → tête de la file de prospection.

Décision William du 2026-09-01 : un avis Google où un client se plaint de ne
pas réussir à joindre l'entreprise met ce lead au-dessus de tous les autres,
**quoi que dise le reste du barème** — seule une disqualification le devance.
Ces clients décrivent mot pour mot le problème que l'offre règle.

Le garde-fou est mesuré, pas théorique. Sur les 405 fiches recherchées en
prod, l'appariement de mots-clés (`rappel`, `répond`, `joindre`, `retour
d'appel`, `oublié`) sur la citation d'avis marque **60 boîtes** — dont **35
avis 5 ★ qui vantent la rapidité de réponse**. Les citations de ce fichier
sont réelles, prises dans la base : quatre plaintes, deux félicitations. Avec
un écrasement à 100, un faux positif propulserait un client satisfait en tête
de file, d'où la double garde : le modèle lit le SENS, le code exige qu'un
avis du lot porte une note assez basse pour attester d'une plainte.
"""
from __future__ import annotations

from src.lib.lead_scoring import NOTE_MAX_PLAINTE, SCORE_INJOIGNABLE, calculer_score
from src.tools.research import _note_la_plus_basse, signaux_mesures

# Citations réelles de la base (companies.research_json->recent_review_snippet).
PLAINTES = [
    (1, "Il ne répond pas au téléphone non plus, donc impossible de le rejoindre."),
    (1, "J'ai demandé un retour d'appel, mais je n'ai jamais reçu de réponse."),
    (2, "Appelé à plusieurs reprises, mais soit personne ne répondait."),
    (3, "Service à la clientèle difficile à joindre : délais de réponse de plusieurs jours."),
]
FELICITATIONS = [
    (5, "Demande envoyée un vend soir 22h30 / Rappel tôt samedi am et visite le jour-même."),
    (5, "Amine a répondu rapidement à mon appel et a pris le temps de tout m'expliquer."),
]


def test_une_plainte_met_le_lead_en_tete() -> None:
    for note, citation in PLAINTES:
        score, trace = calculer_score({
            "avis_disent_injoignable": True,
            "avis_note_min": note,
        })
        assert score == SCORE_INJOIGNABLE, citation
        assert "avis_disent_injoignable" in trace[0]


def test_la_tete_de_file_ignore_tout_le_reste_du_bareme() -> None:
    # Une boîte par ailleurs sans intérêt : ouverte 24 h, outillée, sans avis
    # récents. Elle passe quand même devant tout le monde.
    score, _ = calculer_score({
        "avis_disent_injoignable": True, "avis_note_min": 1,
        "ferme_soir_ou_weekend": False,
        "outil_en_place": True,
        "service_reponse_humain_24_7": True,
    })
    assert score == SCORE_INJOIGNABLE


def test_la_disqualification_reste_souveraine() -> None:
    # « sauf s'il est disqualifié » — décision William. Une municipalité dont
    # un citoyen dit qu'elle ne rappelle pas n'est pas un prospect.
    score, trace = calculer_score(
        {"avis_disent_injoignable": True, "avis_note_min": 1}, disqualifie=True
    )
    assert score == 0
    assert trace == ["disqualifie -> 0"]


def test_un_avis_elogieux_ne_propulse_personne() -> None:
    # Le piège mesuré : 35 des 60 correspondances par mots-clés étaient des
    # 5 ★ qui félicitent la rapidité. Si le modèle se trompe de sens, la note
    # de l'avis le rattrape.
    for note, citation in FELICITATIONS:
        score, _ = calculer_score({
            "avis_disent_injoignable": True,
            "avis_note_min": note,
            "avis_total": 138,
        })
        assert score < SCORE_INJOIGNABLE, citation
        # Le lead retombe sur son score normal, il n'est pas puni non plus.
        assert score == calculer_score({"avis_total": 138})[0]


def test_sans_note_d_avis_le_constat_tombe() -> None:
    # Aucun avis dans le lot : le modèle ne peut pas avoir lu une plainte.
    score, _ = calculer_score({"avis_disent_injoignable": True, "avis_note_min": None})
    assert score < SCORE_INJOIGNABLE


def test_la_bordure_de_la_garde_est_incluse() -> None:
    assert calculer_score({
        "avis_disent_injoignable": True, "avis_note_min": NOTE_MAX_PLAINTE,
    })[0] == SCORE_INJOIGNABLE
    assert calculer_score({
        "avis_disent_injoignable": True, "avis_note_min": NOTE_MAX_PLAINTE + 1,
    })[0] < SCORE_INJOIGNABLE


def test_un_constat_absent_ne_change_rien() -> None:
    socle = {"avis_total": 138, "ferme_soir_ou_weekend": True}
    assert calculer_score(socle)[0] == calculer_score(
        {**socle, "avis_disent_injoignable": False, "avis_note_min": 1}
    )[0]


# --- la mesure de la note la plus basse -------------------------------------

def test_la_note_la_plus_basse_du_lot_est_retenue() -> None:
    place = {"reviews": [{"rating": 5}, {"rating": 2}, {"rating": 4}]}
    assert _note_la_plus_basse(place) == 2


def test_sans_avis_la_note_reste_inconnue() -> None:
    assert _note_la_plus_basse({}) is None
    assert _note_la_plus_basse({"reviews": [{"text": "sans note"}]}) is None


def test_la_note_min_voyage_dans_les_signaux_mesures() -> None:
    mesures = signaux_mesures(
        {"userRatingCount": 138, "reviews": [{"rating": 5}, {"rating": 1}]},
        {"status": "http_200", "outils_detectes": []},
    )
    assert mesures["avis_note_min"] == 1
