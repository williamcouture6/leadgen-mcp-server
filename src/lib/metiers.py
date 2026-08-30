"""Résolution déterministe des métiers depuis `research_json.services_offered`.

Le cœur de la « structure en trois temps » (spec d'offre §3). Le rédacteur LLM
ne classe RIEN : il reçoit les métiers déjà résolus et il écrit.

**Fonction pure, pas de colonne.** AC1c (la vue de sélection, les colonnes
dérivées, les index GIN, le backfill) est différé après le premier lot. Le jour
où il se construit, cette fonction devient le calcul de la colonne : rien n'est
jeté.

⚠️ **Appariement par racines, jamais un LLM** : auditable, rejouable, gratuit,
sans dérive. « Déneigement commercial », « transport de neige » et « déneigement
de toiture » tombent tous dans *déneigement*.

Les deux garde-fous de la §3, et pourquoi ils ne sont pas symétriques :

1. **Défaut inversé sur l'inconnu.** Un métier non reconnu ne donne pas « aucun
   mois », il donne **tous les mois**. Sur la PERMISSION (ai-je le droit
   d'écrire?) on refuse par défaut ; sur l'OPTIMISATION (quand est-ce le meilleur
   moment?) on autorise par défaut. Les traiter pareil est ce qui fabrique les
   pertes silencieuses.
2. **Appariement volontairement généreux.** Croire à tort qu'elle fait du
   déneigement coûte un courriel mal placé ; rater son déneigement coûte sa
   fenêtre entière. **On inclut dans le doute.**
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

# ----------------------------------------------------------------------
# Le dictionnaire
# ----------------------------------------------------------------------
# Les racines sont comparées sur une forme SANS ACCENTS et en minuscules, pour
# qu'un libellé mal orthographié ou mal encodé ne fasse pas rater une fenêtre
# (garde-fou nº2). Écrire les racines sans accent ici, donc.
#
# 🔴 Chaque racine est ancrée sur un DÉBUT DE MOT, jamais cherchée au milieu.
# Le test l'a trouvé avant la production : « aménagement » contient « ménage »,
# donc tout paysagiste devenait aussi un service de ménage — un métier sans
# saison documentée, qui tombe sur le défaut inversé et ouvre les douze mois.
# Un paysagiste pur serait ainsi devenu joignable en octobre, ce que toute la
# règle saisonnière existe précisément pour empêcher. « Généreux » veut dire
# « préfixe d'un mot », pas « n'importe où dans la chaîne ».
RACINES: dict[str, tuple[str, ...]] = {
    "déneigement": ("deneig", "neige", "souffleuse", "deglac"),
    "paysagement": ("paysag", "amenagement paysager", "plate-bande", "platebande", "haie"),
    "tonte": ("tonte", "pelouse", "gazon", "tondre"),
    "lavage de vitres": ("vitre", "fenetre"),
    "extermination": ("extermin", "parasitaire", "vermine", "punaise", "fourmi", "rongeur"),
    "ménage": ("menage", "entretien menager", "nettoyage residentiel"),
    "piscine": ("piscine", "spa"),
    "pavage": ("pavage", "asphalte", "pave uni", "paveuni"),
    "excavation": ("excavation", "terrassement", "creus"),
    "toiture": ("toiture", "bardeau", "couvreur"),
}

# Quand un libellé apparie le métier de gauche, il n'apparie PAS ceux de droite.
# Une seule entrée, et la spec la nomme elle-même : « déneigement de toiture »
# est un libellé de DÉNEIGEMENT. Sans cette règle il ferait aussi naître un
# métier « toiture », qui n'a pas de saison documentée, donc ouvre les douze
# mois — et une entreprise de déneigement pur recevrait un courriel en juillet.
# ⚠️ N'ajouter une entrée ici qu'avec un libellé réel à l'appui : chaque
# exception rogne le garde-fou nº2 (« on inclut dans le doute »).
ECRASE: dict[str, tuple[str, ...]] = {
    "déneigement": ("toiture",),
}

_RACINES_RE: dict[str, tuple[re.Pattern[str], ...]] = {
    metier: tuple(re.compile(r"\b" + re.escape(r)) for r in racines)
    for metier, racines in RACINES.items()
}

# Début de saison — (mois, jour). C'est le SEUL fait demandé par métier, et il
# est documentable ; c'est ce qui rend la règle de William (2026-08-29)
# meilleure que le calendrier détaillé de la recherche, lequel reposait sur
# « quand l'entrepreneur est mentalement disponible », que rien ne mesure.
#
# Les métiers ABSENTS d'ici n'ont pas de saison documentée : ils tombent sur le
# défaut inversé (tous les mois). Ne PAS en inventer une.
SAISONS: dict[str, tuple[int, int]] = {
    "déneigement": (11, 15),      # contrat type de l'Office de la protection du consommateur
    "paysagement": (4, 15),       # dégel — Landscape Ontario, climat comparable
    "tonte": (5, 1),              # ⚠️ aucune source
    "lavage de vitres": (4, 1),   # 2 opérateurs QC vérifiés mot pour mot
    "extermination": (4, 1),      # ⚠️ source faible (données de Montréal sur les punaises)
}

# La fenêtre s'ouvre 3 mois avant le début de la saison et se ferme 2 mois après.
MOIS_AVANT = 3
MOIS_APRES = 2

# Deux métiers sont « de la même saison » si leurs débuts de saison sont à moins
# de 3 mois l'un de l'autre. Le seuil sépare proprement les cas réels : tonte
# (1er mai) et paysagement (15 avril) sont à un demi-mois ; déneigement
# (15 novembre) et paysagement à cinq mois.
ECART_MEME_SAISON_MOIS = 3

TOUS_LES_MOIS: frozenset[int] = frozenset(range(1, 13))

# Le métier de la scène est « minoritaire » sous ce seuil : le 2ᵉ temps devient
# alors obligatoire ET doit nommer le dominant. L'ENAP a mesuré que 14
# déneigeurs sur 21 tirent 25 % ou moins de leur revenu du déneigement — leur
# parler 100 % neige, c'est leur parler de leur métier d'appoint.
SEUIL_METIER_MINORITAIRE = 0.25


def _sans_accents(texte: str) -> str:
    decompose = unicodedata.normalize("NFD", texte.lower())
    return "".join(c for c in decompose if unicodedata.category(c) != "Mn")


def fenetre_mois(metier: str) -> frozenset[int]:
    """Les mois touchés par la fenêtre du métier.

    **Arrondi généreux** (décision §3) : un mois est dans la fenêtre dès qu'elle
    le touche, même d'un seul jour. Même principe que le garde-fou nº2 — inclure
    un mois de trop coûte un courriel un peu tôt, en exclure un coûte une saison.

    Un métier sans saison documentée rend TOUS les mois (défaut inversé).
    """
    saison = SAISONS.get(metier)
    if saison is None:
        return TOUS_LES_MOIS
    mois_debut, _jour = saison
    # Mois du jour d'ouverture, mois du jour de fermeture, et tout ce qu'il y a
    # entre les deux. Les bornes au demi-mois n'ont pas d'effet ici : le mois de
    # la borne est inclus dès qu'elle le touche.
    ouverture = ((mois_debut - MOIS_AVANT - 1) % 12) + 1
    longueur = MOIS_AVANT + MOIS_APRES + 1
    return frozenset(((ouverture - 1 + i) % 12) + 1 for i in range(longueur))


def _jours_avant_prochaine_saison(metier: str, aujourdhui: date) -> int:
    """Combien de jours avant le prochain début de saison de ce métier.

    Sert à départager les métiers dont la fenêtre est ouverte : on parle de
    celui dont la saison arrive le plus tôt. Un métier sans saison documentée
    n'est jamais choisi comme scène tant qu'un métier daté est disponible.
    """
    saison = SAISONS.get(metier)
    if saison is None:
        return 10_000
    mois, jour = saison
    prochain = date(aujourdhui.year, mois, jour)
    if prochain < aujourdhui:
        prochain = date(aujourdhui.year + 1, mois, jour)
    return (prochain - aujourdhui).days


def _meme_saison(metier_a: str, metier_b: str) -> bool:
    """Les deux métiers tombent-ils dans la même saison ?

    ⚠️ Défaut à **True** quand au moins un des deux n'a pas de saison
    documentée. `meme_saison=True` produit « Tu fais aussi de la tonte », qui ne
    fait AUCUNE affirmation temporelle et ne peut donc pas être faux.
    `meme_saison=False` produit « Pour le reste de l'année… », qui affirme un
    contraste et devient un mensonge si on se trompe. Sur une affirmation
    vérifiable par le lecteur, on choisit la forme qui ne peut pas mentir.
    """
    sa, sb = SAISONS.get(metier_a), SAISONS.get(metier_b)
    if sa is None or sb is None:
        return True
    ecart = abs(sa[0] - sb[0])
    return min(ecart, 12 - ecart) <= ECART_MEME_SAISON_MOIS


@dataclass(frozen=True)
class MetiersResolus:
    """Ce que le rédacteur reçoit. Il ne classe rien, il écrit."""

    metiers: tuple[str, ...]
    """Tous les métiers reconnus, ordonnés par nombre de libellés décroissant."""

    dominant: str | None
    """Le premier. 🔴 GOUVERNE LE LEXIQUE — pas la scène. Sinon un laveur de
    vitres démarché en août à propos de la neige se ferait demander la grandeur
    de son entrée de garage, alors qu'il lave des vitres onze mois par année."""

    scene: str | None
    """Parmi les métiers dont la fenêtre est OUVERTE ce mois-ci, celui dont la
    saison arrive le plus tôt. Fournit la scène de l'ouvreur, rien d'autre :
    l'ENAP a mesuré que 14 déneigeurs sur 21 tirent 25 % ou moins de leur revenu
    du déneigement, donc basculer tout le message dessus viserait à côté."""

    autres: tuple[str, ...]
    """Les métiers restants, ordonnés. Énumérés SANS hiérarchie : on n'écrit
    jamais « c'est ton métier principal », formulation invérifiable qui devient
    fausse dès que le classement se trompe."""

    meme_saison: bool
    """Les autres partagent-ils la saison de la scène ? Décide entre
    « Pour le reste de l'année, tu fais X » et « Tu fais aussi X »."""

    joignable: bool
    """Au moins une fenêtre ouverte. Faux = l'entreprise attend son ouverture.
    ⚠️ Sans AC1c, RIEN n'applique ce prédicat automatiquement : chaque lot se
    sélectionne par requête, à la main."""

    deuxieme_temps_obligatoire: bool
    """Vrai dès que l'entreprise est multi-métier. Il ne saute JAMAIS au motif
    que la scène serait « le métier principal »."""

    scene_est_minoritaire: bool
    """La scène pèse 25 % ou moins des libellés : le 2ᵉ temps doit alors NOMMER
    le métier dominant."""

    source: str
    """`services_offered` ou `inconnu`."""

    fenetre_ouverte: tuple[str, ...]
    """Les métiers dont la fenêtre est ouverte ce mois-ci. Sert au diagnostic."""


