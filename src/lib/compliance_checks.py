"""Deterministic compliance checks for outreach emails — no LLM needed.

Chaque check retourne (passed: bool, message: str, severity: 'block'|'warn').
L'orchestrateur (`tools/compliance.py`) les collecte en verdict. Ces checks
tournent AVANT le LLM judge pour court-circuiter sur les violations dures.

Lit les env vars suivantes :
  - LEGAL_COMPANY_NAME       (LCAP: identification expéditeur)
  - LEGAL_COMPANY_ADDRESS    (LCAP: adresse postale)
  - UNSUBSCRIBE_URL          (LCAP: lien désabonnement)
  - DPO_EMAIL                (Loi 25: canal vie privée — warn only)
  - WARMUP_END_DATE          (gate envoi pendant warmup Instantly — FAIL-CLOSED:
                              absent/invalide = BLOQUE l'envoi)
  - WARMUP_DISABLED          (échappatoire explicite: 'true' = désactive le gate)
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


def _mentions_reduites() -> bool:
    """Décision William du 2026-08-30, portée telle quelle.

    Le courriel reçu ne porte que la signature du COMPTE d'envoi Instantly
    (nom, domaine) augmentée du lien de désabonnement. Ce qui tombe :
    l'adresse postale (identification LCAP) et le canal vie privée explicite
    (Loi 25). Ce qui reste EXIGÉ, drapeau ou pas : le nom légal et un
    désabonnement qui fonctionne — les deux que la signature porte réellement,
    et les deux qui protègent le destinataire.

    Le contexte qui a mené là, parce qu'il est contre-intuitif :
    `INSTANTLY_CAMPAIGN_FOOTER` n'est qu'une DÉCLARATION, lue par
    `tools/compliance.py` pour la donner aux checks. Aucun code ne la pousse à
    l'ESP. Vérifié le 2026-08-30 : aucun pied de page de campagne n'existe côté
    Instantly, et la variable est vide. La « mesure » de la spec (88 mots de
    pied de page, 43 % du message) comptait donc un doublon inexistant.

    ⚠️ Doit être posé VOLONTAIREMENT. Absent, le comportement est celui d'avant :
    l'absence d'adresse BLOQUE. On n'assouplit pas une garde en silence — on
    nomme l'exception, on la date, et on la rend greppable. Même forme que
    `WARMUP_DISABLED` : seules des valeurs explicites l'allument.
    """
    return os.environ.get("LCAP_MENTIONS_REDUITES", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def check_legal_footer(email_body: str, appended_footer: str = "") -> CheckResult:
    """`appended_footer` couvre le cas où l'ESP (Instantly) injecte un footer
    LCAP (nom légal + adresse + lien désabo) au moment de l'envoi — donc absent
    du `email_body` généré par WF-4 mais présent dans le mail effectivement reçu.
    On scanne body + footer comme un seul texte pour valider les requis LCAP.

    Sous `LCAP_MENTIONS_REDUITES`, l'adresse postale n'est plus exigée (voir
    `_mentions_reduites`). Le nom légal et le désabonnement le restent.
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

    reduites = _mentions_reduites()
    if not reduites:
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

    if missing:
        message = f"{len(missing)} champ(s) LCAP manquant(s)"
    elif reduites:
        # La trace vit dans les notes de conformité du message : une décision
        # assumée doit rester lisible six mois plus tard, sinon elle se lit
        # comme un bug.
        message = "footer LCAP — mentions réduites (décision 2026-08-30), adresse postale non exigée"
    else:
        message = "footer LCAP complet"

    return CheckResult(
        name="legal_footer",
        passed=not missing,
        severity="block",
        message=message,
        matches=missing,
    )


def check_loi25_privacy_contact(email_body: str, appended_footer: str = "") -> CheckResult:
    combined = (email_body + "\n" + appended_footer) if appended_footer else email_body
    body_low = combined.lower()
    dpo = os.environ.get("DPO_EMAIL", "").lower()
    has_dpo = dpo and dpo in body_low
    has_privacy_link = bool(re.search(r"confidentialit[ée]|vie priv[ée]e|/privacy|/confidentialite", body_low))
    reduites = _mentions_reduites()
    passed = has_dpo or has_privacy_link or reduites

    if has_dpo or has_privacy_link:
        message = "canal vie privée explicite trouvé"
    elif reduites:
        message = "mentions réduites (décision 2026-08-30), canal vie privée non exigé"
    else:
        message = "aucun canal vie privée explicite (DPO_EMAIL ou lien politique)"

    return CheckResult(
        name="loi25_privacy_contact",
        passed=passed,
        severity="warn",
        message=message,
        matches=[] if passed else ["recommandé: ajouter 'Questions confidentialité : william@couture-ia.com' dans la signature"],
    )


