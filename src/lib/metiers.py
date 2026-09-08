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
    # « plates-bandes » (23 fiches au 2026-08-30) et « murs de soutenement »
    # (19, meme mesure) etaient des
    # trous mesures par la spec §3 et jamais bouches. « plate-bande » au
    # SINGULIER apparait dans 0 fiche : c'etait du code mort.
    # Murs de soutenement -> paysagement (decision William, 2026-08-30) :
    # c'est de l'amenagement exterieur, et ca lui donne la fenetre du
    # paysagement plutot que les douze mois d'excavation.
    "paysagement": (
        "paysag", "amenagement paysager", "plate-bande", "plates-bande",
        "platebande", "haie", "soutenement",
    ),
    "tonte": ("tonte", "pelouse", "gazon", "tondre"),
    # « pression » (34 fiches au 2026-08-30) -> lavage de vitres (decision William,
    # 2026-08-30) : meme client, meme saison d'avril, meme equipement.
    "lavage de vitres": ("vitre", "fenetre", "pression"),
    "extermination": ("extermin", "parasitaire", "vermine", "punaise", "fourmi", "rongeur"),
    "ménage": ("menage", "entretien menager", "nettoyage residentiel"),
    "piscine": ("piscine", "spa"),
    "pavage": ("pavage", "asphalte", "pave uni", "paveuni"),
    # ⚠️ « creus » (et non « creusage ») appariait « piscine CREUSEE », le
    # terme standard au Quebec pour une piscine enterree. Mesure en base :
    # 20 des 22 libelles contenant « creus » etaient des piscines au 2026-08-30,
    # 2 seulement
    # de la vraie excavation. Meme famille de collision que
    # « amenagement »/« menage ». La racine est donc resserree sur le NOM DE
    # L'ACTE, pas sur l'adjectif.
    "excavation": ("excavation", "terrassement", "creusage", "creusement"),
    "toiture": ("toiture", "bardeau", "couvreur"),
}

# Quand un libellé apparie le métier de gauche, il n'apparie PAS ceux de droite.
# Une seule entrée, et la spec la nomme elle-même : « déneigement de toiture »
# est un libellé de DÉNEIGEMENT. Sans cette règle il ferait aussi naître un
# métier « toiture », qui n'a pas de saison documentée, donc ouvre les douze
# mois — et une entreprise de déneigement pur recevrait un courriel en juillet.
# ⚠️ N'ajouter une entrée ici qu'avec un libellé réel à l'appui : chaque
# exception rogne le garde-fou nº2 (« on inclut dans le doute »).
# 🔴 Libellés qui contiennent la racine d'un métier SANS être ce métier.
#
# Trouvés le 2026-09-02, au premier passage réel du pipeline, par le juge LLM —
# pas par un test. Il a refusé le gabarit A de « Côté Ruelle - Paysagiste » en
# disant que « tonte » et « excavation » n'étaient ancrés nulle part. Vérifié :
# il avait raison sur la tonte.
#
#   « Plantations (arbres, arbustes, GAZON EN ROULEAU) »  → racine `gazon`
#     Poser de la tourbe n'est pas tondre. Écrire « tu fais de la tonte aussi »
#     à un paysagiste qui pose du gazon en plaques, il le voit tout de suite.
#     8 entreprises sur 403, mesuré le 2026-09-02.
#
#   « Installation de CLÔTURES DE PISCINE »              → racine `piscine`
#     Poser une clôture n'est pas entretenir une piscine. 5 entreprises,
#     mesuré le 2026-09-02.
#
# ⚠️ `terrassement` → excavation est CONSERVÉ malgré le signalement du juge :
# 37 entreprises le portaient au 2026-09-02, et « tu fais du terrassement
# aussi » dit à un
# paysagiste qui fait du nivellement est défendable — il fait vraiment ça. La
# frontière retenue est : on retire ce qui est FAUX, pas ce qui est large.
#
# Une exclusion s'applique AU LIBELLÉ : si un libellé la contient, il ne compte
# pas pour ce métier-là. Il peut toujours compter pour un autre — « gazon en
# rouleau » reste du paysagement.
EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "tonte": (
        "gazon en rouleau",
        "gazon en plaque",
        "gazon en plaques",
        "pose de gazon",
        "gazon synthetique",
        "gazon artificiel",
    ),
    "piscine": (
        "cloture de piscine",
        "clotures de piscine",
        "abri de piscine",
        "abris de piscine",
        "enrochement de piscine",
        # 🔧 Ajoutées le 2026-09-02 par le conseil de revue. Elles visent le
        # CONTOUR, pas la piscine.
        #
        # Le cas réel : « Le Gars Des Vitres » (industry `lavage de vitres`) a
        # six libellés de lavage plus « Lavage à pression (patios, entrées,
        # façades, CONTOURS DE PISCINE) ». Il résolvait `piscine`, et en avril
        # la piscine GAGNAIT la scène — sa saison du 1er mai est plus proche
        # que la prochaine saison de vitres, celle du 1er avril venant de
        # passer. Un laveur de vitres recevait donc un courriel sur la saison
        # des piscines, en pleine saison de vitres. En juillet, pire : il
        # n'était joignable QUE grâce au faux positif.
        #
        # ⚠️ CE DÉFAUT EST INVISIBLE EN SEPTEMBRE, où seul le déneigement est
        # ouvert. Il ne se serait montré qu'au printemps, sur un lot réel. Le
        # conseil l'a trouvé en rejouant la résolution à d'autres dates que
        # celle du jour — c'est la leçon à retenir : une règle saisonnière ne
        # se teste pas au mois courant.
        #
        # « tour de piscine » est un sous-motif de « contour » ET de
        # « pourtour » : ces cinq entrées couvrent toute la famille.
        "tour de piscine",
        "tours de piscine",
        "deck de piscine",
        "decks de piscine",
        "amenagement avec piscine",
    ),
}

