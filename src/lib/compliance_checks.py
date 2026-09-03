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


# 🔴 SÉPARÉ des actions inventées le 2026-08-31, décision William.
#
# Ces trois formules ne sont PAS des mensonges : « j'ai vu ton site » peut être
# parfaitement vrai. Elles sont interdites pour une raison de FORME — elles
# mettent la recherche en scène au lieu de la prouver, et c'est le tell nº1 du
# courriel de masse.
#
# Or la règle tranchée le 2026-08-31 est nette : seul ce que le prospect peut
# VÉRIFIER a le droit de tuer un brouillon. Une mise en scène maladroite n'est
# pas vérifiable par lui — il ne sait pas qu'une règle existe. Elles passent
# donc en `info` : écrites dans les notes, comptées au résumé, mais le courriel
# part.
#
# ⚠️ Elles restent SÉPARÉES des actions inventées, et c'est tout l'intérêt de
# la scission : « j'ai testé ton formulaire » est un mensonge que le prospect
# peut démentir, « j'ai vu ton site » ne l'est pas. Les garder dans le même
# dictionnaire forçait à choisir un seul sort pour les deux.
MISE_EN_SCENE_PATTERNS: dict[str, str] = {
    # `[es]?` et pas une limite de mot seule : « j'ai VUE ton site » — accord fautif mais très
    # courant à l'écrit rapide — échappait au motif.
    r"j'ai vu[es]?\b": "formule 'j'ai vu' (met la recherche en scène — règle nº4)",
    r"j'ai lu[es]?\b": "formule 'j'ai lu' (met la recherche en scène — règle nº4)",
    r"j'ai remarqu[ée]": "formule 'j'ai remarqué' (met la recherche en scène — règle nº4)",
}

# 🔴 La dette d'honnêteté du 2026-08-26, armée en déterministe le 2026-08-31.
#
# Le site n'existe PAS au moment du courriel : il se fabrique à la main APRÈS
# une réponse positive. Toute formulation qui le dit fait est un mensonge que le
# prospect découvrira au pire moment — et c'est le seul destinataire capable de
# le détecter en une seconde.
#
# Pourquoi en déterministe et pas seulement au juge : ces phrases sont FIXES et
# la faute coûte une relation. Le juge les connaît (`compliance.md` §1ter) mais
# il est probabiliste ; ici on veut un refus certain. Vérifié le 2026-08-31 :
# avant ce bloc, « j'en ai aussi profité pour te refaire un site web au goût du
# jour » passait les 14 checks.
SITE_DEJA_FAIT_PATTERNS: dict[str, str] = {
    r"au go[uû]t du jour": (
        "« au goût du jour » — présume que son site est démodé (jamais regardé), "
        "et ment aux 97 entreprises qui n'en ont pas"
    ),
    r"j'en ai (?:aussi )?profit[ée]": (
        "« j'en ai profité pour » — dit le site DÉJÀ FAIT. Il se fabrique après "
        "le oui : c'est la dette d'honnêteté refermée le 2026-08-26"
    ),
    r"ton (?:nouveau )?site est (?:pr[êe]t|fait|termin[ée]|refait)": (
        "affirme que le site existe déjà"
    ),
    r"je te (?:l'|le )envoie": (
        "« je te l'envoie » — présume le site fait. Le CTA demande le OUI, "
        "la fabrication vient après"
    ),
}


SOCIAL_PROOF_PATTERNS: dict[str, str] = {
    r"\bd[ée]ploy[ée] chez\b": "claim 'déployé chez' (preuve sociale)",
    r"\bnos clients\b": "claim 'nos clients' (preuve sociale)",
    r"\bmes clients\b": "claim 'mes clients' (preuve sociale)",
    # ⚠️ « deux X à Y » et « trois X à Y » ne vivent PLUS ici : ils exigent la
    # casse, que `_find_matches` détruit. Voir SOCIAL_PROOF_PATTERNS_CASSE.
    r"\bplusieurs .{0,30} à (Montréal|Laval|Québec|Sherbrooke|Gatineau)": "claim de plusieurs clients dans une ville",
    r"on a mis en place .{0,40} pour": "claim 'on a mis en place X pour [client]'",
    r"j'ai mis en place .{0,40} pour": "claim 'j'ai mis en place X pour [client]'",
    r"\bcomme .{0,30} que j'accompagne\b": "claim 'comme X que j'accompagne'",
}


