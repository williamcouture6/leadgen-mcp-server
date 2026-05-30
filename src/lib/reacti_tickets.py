"""REACTI — grille tickets moyens par verticale + calcul commission.

Track **REACTI** (voir `docs/reacti/commission-structure.md`). Version MACHINE
de la grille markdown — source de vérité unique côté code. Sert à injecter le
ticket moyen + la commission estimée dans le brief pré-RDV Slack (WF-8) quand un
prospect REACTI book un appel découverte.

Le ticket affiché = ticket moyen PAR DÉFAUT de la verticale (carte de tarif).
Le vrai ticket se confirme au discovery call → la grille n'est qu'un ancrage.

GATE : `resolve_vertical()` retourne `None` pour un prospect non-REACTI (ex:
santé/pro = track OPT). Aucune verticale matchée → aucun ticket dans le brief →
le brief OPT reste strictement inchangé.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Taux de commission — ✅ VERROUILLÉ 2026-05-30. Source de vérité unique (code).
COMMISSION_RATE = 0.15

# verticale -> (label humain, ticket moyen défaut $ CAD = 1er contrat/saison).
# Aligné sur docs/reacti/commission-structure.md.
_VERTICALS: dict[str, tuple[str, int]] = {
    "deneigement": ("Déneigement", 700),
    "tonte": ("Tonte / entretien pelouse", 800),
    "paysagiste_4_saisons": ("Paysagiste 4-saisons (annuel)", 1500),
    "piscine": ("Entretien piscine", 1500),
    "extermination": ("Extermination", 500),
    "vitres": ("Nettoyage de vitres", 250),
}

# Verticales non-ambigües : 1er mot-clé matché gagne.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("piscine", ("piscine", "pool", "spa")),
    ("extermination", ("extermination", "exterminateur", "pest", "vermin", "nuisible", "parasit")),
    ("vitres", ("vitre", "fenetre", "window", "glass", "lave-vitre")),
)

# Saison — pour départager tonte / déneigement / paysagiste 4-saisons.
# Réalité QC : une même boîte tond l'été ET déneige l'hiver -> 4-saisons.
_WINTER_KW = ("deneigement", "snow", "plow", "plowing", "snowplow", "neige")
_SUMMER_KW = ("tonte", "pelouse", "gazon", "lawn", "mowing", "mow", "paysag", "landscap")


@dataclass(frozen=True)
class ReactiTicket:
    """Ticket résolu pour une verticale REACTI."""
    vertical: str       # clé interne (ex: 'deneigement')
    label: str          # libellé humain pour le brief
    ticket: int         # ticket moyen défaut $
    commission: int     # ticket * COMMISSION_RATE, arrondi

    @property
    def rate_pct(self) -> int:
        return round(COMMISSION_RATE * 100)


def commission_for(ticket: int) -> int:
    """Commission $ = ticket × taux, arrondi à l'entier."""
    return round(ticket * COMMISSION_RATE)


def _norm(s: str | None) -> str:
    """Lowercase + retrait des accents (NFKD) pour un matching robuste."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def resolve_vertical(
    *,
    industry: str | None = None,
    google_types: list[str] | None = None,
    explicit: str | None = None,
) -> str | None:
    """Clé verticale REACTI, ou None si non-REACTI (= GATE).

    Ordre de résolution :
      1. `explicit` — champ verticale stocké (posé par le sourcing REACTI, à venir).
      2. Inférence mots-clés sur `industry` + `google_types`.

    None => prospect hors REACTI (ex: track OPT) => pas de ticket dans le brief.
    """
    if explicit and explicit in _VERTICALS:
        return explicit

    hay = _norm(" ".join(filter(None, [industry or "", " ".join(google_types or [])])))
    if not hay.strip():
        return None

    # 1) verticales non-ambigües
    for key, kws in _KEYWORDS:
        if any(k in hay for k in kws):
            return key

    # 2) logique saison (tonte / déneigement / 4-saisons)
    has_winter = any(k in hay for k in _WINTER_KW)
    has_summer = any(k in hay for k in _SUMMER_KW)
    if has_winter and has_summer:
        return "paysagiste_4_saisons"
    if has_winter:
        return "deneigement"
    if has_summer:
        return "tonte"
    return None


def ticket_for_company(
    *,
    industry: str | None = None,
    google_types: list[str] | None = None,
    explicit: str | None = None,
) -> ReactiTicket | None:
    """Résout la verticale d'une entreprise et retourne son ticket/commission.

    Retourne None si la boîte ne matche aucune verticale REACTI (no-op brief OPT).
    """
    v = resolve_vertical(industry=industry, google_types=google_types, explicit=explicit)
    if not v:
        return None
    label, ticket = _VERTICALS[v]
    return ReactiTicket(
        vertical=v,
        label=label,
        ticket=ticket,
        commission=commission_for(ticket),
    )