# Bornes par (track, gabarit). Le pivot `agence-ia` fait 206 (A) et 224 (B)
# mots ; ses relances, 40 à 100. La piste OPT vise 60 à 90 mots et porte des
# gabarits qui s'appellent AUSSI « A » et « B » — indexer sur le seul gabarit
# ferait échouer 100 % des brouillons OPT, alors que la spec exige que le
# prompt OPT reste intact.
_BORNES_LONGUEUR = {
    ("agence-ia", "A"): (180, 250),
    ("agence-ia", "B"): (180, 250),
    # 🔧 Borne haute montée de 100 à 120 le 2026-08-30 (AC1b). Les 100 avaient
    # été posés sur une ESTIMATION (« ≈ 88 mots ») qui ne comptait pas de vrai
    # ouvreur — or l'ouvreur généré fait jusqu'à 45 mots à lui seul. Mesuré sur
    # les corps réels : 97 et 98 mots, et la variante sans site à 102, donc
    # DÉJÀ en échec. La spec interdit explicitement de laisser une marge d'un
    # mot ; 120 donne ~20 mots de jeu à chacune.
    ("agence-ia", "RELANCE"): (40, 120),
}
_BORNES_DEFAUT = (60, 95)


def check_length(
    email_body: str,
    template: str | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    track: str | None = None,
) -> CheckResult:
    cle = (track or "", (template or "").upper())
    defaut_min, defaut_max = _BORNES_LONGUEUR.get(cle, _BORNES_DEFAUT)
    if min_words is None:
        min_words = defaut_min
    if max_words is None:
        max_words = defaut_max
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


# Invitation explicite à répondre. Formules réellement utilisées par les
# prompts `agence-ia`/OPT (ex. "Dis-moi juste si tu veux le voir.",
# "...un appel rapide ?") pour le CTA du corps généré. "un appel rapide" est
# DISTINCT de "Un appel que tu peux pas prendre" (bloc service, canal — pas
# une action) : ce dernier ne matche pas.
# "(15|20|25|30) minutes" : rétrocompat OPT — motif FERMÉ et spécifique
# (pas un "?" générique), donc il ne recrée pas le faux vert : ni le bloc
# service ni la ligne de renvoi ne contiennent cette formule numérique.
_EXPLICIT_INVITE_RE = re.compile(
    r"dis[\s-]moi|juste[\s-][àa][\s-]me[\s-]dire|fais[\s-]moi[\s-]signe|"
    r"me[\s-]le[\s-]dire|fais[\s-]moi[\s-]savoir|un appel rapide|"
    r"\b(15|20|25|30)\s*minutes?\b"
)


def check_cta_present(email_body: str) -> CheckResult:
    """Le corps entier (hors signature) contient-il une invitation explicite
    à répondre ?

    Ancienne règle (v1) : `has_question ET (has_call_invite OU has_time_ask)`.
    Mesurée FAUSSE VERTE à DEUX MOITIÉS sur CORPS_A (voir
    tests/fixtures/corps_ac1.py) : `has_call_invite` verdissait sur le bloc
    SERVICE ("Un appel que tu peux pas prendre, un texto...") qui décrit un
    CANAL, pas une action ; `has_question` verdissait sur la ligne de RENVOI
    ("...tu peux-tu me pointer la bonne personne?"), présente dans tous les
    gabarits/relances et donc incapable de faire jamais échouer le check. Le
    vrai CTA ("Dis-moi juste si tu veux le voir.") ne portait ni l'un ni
    l'autre et n'était jamais regardé.

    v2 : `question (hors ligne de renvoi) OU invitation explicite`. Encore
    FAUSSE VERTE : une question RHÉTORIQUE dans l'ouvreur généré (ex. "Est-ce
    que ça t'arrive souvent de manquer des appels le soir?") porte un "?" qui
    n'est pas dans la ligne de renvoi, donc `has_question` verdit — alors que
    ce corps ne demande rien. Un "?" ne distingue pas une DEMANDE d'une
    question rhétorique.

    v3 (actuelle) : on abandonne `has_question` entièrement — le "?" seul ne
    prouve jamais qu'on sollicite une réponse. On exige une FORMULE
    d'invitation explicite. Ça rend la regex d'exclusion de la ligne de
    renvoi (v2) inutile : plus de "?" compté, plus besoin de l'exclure —
    supprimée.

    Compromis assumé : une future formulation du CTA absente de la liste
    produirait un FAUX REFUS (brouillon envoyé en relecture manuelle plutôt
    qu'auto-approuvé), pas un faux vert. C'est le bon sens d'erreur : un faux
    refus est VISIBLE (le brouillon atterrit dans la file « à relire » du
    résumé quotidien) alors qu'un faux vert expédie un courriel que personne
    n'a lu. On choisit délibérément l'erreur récupérable.
    """
    body = _body_without_signature(email_body).lower()
    has_explicit_invite = bool(_EXPLICIT_INVITE_RE.search(body))
    passed = has_explicit_invite
    return CheckResult(
        name="cta_present",
        passed=passed,
        severity="warn",
        message="CTA (invitation explicite) présent" if passed else "CTA faible ou absent",
        matches=[] if passed else [f"invitation_explicite={has_explicit_invite}"],
    )