# 🔴 Motifs qui ont besoin de la CASSE — cherchés hors de `_find_matches`.
#
# CE QUE CES DEUX MOTIFS VISENT : « deux cliniques à Montréal », un claim de
# plusieurs clients dans un lieu. Ce qu'ils attrapaient AVANT le 2026-09-01 :
# n'importe quel « à » dans les 30 caractères suivants. Sévérité `block`, donc
# brouillon mort et contact gelé à vie.
#
# Mesuré sur le VRAI chemin (`check_fake_social_proof`, pas un `re.search` de
# laboratoire) — 7 tournures innocentes sur 7 étaient bloquées :
#   « il pose deux ou trois questions à ton client »  ← la façon naturelle de
#     décrire la qualification, donc le cœur du gabarit D
#   « ça change deux choses à ton entreprise » · « deux minutes à répondre »
#   « t'as deux façons à considérer » · « trois affaires à régler avant l'hiver »
#   « il te reste deux semaines à attendre » · « deux ou trois clics à faire »
# Ce n'était pas propre à D : l'OUVREUR GÉNÉRÉ de A et B peut tomber dedans sur
# n'importe quel lead.
#
# LE CORRECTIF : exiger une MAJUSCULE après « à », c'est-à-dire un nom propre.
#
# 🔴 ET POURQUOI ILS NE PEUVENT PAS RESTER DANS `SOCIAL_PROOF_PATTERNS`.
# `_find_matches` fait `body.lower()` ET passe `re.IGNORECASE`. Deux raisons
# indépendantes pour qu'une classe `[A-ZÀ-Ü]` n'y matche jamais — le motif
# resserré y serait MORT, et la garde entièrement désarmée, en silence. C'est
# exactement le défaut que la docstring de `_find_matches` raconte pour
# l'apostrophe courbe, et il a failli se reproduire le jour même : la première
# mesure du resserrage utilisait `re.search` sur le texte brut et montrait 5/5
# et 0/7. Elle ne testait pas le chemin réel. Toute mesure de garde passe
# désormais par la fonction de check elle-même.
#
# CE QUI RESTE OUVERT, ASSUMÉ : un lieu en minuscules (« deux clients à
# laval ») passe. Le juge couvre la preuve sociale subtile (compliance.md §2),
# et une garde déterministe vaut mieux étroite et juste que large et fausse —
# celle qui refuse 7 phrases honnêtes pour 5 vraies se fait contourner par la
# rédaction, pas respecter.
SOCIAL_PROOF_PATTERNS_CASSE: dict[str, str] = {
    # `(?i:...)` sur le SEUL mot-nombre : « Deux cliniques à Montréal » ouvre
    # une phrase, donc la majuscule y est normale et ne dit rien. La casse ne
    # porte de l'information qu'APRÈS « à », où elle distingue un nom propre
    # d'un mot ordinaire. Un `re.IGNORECASE` global rendrait `[A-ZÀ-Ü]`
    # équivalent à `[a-zà-ü]` et détruirait la garde — c'est exactement le
    # piège de `_find_matches`. Ce cas vient d'un test PRÉEXISTANT qui a cassé
    # au premier jet.
    r"\b(?i:deux) .{0,30} à [A-ZÀ-Ü]": "tournure 'deux X à [NomPropre]' (souvent fausse preuve sociale)",
    r"\b(?i:trois) .{0,30} à [A-ZÀ-Ü]": "tournure 'trois X à [NomPropre]'",
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
    """🔴 Normalise l'APOSTROPHE avant de chercher.

    Tous les motifs d'action à la première personne sont écrits avec une
    apostrophe DROITE (« j'ai vu »). Un traitement de texte, un copier-coller
    depuis Word, ou un modèle qui soigne sa typographie produisent l'apostrophe
    COURBE U+2019 — et le motif ne matche plus rien.

    Le filet est unique : `prompts/compliance.md` interdit explicitement au juge
    LLM de re-checker les actions au passé en première personne, parce qu'on lui
    a dit que le déterministe s'en charge. Un caractère invisible désarmait donc
    la règle entièrement, sans que rien ne le signale.
    """
    hits: list[tuple[str, str]] = []
    low = body.lower().replace("’", "'").replace("ʼ", "'")
    for pattern, label in patterns.items():
        for m in re.finditer(pattern, low, flags=re.IGNORECASE):
            hits.append((m.group(0), label))
    return hits


def _find_matches_casse(body: str, patterns: dict[str, str]) -> list[tuple[str, str]]:
    """Comme `_find_matches`, mais la CASSE est conservée.

    Même normalisation d'apostrophe — le piège du U+2019 vaut ici aussi — et
    surtout PAS de `.lower()` ni de `re.IGNORECASE`, sinon une classe
    `[A-ZÀ-Ü]` matcherait tout et la garde deviendrait plus large qu'avant au
    lieu de plus étroite.

    Sert à `SOCIAL_PROOF_PATTERNS_CASSE`, dont les motifs distinguent
    « deux entreprises à Laval » (preuve sociale) de « deux questions à ton
    client » (description du service) sur la seule majuscule.
    """
    hits: list[tuple[str, str]] = []
    texte = body.replace("’", "'").replace("ʼ", "'")
    for pattern, label in patterns.items():
        for m in re.finditer(pattern, texte):
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
        severity="info",
        message=f"{len(hits)} mot(s) banni(s) trouvé(s)" if hits else "aucun mot banni",
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_subject_banned_words(subject: str) -> CheckResult:
    if not subject:
        return CheckResult("subject_banned_words", True, "info", "sujet vide — check ignoré", [])
    hits = _find_matches(subject, BANNED_PATTERNS)
    for m in re.finditer(r"\bIA\b", subject):
        start = max(0, m.start() - 20)
        ctx = subject[start : m.end()].lower()
        if "couture ia" not in ctx:
            hits.append(("IA", "mot 'IA' isolé dans le sujet"))
    return CheckResult(
        name="subject_banned_words",
        passed=not hits,
        severity="info",
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
    hits += _find_matches_casse(subject, SOCIAL_PROOF_PATTERNS_CASSE)
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
    hits += _find_matches_casse(body, SOCIAL_PROOF_PATTERNS_CASSE)
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


def mentions_manquantes_dans_la_config(
    appended_footer: str = "", track: str | None = None
) -> list[str]:
    """Ce qui manque au pied de page DÉCLARÉ, quand les mentions sont réduites.

    🔴 **C'est une faute de CONFIGURATION, jamais une faute du brouillon.**

    Le piège que ça referme, trouvé par le conseil du 2026-08-30 : depuis que le
    corps ne porte plus de signature, le nom légal et le lien de désabonnement
    ne vivent QUE dans `INSTANTLY_CAMPAIGN_FOOTER`. Cette variable vide,
    `check_legal_footer` accusait le CORPS d'un manquement qui venait de
    l'environnement — verdict `blocked`, donc `compliance_check_passed = false`,
    donc le brouillon quittait le lot POUR TOUJOURS (la requête ne reprend que
    les `is.null`) et son contact restait gelé à vie dans la fenêtre WF-4.
    Vingt contacts brûlés par jour, 255 en deux semaines, zéro courriel envoyé,
    et la suite de tests verte du début à la fin.

    L'appelant doit donc refuser de LANCER LA PASSE plutôt que de refuser les
    messages : rien n'est écrit en base, et tout repart tout seul le jour où la
    variable est remplie.

    Rend la liste vide quand tout va bien, ou quand les mentions ne sont pas
    réduites (le corps est alors censé tout porter, et `check_legal_footer` est
    le bon juge).
    """
    if not _mentions_reduites():
        # 🔴 LE DRAPEAU ABSENT EST LUI-MÊME UNE FAUTE DE CONFIGURATION sur
        # `agence-ia`, et c'est le cas PAR DÉFAUT — donc le plus dangereux.
        #
        # Trouvé le 2026-08-31 en préparant le go-live. Le layer 0 ne couvrait
        # que « drapeau POSÉ + pied de page vide ». Or depuis AC1b, les corps
        # `agence-ia` ne portent PLUS de signature, par conception. Sans le
        # drapeau, `check_legal_footer` cherche donc l'adresse postale dans un
        # texte qui n'en a jamais eu, et rend `blocked` :
        #
        #     verdict='blocked' → compliance_check_passed=false
        #     → le brouillon quitte le lot POUR TOUJOURS
        #     → son contact gèle à vie
        #
        # Exactement le désastre que le layer 0 existe pour empêcher, atteint
        # par l'autre porte. Et par la porte la plus probable : celle qu'on
        # emprunte quand on OUBLIE de poser une variable sur Railway.
        #
        # ⚠️ Ce n'est PAS le cas d'OPT : ses corps portent leur signature, donc
        # l'absence du drapeau y est le comportement normal et correct.
        if track == "agence-ia":
            return [
                "LCAP_MENTIONS_REDUITES n'est pas posée alors que la piste "
                "`agence-ia` écrit des corps SANS signature (décision du "
                "2026-08-30). `check_legal_footer` cherche donc l'adresse "
                "postale dans un texte qui n'en a jamais eu, et refuserait "
                "tous les brouillons du lot."
            ]
        return []

    texte = re.sub(r"\s+", " ", (appended_footer or "").lower())
    manquants: list[str] = []

    if not texte.strip():
        return [
            "INSTANTLY_CAMPAIGN_FOOTER est VIDE alors que LCAP_MENTIONS_REDUITES "
            "est actif : plus rien ne porte le nom légal ni le désabonnement"
        ]

    nom = os.environ.get("LEGAL_COMPANY_NAME", "")
    if not nom:
        manquants.append("LEGAL_COMPANY_NAME: env var manquante")
    else:
        absents = [t.lower() for t in nom.split() if len(t) >= 3 and t.lower() not in texte]
        if absents:
            manquants.append(
                f"INSTANTLY_CAMPAIGN_FOOTER ne porte pas le nom légal (absents: {absents})"
            )

    desabo = os.environ.get("UNSUBSCRIBE_URL", "")
    if not desabo:
        manquants.append("UNSUBSCRIBE_URL: env var manquante")
    elif desabo.lower() not in texte and "stop" not in texte:
        # 🔧 Le message DIT ce qu'il cherchait et ce qu'il a vu — 2026-09-02.
        #
        # L'ancienne version se contentait de « ne porte pas le lien de
        # désabonnement ». Vrai, mais inexploitable : deux causes très
        # différentes produisent ce message au mot près, et rien ne permet de
        # les distinguer depuis l'extérieur.
        #   · Railway n'a gardé que la PREMIÈRE LIGNE d'une valeur multi-lignes
        #     (le nom légal passe, le lien a disparu avec le reste) ;
        #   · UNSUBSCRIBE_URL porte une barre oblique finale, donc n'est plus
        #     une sous-chaîne du pied de page.
        # Observé pour de vrai le 2026-09-02 : la garde a bloqué le premier
        # passage réel du pipeline, et il a fallu tester les deux hypothèses en
        # local pour deviner laquelle.
        #
        # Ni l'URL ni le pied de page ne sont des secrets — c'est de la
        # signature publique, lue par 363 prospects.
        apercu = texte[:80] + ("…" if len(texte) > 80 else "")
        manquants.append(
            f"INSTANTLY_CAMPAIGN_FOOTER ne porte pas le lien de désabonnement. "
            f"Cherché : {desabo!r} (valeur de UNSUBSCRIBE_URL, comparée telle "
            f"quelle — une barre oblique finale suffit à la faire échouer). "
            f"Pied de page vu, {len(texte)} caractères : {apercu!r}"
        )

    return manquants


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
        severity="info",
        message=message,
        matches=[] if passed else ["recommandé: ajouter 'Questions confidentialité : william@couture-ia.com' dans la signature"],
    )


# Bornes par (track, gabarit). Le pivot `agence-ia` fait 206 (A) et 224 (B)
# mots ; ses relances, 40 à 100. La piste OPT vise 60 à 90 mots et porte des
# gabarits qui s'appellent AUSSI « A » et « B » — indexer sur le seul gabarit
# ferait échouer 100 % des brouillons OPT, alors que la spec exige que le
# prompt OPT reste intact.
_BORNES_LONGUEUR = {
    # 🔧 Borne haute portee de 250 a 270 le 2026-08-30, apres le conseil de
    # revue. Les 250 avaient ete calibres sur des corps de 206 et 224 mots qui
    # ne portaient PAS deux elements aujourd'hui obligatoires : le 2e temps
    # (jusqu'a ~10 mots, obligatoire des que l'entreprise est multi-metier,
    # soit 44 % de la liste) et la variante « sans site » (+6 mots, 97
    # entreprises). Mesure avec les deux : CORPS_B_SANS_SITE = 246 mots, soit
    # 4 de marge -- exactement la marge d'un cheveu que la spec interdit.
    # La spec prevoit elle-meme cet arbitrage : « soit on allonge le repli,
    # soit on baisse la borne, mais on ne laisse pas une marge d'un mot ».
    ("agence-ia", "A"): (180, 270),
    ("agence-ia", "B"): (180, 270),
    # C et D, ajoutés le 2026-09-01. Mesurés sur les quatre variantes de chacun
    # (avec la note / repli sur les services, avec site / sans site) :
    #   C : 230 · 230 · 237 · 237     D : 228 · 228 · 235 · 235
    # Les mêmes bornes que A et B, et ce n'est pas de la paresse : les quatre
    # gabarits partagent la même contrainte de lecture — un courriel froid à un
    # contracteur qui le lit sur son téléphone entre deux jobs.
    #
    # ⚠️ Sans ces deux lignes, C et D n'auraient PAS été refusés : `check_length`
    # retombe sur les bornes de ("agence-ia", "A") pour un gabarit inconnu de la
    # même piste. Le repli aurait donc donné le bon résultat par accident. On
    # les écrit quand même — un repli qui tombe juste masque le jour où il
    # tombera faux, et la borne de C doit se lire ici, pas se déduire.
    ("agence-ia", "C"): (180, 270),
    ("agence-ia", "D"): (180, 270),
    # 🔧 Borne haute montée de 100 à 120 le 2026-08-30 (AC1b). Les 100 avaient
    # été posés sur une ESTIMATION (« ≈ 88 mots ») qui ne comptait pas de vrai
    # ouvreur — or l'ouvreur généré fait jusqu'à 45 mots à lui seul. Mesuré sur
    # les corps réels : 97 et 98 mots, et la variante sans site à 102, donc
    # DÉJÀ en échec. La spec interdit explicitement de laisser une marge d'un
    # mot ; 120 donne ~20 mots de jeu à chacune.
    # 🔧 Borne haute portée de 120 à 145 le 2026-09-01. Les deux relances ont été
    # réécrites par William et sont désormais des textes ENTIÈREMENT FIXES — ni
    # métier, ni ville, ni ouvreur généré. Leur longueur est donc déterministe :
    # 86 mots pour la relance 1, 125 pour la relance 2 avec ses deux chiffres.
    #
    # Laisser la borne à 120 aurait posé une remarque `length` sur CHAQUE envoi,
    # pour toujours, sans qu'aucune action ne soit possible — le texte est
    # décidé. Une remarque permanente n'informe de rien : elle apprend à ignorer
    # les remarques, et la prochaine, la vraie, passerait avec elle.
    #
    # 145 laisse 20 mots de jeu à la relance 2. La borne garde donc son seul rôle
    # utile ici : attraper un modèle qui réécrirait la relance au lieu de la
    # recopier.
    ("agence-ia", "RELANCE"): (40, 145),
}
_BORNES_DEFAUT = (60, 95)


def check_length(
    email_body: str,
    template: str | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
    track: str | None = None,
) -> CheckResult:
    piste = track or ""
    gabarit = (template or "").upper()

    # 🔴 Le repli ne traverse JAMAIS la frontière des pistes.
    #
    # Mesuré le 2026-08-30 : un gabarit inconnu sur `agence-ia` retombait sur
    # les bornes OPT (60-95 mots) et refusait un corps de 217 mots. Le cas
    # n'est pas théorique — il suffit que la conformité lise le PARAMÈTRE
    # `template_choice='AB'` au lieu de la variante écrite, ce que la tâche 18
    # allait poser dans n8n :
    #
    #     template=A   → passed=True   217 mots (cible 180-270)
    #     template=AB  → passed=False  217 mots (cible 60-95)
    #
    # 100 % des brouillons refusés en `needs_revision`, donc sortis du lot
    # pour toujours, contacts gelés à vie. Une piste dont on connaît les bornes
    # doit rester dans SES bornes, même quand le gabarit est méconnaissable :
    # on préfère un gabarit approximatif de la bonne piste à un gabarit exact
    # de la mauvaise.
    bornes = _BORNES_LONGUEUR.get((piste, gabarit))
    if bornes is None:
        bornes = _BORNES_LONGUEUR.get((piste, "A"), _BORNES_DEFAUT)
    defaut_min, defaut_max = bornes
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
        severity="info",
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
    r"h[ée]site[\s-]pas|"
    # 🔧 La durée doit être PROPOSÉE, pas seulement mentionnée. Corrigé le
    # 2026-09-02 sur trouvaille du conseil.
    #
    # `\b(15|20|25|30)\s*minutes?\b` seul verdissait la relance 2 sur
    # « comparé à celles qui répondent en 30 minutes » — une STATISTIQUE, pas
    # une invitation. Le check était donc vert pour la mauvaise raison : le
    # jour où cette phrase est reformulée, un test sans aucun rapport avec le
    # CTA tombe, et personne ne comprend pourquoi.
    #
    # Le préfixe exige maintenant une formule qui OFFRE la durée. « en 30
    # minutes » et « qui attend 30 minutes » ne matchent plus ; « on se parle
    # 20 minutes? » et « ça prend 15 minutes » matchent toujours.
    r"(?:pour|de|d'|prend|prendre|prendrait|dure|durerait|se parler?|jaser)"
    r"\s+(?:environ\s+|à peu près\s+)?(?:15|20|25|30)\s*minutes?\b|"
    # Ou une durée qui POSE une question — « 15 minutes ? » est la forme
    # historique du CTA de la piste OPT, et c'en est bien une.
    # La statistique de la relance 2 finit sur un point : « répondent en 30
    # minutes. Sans compter que… ». Le « ? » suffit donc à trancher.
    r"(?:15|20|25|30)\s*minutes?\s*\?"
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
        severity="info",
        message="CTA (invitation explicite) présent" if passed else "CTA faible ou absent",
        matches=[] if passed else [f"invitation_explicite={has_explicit_invite}"],
    )