# 🔴 Un métier qui EXIGE un signal pour avoir le droit d'OUVRIR un courriel.
#
# Décision William du 2026-09-04 : le métier `piscine`, c'est « surtout le
# nettoyage / entretien de piscine ». Pas l'installation, pas la construction,
# pas l'aménagement autour.
#
# ⚠️ CE N'EST PAS UNE EXCLUSION. Le métier reste RECONNU — il se fait nommer au
# 2ᵉ temps (« tu fais aussi de la piscine »), et la garde `ECRASE["piscine"]`
# qui empêche « piscine creusée » de devenir de l'excavation continue de
# fonctionner. Ce que l'exigence retire, c'est seulement le droit d'ouvrir : la
# scène, et la joignabilité qui en découle.
#
# Ça reprend une décision plus ancienne que le dictionnaire ignorait : le
# catalogue de sourcing (migration 0026, 2026-08-05) avait retiré « installation
# de piscine » et « piscines et spas » au motif que « l'offre vise l'ENTRETIEN
# récurrent, pas l'installation one-shot ni le détaillant ». Le catalogue le
# savait, la table des métiers ne le savait pas.
#
# MESURÉ AVANT LA RÈGLE le 2026-09-04, sur les 50 libellés qui mentionnaient
# une piscine :
#    6 parlent d'entretien — dont 3 chez le seul vrai piscinier de la liste
#   11 d'installation, de construction ou de vente
#   19 d'aménagement autour (contour, deck, pavé-uni, terrassement)
#   14 d'autre chose (« Piscines » seul, rénovation, changement de toile)
# Les exclusions posées plus haut n'attrapaient que la famille « contour ». Une
# liste d'exclusions ne serait jamais complète — il en manquait encore quatre
# familles, et il en apparaîtra d'autres à chaque nouvelle fiche.
#
# POURQUOI UNE EXIGENCE PLUTÔT QU'UNE LISTE QUI S'ALLONGE : on inverse la charge
# de la preuve. Le mot « piscine » ne prouve rien à lui seul ; c'est le verbe
# qui dit le métier. Un libellé qui parle d'entretien est retenu, tous les
# autres sont ignorés — sans avoir à les énumérer.
#
# ⚠️ Ça ROGNE le garde-fou nº2 (« on inclut dans le doute »), et c'est assumé
# ici parce que le doute penchait du mauvais côté : 44 libellés sur 50 étaient,
# à cette date,
# des faux positifs. La règle générale reste l'inclusion ; `piscine` est
# l'exception, justifiée par la mesure.
EXIGE: dict[str, tuple[str, ...]] = {
    "piscine": (
        "entretien",
        "nettoyage",
        "ouverture",
        "fermeture",
        "traitement",
        "analyse",
        "hebdomadaire",
        "saisonnier",
    ),
}


