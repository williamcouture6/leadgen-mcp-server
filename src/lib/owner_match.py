"""Logique pure de confiance sur l'identité du décideur (scraping WF-3).

Source unique de la décision `owner_confidence` pour un email scrapé. Combine :
  - un matching DÉTERMINISTE email-nominatif ↔ nom de décideur (corroboration),
  - le champ `confidence` (high|medium|low) que le Research Agent attribue à
    chaque `decideur_candidat`.

Aucune dépendance DB/réseau : testable isolément.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _name_tokens(name: str) -> list[str]:
    s = _strip_accents(name).lower()
    return [t for t in re.split(r"[^a-z]+", s) if len(t) >= 2]


def _local_tokens(local: str) -> list[str]:
    s = _strip_accents(local).lower()
    return [t for t in re.split(r"[^a-z]+", s) if t]


def email_matches_name(local: str, nom_complet: str) -> bool:
    """True si le local d'un email nominatif corrobore un nom complet.

    Patterns acceptés (accents ignorés, ordre libre) : prénom+nom comme tokens
    séparés (jean.tremblay), collés (jeantremblay / tremblayjean), ou
    initiale+nom (j.tremblay / jtremblay) et prénom+initiale (jeant).
    """
    nt = _name_tokens(nom_complet)
    if len(nt) < 2:
        return False
    first, last = nt[0], nt[-1]
    lt = _local_tokens(local)
    joined = "".join(lt)
    if first in lt and last in lt:
        return True
    if joined in (first + last, last + first):
        return True
    if joined in (first[0] + last, last + first[0], first + last[0], last[0] + first):
        return True
    return False


def _match_nominative(local: str, decideurs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for d in decideurs or []:
        nom = (d or {}).get("nom_complet")
        if nom and email_matches_name(local, nom):
            return d
    return None


def _single_high_confidence(decideurs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """L'unique décideur à confidence=high, sinon None (>=2 high = ambigu)."""
    highs = [
        d for d in (decideurs or [])
        if (d or {}).get("confidence") == "high" and (d or {}).get("nom_complet")
    ]
    return highs[0] if len(highs) == 1 else None


def _split_name(nom_complet: str) -> tuple[str | None, str | None]:
    toks = (nom_complet or "").split()
    if not toks:
        return None, None
    if len(toks) == 1:
        return toks[0], None
    return toks[0], " ".join(toks[1:])


def _name_from_local(local: str) -> str | None:
    toks = [t for t in re.split(r"[^a-zA-Z]+", local) if len(t) >= 2]
    if len(toks) >= 2:
        return f"{toks[0].capitalize()} {toks[1].capitalize()}"
    return None


@dataclass(frozen=True)
class ScrapedContactDecision:
    owner_confidence: str            # 'confirmed' | 'potential' | 'unknown'
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    potential_owner: dict[str, Any] | None = None


def classify_scraped_contact(
    email_obj: dict[str, Any],
    decideur_candidats: list[dict[str, Any]] | None,
) -> ScrapedContactDecision:
    """Décide le `owner_confidence` d'un email scrapé.

    Ordre des règles (du plus sûr au moins sûr) :
      (a) email nominatif matché à un décideur          -> confirmed (nom attaché)
      (b) un seul décideur confidence=high              -> confirmed (même email générique)
      (c) un décideur existe (basse/moyenne confiance)  -> potential (nom dans potential_owner)
      (d) nominatif non matché, nom dérivable du local  -> potential (nom dérivé)
      (e) sinon                                          -> unknown
    """
    decideurs = decideur_candidats or []
    kind = email_obj.get("kind")
    local = email_obj.get("local", "")

    if kind == "nominative":
        matched = _match_nominative(local, decideurs)
        if matched:
            fn, ln = _split_name(matched.get("nom_complet", ""))
            return ScrapedContactDecision("confirmed", fn, ln, matched.get("titre"))

    high = _single_high_confidence(decideurs)
    if high:
        fn, ln = _split_name(high.get("nom_complet", ""))
        return ScrapedContactDecision("confirmed", fn, ln, high.get("titre"))

    if decideurs:
        d = decideurs[0]
        return ScrapedContactDecision(
            "potential",
            potential_owner={
                "nom_complet": d.get("nom_complet"),
                "titre": d.get("titre"),
                "source_url": d.get("source_url"),
            },
        )

    if kind == "nominative":
        derived = _name_from_local(local)
        if derived:
            return ScrapedContactDecision(
                "potential",
                potential_owner={"nom_complet": derived, "titre": None, "source_url": None},
            )

    return ScrapedContactDecision("unknown")


_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _decideur_fields(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "nom_complet": d.get("nom_complet"),
        "titre": d.get("titre"),
        "source_url": d.get("source_url"),
    }


def summarize_company_decideur(
    decideur_candidats: list[dict[str, Any]] | None,
    emails_found: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Résume le décideur d'une company en `(decideur_confirme, decideur_potentiel)`.

    Exclusif : au plus un des deux est non-None. Réutilise le matching déterministe
    et la confiance des `decideur_candidats` (high|medium|low).
      1. email nominatif matché à un décideur          -> confirme (fait, sans confidence)
      2. un seul décideur confidence=high              -> confirme
      3. sinon, meilleur candidat (rang high>medium>low) -> potentiel (avec confidence)
      4. aucun candidat nommé                           -> (None, None)
    """
    decideurs = [d for d in (decideur_candidats or []) if (d or {}).get("nom_complet")]
    if not decideurs:
        return None, None

    for em in emails_found or []:
        if em.get("kind") == "nominative":
            matched = _match_nominative(em.get("local", ""), decideurs)
            if matched:
                return _decideur_fields(matched), None

    high = _single_high_confidence(decideurs)
    if high:
        return _decideur_fields(high), None

    best = max(decideurs, key=lambda d: _CONFIDENCE_RANK.get(d.get("confidence"), 0))
    potentiel = _decideur_fields(best)
    potentiel["confidence"] = best.get("confidence")
    return None, potentiel
