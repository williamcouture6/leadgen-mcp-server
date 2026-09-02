"""« Je n'arrive pas à les joindre » → tête de la file, sans toucher au score.

Décision William du 2026-09-01 : un avis Google où un client se plaint de ne
pas réussir à joindre l'entreprise place ce lead devant les autres — mais
**son score reste celui du barème**. Un lead à 8 reste à 8 : son score dit ce
qu'il vaut, et une note écrite à côté dit pourquoi il passe quand même devant.
Écraser le score à 100 aurait détruit l'information et rendu tous les leads
prioritaires indistinguables.

Le garde-fou est mesuré, pas théorique. Sur les 405 fiches recherchées en
prod, l'appariement des mots-clés prévus par la spec 2026-08-27 (`rappel`,
`répond`, `joindre`, `retour d'appel`, `oublié`) sur la citation d'avis marque
**60 boîtes** — dont **35 avis 5 ★ qui vantent la rapidité de réponse**. Les
citations de ce fichier sont réelles, prises dans la base : quatre plaintes,
deux félicitations. Le modèle lit le sens ; le code exige en plus qu'un avis
du lot porte une note assez basse pour attester d'une plainte.
"""
from __future__ import annotations

from src.lib.lead_scoring import (
    MARQUEUR_TETE_DE_FILE,
    NOTE_MAX_PLAINTE,
    calculer_score,
    est_tete_de_file,
)
from src.tools import db
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


# --- le signal ---------------------------------------------------------------

def test_une_plainte_met_le_lead_en_tete() -> None:
    for note, citation in PLAINTES:
        assert est_tete_de_file(
            {"avis_disent_injoignable": True, "avis_note_min": note}
        ), citation


def test_un_avis_elogieux_ne_propulse_personne() -> None:
    # Le piège mesuré : 35 des 60 correspondances par mots-clés étaient des
    # 5 ★ qui félicitent la rapidité. Si le modèle se trompe de sens, la note
    # de l'avis le rattrape.
    for note, citation in FELICITATIONS:
        assert not est_tete_de_file(
            {"avis_disent_injoignable": True, "avis_note_min": note}
        ), citation


def test_sans_note_d_avis_le_constat_tombe() -> None:
    # Aucun avis dans le lot : le modèle ne peut pas avoir lu une plainte.
    assert not est_tete_de_file({"avis_disent_injoignable": True, "avis_note_min": None})


def test_la_bordure_de_la_garde_est_incluse() -> None:
    assert est_tete_de_file(
        {"avis_disent_injoignable": True, "avis_note_min": NOTE_MAX_PLAINTE}
    )
    assert not est_tete_de_file(
        {"avis_disent_injoignable": True, "avis_note_min": NOTE_MAX_PLAINTE + 1}
    )


def test_une_boite_disqualifiee_ne_passe_jamais_devant() -> None:
    # « sauf s'il est disqualifié » — décision William. Une municipalité dont
    # un citoyen dit qu'elle ne rappelle pas n'est pas un prospect.
    assert not est_tete_de_file(
        {"avis_disent_injoignable": True, "avis_note_min": 1}, disqualifie=True
    )


# --- le score ne bouge pas ---------------------------------------------------

def test_le_score_reste_celui_du_bareme() -> None:
    socle = {"avis_total": 138, "ferme_soir_ou_weekend": True}
    sans = calculer_score(socle)[0]
    avec = calculer_score({
        **socle, "avis_disent_injoignable": True, "avis_note_min": 1,
    })[0]
    assert avec == sans, "le signal marque le lead, il ne gonfle pas sa note"


def test_un_petit_score_reste_petit() -> None:
    # Le cas réel : cVert Québec est à 8 et son avis dit « jamais eu de retour
    # d'appel ». Il passe devant, il reste à son score.
    signaux = {
        "avis_disent_injoignable": True, "avis_note_min": 1,
        "outil_en_place": True, "service_reponse_humain_24_7": True,
    }
    assert calculer_score(signaux)[0] < 10
    assert est_tete_de_file(signaux)


# --- la note écrite en base --------------------------------------------------

def test_la_note_explique_la_place_en_tete() -> None:
    patch = db.extract_lead_potential_patch({
        "lead_potential": {
            "signaux": {
                "avis_disent_injoignable": True, "avis_note_min": 1,
                "avis_total": 138,
            },
            "reasoning": "138 avis, un client dit ne jamais avoir eu de retour d'appel.",
        }
    })
    raison = patch["lead_potential_reason"]
    assert raison.startswith(MARQUEUR_TETE_DE_FILE), raison
    assert "138 avis" in raison, "la justification du modèle est conservée"
    # Le score, lui, n'a pas bougé.
    assert patch["lead_potential_score"] == calculer_score({"avis_total": 138})[0]


def test_la_marque_tient_meme_sans_justification_du_modele() -> None:
    patch = db.extract_lead_potential_patch({
        "lead_potential": {"signaux": {"avis_disent_injoignable": True, "avis_note_min": 2}}
    })
    assert patch["lead_potential_reason"] == MARQUEUR_TETE_DE_FILE


def test_pas_de_marque_sans_signal() -> None:
    patch = db.extract_lead_potential_patch({
        "lead_potential": {"signaux": {"avis_total": 138}, "reasoning": "rien de spécial"}
    })
    assert patch["lead_potential_reason"] == "rien de spécial"


def test_une_disqualification_efface_la_marque_et_le_score() -> None:
    patch = db.extract_lead_potential_patch({
        "disqualifications": ["installation municipale"],
        "lead_potential": {
            "signaux": {"avis_disent_injoignable": True, "avis_note_min": 1},
            "reasoning": "ville de Québec",
        },
    })
    assert patch["lead_potential_score"] == 0
    assert MARQUEUR_TETE_DE_FILE not in patch["lead_potential_reason"]


def test_la_troncature_a_500_garde_la_marque_lisible() -> None:
    patch = db.extract_lead_potential_patch({
        "lead_potential": {
            "signaux": {"avis_disent_injoignable": True, "avis_note_min": 1},
            "reasoning": "x" * 900,
        }
    })
    raison = patch["lead_potential_reason"]
    assert len(raison) == 500
    # La marque est en tête, donc elle survit — c'est ce qui rend le
    # `like '⚑%'` fiable pour sortir la file prioritaire.
    assert raison.startswith(MARQUEUR_TETE_DE_FILE)


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
