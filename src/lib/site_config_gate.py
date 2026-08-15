"""Garde P4.10 — un cold email agence-ia ne part que si le prospect a un site
produit par le pipeline de refonte (repo agence-ia, `lab/scrape-site`), rangé
dans `agence.site_configs` par P4.9.

Ce module DÉCIDE et rien d'autre : il ne pose pas de note, n'alerte personne et
n'écrit jamais en base. `tools/send.py` traduit la décision en statut, note et
alerte Slack.

Design : docs/superpowers/specs/2026-08-14-p4.10-garde-envoi-site-config-design.md
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import supabase_client as db

_AGENCE_SCHEMA = "agence"
# Le seul verdict qui laisse partir un courriel. 'a-verifier' bloque aussi :
# William le débloque en passant le verdict à 'ok' en base.
VERDICT_QUI_PASSE = "ok"


@dataclass(frozen=True)
class SiteConfigDecision:
    """`allowed` = le courriel a le droit de partir.
    `reason` = pourquoi pas, texte destiné à `skipped_reason` et aux notes.
    `read_failed` = la lecture elle-même a planté (panne → Slack), par
    opposition à une ligne simplement absente (attente normale → silence).
    """
    allowed: bool
    reason: str | None = None
    read_failed: bool = False


async def check_site_config(company_id: str | None) -> SiteConfigDecision:
    """Le prospect a-t-il un site produit par le pipeline, en état d'être servi ?

    Fail closed : toute incertitude (pas de company_id, lecture en erreur,
    verdict inattendu) bloque l'envoi. Un envoi retardé se rattrape à la passe
    suivante ; un courriel parti ne se rattrape pas.
    """
    if not company_id:
        return SiteConfigDecision(False, "contact sans company_id")

    try:
        rows = await db.select(
            "site_configs",
            params={
                "select": "verdict,gele",
                "company_id": f"eq.{company_id}",
                "limit": "1",
            },
            schema=_AGENCE_SCHEMA,
        )
    except Exception as e:  # noqa: BLE001 — dans le doute, on ne pousse pas
        return SiteConfigDecision(
            False, f"lecture_echouee: {e!r}", read_failed=True,
        )

    if not rows:
        return SiteConfigDecision(False, "aucun config produit (site_configs absent)")

    row = rows[0]
    verdict = (row.get("verdict") or "").strip()
    if verdict == VERDICT_QUI_PASSE:
        return SiteConfigDecision(True)

    if verdict == "a-verifier":
        raison = "verdict='a-verifier' (relecture requise)"
    elif verdict == "refuse":
        raison = "verdict='refuse'"
    else:
        raison = f"verdict inconnu: {verdict!r}"

    # `gele` ne décide de rien (il protège l'écriture des retouches manuelles),
    # mais il aide à comprendre un refus quand on lit les notes.
    if row.get("gele"):
        raison = f"{raison} · gele=true"
    return SiteConfigDecision(False, raison)