ECRASE: dict[str, tuple[str, ...]] = {
    "déneigement": ("toiture",),
    # Ceinture ET bretelles avec la racine resserree ci-dessus : si un libelle
    # de piscine amenait quand meme l'excavation (« creusage pour piscine »),
    # c'est la piscine qui gagne. Une piscine creusee n'est pas un contrat
    # d'excavation, et excavation n'ayant pas de saison documentee, elle
    # ouvrirait les douze mois a une entreprise saisonniere.
    "piscine": ("excavation",),
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
    # 🔧 Ajoutée le 2026-09-02, décision William. C'était le seul des cinq
    # métiers sans date qui en méritait vraiment une : « je crois que piscine
    # n'est pas 12 mois sur 12 ».
    #
    # La raison est TECHNIQUE et pas météo, ce qui la rend datable : dès que
    # l'eau atteint 12 °C, les algues prolifèrent. Ouvrir tard, c'est ouvrir
    # une piscine verte. Les guides québécois (CAA-Québec, MétéoMédia,
    # pisciniers de la Rive-Nord) recommandent d'ouvrir AVANT le 1er mai,
    # parfois dès la mi-avril selon la région. Le 1er mai est la borne que les
    # sources nomment explicitement ; l'écart de deux semaines avec la mi-avril
    # ne déplace que les bordures de février et de juillet, et ne change ni qui
    # est joignable en janvier, ni en septembre.
    #
    # ⚠️ CE QUE CETTE LIGNE CHANGE, et ce n'est pas cosmétique : `piscine`
    # devient un métier capable d'OUVRIR un courriel. Sans saison, elle tombait
    # sous la règle « un métier 12 mois sur 12 n'enclenche pas la séquence », et
    # les fiches qui ne l'ont qu'en secondaire n'étaient joignables que par un
    # autre de leurs métiers.
    "piscine": (5, 1),            # avant que l'eau atteigne 12 °C — les algues partent
}

# La fenêtre s'ouvre 3 mois avant le début de la saison et se ferme 2 mois après.
# 🔴 LA FENÊTRE EST PROPRE À CHAQUE MÉTIER — décision William du 2026-09-04.
#
# Elle était globale : 3 mois avant, 2 mois après. William a remis en question
# le « 2 mois après » en lisant les mesures : « contacter du déneigement
# jusqu'au 15 janvier, c'est tard non ? » Il avait raison, et deux fois.
#
# 1. LES DEUX MOIS D'APRÈS RENDAIENT L'ACCROCHE FAUSSE. Les gabarits C et D
#    ouvrent sur « La saison approche ». Pendant les mois qui SUIVENT le début
#    de la saison, elle n'approche pas — elle est commencée. Mesuré sur les six
#    métiers : la phrase était fausse deux mois par métier, dont décembre et
#    janvier pour le déneigement. Or janvier est le mois où 252 leads sur 260
#    sont joignables : le plus gros de la campagne aurait porté une phrase
#    fausse.
#
# 2. UN DÉNEIGEUR A SIGNÉ SES CONTRATS EN OCTOBRE. Le rejoindre en janvier,
#    c'est arriver quand tout est décidé. William s'en souvenait d'ailleurs
#    comme d'une règle à « 1 mois après » — son souvenir était meilleur que la
#    spec du 2026-08-27, qui disait 2.
#
# ⚠️ CE QUI SUIT DÉCRIVAIT LE PREMIER RÉGLAGE, PAS CELUI QUI EST EN VIGUEUR.
# Il est gardé parce qu'il raconte pourquoi la règle unique est morte ; les
# valeurs vivantes sont dans `FENETRE_PAR_METIER`, plus bas, et elles seules
# font foi. Le premier réglage disait « 4 mois avant les métiers de printemps,
# 3 avant le déneigement, 1 mois après PARTOUT » — le « 1 mois après partout »
# a été abandonné le jour même, parce qu'il tuait juillet.
MOIS_AVANT_DEFAUT = 3
MOIS_APRES_DEFAUT = 2