def _warmup_disabled() -> bool:
    """Échappatoire EXPLICITE pour désactiver le gate (warmup terminé / aucun
    warmup requis). Doit être posée volontairement — jamais l'état par défaut."""
    return os.environ.get("WARMUP_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def check_warmup_window(today: date | None = None) -> CheckResult:
    # Désactivation explicite et volontaire du gate.
    if _warmup_disabled():
        return CheckResult(
            "warmup_window", True, "block",
            "WARMUP_DISABLED=true — gate désactivé explicitement (envoi autorisé)", [],
        )

    raw = os.environ.get("WARMUP_END_DATE", "").strip()
    if not raw:
        # FAIL-CLOSED : absence de config = on BLOQUE. Une barrière de sécurité ne
        # doit jamais s'ouvrir par oubli d'une variable d'env (sinon envoi accidentel).
        return CheckResult(
            "warmup_window", False, "block",
            "WARMUP_END_DATE non configuré — envoi BLOQUÉ par sécurité (fail-closed)",
            [
                "Pour autoriser l'envoi : poser WARMUP_END_DATE=YYYY-MM-DD "
                "(date passée si le warmup est terminé)",
                "OU poser WARMUP_DISABLED=true si aucun warmup n'est requis",
            ],
        )
    try:
        end_date = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        # FAIL-CLOSED : config douteuse = on BLOQUE plutôt que d'envoyer à l'aveugle.
        return CheckResult(
            "warmup_window", False, "block",
            f"WARMUP_END_DATE format invalide ({raw!r}) — envoi BLOQUÉ par sécurité, "
            f"attendu YYYY-MM-DD",
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


_REGISTRE_PAR_TRACK = {"agence-ia": "tu", "OPT": "vous"}


def check_registre(email_body: str, track: str | None = None) -> CheckResult:
    """Exige la COHÉRENCE du registre, pas un registre en particulier.

    Le défaut réel est le mélange (« ton site » puis « vous pouvez »), qui
    révèle un gabarit mal assemblé. Le registre attendu se dérive du track :
    `agence-ia` tutoie (décision du pivot), `OPT` vouvoie. Un track inconnu
    retombe sur `vous` — le comportement historique.

    Le seuil diffère entre les deux branches, et c'est voulu : la conjugaison
    à la 2e personne du singulier se répète naturellement dans un corps
    tutoyé (14 occurrences dans le gabarit A), alors que le vouvoiement peut
    tenir en deux mots. Le `>= 2` côté `vous` vient de l'ancien check et
    protège contre un corps trop neutre où une occurrence isolée ne prouve
    rien.
    """
    body_low = _body_without_signature(email_body).lower()
    # (?<!rendez-) : « rendez-vous » contient le token « vous » parce que le
    # trait d'union n'est pas un caractère de mot. Sans ce garde-fou, un seul
    # « rendez-vous » — le vocabulaire même du produit — bloque un corps
    # tutoyé, et un refus fige le contact à vie.
    vous_hits = re.findall(r"(?<!rendez-)\b(vous|votre|vos)\b", body_low)
    tu_hits = re.findall(r"\b(tu|t'as|t'es|t'en|tes|ton|ta|te|toi)\b", body_low)
    attendu = _REGISTRE_PAR_TRACK.get(track or "", "vous")

    if attendu == "tu":
        passed = bool(tu_hits) and not vous_hits
        intrus = vous_hits
    else:
        passed = len(vous_hits) >= 2 and not tu_hits
        intrus = tu_hits

    return CheckResult(
        name="registre",
        passed=passed,
        severity="block",
        message=f"registre attendu={attendu} tu={len(tu_hits)} vous={len(vous_hits)}",
        matches=[] if passed else (intrus or [f"registre {attendu} insuffisant"]),
    )


_MAX_PIS = 4


def check_tics_de_langage(email_body: str) -> CheckResult:
    """Le budget de « pis ».

    La voix québécoise de la copie en utilise, c'est voulu. Ce qui ne l'est
    pas, c'est la dérive du paragraphe GÉNÉRÉ sur des centaines de leads. Le
    seuil est haut exprès : les corps de référence en portent 1 et 3.
    """
    body = _body_without_signature(email_body).lower()
    hits = re.findall(r"\bpis\b", body)
    passed = len(hits) <= _MAX_PIS
    return CheckResult(
        name="tics_de_langage",
        passed=passed,
        severity="block",
        message=f"{len(hits)} « pis » (max {_MAX_PIS})",
        matches=[] if passed else [f"{len(hits)} occurrences de « pis »"],
    )


def run_all(
    email_body: str,
    social_proof_count: int,
    available_slots: list[dict] | None = None,
    template: str | None = None,
    email_subject: str | None = None,
    appended_footer: str = "",
    track: str | None = None,
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
        check_length(email_body, template=template, track=track),
        check_cta_present(email_body),
        check_cta_slots_real(email_body, available_slots),
        check_registre(email_body, track=track),
        check_tics_de_langage(email_body),
    ]
