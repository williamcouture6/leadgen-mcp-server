"""Le lexique de métier — une table déterministe, pas de la génération.

Le rédacteur reçoit les mots **déjà choisis**. Sur 378 leads, un équivalent de
« grandeur d'entrée » réinventé à chaque fois dérive ; une table, non — et elle
se relit.

🔴 **Le lexique suit le métier DOMINANT, pas le métier de la scène.**
Seule la scène de l'ouvreur suit la saison. Sinon un laveur de vitres démarché
en août à propos de la neige se ferait demander la grandeur de son entrée de
garage dans le bloc service, alors qu'il lave des vitres commerciales onze mois
par année. ⚠️ L'ordre de résolution de la spec du 2026-08-26 disait l'inverse
(« métier de saison d'abord ») ; il est corrigé dans le même commit que ce
fichier, sinon les deux specs se contredisent à l'implémentation.

⚠️ Le lexique s'applique **au texte fixe autant qu'à l'ouvreur**. « Grandeur
d'entrée » dans le bloc service partirait sinon à un laveur de vitres, ce qui
révèle le gabarit en une seconde.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Lexique:
    metier: str
    ou_il_est: str
    """Où l'entrepreneur se trouve quand il ne peut pas répondre. Sert à
    l'ouvreur : « Le client, lui, il sait pas que t'es {ou_il_est}. »"""

    questions: tuple[str, str, str]
    """Les trois questions que le système pose au client final. Servent au bloc
    service : « Il demande {q1}, {q2}, {q3}. »"""

    phrase_du_rush: str = ""
    """Le moment où les appels arrivent tous en même temps, dans SES mots.

    🔴 Sert la TROISIÈME version de `{ANCRE_CD}` — « J'imagine qu'à la première
    bordée, ça rentre pas mal tout en même temps! » — choisie par William le
    2026-09-14 pour les entreprises qui n'ont ni note citable ni deux services
    à énumérer. Sans elle, le paragraphe n'avait plus rien à dire de vrai.

    ⚠️ LA PHRASE ENTIÈRE, jamais un morceau à recoller. Le premier jet ne
    stockait que la clause de temps, à glisser après « J'imagine qu' » : ça
    donnait « J'imagine qu'quand le gazon repart » sur la moitié de la table.
    L'élision française ne se décide pas au montage, et une phrase fixe que le
    rédacteur recopie n'a aucune raison d'être coupée en deux.

    ⚠️ Elle suit le métier DOMINANT et jamais la scène — contrairement au lieu,
    qui appartient à l'ouvreur. Le dominant ne dépend pas de la date, donc le
    rédacteur et le juge en rendent toujours la même, même rejoués des mois
    plus tard. Sur les 7 entreprises concernées, dominant et scène coïncident
    (elles n'ont qu'un métier) : le choix ne se voit qu'au rejeu.
    """

    est_repli: bool = False
    """Vrai quand aucun métier n'a été reconnu. ⚠️ À COMPTER par l'appelant :
    si le repli sert souvent, ce n'est pas la copie qui est en cause, c'est WF-3
    qui n'a pas assez creusé — et on le saura au lieu de le deviner."""


_TABLE: dict[str, Lexique] = {
    "ménage": Lexique(
        "ménage", "chez un client",
        ("l'adresse", "la grandeur du logement", "à quelle fréquence"),
        "J'imagine qu'au grand ménage du printemps, ça rentre pas mal tout en même temps!",
    ),
    "paysagement": Lexique(
        "paysagement", "sur un terrain",
        ("l'adresse", "la grandeur du terrain", "ce qu'il veut faire faire"),
        "J'imagine qu'au printemps, ça rentre pas mal tout en même temps!",
    ),
    "tonte": Lexique(
        "tonte", "sur un terrain",
        ("l'adresse", "la grandeur du terrain", "ce qu'il veut faire faire"),
        "J'imagine qu'au printemps, quand le gazon repart, ça rentre pas mal tout en même temps!",
    ),
    "déneigement": Lexique(
        "déneigement", "dans ta machine",
        ("l'adresse", "la grandeur de l'entrée", "à la saison ou à la bordée"),
        "J'imagine qu'à la première bordée, ça rentre pas mal tout en même temps!",
    ),
    "piscine": Lexique(
        "piscine", "chez un client",
        ("l'adresse", "creusée ou hors-terre", "ce qui va pas"),
        "J'imagine qu'aux premières chaleurs, ça rentre pas mal tout en même temps!",
    ),
    "lavage de vitres": Lexique(
        "lavage de vitres", "en haut d'une échelle",
        ("l'adresse", "le nombre d'étages", "combien de fenêtres"),
        "J'imagine qu'au grand ménage du printemps, ça rentre pas mal tout en même temps!",
    ),
    "pavage": Lexique(
        "pavage", "sur un chantier",
        ("l'adresse", "la surface à faire", "asphalte ou pavé uni"),
        "J'imagine qu'au printemps, quand l'asphalte repart, ça rentre pas mal tout en même temps!",
    ),
    "excavation": Lexique(
        "excavation", "dans ta machine",
        ("l'adresse", "l'accès au terrain", "ce qu'il y a à creuser"),
        "J'imagine qu'au dégel, ça rentre pas mal tout en même temps!",
    ),
    "extermination": Lexique(
        "extermination", "chez un client",
        ("l'adresse", "ce qu'il a vu", "depuis quand"),
        "J'imagine qu'aux premières chaleurs, quand les bibittes sortent, ça rentre pas mal tout en même temps!",
    ),
    "toiture": Lexique(
        "toiture", "sur un toit",
        ("l'adresse", "la grandeur du toit", "si ça coule déjà"),
        "J'imagine qu'après un gros coup de vent, ça rentre pas mal tout en même temps!",
    ),
}

REPLI = Lexique(
    "repli", "sur un contrat",
    ("l'adresse", "ce qu'il cherche", "quand il en a besoin"),
    # ⚠️ Le repli n'atteint JAMAIS la 3ᵉ ancre : elle ne sert qu'en C et D, et
    # `tete_fixe_servable` refuse déjà ces gabarits sans métier reconnu. La
    # clause neutre est là pour que la table reste complète, pas pour partir.
    "J'imagine qu'en début de saison, ça rentre pas mal tout en même temps!",
    est_repli=True,
)


def lexique_pour(dominant: str | None) -> Lexique:
    """Le lexique du métier DOMINANT. `None` ou métier inconnu → le repli.

    ⚠️ Ne jamais passer `scene` ici : c'est précisément l'erreur que la §3 a dû
    trancher.
    """
    if not dominant:
        return REPLI
    return _TABLE.get(dominant, REPLI)


# Les métiers du dictionnaire de `metiers.py` doivent tous avoir une entrée :
# un métier reconnu qui retomberait sur le repli passerait pour une lacune de
# WF-3 alors que c'est une lacune de cette table. Vérifié par un test.
METIERS_COUVERTS: frozenset[str] = frozenset(_TABLE)
