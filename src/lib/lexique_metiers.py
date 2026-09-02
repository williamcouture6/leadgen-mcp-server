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

    est_repli: bool = False
    """Vrai quand aucun métier n'a été reconnu. ⚠️ À COMPTER par l'appelant :
    si le repli sert souvent, ce n'est pas la copie qui est en cause, c'est WF-3
    qui n'a pas assez creusé — et on le saura au lieu de le deviner."""


_TABLE: dict[str, Lexique] = {
    "ménage": Lexique(
        "ménage", "chez un client",
        ("l'adresse", "la grandeur du logement", "à quelle fréquence"),
    ),
    "paysagement": Lexique(
        "paysagement", "sur un terrain",
        ("l'adresse", "la grandeur du terrain", "ce qu'il veut faire faire"),
    ),
    "tonte": Lexique(
        "tonte", "sur un terrain",
        ("l'adresse", "la grandeur du terrain", "ce qu'il veut faire faire"),
    ),
    "déneigement": Lexique(
        "déneigement", "dans la machine",
        ("l'adresse", "la grandeur de l'entrée", "à la saison ou à la bordée"),
    ),
    "piscine": Lexique(
        "piscine", "chez un client",
        ("l'adresse", "creusée ou hors-terre", "ce qui va pas"),
    ),
    "lavage de vitres": Lexique(
        "lavage de vitres", "en haut d'une échelle",
        ("l'adresse", "le nombre d'étages", "combien de fenêtres"),
    ),
    "pavage": Lexique(
        "pavage", "sur un chantier",
        ("l'adresse", "la surface à faire", "asphalte ou pavé uni"),
    ),
    "excavation": Lexique(
        "excavation", "dans la machine",
        ("l'adresse", "l'accès au terrain", "ce qu'il y a à creuser"),
    ),
    "extermination": Lexique(
        "extermination", "chez un client",
        ("l'adresse", "ce qu'il a vu", "depuis quand"),
    ),
    "toiture": Lexique(
        "toiture", "sur un toit",
        ("l'adresse", "la grandeur du toit", "si ça coule déjà"),
    ),
}

REPLI = Lexique(
    "repli", "sur un contrat",
    ("l'adresse", "ce qu'il cherche", "quand il en a besoin"),
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
