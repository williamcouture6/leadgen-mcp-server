"""Barème de potentiel du lead : le modèle observe, le code pondère.

Pourquoi ce partage (décision William, 2026-09-01) : un LLM ne tient pas de
registre entre plusieurs ajustements. Il produit un seul nombre d'un coup et
reconstruit la justification après. Mesuré sur les 283 scores de prod : 27
valeurs distinctes seulement, dont 72 pour un quart de la base, et jamais rien
au-dessus de 82 — un classificateur à quelques archétypes déguisé en échelle
de 0 à 100. Les quatre justifications à 72 disaient la même phrase.

Le modèle rend donc des CONSTATS (« la boîte promet l'urgence 24/7 »), certains
signaux sont MESURÉS en Python (avis, horaires, outils), et ce module fait la
seule chose qu'un LLM ne sait pas faire : additionner toujours pareil.

Les poids ci-dessous sont un point de départ argumenté, pas une vérité : sans
un seul courriel envoyé, personne ne peut dire si le rythme des avis vaut 6
points ou 20. Ils sont regroupés dans `POIDS` pour être re-réglés d'une seule
main — et comme les signaux vivent dans `research_json`, re-régler ne coûte
qu'un UPDATE SQL, jamais un rescoring LLM.
"""
from __future__ import annotations

from typing import Any

# Un signal inconnu (`None`) ne vaut JAMAIS un signal absent : il ne rapporte
# ni ne retire rien. Sans ça, une fiche Google sans horaires ferait chuter une
# boîte qui ferme peut-être le soir.
# Un lead dont les avis disent qu'on n'arrive pas à le joindre passe en tête de
# file — mais son score N'EST PAS gonflé pour autant (décision William,
# 2026-09-01) : un lead à 8 reste à 8, son score continue de dire ce qu'il
# vaut, et c'est cette marque, écrite dans `lead_potential_reason`, qui
# explique sa place. Écraser le score aurait détruit l'information et rendu
# les leads prioritaires indistinguables entre eux.
MARQUEUR_TETE_DE_FILE = "⚑ TÊTE DE FILE — un avis dit ne pas réussir à joindre l'entreprise."
# La plainte doit être attestée par la note de l'avis, sinon le constat tombe.
NOTE_MAX_PLAINTE = 3

POIDS: dict[str, int] = {
    "base": 25,
    # Exposition hors-heures — l'ancre du barème.
    "ferme_soir_ou_weekend": 14,
    "contradiction_urgence": 10,      # promet le 24/7 ET ferme le soir/weekend
    # Volume de demandes entrantes.
    "avis_20_99": 4,
    "avis_100_299": 8,
    "avis_300_plus": 12,
    "avis_30j_un": 3,
    "avis_30j_trois": 6,
    # Le signal le plus direct qu'une demande entrante reste sans réponse :
    # des avis récents que personne n'a pris la peine de répondre.
    "avis_30j_sans_reponse_un": 6,
    "avis_30j_sans_reponse_deux": 12,
    "villes_3_plus": 4,
    "metiers_3_plus": 3,
    # Dépendance au téléphone.
    "aucun_rdv_en_ligne": 8,
    "saisonnier": 4,
    # Ce qui fait descendre — jamais disqualifier : la boîte reste joignable.
    "outil_en_place": -20,
    "service_reponse_humain_24_7": -25,
}


def _entier(valeur: Any) -> int | None:
    """`True` vaut 1 en Python — on refuse les booléens là où on attend un compte."""
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        return None
    return valeur


def calculer_score(
    signaux: Any, *, disqualifie: bool = False
) -> tuple[int, list[str]]:
    """Rend (score 0-100, trace lisible).

    La trace n'est pas persistée : les signaux le sont, donc elle se recalcule
    à volonté. Elle sert au debug et aux tests.
    """
    if disqualifie:
        return 0, ["disqualifie -> 0"]
    if not isinstance(signaux, dict):
        return 0, ["aucun signal"]

    score = POIDS["base"]
    trace = [f"base +{POIDS['base']}"]

    def ajoute(cle: str) -> None:
        nonlocal score
        score += POIDS[cle]
        trace.append(f"{cle} {POIDS[cle]:+d}")

    ferme = signaux.get("ferme_soir_ou_weekend")
    if ferme is True:
        ajoute("ferme_soir_ou_weekend")
        if signaux.get("promet_urgence_24_7") is True:
            ajoute("contradiction_urgence")

    avis = _entier(signaux.get("avis_total"))
    if avis is not None:
        if avis >= 300:
            ajoute("avis_300_plus")
        elif avis >= 100:
            ajoute("avis_100_299")
        elif avis >= 20:
            ajoute("avis_20_99")

    recents = _entier(signaux.get("avis_30j"))
    if recents is not None:
        if recents >= 3:
            ajoute("avis_30j_trois")
        elif recents >= 1:
            ajoute("avis_30j_un")

    sans_reponse = _entier(signaux.get("avis_30j_sans_reponse"))
    if sans_reponse is not None:
        if sans_reponse >= 2:
            ajoute("avis_30j_sans_reponse_deux")
        elif sans_reponse >= 1:
            ajoute("avis_30j_sans_reponse_un")

    villes = _entier(signaux.get("villes_desservies"))
    if villes is not None and villes >= 3:
        ajoute("villes_3_plus")

    metiers = _entier(signaux.get("metiers_offerts"))
    if metiers is not None and metiers >= 3:
        ajoute("metiers_3_plus")

    # `False` explicite seulement : « on n'a pas regardé » ne vaut pas « il n'y
    # en a pas ». Le scrape rend False quand il a lu le site sans rien trouver.
    if signaux.get("rdv_en_ligne") is False:
        ajoute("aucun_rdv_en_ligne")

    if signaux.get("saisonnier") is True:
        ajoute("saisonnier")

    if signaux.get("outil_en_place") is True:
        ajoute("outil_en_place")

    if signaux.get("service_reponse_humain_24_7") is True:
        ajoute("service_reponse_humain_24_7")

    borne = max(0, min(100, score))
    if borne != score:
        trace.append(f"borne 0-100 ({score} -> {borne})")
    return borne, trace


def est_tete_de_file(signaux: Any, *, disqualifie: bool = False) -> bool:
    """Ce lead doit-il passer devant les autres, score mis à part ?

    Double garde, parce que le mot-clé seul se trompe une fois sur deux.
    Mesuré le 2026-09-01 sur les 405 fiches recherchées : l'appariement des
    racines prévues par la spec (`rappel`, `répond`, `joindre`, `retour
    d'appel`, `oublié`) marque 60 boîtes, dont **35 avis 5 ★ qui VANTENT la
    rapidité de réponse** — « Rappel tôt samedi am », « a répondu rapidement à
    mon appel ». Le modèle juge donc le SENS de l'avis, et le code exige en
    plus qu'un avis du lot porte une note assez basse pour attester d'une
    plainte.

    Une boîte disqualifiée ne passe jamais devant : ce n'est plus un prospect.
    """
    if disqualifie or not isinstance(signaux, dict):
        return False
    if signaux.get("avis_disent_injoignable") is not True:
        return False
    note_min = _entier(signaux.get("avis_note_min"))
    return note_min is not None and note_min <= NOTE_MAX_PLAINTE
