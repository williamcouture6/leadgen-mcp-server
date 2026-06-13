"""Filtre de junk au sourcing WF-1 — rejette les entités que Google Places
retourne mais qui ne sont JAMAIS un prospect contracteur service à domicile.

Deux signaux complémentaires :

1. `DISQUALIFIED_PRIMARY_TYPES` — `places.primaryType` Google jamais pertinent
   (spa détente, hôtel, gym, OSBL...). Bug 2026-06-12 : le mot-clé catalogue
   "piscines et spas" ramenait des spas bien-être (Strøm, Nordik, Spa Escale
   Santé...) — 20 entrées hors-cible. Le mot-clé a été resserré
   ("installation/entretien de piscine"), ce filtre reste la 2e barrière si un
   futur mot-clé dérape de nouveau.

2. `CHAIN_NAME_SUBSTRINGS` — chaînes / franchises retail multi-succursales
   (Trévi, Club Piscine, Club Spa, Piscines Soucy). Pas le buyer de l'offre
   agence-ia (corporate, a déjà marketing/IT — voir garde anti-chaîne CLAUDE.md).
   Match substring case-insensitive sur le nom : attrape toutes les succursales
   peu importe le `primaryType`.

NE filtre PAS `sporting_goods_store` / `store` : trop d'installateurs piscine
locaux légit sont taggés ainsi par Google (Piscines Gratton, Concept Piscine
Design, Aqua Fibre...). On tranche ces chaînes par NOM, pas par type — sinon on
jetterait les vrais contracteurs avec les détaillants.
"""
from __future__ import annotations

# primaryType Google jamais un prospect contracteur service à domicile.
DISQUALIFIED_PRIMARY_TYPES: frozenset[str] = frozenset({
    "spa",
    "massage_spa",
    "hotel",
    "gym",
    "non_profit_organization",
})

# Sous-chaînes de nom = chaînes/franchises retail. Match case-insensitive.
CHAIN_NAME_SUBSTRINGS: tuple[str, ...] = (
    "trévi",
    "trevi",
    "club piscine",
    "club spa",
    "piscines soucy",
)


def sourcing_disqualify_reason(
    name: str | None, primary_type: str | None
) -> str | None:
    """Raison de rejet (str courte) si le lieu est du junk au sourcing, sinon
    `None`. Pur, sans I/O — testable seul, appelé dans la boucle WF-1 avant
    `insert_company` pour skipper l'insert.
    """
    if primary_type and primary_type in DISQUALIFIED_PRIMARY_TYPES:
        return f"primary_type:{primary_type}"
    if name:
        low = name.lower()
        for needle in CHAIN_NAME_SUBSTRINGS:
            if needle in low:
                return f"chain:{needle}"
    return None
