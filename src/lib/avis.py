"""L'ancre factuelle du bloc 2 : la note Google et le nombre d'avis.

Un seul endroit pour trois choses qui doivent rester d'accord entre elles :
le **plancher de qualité**, le **formatage du chiffre**, et le **bloc de faits
vérifiés** servi au rédacteur ET au juge.

🔴 **Pourquoi le juge doit le recevoir lui aussi.** Sans la valeur de colonne
sous les yeux, il ne peut pas déclarer un chiffre inventé : il n'a aucun moyen
de savoir. C'est le bug déjà payé une fois (`0732d20`, le juge ne voyait pas la
fiche contact et produisait des faux positifs). Le déterministe
(`check_avis_conformes`) est la vraie garde ; ce bloc évite au juge de crier au
loup sur un chiffre exact.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping

# Plancher de qualité — bloquant trouvé par le 2ᵉ conseil de revue.
# Mesuré : 89 des 255 (35 %) sont en dessous. A.M.G. Neige 2,3 ⭐ sur 27 avis,
# Groupe Essa 2,9 sur 504, Herbofleurs 3,0 sur **2 avis**. Sans plancher, un
# tiers de la liste lit sa propre mauvaise note en pleine face, et le
# paragraphe de la version B (« c'est probablement pas parce que le monde
# t'aime pas ») se lit comme du sarcasme.
PLANCHER_NB_AVIS = 10
PLANCHER_NOTE = 4.0


def bloc_avis_autorise(
    google_rating: float | None, google_reviews_count: int | None
) -> bool:
    """La citation chiffrée peut-elle sortir ?

    ⚠️ Ce n'est PAS « le bloc 2 sort-il ». Le repli retire la **citation**, pas
    le paragraphe : la v3 faisait sauter le bloc entier, ce qui amputait ~25
    mots et laissait le corps à un mot de la borne basse.
    """
    if google_rating is None or google_reviews_count is None:
        return False
    return (
        google_reviews_count >= PLANCHER_NB_AVIS
        and float(google_rating) >= PLANCHER_NOTE
    )


# Les separateurs qui, dans une fiche Google, introduisent le bourrage de
# mots-cles apres le vrai nom commercial.
_SEPARATEURS_NOM = ("-", "|", ",", "/", ":", "–", "—")


# ── Le nom d'usage : ce que l'entreprise s'appelle vraiment ─────────────────
#
# 🔴 CE QUE CE BLOC FERME : DEUX SOURCES DE VERITE POUR UN SEUL NOM.
#
# Mesure du 2026-09-17, sur les 343 entreprises ayant un contact joignable :
# **67 (20 %)** portent un nom que le REDACTEUR imprime differemment de ce que
# le JUGE lit. Le redacteur recevait `companies.name` (Google Places) coupe par
# `nom_commercial` ; le juge ne recevait AUCUN nom et deduisait le vrai du
# `company_summary`, ecrit en lisant le site. Rien ne les reliait.
#
# Trois brouillons refuses le meme soir, dont un BLOQUE :
#   · « Vitres & Gouttieres - 123Entretien » -> le courriel a attribue la note
#     Google a « Vitres & Gouttieres ». Elle s'appelle 123Entretien. BLOCKED,
#     « fait invente sur CE prospect » ;
#   · « Paysagement Deneigement Gagne » -> son vrai nom est Paysagement Gagne ;
#   · « lavage des vitres Entretien Menager •Jolie Quebec » -> libelle brut
#     recopie tel quel dans le corps.
#
# ⚠️ POURQUOI ON N'A PAS ETENDU `nom_commercial`, et pourquoi il ne faut pas
# reessayer : sa regle suppose « vrai nom a GAUCHE, mots-cles a DROITE ». Or
# « Vitres Royal - Lavages de Vitres » a son vrai nom a gauche, et
# « Exterminateur Laval - Morin Extermination Inc. » a droite. Aucune
# heuristique deterministe ne choisit le bon cote : chaque regle ajoutee
# FABRIQUE de nouveaux noms faux. Et 14 des 20 pires cas n'ont aucun
# separateur — le bourrage est a l'interieur du nom.
#
# Decision William, 2026-09-17 (option 2 d'un comparatif a quatre).

# Les memes listes que `tools/research.py:363-387`, RECOPIEES a dessein :
# `lib/` ne peut pas dependre de `tools/` sans inverser la dependance. Si l'une
# des deux change, l'autre doit suivre — `tests/test_nom_usage.py` le verifie.
#
# ⚠️ `_NOM_MOTS_GEO` de `research.py` n'est PAS reprise, et c'est le point
# delicat : la-bas on cherche le RADICAL DE MARQUE, donc la geographie est du
# bruit. Ici on cherche l'EGALITE entre deux ecritures du meme nom, et
# « Jolie Quebec » perdrait la moitie du sien.
_MOTS_NON_SIGNIFIANTS = frozenset({
    # suffixes legaux
    "inc", "incorporee", "incorporated", "ltee", "ltd", "limitee", "limited",
    "enr", "senc", "sencrl", "srl", "sa", "cie", "corp", "co",
    # mots outils
    "le", "la", "les", "l", "de", "du", "des", "d", "et", "en", "au", "aux",
    "a", "the", "of", "and", "pour", "chez", "sur", "par",
    # enveloppes corporatives
    "groupe", "groupes", "entreprise", "entreprises", "compagnie", "company",
    "service", "services", "equipe", "team",
})

# Au-dela, ce n'est plus un nom : c'est un slogan ou une phrase du resume.
_NOM_USAGE_MAX_MOTS = 6
# Meme seuil que `research._NOM_RADICAL_MIN`, pour la meme raison : en dessous
# de 5 caracteres, une sous-chaine apparie n'importe quoi.
_NOM_USAGE_COMPACT_MIN = 5


def _tokens_de_nom(texte: str) -> tuple[list[str], list[str], str]:
    """(tokens significatifs, tokens BRUTS, forme compacte) — sans accents.

    ⚠️ On GARDE les tokens d'une lettre et les nombres, contrairement a
    `research._tokens_nom`. Les jeter accepterait « N. Theoret » pour
    « Deneigement Theoret » — le token « n » est justement ce qui prouve que le
    candidat dit autre chose.
    """
    plat = unicodedata.normalize("NFKD", texte.lower())
    plat = "".join(c for c in plat if not unicodedata.combining(c))
    bruts = [t for t in re.split(r"[^a-z0-9]+", plat) if t]
    significatifs = [t for t in bruts if t not in _MOTS_NON_SIGNIFIANTS]
    return significatifs, bruts, "".join(bruts)


def nom_usage_fiable(candidat: str | None, nom_brut: str | None) -> str | None:
    """Le nom rendu par le modele, ou None s'il ne se prouve pas depuis Google.

    🔴 CE QU'ELLE PROUVE, ET CE QU'ELLE NE PROUVE PAS. Elle verifie que le
    candidat est CONTENU dans ce que Google affiche — pas qu'il est le bon nom.
    Un modele qui rendrait « Deneigement » pour « Paysagement Deneigement
    Gagne » passerait. C'est un faux positif possible, non mesure ; le dry-run
    du backfill est ce qui le montrera.

    Le choix assume est la PRECISION contre le rappel : un nom d'usage vrai mais
    absent du libelle Google (site « AquaVerre », Google « Lavage de vitres
    Montreal ») est refuse. On ne peut pas le prouver, donc on ne l'imprime pas
    — et le repli reste le comportement d'avant, jamais pire.
    """
    if not isinstance(candidat, str) or not isinstance(nom_brut, str):
        return None
    propre = re.sub(r"\s+", " ", candidat).strip()

    # 🔴 DEUX NETTOYAGES TROUVES PAR UN GALOP D'ESSAI SUR 18 FICHES REELLES,
    # avant tout deploiement. Sans eux, la garde acceptait des noms exacts
    # mais imprononcables dans un courriel :
    #
    #   · « Symetric (Cedres Gatineau) » — le modele GLOSE entre parentheses
    #     pour rattacher le nom du site au libelle Google. La glose n'est pas
    #     le nom : on la retire, et il reste « Symetric », qui est justement
    #     le bon.
    #   · « Les Entreprises J.S. Lauzon Inc » — le suffixe legal en pleine
    #     phrase parlee (« ... Inc a 4,2 etoiles sur 13 avis »). Le corps
    #     tutoie ; il ne dit pas « Inc ».
    #
    # Les deux se retirent AVANT la comparaison, jamais apres : un nom reduit
    # doit encore se prouver contre le libelle Google.
    # ⚠️ EN QUEUE SEULEMENT. Retirer toutes les parentheses recomposait un nom :
    # « Foo (Bar) Baz » devenait « Foo Baz », qui ne s'ecrit nulle part. La glose
    # du modele arrive toujours a la fin (« Symetric (Cedres Gatineau) »).
    propre = re.sub(r"\s*\([^)]*\)\s*$", "", propre).strip()
    propre = re.sub(
        r"[\s,]+(inc|ltee|ltd|limitee|enr|senc|sencrl|srl|cie|corp)\.?$",
        "", propre, flags=re.IGNORECASE,
    ).strip()
    propre = propre.strip(" \t\n-|,/:•·–—.")
    if not propre:
        return None

    # 1. Un nom d'usage n'est pas une enumeration. Sans cette regle, le modele
    #    qui recopie le libelle Google bourre — qu'il a sous les yeux dans son
    #    prompt — le ferait accepter par l'inclusion : on imprimerait le
    #    bourrage ENTIER, pire qu'aujourd'hui.
    # ⚠️ LA VIRGULE EST UNE EXCEPTION, et l'oublier contredisait `nom_commercial`
    # 100 lignes plus bas — qui la traite deja ainsi depuis le 2026-08-30 :
    # elle s'ecrit COLLEE au mot qui precede (« Piscines Elegance, Quebec »),
    # donc exiger une espace devant elle ne couperait jamais rien.
    #
    # Sans cette exception, il suffisait au modele de recopier le libelle Google
    # en remplacant le tiret par une virgule pour que le bourrage ENTIER passe :
    # « Vitres & Gouttieres, 123Entretien » etait accepte. 33 fiches non
    # terminales portent une virgule dans leur nom. Trouve par un conseil de
    # relecture avant tout deploiement.
    if "•" in propre or "," in propre:
        return None
    if any(f" {sep}" in propre for sep in _SEPARATEURS_NOM):
        return None

    mots_candidat, bruts_candidat, compact_candidat = _tokens_de_nom(propre)
    mots_brut, bruts_brut, compact_brut = _tokens_de_nom(nom_brut)

    # 2. « Les », « Inc. », « Services » : rien de significatif. C'est le cas
    #    mesure — le modele a rendu « Les » pour « Lavage de vitres Services
    #    Aqua-Verre inc. ».
    if not mots_candidat:
        return None
    if len(mots_candidat) > _NOM_USAGE_MAX_MOTS:
        return None

    # 3. 🔴 INCLUSION SUR LES TOKENS BRUTS, PAS SEULEMENT SIGNIFIANTS.
    #
    #    Ne comparer que les tokens significatifs laissait le modele AJOUTER une
    #    enveloppe que Google ne montre nulle part — « groupe », « service »,
    #    « les » sont dans `_MOTS_NON_SIGNIFIANTS`, donc invisibles a la
    #    comparaison. Mesure du conseil de relecture, avant deploiement :
    #
    #        ('Groupe Sani',  'Sani Nettoyage')        -> 'Groupe Sani'   ❌
    #        ('Service Pro',  'Pro Deneigement Laval') -> 'Service Pro'   ❌
    #        ('Les Toitures', 'Toitures Quebec')       -> 'Les Toitures'  ❌
    #
    #    Un mot que rien ne prouve, dans la premiere ligne que le prospect lit :
    #    c'est exactement le « fait invente sur CE prospect » que cette colonne
    #    existe pour empecher. La docstring PROMETTAIT l'inclusion ; elle la
    #    tient maintenant.
    #
    #    ⚠️ Ce que ca coute, assume : « SLGN Groupe » (lu sur le site) est refuse
    #    parce que « groupe » n'est pas dans le libelle Google « SLGN
    #    Deneigement ». On retombe sur le libelle Google. C'est le bon sens du
    #    compromis : on n'imprime que ce qu'on peut prouver.
    #
    #    Accepte toujours : « Paysagement Gagne » ⊂ « Paysagement Deneigement
    #    Gagne » (bourrage interne), « 123Entretien » ⊂ « Vitres & Gouttieres -
    #    123Entretien » (le vrai nom etait a DROITE).
    if set(bruts_candidat) <= set(bruts_brut):
        return propre

    # 4. Repli sur la forme collee : « 123 Entretien » (site) contre
    #    « 123Entretien » (Google), ou « A Point » contre « APoint Deneigement ».
    if (
        len(compact_candidat) >= _NOM_USAGE_COMPACT_MIN
        and compact_candidat in compact_brut
    ):
        return propre

    return None


def nom_a_imprimer(company: Mapping[str, Any]) -> str:
    """LE seul point de resolution du nom, pour le redacteur ET pour le juge.

    Les deux doivent appeler CECI, jamais `nom_commercial` directement : c'est
    le fait qu'ils resolvaient le nom chacun de leur cote qui a produit le
    blocage du 2026-09-17.

    `nom_usage` vide — c'est-a-dire les 1132 fiches au jour 1 — rend exactement
    ce que le redacteur imprimait avant. Le repli n'est pas une degradation,
    c'est le comportement d'hier.
    """
    usage = company.get("nom_usage")
    if isinstance(usage, str) and usage.strip():
        return usage.strip()
    return nom_commercial(company.get("name"))


def nom_commercial(nom_brut: str | None) -> str:
    """LE REPLI, quand `companies.nom_usage` est vide — voir `nom_a_imprimer`.

    ⚠️ Cette fonction a ete l'autorite d'impression jusqu'a la migration 0072
    du 2026-09-17. Elle ne l'est plus : le redacteur et le juge passent
    desormais par `nom_a_imprimer`, qui prefere le nom lu sur le site. Ne pas
    l'appeler directement depuis un chemin de prompt — c'est le fait que les
    deux acteurs resolvaient le nom chacun de leur cote qui a produit le
    blocage du 2026-09-17.

    Ce qu'elle fait, et qui reste vrai comme repli :

    🔴 Les noms en base viennent de fiches Google BOURREES DE MOTS-CLES.
    Mesure reelle : « Vitres Ultra Nettes -lavage de vitres residentiel
    -lavage de vitres condo -nettoyage de gouttieres » fait **14 mots** au lieu
    de 3.

    Recopie brut dans l'ancre factuelle, il ne fait pas que se lire comme du
    spam : il pousse le corps hors des bornes. Mesure du conseil du 2026-08-30 :
    CORPS_B avec ce nom ET son 2e temps obligatoire = 251 mots contre une borne
    de 250 -- `check_length` echoue, verdict `needs_revision`, brouillon mort et
    contact gele a vie dans la fenetre WF-4.

    ⚠️ Ce n'est ni le nom SEUL ni le 2e temps SEUL qui cassaient : chacun passe.
    C'est leur addition, et B est la version la plus serree.

    On coupe au premier separateur (decision William, 2026-08-30) : simple,
    previsible, et ca rend presque toujours le vrai nom commercial.
    """
    nom = (nom_brut or "").strip()
    if not nom:
        return ""

    # 🔴 Un separateur ne coupe QUE s'il est precede d'une espace.
    #
    # Correctif du conseil final. La premiere version coupait au premier tiret
    # trouve, sans regarder ce qui le precedait : le trait d'union INTERNE des
    # noms propres composes se faisait prendre pour un separateur de mots-cles.
    # Mesure sur la base : 93 noms sur 816 portent un trait d'union, dont 56
    # au-dessus du plancher d'avis -- soit ~10 % de la liste dont le SEUL fait
    # personnalise du courriel, imprime dans l'ancre chiffree, partait ecorche :
    #
    #   « Chasse-Neige Express »      -> « Chasse »
    #   « Deneigement Rive-Sud »      -> « Deneigement Rive »
    #   « Paysagement Saint-Nicolas » -> « Paysagement Saint »
    #   « 9265-1234 Quebec inc. »     -> « 9265 »
    #
    # Le bourrage de mots-cles, lui, est TOUJOURS precede d'une espace
    # (« Vitres Ultra Nettes -lavage de vitres condo ») : la regle separe donc
    # exactement les deux cas.
    #
    # ⚠️ La virgule fait exception : elle s'ecrit collee au mot qui precede
    # (« Piscines Elegance, Quebec »), donc exiger une espace avant elle ne
    # couperait jamais rien.
    coupe = len(nom)
    for sep in _SEPARATEURS_NOM:
        depart = 1  # un separateur EN TETE ne coupe rien : sinon chaine vide.
        while True:
            i = nom.find(sep, depart)
            if i < 0:
                break
            if sep == "," or nom[i - 1] == " ":
                coupe = min(coupe, i)
                break
            depart = i + 1
    return nom[:coupe].strip(" -|,/:") or nom


def formater_note(google_rating: float) -> str:
    """La note telle qu'elle doit apparaître dans le corps : une décimale,
    virgule décimale française.

    `check_avis_conformes` compare sur un arrondi à une décimale, donc cette
    fonction et lui doivent rester d'accord — c'est pourquoi elles vivent
    ensemble.
    """
    return f"{round(float(google_rating), 1):.1f}".replace(".", ",")


LN = chr(10)


def _consigne_de_repli(nb_services: int | None, phrase_du_rush: str | None) -> str:
    """Quelle version du 2ᵉ paragraphe servir quand la note ne se cite pas.

    🔴 IL Y EN A TROIS DEPUIS LE 2026-09-14, et la troisième est née d'un trou
    qu'on fermait jusque-là en REFUSANT le gabarit. La version de repli demande
    « je vois que tu fais autant X que Y pis Z » : avec un seul service, la
    forme est impossible et « on comprend que tu en couvres beaucoup! » est
    faux. `tete_fixe_servable` écartait donc C et D pour ces entreprises — 7 en
    base, soit ~2 % du test A/B qui ne pouvaient tirer que A ou B.

    William a choisi la supposition (« J'imagine qu'à la première bordée, ça
    rentre pas mal tout en même temps! ») parmi trois formes lues côte à côte.
    Elle marche parce qu'elle n'affirme rien sur l'entreprise : c'est sa
    tournure habituelle, et la seule des trois qui ne répète pas le métier déjà
    nommé à la première ligne.

    ⚠️ `nb_services is None` = l'appelant ne sait pas. On rend alors l'ancienne
    consigne : elle est vraie dans le cas général, et fabriquer une supposition
    sans savoir combien de services existent l'enverrait à une entreprise qui a
    de quoi énumérer.
    """
    repli_ordinaire = (
        "  Sers la version de repli du bloc 2 : la phrase d'argument reste, "
        "la citation chiffrée saute."
    )
    if nb_services is None or nb_services >= 2 or not phrase_du_rush:
        return repli_ordinaire
    return (
        f"  ⚠️ **Un seul service ({nb_services}) : l'énumération de repli est "
        "IMPOSSIBLE.**"
        + LN
        + "  Sers la TROISIÈME version du bloc 2, celle de la supposition. Sa "
        "première phrase"
        + LN
        + "  est écrite — recopie-la telle quelle :"
        + LN
        + f"      {phrase_du_rush}"
        + LN
        + "  Puis enchaîne sur « Pourtant je suis certain qu'il serait "
        "possible… », mot pour"
        + LN
        + "  mot comme dans les deux autres versions. N'écris PAS « je vois que "
        "tu fais … » :"
        + LN
        + "  le métier est déjà nommé à la première ligne."
    )


def bloc_faits_verifies(
    google_rating: float | None,
    google_reviews_count: int | None,
    *,
    nb_services: int | None = None,
    phrase_du_rush: str | None = None,
    nom_entreprise: str | None = None,
) -> str:
    """Le bloc court et distinct, servi au rédacteur et au juge.

    Il vient AVANT le JSON de recherche : un fait qui doit être recopié au mot
    près ne se met pas au milieu de 80 lignes de JSON.

    ⚠️ Le cas « pas d'avis » est dit EXPLICITEMENT. Le silence serait lu comme
    « pas encore cherché », et le modèle comblerait le vide — c'est exactement
    la façon dont un chiffre inventé apparaît.
    """
    entete = "## Faits vérifiés (valeurs de colonne — à recopier au mot près, jamais à arrondir ni à embellir)"

    # 🔴 LE NOM DANS LE BLOC : C'EST TOUT L'OBJET DE LA 0072.
    #
    # Avant elle, le rédacteur résolvait le nom de son côté (`nom_commercial`)
    # et le juge n'en recevait AUCUN — il déduisait le vrai du `company_summary`.
    # Deux sources de vérité, 67 divergences sur 343 fiches joignables, et un
    # brouillon BLOQUÉ le 2026-09-17 pour « fait inventé » alors que le
    # rédacteur avait obéi à sa règle.
    #
    # Le bloc étant servi aux DEUX (personalize.py et compliance.py), y mettre
    # le nom est ce qui les remet d'accord — il n'y a plus qu'une valeur.
    if nom_entreprise:
        entete += (
            f"\n- Nom de l'entreprise : **{nom_entreprise}**\n"
            "  C'est le nom à écrire, tel quel. Il fait foi **même s'il diffère\n"
            "  du nom qui apparaît dans le research_json** : celui-ci est un\n"
            "  résumé rédigé à une autre date, pas une valeur de colonne."
        )

    if google_rating is None and google_reviews_count is None:
        return (
            f"{entete}\n"
            "- Avis Google : **aucune note et aucun avis en base pour cette entreprise.**\n"
            "  N'écris AUCUN chiffre d'étoiles ni d'avis."
            + LN
            + _consigne_de_repli(nb_services, phrase_du_rush)
        )

    note = formater_note(google_rating) if google_rating is not None else "aucune note"
    compte = (
        str(int(google_reviews_count))
        if google_reviews_count is not None
        else "aucun compte"
    )

    if bloc_avis_autorise(google_rating, google_reviews_count):
        consigne = (
            # ⚠️ « avis Google » : le gabarit le dit ainsi depuis le
            # 2026-09-14, et ce bloc est ce que le rédacteur RECOPIE. Les
            # deux doivent se suivre, sinon il recopie une forme que le
            # gabarit contredit trois lignes plus bas.
            f"  ✅ Tu PEUX citer : « {note} étoiles sur {compte} avis Google ».\n"
            "  Ces deux chiffres se recopient exactement, sans les modifier."
        )
    else:
        consigne = (
            f"  ❌ Sous le plancher de qualité ({PLANCHER_NB_AVIS} avis et "
            f"{formater_note(PLANCHER_NOTE)} étoiles).\n"
            "  N'écris AUCUN chiffre d'étoiles ni d'avis : lui renvoyer sa propre\n"
            "  mauvaise note en pleine face ruine le courriel."
            + LN
            + _consigne_de_repli(nb_services, phrase_du_rush)
        )

    return (
        f"{entete}\n"
        f"- Note Google : {note}\n"
        f"- Nombre d'avis : {compte}\n"
        f"{consigne}"
    )