# (mois avant, mois après) par métier. Un métier absent prend les défauts.
#
# 🔧 Deuxième réglage, le 2026-09-04 : la première version (4 avant / 1 après
# partout sauf déneigement) a produit DEUX effets de bord qu'une paire de tests
# écrits une semaine plus tôt a attrapés. Les valeurs ci-dessous les referment,
# et les deux tests redeviennent la garde qu'ils étaient.
#
#   · JUILLET DEVENAIT UN MOIS MORT — 3 leads joignables. En fermant tout à
#     1 mois après, la tonte et la piscine se fermaient en juin, et le
#     déneigement n'ouvre qu'en août. `test_les_douze_mois_ont_quelqu_un` le
#     disait dans son propre commentaire : « si ce test casse, une période de
#     l'année devient morte ». Les 2 mois après rendus à la tonte et à la
#     piscine rouvrent juillet.
#
#   · EN DÉCEMBRE, 83 DÉNEIGEURS SUR 153 PARLAIENT DE PAYSAGEMENT. Ouvrir le
#     paysagement 4 mois avant le 15 avril le faisait démarrer en décembre ;
#     sa saison devenait alors plus PROCHE que la prochaine neige, à onze mois.
#     On aurait parlé d'aménagement paysager à un déneigeur en pleine tempête.
#     À 3 mois avant, la fenêtre s'ouvre en JANVIER et décembre reste au
#     déneigement.
#
#     ✅ ARBITRAGE TRANCHÉ PAR WILLIAM LE 2026-09-07 — ne pas le rouvrir.
#     De JANVIER À MARS, **93 déneigeurs** ont pour scène un métier de
#     printemps (67 paysagement, 20 lavage de vitres, 6 tonte) alors qu'ils
#     déneigent encore, et leur 2ᵉ temps dit « pour le reste de l'année, j'ai
#     aussi vu que tu fais du déneigement » en pleine tempête. Ça a été porté
#     à William avec ce décompte ; sa réponse : « ce n'est pas un problème ».
#     Et il a raison sur le fond — leur saison de printemps approche vraiment,
#     et janvier est le mois où un contracteur planifie son année. Un futur
#     conseil qui « découvre » ce cas doit le classer sans suite.
#
# Le déneigement garde 1 mois après : sa saison démarre le 15 novembre et ses
# contrats se signent en septembre-octobre. Le rejoindre en janvier, c'est
# arriver quand tout est décidé — la remarque de William qui a lancé tout ce
# réglage.
# 🔴 CES SIX PAIRES SONT UNE DÉCISION DE WILLIAM, PAS UN RÉGLAGE TECHNIQUE.
# Décidées le 2026-09-04, mot pour mot : « on commence à écrire 4 mois avant le
# commencement du service et on arrête 1 mois après pour paysagement, tonte,
# lavage de vitres, extermination et piscine ; pour déneigement, 3 mois avant et
# 1 mois après », puis, dans le message suivant : « pour paysagement, tonte,
# lavage de vitres, extermination et piscine on fait 2 mois après à la place de
# 1 ; pour lavage de vitres, paysagement et extermination on commence 3 mois
# avant à la place de 4 ». Ne pas les changer sans le lui demander.
# ⚠️ CES NOMBRES SONT DES MOIS, ET L'ARRONDI EST GÉNÉREUX — l'avance réelle
# dépasse donc « avant × 30 jours » pour les métiers dont la saison démarre en
# milieu de mois. `fenetre_mois` ouvre au 1er du mois atteint, jamais au jour
# anniversaire. Mesuré le 2026-09-07 :
#
#     déneigement  3 mois avant le 15/11 → ouvre le 1er août    = 106 jours
#     paysagement  3 mois avant le 15/04 → ouvre le 1er janvier = 104 jours
#     lavage, exterm.  3 mois avant le 01/04 → 1er janvier      =  90 jours
#     tonte, piscine   4 mois avant le 01/05 → 1er janvier      = 120 jours
#
# Ça compte pour juger la phrase « La saison approche » : son maximum réel est
# de 106 jours, pas de 90. Un conseil de relecture l'a relevé le 2026-09-07,
# parce que deux commentaires disaient « la fenêtre s'ouvre le 15 » — ce qui
# aurait trompé une session future venue arbitrer la longueur de l'ouvreur.
FENETRE_PAR_METIER: dict[str, tuple[int, int]] = {
    "déneigement": (3, 1),
    "paysagement": (3, 2),
    "lavage de vitres": (3, 2),
    "extermination": (3, 2),
    # 4 mois avant : ce sont eux qui couvrent juillet, et leur saison démarre
    # au 1er mai — un mois plus tard que le paysagement.
    "tonte": (4, 2),
    "piscine": (4, 2),
}


def _fenetre_du_metier(metier: str) -> tuple[int, int]:
    """(mois avant, mois après) pour ce métier."""
    return FENETRE_PAR_METIER.get(metier, (MOIS_AVANT_DEFAUT, MOIS_APRES_DEFAUT))


