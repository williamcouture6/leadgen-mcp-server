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


def nom_commercial(nom_brut: str | None) -> str:
    """Le nom d'entreprise tel qu'il doit apparaitre dans le corps.

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


def bloc_faits_verifies(
    google_rating: float | None, google_reviews_count: int | None
) -> str:
    """Le bloc court et distinct, servi au rédacteur et au juge.

    Il vient AVANT le JSON de recherche : un fait qui doit être recopié au mot
    près ne se met pas au milieu de 80 lignes de JSON.

    ⚠️ Le cas « pas d'avis » est dit EXPLICITEMENT. Le silence serait lu comme
    « pas encore cherché », et le modèle comblerait le vide — c'est exactement
    la façon dont un chiffre inventé apparaît.
    """
    entete = "## Faits vérifiés (valeurs de colonne — à recopier au mot près, jamais à arrondir ni à embellir)"

    if google_rating is None and google_reviews_count is None:
        return (
            f"{entete}\n"
            "- Avis Google : **aucune note et aucun avis en base pour cette entreprise.**\n"
            "  N'écris AUCUN chiffre d'étoiles ni d'avis. Sers la version de repli du\n"
            "  bloc 2 : la phrase d'argument reste, la citation chiffrée saute."
        )

    note = formater_note(google_rating) if google_rating is not None else "aucune note"
    compte = (
        str(int(google_reviews_count))
        if google_reviews_count is not None
        else "aucun compte"
    )

    if bloc_avis_autorise(google_rating, google_reviews_count):
        consigne = (
            f"  ✅ Tu PEUX citer : « {note} étoiles sur {compte} avis ».\n"
            "  Ces deux chiffres se recopient exactement, sans les modifier."
        )
    else:
        consigne = (
            f"  ❌ Sous le plancher de qualité ({PLANCHER_NB_AVIS} avis et "
            f"{formater_note(PLANCHER_NOTE)} étoiles).\n"
            "  N'écris AUCUN chiffre d'étoiles ni d'avis : lui renvoyer sa propre\n"
            "  mauvaise note en pleine face ruine le courriel. Sers la version de\n"
            "  repli du bloc 2 — la phrase d'argument reste, la citation saute."
        )

    return (
        f"{entete}\n"
        f"- Note Google : {note}\n"
        f"- Nombre d'avis : {compte}\n"
        f"{consigne}"
    )