def _warmup_disabled() -> bool:
    """Échappatoire EXPLICITE pour désactiver le gate (warmup terminé / aucun
    warmup requis). Doit être posée volontairement — jamais l'état par défaut."""
    return os.environ.get("WARMUP_DISABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Tout ce qui ressemble à une note ou à un compte d'avis dans le corps. Les
# motifs sont ANCRÉS sur leur mot (« étoiles », « avis ») : sans ça, « 60
# secondes » et « 24/7 », présents dans tous les corps, seraient pris pour des
# chiffres d'avis et bloqueraient 100 % des brouillons.
# ⚠️ Les SYNONYMES comptent. Ancré uniquement sur « étoiles » et « avis », le
# check laissait passer « 2,9 sur 504 évaluations Google » : le chiffre
# échappait DEUX fois — à la comparaison avec la colonne ET au plancher de
# qualité — et une entreprise notée 2,9 lisait sa propre mauvaise note.
_NOTE_RE = re.compile(r"(\d+(?:[,.]\d)?)\s*(?:étoiles?|etoiles?)", re.IGNORECASE)
_COMPTE_AVIS_RE = re.compile(
    r"(\d+)\s*(?:avis|évaluations?|evaluations?|commentaires?)\b",
    re.IGNORECASE,
)


def check_avis_conformes(
    email_body: str,
    google_rating: float | None = None,
    google_reviews_count: int | None = None,
    track: str | None = None,
) -> CheckResult:
    """Tout chiffre d'avis présent dans le corps doit correspondre à la colonne.

    🔴 **Un chiffre ne dépend jamais du jugement d'un LLM.** Sans ce check, le
    modèle peut écrire « 5 étoiles sur 47 avis » et le juge est aveugle PAR
    CONSTRUCTION : il ne voit pas la valeur de la colonne, donc rien ne lui
    permet de savoir que le chiffre est faux. C'est le bug déjà payé une fois
    (`0732d20`, le juge ne voyait pas la fiche contact).

    Trois décisions, toutes du côté de l'erreur récupérable :

    - **Un chiffre sans donnée en colonne BLOQUE.** C'est le cas « inventé » :
      le bloc 2 aurait dû sauter et le modèle a écrit quand même.
    - **Aucun chiffre PASSE.** C'est le repli du bloc 2, servi à 89 boîtes sur
      255 : la citation saute, il n'y a plus rien à vérifier.
    - **Un compte approximatif (« plus de 40 avis ») BLOQUE**, même s'il est
      vrai. Un faux refus est VISIBLE — le brouillon atterrit dans la file
      « à relire » du résumé quotidien — alors qu'un faux vert expédie un
      chiffre que personne n'a lu. Ouvrir une voie « approximative » serait
      exactement l'endroit où un faux vert irait se cacher.
    """
    from .avis import bloc_avis_autorise

    # ⚠️ Le plancher d'avis est une regle de la piste `agence-ia`. La piste OPT
    # n'a ni bloc 2, ni repli, ni colonnes d'avis cablees : lui appliquer le
    # plancher bloquerait tout corps OPT qui mentionnerait un chiffre, pour une
    # regle qui ne le concerne pas. Meme raison que `check_length` et
    # `check_registre`, qui prennent deja le track.
    plancher_applicable = track == "agence-ia"

    body = _body_without_signature(email_body)
    notes = _NOTE_RE.findall(body)
    comptes = _COMPTE_AVIS_RE.findall(body)

    if not notes and not comptes:
        return CheckResult(
            name="avis_conformes",
            passed=True,
            severity="block",
            message="aucun chiffre d'avis dans le corps (repli du bloc 2)",
            matches=[],
        )

    ecarts: list[str] = []

    # 🔴 L'AUTORISATION avant la conformite. Le check ne comparait le chiffre
    # qu'a la colonne : une note SOUS LE PLANCHER, recopiee fidelement par le
    # modele, passait au vert parce qu'elle etait VRAIE. Le plancher n'etait
    # donc garde que par l'obeissance du LLM -- exactement ce que ce fichier
    # dit refuser. Mesure sur A.M.G. Neige (2,3 sur 27 avis, donnee reelle) :
    # `check_avis_conformes(corps, 2.3, 27)` rendait passed=True.
    # 83 des 255 envoyables sont sous le plancher avec des valeurs non nulles.
    if plancher_applicable and not bloc_avis_autorise(google_rating, google_reviews_count):
        ecarts.append(
            f"citation interdite par le plancher de qualite "
            f"(note={google_rating}, avis={google_reviews_count}) : le corps "
            f"devait servir le repli, sans aucun chiffre"
        )

    attendue = round(google_rating, 1) if google_rating is not None else None
    for brute in notes:
        if attendue is None:
            ecarts.append(f"note « {brute} » annoncée alors qu'aucune note n'est en base")
            continue
        try:
            annoncee = round(float(brute.replace(",", ".")), 1)
        except ValueError:  # pragma: no cover - la regex garantit le format
            ecarts.append(f"note « {brute} » illisible")
            continue
        if annoncee != attendue:
            ecarts.append(f"note annoncée {annoncee} ≠ colonne {attendue}")

    for brut in comptes:
        if google_reviews_count is None:
            ecarts.append(f"« {brut} avis » annoncés alors qu'aucun compte n'est en base")
            continue
        if int(brut) != int(google_reviews_count):
            ecarts.append(f"compte annoncé {brut} ≠ colonne {google_reviews_count}")

    return CheckResult(
        name="avis_conformes",
        passed=not ecarts,
        severity="block",
        message=(
            f"{len(ecarts)} chiffre(s) d'avis non conforme(s)"
            if ecarts
            else "chiffres d'avis conformes à la colonne"
        ),
        matches=ecarts,
    )


def check_mise_en_scene(email_body: str) -> CheckResult:
    """La règle nº4 — « j'ai vu / j'ai lu / j'ai remarqué ».

    🔴 **Sévérité `info`, et c'est un choix, pas un oubli.**

    Ces formules ne sont pas des mensonges : « j'ai vu ton site » peut être
    parfaitement vrai. Elles sont interdites parce qu'elles *mettent la
    recherche en scène* au lieu de la prouver — le tell nº1 du courriel de
    masse. Le prospect, lui, n'a aucun moyen de savoir qu'une règle existe.

    Décision William du 2026-08-31 : seul ce que le prospect peut VÉRIFIER a le
    droit de tuer un brouillon. Une maladresse de forme s'écrit dans les notes
    et se compte au résumé du soir ; le courriel part.

    ⚠️ À ne pas confondre avec `check_first_person_actions`, qui reste
    bloquant : « j'ai testé ton formulaire » est un mensonge que le prospect
    peut démentir.
    """
    body = _body_without_signature(email_body)
    hits = _find_matches(body, MISE_EN_SCENE_PATTERNS)
    return CheckResult(
        name="mise_en_scene",
        passed=not hits,
        severity="info",
        message=(
            f"{len(hits)} formule(s) qui mettent la recherche en scène (règle nº4)"
            if hits
            else "la recherche n'est pas mise en scène"
        ),
        matches=[f"'{snip}' → {label}" for snip, label in hits],
    )


def check_site_au_conditionnel(email_body: str) -> CheckResult:
    """Le site proposé reste-t-il AU CONDITIONNEL ?

    « je pourrais t'en faire une version rafraîchie » est vrai.
    « j'en ai profité pour te le refaire » est faux, et le prospect le
    découvrira — c'est exactement `[[feedback-no-lying-in-outreach]]`.

    🔴 **DÉCLASSÉ de `block` à `info` le 2026-08-31 — décision William,
    réaffirmée après avertissement.**

    Son raisonnement : le prospect ne peut pas savoir que le site n'est pas
    déjà fait, donc ça ne tombe pas sous la règle « seul le vérifiable tue un
    brouillon » qu'il a tranchée le même jour.

    L'objection posée, une fois, et écartée : le prospect qui répond « envoie-le
    tout de suite » le découvre — et c'est le plus intéressé de tous. C'est sa
    décision, son entreprise, sa relation client.

    ⚠️ Le check RESTE ACTIF en `info`. Il n'a pas été supprimé, et c'est
    délibéré : la formulation continue de s'écrire dans `compliance_notes` et
    de se compter au résumé du soir. Le jour où quelqu'un rouvre la question,
    le compteur dit combien de courriels sont partis comme ça.
    """
    body = _body_without_signature(email_body)
    hits = _find_matches(body, SITE_DEJA_FAIT_PATTERNS)
    return CheckResult(
        name="site_au_conditionnel",
        passed=not hits,
        severity="info",
        message=(
            f"{len(hits)} formulation(s) qui disent le site DÉJÀ FAIT"
            if hits
            else "le site reste au conditionnel"
        ),
        matches=[f"'{snip}' → {label}" for snip, label in hits],
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
        severity="info",
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
        severity="info",
        message=f"{len(hits)} « pis » (max {_MAX_PIS})",
        matches=[] if passed else [f"{len(hits)} occurrences de « pis »"],
    )


# 🔴 Les chiffres de marché autorisés dans la relance 2 — décision William du
# 2026-09-01, prise en connaissance de cause.
#
# CE QUE CES CHIFFRES SONT RÉELLEMENT. Écrit ici parce que ce contrôle sera relu
# par quelqu'un qui n'aura pas assisté à la décision, et qu'il doit savoir ce
# qu'il garde. Recherche du 2026-09-01 : 13 agents, 56 chiffres distincts, 6
# passés à un réfutateur.
#
#   « 78 % des clients signent avec la première compagnie qui répond »
#     AUCUNE SOURCE PRIMAIRE. Attribué partout à un « sondage Lead Connect »
#     jamais publié. caseyresponse.com — un CONCURRENT direct qui vend la même
#     offre aux services résidentiels — l'attribue à la Lead Response Management
#     Study d'Oldroyd ; or le texte primaire de cette étude écrit, ligne 752 :
#     « This study did not address close ratios. » Le chiffre change aussi de
#     population selon qui le cite (« acheteurs », « propriétaires »,
#     « homeowners »), ce qui est le marqueur d'un chiffre sans origine.
#
#   « 21 fois plus de clients [retenus] en répondant en 5 minutes plutôt qu'en
#     30 » — LE CHIFFRE EXISTE, texte primaire retrouvé deux fois sur deux
#     serveurs indépendants : « The odds of QUALIFYING a lead if called in 5
#     minutes versus 30 minutes drop 21 times. » Il mesure la QUALIFICATION, pas
#     la rétention ni la vente. Six entreprises d'hypothèque et d'assurance où le
#     même lead est revendu à 4-7 acheteurs — une course qui n'existe pas pour un
#     déneigeur qui reçoit un formulaire sur son propre site. Données 2004-2007,
#     payées et copyrightées par InsideSales.com, dont le PDG est co-auteur.
#
# La position a été présentée à William avec ces éléments, il a tranché « on
# garde », c'est son entreprise et sa relation client. CE CONTRÔLE N'EST PAS LÀ
# POUR REDISCUTER ÇA. Il est là pour la seule chose qui reste défendable : que
# le chiffre écrit soit celui qui a été décidé, et pas une dérive du modèle.
#
# POURQUOI `block` ET PAS `info`. Un 21 devenu 210, ou un 78 devenu 87, est un
# mensonge que le prospect peut vérifier — la catégorie que William a
# explicitement gardée fatale le 2026-08-31. Le chiffre décidé engage William ;
# un chiffre halluciné n'engage personne et ne protège rien.
#
# ⚠️ CE CONTRÔLE NE VALIDE PAS LA PHRASE ENTIÈRE, seulement l'ancre + le nombre.
# Exiger la prose au mot près bloquerait des variantes légitimes de rédaction et
# ferait mourir des brouillons pour une virgule. Ce qui est gardé, c'est le
# chiffre attaché à ce qu'il prétend mesurer.
STATISTIQUES_APPROUVEES: dict[str, tuple[str, str, str]] = {
    "multiplicateur_clients": (
        r"(\d+)\s*fois plus de clients",
        "21",
        "le multiplicateur de la réponse en 5 minutes",
    ),
    "delai_court": (
        r"en moins de\s*(\d+)\s*minutes",
        "5",
        "le délai court de la comparaison",
    ),
    # 🔧 Ancre changée le 2026-09-01 avec la copie. Était `attend (\d+) minutes`
    # (« comparé à un lead qui attend 30 minutes ») ; William a réécrit en
    # « comparé à celles qui répondent en 30 minutes » — la comparaison portait
    # sur un LEAD alors que le sujet de la phrase est les ENTREPRISES.
    #
    # ⚠️ Sans ce changement, l'ancre ne matchait plus rien et la garde du 30
    # serait MORTE EN SILENCE. C'est le défaut propre aux gardes par ancre : un
    # motif qui ne trouve rien est indistinguable d'un corps sans statistique,
    # puisque « absent = conforme ». D'où
    # `test_aucune_ancre_ne_doit_etre_morte`, qui vérifie que les quatre
    # s'allument bien sur le texte canonique — c'est LUI qui rattrape la
    # prochaine réécriture, pas la relecture.
    #
    # `répondent en (\d+)` ne peut pas attraper le délai court : « répondent en
    # MOINS DE 5 minutes » intercale deux mots avant le chiffre.
    "delai_long": (
        r"répondent en\s*(\d+)\s*minutes",
        "30",
        "le délai long de la comparaison",
    ),
    "part_premier_repondant": (
        r"(\d+)\s*%\s*des clients",
        "78",
        "la part des clients qui signent avec le premier répondant",
    ),
}


def check_statistiques_conformes(email_body: str) -> CheckResult:
    """Le chiffre écrit est-il celui qui a été décidé ?

    Absent = conforme : tous les corps ne portent pas de statistique. Présent
    avec la mauvaise valeur = `block`.

    Le contrôle porte sur le CORPS ENTIER, signature comprise — contrairement à
    la plupart des autres. Une statistique glissée sous la ligne de séparation
    partirait quand même chez le prospect.
    """
    ecarts: list[str] = []
    texte = email_body.replace("’", "'").replace("ʼ", "'")

    for nom, (motif, attendu, quoi) in STATISTIQUES_APPROUVEES.items():
        for m in re.finditer(motif, texte, flags=re.IGNORECASE):
            trouve = m.group(1)
            if trouve != attendu:
                ecarts.append(
                    f"'{m.group(0).strip()}' → {quoi} devrait être {attendu}, "
                    f"le corps dit {trouve} ({nom})"
                )

    return CheckResult(
        name="statistiques_conformes",
        passed=not ecarts,
        severity="block",
        message=(
            f"{len(ecarts)} chiffre(s) de marché dérivé(s) de la valeur décidée"
            if ecarts
            else "aucun chiffre de marché dérivé"
        ),
        matches=ecarts,
    )


def run_all(
    email_body: str,
    social_proof_count: int,
    available_slots: list[dict] | None = None,
    template: str | None = None,
    email_subject: str | None = None,
    appended_footer: str = "",
    track: str | None = None,
    google_rating: float | None = None,
    google_reviews_count: int | None = None,
) -> list[CheckResult]:
    return [
        check_warmup_window(),
        check_avis_conformes(email_body, google_rating, google_reviews_count, track),
        check_statistiques_conformes(email_body),
        check_site_au_conditionnel(email_body),
        check_mise_en_scene(email_body),
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