def resoudre_metiers(
    services_offered: list[str] | None, aujourdhui: date
) -> MetiersResolus:
    """Apparie les libellés, choisit la scène, ordonne le reste.

    Déterministe et rejouable : deux appels avec les mêmes entrées rendent le
    même résultat, ce qu'un classement par LLM ne garantit pas.
    """
    libelles = [s for s in (services_offered or []) if isinstance(s, str) and s.strip()]

    # Comptage des libellés par métier. Un libellé peut compter pour plusieurs
    # métiers (« déneigement et aménagement paysager ») : c'est voulu, garde-fou
    # nº2 — on inclut dans le doute.
    compte: dict[str, int] = {}
    ordre_apparition: dict[str, int] = {}
    for rang, libelle in enumerate(libelles):
        plat = _sans_accents(libelle)
        apparies = {
            metier
            for metier, motifs in _RACINES_RE.items()
            if any(m.search(plat) for m in motifs)
        }
        for gagnant, perdants in ECRASE.items():
            if gagnant in apparies:
                apparies -= set(perdants)
        for metier in apparies:
            compte[metier] = compte.get(metier, 0) + 1
            ordre_apparition.setdefault(metier, rang)

    if not compte:
        # Défaut inversé : aucun métier reconnu ne veut pas dire « aucun mois ».
        # Le lead reste joignable toute l'année et reçoit un ouvreur générique.
        return MetiersResolus(
            metiers=(), dominant=None, scene=None, autres=(), meme_saison=True,
            joignable=True, deuxieme_temps_obligatoire=False,
            scene_est_minoritaire=False, source="inconnu", fenetre_ouverte=(),
        )

    # Décroissant par nombre de libellés ; à égalité, l'ordre d'apparition dans
    # `services_offered` (stable, donc rejouable).
    metiers = tuple(
        sorted(compte, key=lambda m: (-compte[m], ordre_apparition[m]))
    )
    dominant = metiers[0]

    mois = aujourdhui.month
    ouverts = tuple(m for m in metiers if mois in fenetre_mois(m))

    # La scène : parmi les métiers OUVERTS, celui dont la saison arrive le plus
    # tôt. 🔴 La clause « dont la fenêtre est ouverte » est ce qui fait tenir la
    # règle. Sans elle, mesuré : 76 des 113 joignables en décembre se font
    # parler d'un métier hors saison. Brille-O-Max (vitres + déneigement) est
    # joignable en décembre UNIQUEMENT grâce au déneigement, et la règle sans
    # clause lui parlait de lavage de vitres, parce que la prochaine saison de
    # vitres (1er avril) arrive avant la prochaine saison de neige (15 novembre).
    scene = None
    if ouverts:
        scene = min(
            ouverts,
            key=lambda m: (_jours_avant_prochaine_saison(m, aujourdhui), metiers.index(m)),
        )

    autres = tuple(m for m in metiers if m != scene)
    total_libelles = sum(compte.values())
    part_scene = (compte[scene] / total_libelles) if scene else 0.0

    return MetiersResolus(
        metiers=metiers,
        dominant=dominant,
        scene=scene,
        autres=autres,
        meme_saison=(
            all(_meme_saison(scene, a) for a in autres) if scene and autres else True
        ),
        joignable=bool(ouverts),
        deuxieme_temps_obligatoire=len(metiers) > 1,
        scene_est_minoritaire=bool(scene) and part_scene <= SEUIL_METIER_MINORITAIRE,
        source="services_offered",
        fenetre_ouverte=ouverts,
    )