# ⚠️ Conservées pour les lecteurs qui les cherchent : ce sont les valeurs PAR
# DÉFAUT, plus la règle globale qu'elles étaient. Ne pas les utiliser dans un
# calcul — passer par `_fenetre_du_metier`, sinon le déneigement se retrouve
# avec la fenêtre des métiers de printemps.
MOIS_AVANT = MOIS_AVANT_DEFAUT
MOIS_APRES = MOIS_APRES_DEFAUT

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
    avant, apres = _fenetre_du_metier(metier)
    ouverture = ((mois_debut - avant - 1) % 12) + 1
    longueur = avant + apres + 1
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


# Les trois moments qu'un ouvreur peut avoir à décrire. 🔴 UN SEUL ÉTAT À LA
# FOIS, jamais deux drapeaux booléens : deux drapeaux finissent toujours par se
# contredire (« pas encore commencée » ET « bien entamée »), et c'est le genre
# d'incohérence qui ne se voit qu'en production, un mois précis de l'année.
MOMENT_A_VENIR = "a_venir"
MOMENT_DEBUT = "debut"
MOMENT_EN_COURS = "en_cours"

# Combien de JOURS après le début la saison compte encore comme « son début ».
#
# Décision William du 2026-09-07 : « la 3e formule, on peut commencer à
# l'envoyer 1 mois après le début de la saison ». Il a choisi d'ajouter une
# TROISIÈME formulation plutôt que de laisser filer ou de raccourcir la fenêtre
# — raccourcir aurait retué juillet, qui serait retombé à 3 leads joignables.
# Ce qu'elle corrige : un tondeur écrit le 31 juillet tond depuis **91 jours**,
# trois mois d'une saison qui en fait six, et lisait « c'est le début ».
#
# 🔴 EN JOURS, PAS EN MOIS, ET LA DIFFÉRENCE N'EST PAS COSMÉTIQUE. La fenêtre
# raisonne en mois calendaires — c'est la bonne granularité pour décider QUI est
# joignable, avec un arrondi volontairement généreux. Mais un déneigeur écrit le
# 10 décembre compterait « 1 mois » (novembre → décembre) alors qu'il n'a que
# **25 jours** de neige derrière lui : la phrase « c'est le début de la saison »
# est encore parfaitement vraie chez lui. Compter en mois l'aurait basculé un
# mois trop tôt sur les 138 déneigeurs de décembre — le plus gros lot de l'hiver.
#
# La règle en une phrase : la FENÊTRE se compte en mois, la PHRASE en jours.
JOURS_ENCORE_LE_DEBUT = 30


def moment_de_la_saison(metier: str, aujourdhui: date) -> str | None:
    """Où en est la saison de ce métier, pour choisir la phrase de l'ouvreur.

    Rend `MOMENT_A_VENIR`, `MOMENT_DEBUT`, `MOMENT_EN_COURS` — ou **None**
    quand il n'y a aucune phrase à choisir : métier sans saison documentée, ou
    mois hors de sa fenêtre (on ne lui écrit pas, la question ne se pose pas).

    🔴 LA FENÊTRE A DEUX MOITIÉS, et c'est ce qui rend le calcul non trivial.
    Elle s'ouvre `avant` mois AVANT le début et se ferme `apres` mois APRÈS. Un
    déneigeur lu le 10 septembre est dans sa fenêtre alors que la neige du
    15 novembre n'est pas tombée ; le même lu le 10 décembre y est aussi, la
    neige ayant commencé. On situe donc le jour par rapport au **dernier début
    passé**, puis on regarde s'il est dans la moitié d'avant ou celle d'après.

    ⚠️ Une version précédente comparait à un nombre de JOURS fixe (200) et
    disait « saison commencée » en mai pour le déneigement, en retrouvant la
    neige de l'automne précédent. La borne juste est la fenêtre elle-même.
    """
    saison = SAISONS.get(metier)
    if saison is None:
        return None
    if aujourdhui.month not in fenetre_mois(metier):
        return None

    mois, jour = saison
    precedent = date(aujourdhui.year, mois, jour)
    if precedent > aujourdhui:
        # Le début de CETTE année est encore devant : le dernier début passé
        # est celui de l'an dernier. Indispensable pour les saisons à cheval
        # sur le jour de l'An, comme le déneigement du 15 novembre.
        precedent = date(aujourdhui.year - 1, mois, jour)

    mois_ecoules = (
        (aujourdhui.year - precedent.year) * 12 + (aujourdhui.month - precedent.month)
    )
    _, apres = _fenetre_du_metier(metier)
    if mois_ecoules > apres:
        # Trop loin du dernier début pour être dans la moitié d'APRÈS : on est
        # donc dans la moitié d'AVANT, celle qui précède le prochain début.
        return MOMENT_A_VENIR
    if (aujourdhui - precedent).days <= JOURS_ENCORE_LE_DEBUT:
        return MOMENT_DEBUT
    return MOMENT_EN_COURS


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

    scene_moment_saison: str | None
    """Où en est la saison de la SCÈNE — un des trois `MOMENT_*`, ou None.

    C'est lui qui choisit l'ouvreur de C et D, et il y en a **trois** :
      · `MOMENT_A_VENIR`  → « La saison approche »
      · `MOMENT_DEBUT`    → « C'est le début de la saison »
      · `MOMENT_EN_COURS` → la formulation de pleine saison

    **None** = aucune date à affirmer : pas de scène, scène sans saison
    documentée, ou mois hors fenêtre. Dans le doute on n'affirme rien.

    🔴 C'est UN état, pas deux booléens. Deux drapeaux (« commencée »,
    « bien entamée ») finiraient par se contredire un mois précis de l'année,
    et l'incohérence ne se verrait qu'en production."""


