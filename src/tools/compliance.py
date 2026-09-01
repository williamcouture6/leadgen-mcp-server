"""Tool `compliance` — Compliance Agent (WF-5).

Pre-send firewall pour les drafts outbound. Deux layers :
  1. **Deterministic checks** (rapide, sans LLM) — voir `lib/compliance_checks.py`.
     Bloque sur mots bannis, actions 1ère personne, fake social proof, footer
     LCAP, longueur, CTA, registre (cohérence tu/vous), créneaux Cal.com
     fabriqués, warmup window.
  2. **LLM judge** (Claude Sonnet) — voir `prompts/compliance.md`. Détecte les
     violations sémantiques que les regex ne peuvent pas voir (faits non
     vérifiables, preuve sociale subtile, promesses non tenables, etc.).

Un verdict `blocked` du layer 1 court-circuite — layer 2 skipped.

Le verdict final est écrit dans `messages` :
  - `compliance_check_passed` : true (approved) | false (blocked/needs_revision)
  - `compliance_notes` : résumé des violations + suggestions
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..lib.avis import bloc_faits_verifies
from ..lib.compliance_checks import (
    CheckResult,
    mentions_manquantes_dans_la_config,
    run_all,
)
from .research import sans_diagnostic

# ----------------------------------------------------------------------
# Prompt + modèle
# ----------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "compliance.md"
_DEFAULT_MODEL = "claude-sonnet-4-6"


# ----------------------------------------------------------------------
# LLM call (retry sur 529)
# ----------------------------------------------------------------------

def _is_transient_anthropic_error(exc: BaseException) -> bool:
    if isinstance(exc, (RateLimitError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        return status in (502, 503, 504, 529)
    return False


def _message_utilisateur_juge(
    body: str,
    subject: str,
    research_json: dict[str, Any] | None,
    social_proof: list[dict[str, Any]] | None,
    contact: dict[str, Any] | None = None,
    google_rating: float | None = None,
    google_reviews_count: int | None = None,
    followups: dict[str, str] | None = None,
) -> str:
    """Ce que le juge voit. Extrait en fonction pure pour être testable sans
    appeler Anthropic — un bloc qui disparaît du prompt doit casser un test,
    pas une campagne."""
    # 🔴 Les TROIS corps, pas seulement le premier. Le triplet part ensemble :
    # juger le courriel seul laissait deux tiers du contenu inspectés par
    # personne, et c'est exactement ce qui a masqué l'échec des relances sur
    # `check_cta_present`.
    bloc_relances = ""
    for cle, etiquette in (("relance_1", "Relance 1 (jour 3)"), ("relance_2", "Relance 2 (jour 7)")):
        texte = ((followups or {}).get(cle) or "").strip()
        if texte:
            bloc_relances += f"\n**{etiquette}** (en fil, sans objet) :\n{texte}\n"
    if bloc_relances:
        bloc_relances = (
            "\n## Les relances du même envoi — À JUGER AUSSI\n"
            "Elles partent au même prospect, 3 et 7 jours après. Une violation "
            "dans une relance est une violation de l'envoi.\n" + bloc_relances + "\n"
        )

    return (
        f"## Email à juger\n\n"
        f"**Sujet**: {subject}\n\n"
        f"**Corps**:\n{body}\n"
        f"{bloc_relances}\n"
        # 🔴 Les avis AVANT tout le reste. Sans la valeur de colonne sous les
        # yeux, le juge ne peut pas déclarer un chiffre inventé : il n'a aucun
        # moyen de savoir. C'est le bug de 0732d20, où il ne voyait pas la
        # fiche contact et criait au contact_mismatch sur des noms vrais.
        f"{bloc_faits_verifies(google_rating, google_reviews_count)}\n\n"
        f"## Destinataire (contact vérifié — source de vérité de l'identité)\n"
        f"```json\n{json.dumps(contact or {}, ensure_ascii=False, indent=2)}\n```\n"
        f"Le prénom/nom/titre ci-dessus viennent de la fiche contact vérifiée "
        f"(`email_source`: website_scrape = site officiel ; apollo = contact vérifié hérité). "
        f"Ils sont ANCRÉS par cette fiche — ne les traite JAMAIS comme un fait inventé "
        f"ni un contact_mismatch même s'ils n'apparaissent pas dans le research_json.\n\n"
        f"## research_json (faits vérifiables sur l'ENTREPRISE — pas l'identité du contact)\n"
        # `sans_diagnostic` retire la télémétrie du scraper d'emails : le juge
        # n'a pas à voir des compteurs de rejets ni des adresses tierces jetées
        # (bruit + risque de faux contact_mismatch).
        f"```json\n{json.dumps(sans_diagnostic(research_json), ensure_ascii=False, indent=2)}\n```\n\n"
        f"## social_proof disponible\n"
        f"```json\n{json.dumps(social_proof or [], ensure_ascii=False, indent=2)}\n```\n"
    )


@retry(
    retry=retry_if_exception(_is_transient_anthropic_error),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _llm_judge(
    body: str,
    subject: str,
    research_json: dict[str, Any] | None,
    social_proof: list[dict[str, Any]] | None,
    contact: dict[str, Any] | None = None,
    model: str = _DEFAULT_MODEL,
    max_tokens: int = 2500,
    google_rating: float | None = None,
    google_reviews_count: int | None = None,
    followups: dict[str, str] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY non défini")
    client = Anthropic(api_key=api_key)
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user = _message_utilisateur_juge(
        body, subject, research_json, social_proof, contact,
        google_rating, google_reviews_count, followups,
    )
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.1,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"No JSON in compliance LLM response: {text[:300]}")
        return json.loads(match.group(0))


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

class ComplianceCheckIn(BaseModel):
    message_id: str
    skip_llm: bool = False
    model: str = _DEFAULT_MODEL


class ComplianceCheckOut(BaseModel):
    message_id: str
    # "non_juge" = le juge LLM n'a pas répondu, le draft n'a PAS été inspecté.
    # Distinct de "error" (la passe elle-même a échoué) et de NULL en base
    # (jamais tenté). Voir migration 0045.
    verdict: str  # "approved" | "needs_revision" | "blocked" | "non_juge" | "error"
    send_decision: str  # "SEND" | "REVIEW_THEN_SEND" | "DO_NOT_SEND"
    deterministic_blockers: list[dict[str, Any]] = []
    deterministic_warnings: list[dict[str, Any]] = []
    # Remarques de FORME. Elles n'entrent PAS dans le verdict : le courriel
    # part quand même. Elles vivent dans `compliance_notes` et se comptent au
    # résumé du soir. Décision William du 2026-08-31.
    deterministic_infos: list[dict[str, Any]] = []
    llm_judge: dict[str, Any] | None = None
    reasoning: str = ""
    duration_ms: int | None = None
    error_text: str | None = None


async def compliance_check(
    *,
    message_id: str,
    body: str,
    subject: str,
    template_used: str | None,
    research_json: dict[str, Any] | None,
    social_proof: list[dict[str, Any]],
    available_slots: list[dict[str, Any]],
    contact: dict[str, Any] | None = None,
    skip_llm: bool = False,
    model: str = _DEFAULT_MODEL,
    track: str | None = None,
    tentatives: int = 0,
    google_rating: float | None = None,
    google_reviews_count: int | None = None,
    followups: dict[str, str] | None = None,
) -> ComplianceCheckOut:
    """Lance les 2 layers de compliance sur un draft donné.

    `track` sélectionne les critères du layer 1 qui en dépendent (registre
    tu/vous, bornes de longueur). Sans lui, `check_registre` retombe sur
    `vous` et bloque TOUS les corps `agence-ia`, qui tutoient.

    `tentatives` = valeur de `messages.compliance_tentatives` AVANT cette
    passe. Sert de garde anti-boucle quand le juge LLM tombe : voir la
    cascade de verdict plus bas.
    """
    import asyncio

    started = time.monotonic()

    # `compliance_tentatives` absent d'un SELECT rend None côté appelant, et
    # `None >= 2` lève un TypeError qui ferait avorter toute la passe.
    tentatives = tentatives or 0

    # Footer LCAP injecté par l'ESP (Instantly) au moment de l'envoi — pas dans
    # le body généré par WF-4. On le passe aux checks déterministes pour que
    # le scan legal_footer / loi25_privacy le voie. Vide = pas d'ESP footer
    # (mode dev ou tout est dans le body).
    appended_footer = os.environ.get("INSTANTLY_CAMPAIGN_FOOTER", "")

    # Layer 0 — la CONFIGURATION, avant de juger quoi que ce soit.
    #
    # Depuis que le corps ne porte plus de signature (decision du 2026-08-30),
    # le nom legal et le desabonnement ne vivent QUE dans
    # INSTANTLY_CAMPAIGN_FOOTER. Cette variable vide, `check_legal_footer`
    # accusait le CORPS d'un manquement venu de l'environnement : verdict
    # `blocked`, donc `compliance_check_passed = false`, donc le brouillon
    # quittait le lot POUR TOUJOURS et son contact restait gele a vie.
    #
    # On rend donc `error` AVANT toute ecriture : l'appelant ne persiste pas
    # les `error`, rien ne bouge en base, et tout repart de soi-meme le jour ou
    # la variable est remplie. La faute est nommee dans `error_text`, avec le
    # nom de la variable a corriger.
    manquants = mentions_manquantes_dans_la_config(appended_footer, track)
    if manquants:
        return ComplianceCheckOut(
            message_id=message_id,
            verdict="error",
            send_decision="DO_NOT_SEND",
            reasoning=(
                "Configuration LCAP incomplete : la passe est refusee AVANT "
                "d'avoir juge le brouillon. Le brouillon n'est pas en cause et "
                "n'est pas marque."
            ),
            error_text="config_lcap_incomplete: " + " | ".join(manquants),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Layer 1 — deterministic, sur LES TROIS CORPS.
    #
    # 🔴 Critère de fin nº3 de la spec du 2026-08-26 : le critère ne porte pas
    # sur le seul corps de tri. C'est cette formulation-là qui masquait l'échec
    # des deux relances sur `check_cta_present` — un draft « approuvé » dont
    # deux tiers du contenu n'avaient jamais été regardés.
    #
    # Chaque corps est jugé avec SON gabarit : le tri avec A ou B (180-270
    # mots), les relances avec RELANCE (40-120). Les juger tous sous le même
    # gabarit refuserait mécaniquement les relances, qui font 97 mots.
    corps_a_juger: list[tuple[str, str, str | None]] = [("courriel", body, template_used)]
    for cle, etiquette in (("relance_1", "relance 1"), ("relance_2", "relance 2")):
        texte = ((followups or {}).get(cle) or "").strip()
        if texte:
            corps_a_juger.append((etiquette, texte, "RELANCE"))

    det_results: list[CheckResult] = []
    for etiquette, texte, gabarit in corps_a_juger:
        for r in run_all(
            email_body=texte,
            social_proof_count=len(social_proof),
            available_slots=available_slots or None,
            template=gabarit,
            # Le sujet n'appartient qu'au courriel : les relances partent EN FIL
            # et n'en ont pas. Le passer aux trois ferait juger trois fois le
            # même sujet, et un warning y compterait triple.
            email_subject=subject if etiquette == "courriel" else None,
            appended_footer=appended_footer,
            track=track,
            google_rating=google_rating,
            google_reviews_count=google_reviews_count,
        ):
            # L'étiquette voyage avec le résultat : « cta_present » tout court
            # ne dit pas LEQUEL des trois corps est en faute, et c'est la
            # première question qu'on se pose en lisant l'alerte.
            det_results.append(
                r if etiquette == "courriel"
                else CheckResult(
                    name=f"{r.name}[{etiquette}]",
                    passed=r.passed, severity=r.severity,
                    message=f"{etiquette} — {r.message}", matches=r.matches,
                )
            )

    det_blockers = [r for r in det_results if not r.passed and r.severity == "block"]
    det_warnings = [r for r in det_results if not r.passed and r.severity == "warn"]
    # 🔴 Les remarques de FORME n'ont plus le droit de tuer un brouillon.
    #
    # Décision William du 2026-08-31. Le raisonnement qui l'a permise : les
    # corps sont des GABARITS FIXES, seul l'ouvreur est écrit librement. Un
    # « vous » de trop ou un cinquième « pis » n'est pas un mensonge, c'est un
    # texte moins bon — et le prospect n'a aucun moyen de savoir qu'une règle a
    # été enfreinte.
    #
    # Ce qui reste fatal : ce que le prospect peut VÉRIFIER (une note d'avis
    # fausse, une preuve sociale inventée, une action jamais faite, un site
    # annoncé prêt, un créneau qui n'existe pas) et les deux gardes légales.
    #
    # ⚠️ Le geste n'aurait servi à rien en passant les checks de `block` à
    # `warn` : mesuré, un `warn` produit `needs_revision`, qui écrit
    # `compliance_check_passed=false` — donc le brouillon meurt EXACTEMENT
    # comme avec `blocked`. Il fallait une troisième catégorie qui ne touche
    # pas au verdict du tout.
    det_infos = [r for r in det_results if not r.passed and r.severity == "info"]

    if det_blockers:
        return ComplianceCheckOut(
            message_id=message_id,
            verdict="blocked",
            send_decision="DO_NOT_SEND",
            deterministic_blockers=[asdict(r) for r in det_blockers],
            deterministic_warnings=[asdict(r) for r in det_warnings],
            deterministic_infos=[asdict(r) for r in det_infos],
            llm_judge=None,
            reasoning=f"Layer 1 a bloqué {len(det_blockers)} violation(s) déterministe(s).",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # Layer 2 — LLM judge (semantic)
    llm_verdict: dict[str, Any] | None = None
    if not skip_llm:
        try:
            llm_verdict = await asyncio.to_thread(
                _llm_judge, body, subject, research_json, social_proof, contact, model,
                2500, google_rating, google_reviews_count, followups,
            )
        except Exception as e:  # noqa: BLE001
            llm_verdict = {"error": f"LLM judge failed: {type(e).__name__}: {e}"}

    # Verdict final combinant warnings déterministes + LLM.
    #
    # L'ORDRE COMPTE : la panne du juge se teste AVANT tout le reste, parce
    # qu'un courriel non inspecté n'est pas un courriel approuvé. Le cron est
    # quotidien par lots de 20 : quelques minutes d'indisponibilité chez
    # Anthropic suffisaient à approuver le lot du jour, et l'alerte (qui
    # compte `needs_revision + blocked`) restait muette.
    juge_en_panne = bool(llm_verdict and llm_verdict.get("error"))

    if juge_en_panne and tentatives >= 2:
        # 3e tentative : un échec permanent doit réveiller quelqu'un, pas
        # tourner en rond. Devient un vrai refus, donc sort du lot du lendemain.
        final_verdict = "needs_revision"
        final_decision = "REVIEW_THEN_SEND"
    elif juge_en_panne:
        final_verdict = "non_juge"
        final_decision = "DO_NOT_SEND"
    elif llm_verdict and llm_verdict.get("send_decision") == "DO_NOT_SEND":
        final_verdict = "blocked"
        final_decision = "DO_NOT_SEND"
    elif llm_verdict and llm_verdict.get("send_decision") == "REVIEW_THEN_SEND":
        final_verdict = "needs_revision"
        final_decision = "REVIEW_THEN_SEND"
    elif det_warnings:
        final_verdict = "needs_revision"
        final_decision = "REVIEW_THEN_SEND"
    else:
        final_verdict = "approved"
        final_decision = "SEND"

    if juge_en_panne:
        # Surtout PAS « Aucune violation détectée » : rien n'a été inspecté.
        # `compliance_notes` se lit à l'œil nu, et cette phrase-là rassurerait
        # à tort sur le seul cas où il ne faut pas être rassuré.
        reasoning = (
            f"Juge LLM injoignable — corps NON inspecté (tentative {tentatives + 1}). "
            + ("Plafond atteint : passe en refus." if tentatives >= 2
               else "Sera rejugé à la prochaine passe.")
        )
    else:
        reasoning = (
            (llm_verdict or {}).get("reasoning_one_line")
            or (f"{len(det_warnings)} warning(s) déterministe(s)" if det_warnings else "Aucune violation détectée.")
        )

    return ComplianceCheckOut(
        message_id=message_id,
        verdict=final_verdict,
        send_decision=final_decision,
        deterministic_blockers=[],
        deterministic_warnings=[asdict(r) for r in det_warnings],
        deterministic_infos=[asdict(r) for r in det_infos],
        llm_judge=llm_verdict,
        reasoning=reasoning,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def format_compliance_notes(out: ComplianceCheckOut) -> str:
    """Texte concis pour `messages.compliance_notes` (lecture humaine)."""
    parts = [f"[{out.verdict.upper()}] {out.send_decision} — {out.reasoning}"]
    for b in out.deterministic_blockers:
        parts.append(f"BLOCK [{b['name']}]: {b['message']}")
        for m in b.get("matches", [])[:3]:
            parts.append(f"  - {m}")
    for w in out.deterministic_warnings:
        parts.append(f"warn [{w['name']}]: {w['message']}")
    # Les remarques de forme sont ECRITES meme si le courriel part : c'est
    # tout ce qui reste pour les relire. Sans cette boucle, la decision du
    # 2026-08-31 reviendrait a supprimer les checks au lieu de les degrader.
    for i in out.deterministic_infos:
        parts.append(f"remarque [{i['name']}]: {i['message']}")
        for m in i.get("matches", [])[:2]:
            parts.append(f"  - {m}")
    if out.llm_judge and not out.llm_judge.get("error"):
        for v in (out.llm_judge.get("semantic_violations") or [])[:5]:
            parts.append(f"semantic [{v.get('category')}]: {v.get('issue')} → {v.get('suggested_fix')}")
    elif out.llm_judge and out.llm_judge.get("error"):
        parts.append(f"llm_error: {out.llm_judge['error']}")
    return "\n".join(parts)
