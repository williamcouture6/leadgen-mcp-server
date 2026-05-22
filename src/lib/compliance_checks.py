"""Deterministic compliance checks for outreach emails — no LLM needed.

Port de `agents/lib/compliance_checks.py` vers le service Railway. Chaque check
retourne (passed: bool, message: str, severity: 'block'|'warn'). L'orchestrateur
(`tools/compliance.py`) les collecte en verdict. Ces checks tournent AVANT le LLM
judge pour court-circuiter sur les violations dures.

Lit les env vars suivantes :
  - LEGAL_COMPANY_NAME       (LCAP: identification expéditeur)
  - LEGAL_COMPANY_ADDRESS    (LCAP: adresse postale)
  - UNSUBSCRIBE_URL          (LCAP: lien désabonnement)
  - DPO_EMAIL                (Loi 25: canal vie privée — warn only)
  - WARMUP_END_DATE          (gate envoi pendant warmup Instantly)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime

# Tone violations — words that flag the email as AI-generated or sales-y.
BANNED_PATTERNS: dict[str, str] = {
    r"\bintelligence artificielle\b": "expression 'intelligence artificielle'",
    r"\bautomatisation\b": "mot 'automatisation'",
    r"\bautomatiser\b": "verbe 'automatiser'",
    r"\binnovant\b": "mot 'innovant'",
    r"\binnovation\b": "mot 'innovation'",
    r"\btransformer\b": "verbe 'transformer' (jargon corporate)",
    r"j'espère que ce courriel": "tournure 'j'espère que ce courriel...'",
    r"\bimpressionn[ée]\b": "mot 'impressionné(e)'",
    r"\bfascin[ée]\b": "mot 'fasciné(e)'",
    r"\bsolution\b": "mot 'solution' (jargon)",
    r"\bsynerg(?:ie|ies)\b": "mot 'synergie'",
    r"\bstratégique\b": "mot 'stratégique' (jargon)",
    r"\bécosystème\b": "mot 'écosystème' (jargon)",
    r"\bleviers?\b": "mot 'levier(s)' (jargon)",
    r"\bopportunit[ée]\b": "mot 'opportunité' (jargon)",
}

FIRST_PERSON_ACTION_PATTERNS: dict[str, str] = {
    r"j'ai rempli": "claim 'j'ai rempli' (action probablement non effectuée)",
    r"j'ai test[ée]": "claim 'j'ai testé' (action probablement non effectuée)",
    r"j'ai essay[ée]": "claim 'j'ai essayé' (action probablement non effectuée)",
    r"j'ai appel[ée]": "claim 'j'ai appelé' (action probablement non effectuée)",
    r"j'ai téléphon[ée]": "claim 'j'ai téléphoné' (action probablement non effectuée)",
    r"j'ai contact[ée]": "claim 'j'ai contacté' (action probablement non effectuée)",
    r"j'ai parl[ée]": "claim 'j'ai parlé' (action probablement non effectuée)",
    r"j'ai écout[ée]": "claim 'j'ai écouté' (action probablement non effectuée)",
    r"j'ai visit[ée]": "claim 'j'ai visité' (action probablement non effectuée)",
    r"j'ai assist[ée]": "claim 'j'ai assisté' (action probablement non effectuée)",
    r"j'ai discut[ée]": "claim 'j'ai discuté' (action probablement non effectuée)",
    r"j'ai re[çc]u": "claim 'j'ai reçu' (action probablement non effectuée)",
    r"on s'est crois[ée]s": "claim 'on s'est croisés' (rencontre probablement non effectuée)",
    r"on s'est parl[ée]": "claim 'on s'est parlé' (conversation probablement non effectuée)",
    r"\bhier soir,?\s+j['e]": "claim temporel 'hier soir, je/j'ai...' (action probablement fausse)",
    r"\bce matin,?\s+j['e]": "claim temporel 'ce matin, je/j'ai...' (action probablement fausse)",
}

SOCIAL_PROOF_PATTERNS: dict[str, str] = {
    r"\bd[ée]ploy[ée] chez\b": "claim 'déployé chez' (preuve sociale)",
    r"\bnos clients\b": "claim 'nos clients' (preuve sociale)",
    r"\bmes clients\b": "claim 'mes clients' (preuve sociale)",
    r"\bdeux .{0,30} à\b": "tournure 'deux X à Y' (souvent fausse preuve sociale)",
    r"\btrois .{0,30} à\b": "tournure 'trois X à Y'",
    r"\bplusieurs .{0,30} à (Montréal|Laval|Québec|Sherbrooke|Gatineau)": "claim de plusieurs clients dans une ville",
    r"on a mis en place .{0,40} pour": "claim 'on a mis en place X pour [client]'",
    r"j'ai mis en place .{0,40} pour": "claim 'j'ai mis en place X pour [client]'",
    r"\bcomme .{0,30} que j'accompagne\b": "claim 'comme X que j'accompagne'",
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    severity: str  # "block" | "warn"
    message: str
    matches: list[str]


def _body_without_signature(email_body: str) -> str:
    parts = re.split(r"\n\s*(?:—|---)\s*\n", email_body, maxsplit=1)
    return parts[0] if parts else email_body


def _find_matches(body: str, patterns: dict[str, str]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    low = body.lower()
    for pattern, label in patterns.items():
        for m in re.finditer(pattern, low, flags=re.IGNORECASE):
            hits.append((m.group(0), label))
    return hits


def check_banned_words(email_body: str) -> CheckResult:
    body = _body_without_signature(email_body)
    hits = _find_matches(body, BANNED_PATTERNS)
    for m in re.finditer(r"\bIA\b", body):
        start = max(0, m.start() - 20)
        ctx = body[start : m.end()].lower()
        if "couture ia" not in ctx:
            hits.append(("IA", "mot 'IA' isolé (à éviter dans le corps)"))
    return CheckResult(
        name="banned_words",
        passed=not hits,
        severity="block",
        message=f"{len(hits)} mot(s) banni(s) trouvé(s)" if hits else "aucun mot banni",
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_subject_banned_words(subject: str) -> CheckResult:
    if not subject:
        return CheckResult("subject_banned_words", True, "block", "sujet vide — check ignoré", [])
    hits = _find_matches(subject, BANNED_PATTERNS)
    for m in re.finditer(r"\bIA\b", subject):
        start = max(0, m.start() - 20)
        ctx = subject[start : m.end()].lower()
        if "couture ia" not in ctx:
            hits.append(("IA", "mot 'IA' isolé dans le sujet"))
    return CheckResult(
        name="subject_banned_words",
        passed=not hits,
        severity="block",
        message=f"{len(hits)} mot(s) banni(s) dans le sujet" if hits else "aucun mot banni dans le sujet",
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_subject_first_person_actions(subject: str) -> CheckResult:
    if not subject:
        return CheckResult("subject_first_person_actions", True, "block", "sujet vide — check ignoré", [])
    hits = _find_matches(subject, FIRST_PERSON_ACTION_PATTERNS)
    return CheckResult(
        name="subject_first_person_actions",
        passed=not hits,
        severity="block",
        message=(
            f"{len(hits)} action(s) non vérifiable(s) dans le sujet"
            if hits
            else "aucune action 1ère personne dans le sujet"
        ),
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_subject_fake_social_proof(subject: str, social_proof_count: int) -> CheckResult:
    if not subject or social_proof_count > 0:
        msg = "sujet vide" if not subject else "social_proof non vide, check ignoré"
        return CheckResult("subject_fake_social_proof", True, "block", msg, [])
    hits = _find_matches(subject, SOCIAL_PROOF_PATTERNS)
    return CheckResult(
        name="subject_fake_social_proof",
        passed=not hits,
        severity="block",
        message=(
            f"{len(hits)} preuve(s) sociale(s) suspecte(s) dans le sujet"
            if hits
            else "pas de fausse preuve sociale dans le sujet"
        ),
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_first_person_actions(email_body: str) -> CheckResult:
    body = _body_without_signature(email_body)
    hits = _find_matches(body, FIRST_PERSON_ACTION_PATTERNS)
    return CheckResult(
        name="first_person_actions",
        passed=not hits,
        severity="block",
        message=f"{len(hits)} action(s) au passé non vérifiable(s)" if hits else "aucune action première personne",
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_fake_social_proof(email_body: str, social_proof_count: int) -> CheckResult:
    if social_proof_count > 0:
        return CheckResult("fake_social_proof", True, "block", "social_proof non vide, check ignoré", [])
    body = _body_without_signature(email_body)
    hits = _find_matches(body, SOCIAL_PROOF_PATTERNS)
    return CheckResult(
        name="fake_social_proof",
        passed=not hits,
        severity="block",
        message=f"{len(hits)} preuve(s) sociale(s) suspecte(s) (social_proof est vide)" if hits else "pas de fausse preuve sociale",
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_legal_footer(email_body: str, appended_footer: str = "") -> CheckResult:
    """`appended_footer` couvre le cas où l'ESP (Instantly) injecte un footer
    LCAP (nom légal + adresse + lien désabo) au moment de l'envoi — donc absent
    du `email_body` généré par WF-4 mais présent dans le mail effectivement reçu.
    On scanne body + footer comme un seul texte pour valider les requis LCAP.
    """
    combined = (email_body + "\n" + appended_footer) if appended_footer else email_body
    body_low = combined.lower()
    body_norm = re.sub(r"\s+", " ", body_low)

    company_name = os.environ.get("LEGAL_COMPANY_NAME", "")
    address = os.environ.get("LEGAL_COMPANY_ADDRESS", "")
    unsubscribe = os.environ.get("UNSUBSCRIBE_URL", "")

    missing: list[str] = []

    if not company_name:
        missing.append("company_name: env var manquante")
    else:
        tokens = [t.lower() for t in company_name.split() if len(t) >= 3]
        absent_tokens = [t for t in tokens if t not in body_norm]
        if absent_tokens:
            missing.append(f"company_name tokens absents: {absent_tokens}")

    if not address:
        missing.append("address: env var manquante")
    else:
        first_chunk = address.split(",")[0].strip().lower()
        if first_chunk and first_chunk not in body_norm:
            missing.append(f"adresse postale ({first_chunk}) absente")

    if not unsubscribe:
        missing.append("unsubscribe: env var manquante")
    elif unsubscribe.lower() not in body_norm and "stop" not in body_norm:
        missing.append("unsubscribe URL ou mention 'STOP' absente")

    return CheckResult(
        name="legal_footer",
        passed=not missing,
        severity="block",
        message=f"{len(missing)} champ(s) LCAP manquant(s)" if missing else "footer LCAP complet",
        matches=missing,
    )


def check_loi25_privacy_contact(email_body: str, appended_footer: str = "") -> CheckResult:
    combined = (email_body + "\n" + appended_footer) if appended_footer else email_body
    body_low = combined.lower()
    dpo = os.environ.get("DPO_EMAIL", "").lower()
    has_dpo = dpo and dpo in body_low
    has_privacy_link = bool(re.search(r"confidentialit[ée]|vie priv[ée]e|/privacy|/confidentialite", body_low))
    passed = has_dpo or has_privacy_link
    return CheckResult(
        name="loi25_privacy_contact",
        passed=passed,
        severity="warn",
        message="canal vie privée explicite trouvé" if passed else "aucun canal vie privée explicite (DPO_EMAIL ou lien politique)",
        matches=[] if passed else ["recommandé: ajouter 'Questions confidentialité : william@couture-ia.com' dans la signature"],
    )


def check_length(
    email_body: str,
    template: str | None = None,
    min_words: int = 60,
    max_words: int | None = None,
) -> CheckResult:
    if max_words is None:
        max_words = 110 if (template or "").upper() == "B" else 90
    body = _body_without_signature(email_body)
    n = len(body.split())
    in_range = min_words <= n <= max_words
    return CheckResult(
        name="length",
        passed=in_range,
        severity="warn",
        message=f"{n} mots (cible {min_words}-{max_words}, template={template or '?'})",
        matches=[] if in_range else [f"corps = {n} mots"],
    )


def check_cta_present(email_body: str) -> CheckResult:
    body = _body_without_signature(email_body).lower()
    has_time_ask = bool(re.search(r"\b(15|20|25|30)\s*minutes?\b", body))
    has_question = "?" in body
    passed = has_time_ask and has_question
    return CheckResult(
        name="cta_present",
        passed=passed,
        severity="warn",
        message="CTA temps-borné + question présents" if passed else "CTA faible ou absent",
        matches=[] if passed else [f"time_ask={has_time_ask}", f"question={has_question}"],
    )


def check_warmup_window(today: date | None = None) -> CheckResult:
    raw = os.environ.get("WARMUP_END_DATE", "").strip()
    if not raw:
        return CheckResult(
            "warmup_window", True, "block",
            "WARMUP_END_DATE non configuré — gate désactivé", [],
        )
    try:
        end_date = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return CheckResult(
            "warmup_window", True, "block",
            f"WARMUP_END_DATE format invalide ({raw!r}) — gate désactivé, attendu YYYY-MM-DD",
            [f"valeur reçue: {raw!r}"],
        )
    today = today or date.today()
    if today < end_date:
        days_left = (end_date - today).days
        return CheckResult(
            name="warmup_window",
            passed=False,
            severity="block",
            message=f"Warmup actif jusqu'au {end_date.isoformat()} ({days_left} jour(s) restant(s)) — envoi INTERDIT",
            matches=[
                f"date du jour: {today.isoformat()}",
                f"fin warmup: {end_date.isoformat()}",
                "Pour débloquer: attendre la fin du warmup OU commenter WARMUP_END_DATE dans .env",
            ],
        )
    return CheckResult(
        name="warmup_window",
        passed=True,
        severity="block",
        message=f"Warmup terminé ({end_date.isoformat()} passé) — envoi autorisé",
        matches=[],
    )


def check_cta_slots_real(email_body: str, available_slots: list[dict] | None) -> CheckResult:
    if not available_slots:
        return CheckResult(
            "cta_slots_real", True, "block",
            "Pas de liste Cal.com fournie — check ignoré (CTA générique attendu)", [],
        )

    from .calcom import extract_slots_from_text, slot_in_available

    body = _body_without_signature(email_body)
    mentioned = extract_slots_from_text(body)
    if not mentioned:
        return CheckResult(
            "cta_slots_real", True, "block",
            "Aucun créneau précis dans l'email (CTA générique accepté)", [],
        )

    invalid: list[str] = []
    for day_fr, date_fr, time_fr in mentioned:
        if not slot_in_available(day_fr, date_fr, time_fr, available_slots):
            label = f"'{day_fr} {date_fr} {time_fr}'" if date_fr else f"'{day_fr} {time_fr}'"
            invalid.append(f"{label} absent ou incohérent avec Cal.com")

    return CheckResult(
        name="cta_slots_real",
        passed=not invalid,
        severity="block",
        message=(
            f"{len(invalid)} créneau(x) inventé(s) ou incohérent(s) (jour/date/heure)"
            if invalid
            else f"{len(mentioned)} créneau(x) mentionné(s), tous cohérents avec Cal.com"
        ),
        matches=invalid,
    )


def check_vouvoiement(email_body: str) -> CheckResult:
    body = _body_without_signature(email_body)
    body_low = body.lower()
    vous_count = len(re.findall(r"\bvous\b", body_low))
    votre_count = len(re.findall(r"\b(votre|vos)\b", body_low))
    tu_hits = re.findall(r"\b(tu|t'as|t'es|tes|ton|ta)\b", body_low)
    has_vouv = vous_count + votre_count >= 2
    passed = has_vouv and not tu_hits
    msg_parts = [f"vous={vous_count}", f"votre/vos={votre_count}"]
    if tu_hits:
        msg_parts.append(f"tutoiement={tu_hits}")
    return CheckResult(
        name="vouvoiement",
        passed=passed,
        severity="block",
        message=" ".join(msg_parts),
        matches=tu_hits if tu_hits else ([] if has_vouv else ["vouvoiement insuffisant"]),
    )


def run_all(
    email_body: str,
    social_proof_count: int,
    available_slots: list[dict] | None = None,
    template: str | None = None,
    email_subject: str | None = None,
    appended_footer: str = "",
) -> list[CheckResult]:
    return [
        check_warmup_window(),
        check_banned_words(email_body),
        check_subject_banned_words(email_subject or ""),
        check_first_person_actions(email_body),
        check_subject_first_person_actions(email_subject or ""),
        check_fake_social_proof(email_body, social_proof_count),
        check_subject_fake_social_proof(email_subject or "", social_proof_count),
        check_legal_footer(email_body, appended_footer=appended_footer),
        check_loi25_privacy_contact(email_body, appended_footer=appended_footer),
        check_length(email_body, template=template),
        check_cta_present(email_body),
        check_cta_slots_real(email_body, available_slots),
        check_vouvoiement(email_body),
    ]