def metier_depuis_industry(industry: str | None) -> str | None:
    """Le métier que nomme le secteur de sourcing, ou None.

    🔴 LE REPLI QUE LA SPEC PRÉVOYAIT ET QUI MANQUAIT. La spec du 2026-08-27
    décrit `metier_source` comme « services_offered · industry (repli) ·
    inconnu ». Le repli n'avait jamais été écrit : le champ `source` ne
    connaissait que `services_offered` et `inconnu`.

    Ce que ça coûtait : « Niwa Paysagiste », sourcée sur le mot-clé
    `paysagiste`, dont la fiche ne contient aucun libellé où la racine
    `paysag` apparaît. Sa seule reconnaissance était « pavage » — un métier
    sans saison. Avec la règle « un métier 12 mois sur 12 n'enclenche pas la
    séquence », elle ne serait JAMAIS contactée, alors qu'on sait qu'elle est
    paysagiste : c'est le mot-clé qui l'a fait entrer dans la liste.

    Le secteur est une donnée de sourcing, pas une déduction : il vient du
    mot-clé Google Places qui a trouvé l'entreprise. On lui applique les mêmes
    racines qu'aux services.
    """
    if not industry or not isinstance(industry, str):
        return None
    plat = _sans_accents(industry)
    for metier, motifs in _RACINES_RE.items():
        if any(m.search(plat) for m in motifs):
            return metier
    return None


def resoudre_metiers(
    services_offered: list[str] | None,
    aujourdhui: date,
    industry: str | None = None,
) -> MetiersResolus:
    """Apparie les libellés, choisit la scène, ordonne le reste.

    Déterministe et rejouable : deux appels avec les mêmes entrées rendent le
    même résultat, ce qu'un classement par LLM ne garantit pas.

    ⚠️ `industry` est ACCEPTÉ MAIS IGNORÉ. Le repli existe
    (`metier_depuis_industry`) et n'est appelé par personne — décision William
    du 2026-09-02. Le paramètre reste dans la signature pour que le
    débranchement soit vérifiable par un test plutôt que constaté par une
    absence. Voir le bloc de commentaires avant le calcul des fenêtres.
    """
    libelles = [s for s in (services_offered or []) if isinstance(s, str) and s.strip()]

    # Comptage des libellés par métier. Un libellé peut compter pour plusieurs
    # métiers (« déneigement et aménagement paysager ») : c'est voulu, garde-fou
    # nº2 — on inclut dans le doute.
    compte: dict[str, int] = {}
    ordre_apparition: dict[str, int] = {}
    # Les métiers de `EXIGE` dont au moins un libellé porte le signal.
    exigence_satisfaite: set[str] = set()
    for rang, libelle in enumerate(libelles):
        plat = _sans_accents(libelle)
        apparies = {
            metier
            for metier, motifs in _RACINES_RE.items()
            if any(m.search(plat) for m in motifs)
        }
        # Les exclusions AVANT `ECRASE` : un libellé faussement apparié ne doit
        # pas non plus servir à écraser un métier légitime. « Installation de
        # clôtures de piscine » ne doit ni ajouter `piscine`, ni faire
        # disparaître `excavation` par la règle piscine→excavation.
        for metier, phrases in EXCLUSIONS.items():
            if metier in apparies and any(p in plat for p in phrases):
                apparies.discard(metier)
        # Puis les EXIGENCES. 🔴 Elles n'écartent PAS le métier : elles notent
        # si le signal a été vu au moins une fois. Voir `EXIGE`.
        for metier, signaux in EXIGE.items():
            if metier in apparies and any(sig in plat for sig in signaux):
                exigence_satisfaite.add(metier)
        for gagnant, perdants in ECRASE.items():
            if gagnant in apparies:
                apparies -= set(perdants)
        # 🔴 On réordonne selon RACINES avant d'insérer, et ce n'est PAS
        # cosmétique. `apparies` est un SET de chaînes : son ordre d'itération
        # dépend de la randomisation du hash de Python, qui change à CHAQUE
        # processus. Il décidait donc l'ordre d'insertion dans `compte`, donc
        # l'ordre d'apparition, donc la rupture d'égalité du tri stable plus
        # bas — donc `dominant`, qui gouverne le LEXIQUE.
        #
        # Mesuré avant correctif, même entreprise, seeds différents :
        #   PYTHONHASHSEED=1 → Piscines Élégance dominant='piscine'
        #   PYTHONHASHSEED=0 → la même, dominant='excavation'
        # L'installateur de piscines se faisait demander « l'accès au terrain,
        # ce qu'il y a à creuser » un envoi sur deux.
        #
        # ⚠️ Un test de rejouabilité DANS UN SEUL PROCESSUS ne peut pas voir
        # ça : le hash est tiré une fois au démarrage. Il faut des processus
        # séparés — voir `test_le_dominant_est_stable_entre_processus`.
        for metier in _RACINES_RE:
            if metier not in apparies:
                continue
            compte[metier] = compte.get(metier, 0) + 1
            ordre_apparition.setdefault(metier, rang)

    if not compte:
        # Défaut inversé : aucun métier reconnu ne veut pas dire « aucun mois ».
        # Le lead reste joignable toute l'année et reçoit un ouvreur générique.
        return MetiersResolus(
            metiers=(), dominant=None, scene=None, autres=(), meme_saison=True,
            joignable=True, deuxieme_temps_obligatoire=False,
            scene_est_minoritaire=False, source="inconnu", fenetre_ouverte=(),
            scene_moment_saison=None,
        )

    # Décroissant par nombre de libellés ; à égalité, l'ordre d'apparition dans
    # `services_offered` (stable, donc rejouable).
    metiers = tuple(
        sorted(compte, key=lambda m: (-compte[m], ordre_apparition[m]))
    )
    dominant = metiers[0]
    source = "services_offered"

    # 🔴 LE REPLI SUR `industry` EXISTE MAIS N'EST PAS BRANCHÉ — décision
    # William du 2026-09-02. NE PAS LE REBRANCHER SANS LUI DEMANDER.
    #
    # ⚠️ La spec du 2026-08-27 le prévoit pourtant : elle décrit `metier_source`
    # comme « services_offered · industry (repli) · inconnu ». Il a été écrit le
    # 2026-09-02, mesuré, puis débranché le jour même. Sans cette note, la
    # prochaine session le rebranchera en croyant réparer un oubli.
    #
    # CE QU'IL FAISAIT : « Niwa Paysagiste », sourcée sur le mot-clé
    # `paysagiste`, n'a aucun libellé où la racine `paysag` apparaît — sa seule
    # reconnaissance est « pavage ». Le repli lui rendait `paysagement` depuis
    # son secteur, et elle devenait joignable en janvier.
    #
    # POURQUOI WILLIAM L'A REFUSÉ : « les compagnies comme Niwa Paysagiste qui
    # n'ont de reconnu qu'un métier sans réel lien, et qui est 12 mois sur 12,
    # on doit faire en sorte qu'elles ne soient pas contactées. »
    #
    # Le raisonnement tient : si la seule chose qu'on reconnaît d'un paysagiste
    # est « pavage », notre donnée sur lui est MAUVAISE. Lui écrire sur la foi
    # du mot-clé de sourcing, c'est deviner son métier — et le courriel entier
    # repose sur le fait qu'on parle de ce qu'il fait vraiment.
    #
    # ⚠️ Ce n'est PAS le même cas que « aucun métier reconnu », qui reste
    # joignable toute l'année (défaut inversé, garde-fou nº2). Là on n'a rien
    # et on le sait ; ici on a quelque chose et c'est faux. Une reconnaissance
    # fausse est pire qu'une absence de reconnaissance, parce qu'elle a l'air
    # d'une information.
    #
    # LA VRAIE CORRECTION pour ces fiches est en amont : que WF-3 extraie mieux
    # leurs services. 3 fiches sont dans ce cas au 2026-09-02.

    mois = aujourdhui.month
    # 🔴 L'EXIGENCE JOUE ICI, et nulle part ailleurs : un métier qui en a une
    # et dont le signal n'a jamais été vu ne peut pas OUVRIR un courriel — donc
    # ni fournir la scène, ni rendre l'entreprise joignable. Il reste dans
    # `metiers` et se fait nommer au 2ᵉ temps.
    #
    # ⚠️ PREMIÈRE TENTATIVE, LE 2026-09-04 : l'exigence retirait le métier de
    # la reconnaissance. Ça cassait `test_piscine_creusee_nest_PAS_de_lexcavation`,
    # et pour une bonne raison — la garde qui empêche « piscine creusée » de
    # devenir de l'excavation passe par `ECRASE["piscine"]`, qui a besoin que
    # `piscine` soit RECONNUE. Retirer la reconnaissance retirait la garde avec.
    # Le test préexistant a attrapé le mauvais placement ; la règle est la même,
    # elle s'applique juste au bon endroit.
    ouverts = tuple(
        m for m in metiers
        if mois in fenetre_mois(m)
        and (m not in EXIGE or m in exigence_satisfaite)
    )

    # La scène : parmi les métiers OUVERTS, celui dont la saison arrive le plus
    # tôt. 🔴 La clause « dont la fenêtre est ouverte » est ce qui fait tenir la
    # règle. Sans elle, mesuré : 76 des 113 joignables en décembre se font
    # parler d'un métier hors saison. Brille-O-Max (vitres + déneigement) est
    # joignable en décembre UNIQUEMENT grâce au déneigement, et la règle sans
    # clause lui parlait de lavage de vitres, parce que la prochaine saison de
    # vitres (1er avril) arrive avant la prochaine saison de neige (15 novembre).
    # 🔴 SEUL UN MÉTIER À SAISON DÉCLARÉE PEUT FOURNIR LA SCÈNE — 2026-09-02.
    #
    # Un métier absent de `SAISONS` a une fenêtre ouverte TOUS LES MOIS (garde-fou
    # nº2, on inclut dans le doute). Il gagnait donc la scène chaque fois que le
    # métier dominant était hors saison — et il la gagnait avec zéro jour avant
    # « sa prochaine saison », puisqu'il n'en a pas.
    #
    # Trouvé par le juge au deuxième passage réel : il a refusé le courriel
    # d'« Amaranthe Jardins - Paysagiste » parce que « l'angle d'ouverture sur le
    # pavage est décalé par rapport au profil réel (paysagiste écoresponsable de
    # jardins de ville) ». En septembre le paysagement est hors fenêtre, et le
    # pavage — qui n'a pas de saison — prenait l'ouverture.
    #
    # Mesuré le 2026-09-02 sur les 403 fiches d'alors : **90, soit 22 %**,
# avaient pour scène un métier
    # sans saison, et souvent contre leur métier dominant — on écrivait à
    # « Candide Villeneuve Paysagiste » au sujet de l'EXCAVATION.
    #
    # La règle qui manquait : la scène est un CROCHET SAISONNIER (« la saison
    # approche », « pour le reste de l'année »). Un métier sans saison n'en
    # fournit aucun ; il n'est pas « toujours de saison », il est simplement
    # hors du raisonnement. Il reste dans `autres` et se fait nommer au 2ᵉ temps.
    #
    # Quand aucun métier saisonnier n'est ouvert, `scene` reste None et
    # l'appelant retombe sur le dominant en signalant « hors fenêtre » — le
    # chemin qui existait déjà, et le bon : on parle de son vrai métier.
    ouverts_saisonniers = tuple(m for m in ouverts if m in SAISONS)

    scene = None
    if ouverts_saisonniers:
        scene = min(
            ouverts_saisonniers,
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
        source=source,
        fenetre_ouverte=ouverts,
        scene_moment_saison=moment_de_la_saison(scene, aujourdhui) if scene else None,
    )
