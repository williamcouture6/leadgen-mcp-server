"""Tests for the deterministic compliance checks in lib/compliance_checks.py.

Focus on the LCAP / Loi 25 / anti-mensonge invariants that ABSOLUTELY must
not regress, in priority order :

1. legal_footer       — LCAP: nom légal + adresse + unsubscribe obligatoires
2. first_person_actions — anti-mensonge: "j'ai testé/appelé/visité" bloqué
3. fake_social_proof  — anti-preuve-sociale-inventée quand social_proof_count=0
4. cta_slots_real     — anti-créneau-inventé (doit matcher Cal.com)
5. vouvoiement        — culture business QC: vous/votre ≥2, jamais tutoiement
6. warmup_window      — gate délivrabilité avant fin warmup Instantly
7. banned_words       — détection vocabulaire IA-generated / sales-y

Chaque check est testé par paire (cas légit qui passe / violation qui block)
pour pin le contrat exact. Quand le test casse, ça veut dire qu'une regex
a été changée et le comportement compliance a bougé — INTENTIONNEL ou
RÉGRESSION à valider explicitement.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.lib import compliance_checks as cc


# ---------------- 1. legal_footer (LCAP) ----------------

def test_legal_footer_passes_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "William Couture Pilote")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse, Lévis QC G6K 1C9")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    body = (
        "Bonjour,\n\nVotre clinique m'intéresse. 15 minutes ?\n\n"
        "—\nWilliam\n\n"
        "William Couture Pilote — 193 rue de l'Anse, Lévis QC G6K 1C9 · "
        "https://couture-ia.com/unsubscribe"
    )
    r = cc.check_legal_footer(body)
    assert r.passed, f"footer LCAP devrait passer: {r.matches}"


def test_legal_footer_blocks_missing_company_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "William Couture Pilote")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://x.com/u")
    # Body ne mentionne PAS "William Couture Pilote"
    body = "Hello\n\n193 rue de l'Anse · https://x.com/u"
    r = cc.check_legal_footer(body)
    assert not r.passed
    assert any("company_name" in m for m in r.matches)


def test_legal_footer_blocks_missing_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "999 rue Fictive, Lévis")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://x.com/u")
    body = "Couture · https://x.com/u"  # adresse absente
    r = cc.check_legal_footer(body)
    assert not r.passed
    assert any("adresse" in m.lower() for m in r.matches)


def test_legal_footer_blocks_missing_unsubscribe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    body = "Couture · 193 rue de l'Anse"  # ni URL ni mention STOP
    r = cc.check_legal_footer(body)
    assert not r.passed
    assert any("unsubscribe" in m.lower() for m in r.matches)


def test_legal_footer_accepts_appended_footer_from_esp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quand l'ESP (Instantly) injecte le footer LCAP au moment de l'envoi,
    le body généré par WF-4 NE le contient pas — mais le check doit passer
    si l'appended_footer fourni couvre les requis."""
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture IA Inc")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse, Lévis")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    body = "Bonjour, intéressé par 15 minutes ?\n\n—\nWilliam"  # rien de LCAP
    footer = (
        "Couture IA Inc — 193 rue de l'Anse, Lévis · "
        "https://couture-ia.com/unsubscribe"
    )
    r = cc.check_legal_footer(body, appended_footer=footer)
    assert r.passed, f"avec appended_footer ça doit passer: {r.matches}"


def test_legal_footer_stop_mention_acceptable_substitute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si pas l'URL exacte mais mention 'STOP' présente → OK (pattern LCAP standard)."""
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "Couture")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://example.com/u")
    body = "Couture · 193 rue de l'Anse · Répondez STOP pour vous désinscrire"
    r = cc.check_legal_footer(body)
    assert r.passed


# ---------------- 2. first_person_actions (anti-mensonge) ----------------

@pytest.mark.parametrize("phrase", [
    "Hier soir, j'ai testé votre formulaire",
    "Ce matin, j'ai appelé chez vous",
    "J'ai visité votre site",
    "On s'est croisés au salon",
    "J'ai téléphoné à votre clinique",
    "J'ai rempli le formulaire de contact",
])
def test_first_person_actions_blocks_unverifiable_claim(phrase: str) -> None:
    r = cc.check_first_person_actions(f"Bonjour,\n\n{phrase}. Discutons.")
    assert not r.passed, f"devait bloquer: {phrase!r}"
    assert r.severity == "block"


def test_first_person_actions_passes_when_no_claim() -> None:
    body = "Bonjour,\n\nVotre clinique m'a marqué. 15 minutes ?\n\n—\nWilliam"
    r = cc.check_first_person_actions(body)
    assert r.passed


def test_first_person_actions_passes_when_phrase_in_signature_only() -> None:
    """Les claims dans la signature (après —\\n) ne doivent PAS être détectés
    (la signature est strip avant le check)."""
    body = (
        "Bonjour, intéressé ?\n\n—\nWilliam\n"
        "(J'ai déjà aidé d'autres cliniques — exemple en signature)"
    )
    r = cc.check_first_person_actions(body)
    assert r.passed


# ---------------- 3. fake_social_proof ----------------

@pytest.mark.parametrize("phrase", [
    "Mes clients ont vu une hausse de RDV",
    "Nos clients en physio",
    "Deux cliniques à Montréal utilisent déjà",
    "J'ai mis en place un système pour une clinique",
])
def test_fake_social_proof_blocks_when_no_real_references(phrase: str) -> None:
    """social_proof_count=0 (cas Couture IA actuel) → claim qui suggère
    existence de clients passés = mensonge bloqué."""
    r = cc.check_fake_social_proof(f"Bonjour,\n{phrase}.", social_proof_count=0)
    assert not r.passed, f"devait bloquer (0 client refs): {phrase!r}"


def test_fake_social_proof_skipped_when_real_references_exist() -> None:
    """Si Couture IA a >=1 référence client, les claims sont autorisés.
    Le check est juste ignoré (pas notre rôle de juger la véracité ici)."""
    body = "Mes clients ont vu une hausse"
    r = cc.check_fake_social_proof(body, social_proof_count=1)
    assert r.passed
    assert "ignoré" in r.message


def test_fake_social_proof_neutral_phrase_passes() -> None:
    body = "Bonjour,\nVotre clinique m'intéresse. 15 minutes pour en discuter ?"
    r = cc.check_fake_social_proof(body, social_proof_count=0)
    assert r.passed


# ---------------- 4. cta_slots_real (anti-créneau-inventé) ----------------

def test_cta_slots_real_skipped_when_no_slots_provided() -> None:
    """Pas de liste Cal.com → CTA générique attendu, check ignoré."""
    r = cc.check_cta_slots_real("Mardi 15h ?", available_slots=None)
    assert r.passed


def test_cta_slots_real_passes_when_no_specific_slot_in_email() -> None:
    """CTA générique ('15 minutes cette semaine ?') → pas de créneau précis,
    rien à valider, OK."""
    slots = [{"day_fr": "mardi", "date_fr": "27 mai", "times": ["14h", "15h"]}]
    r = cc.check_cta_slots_real("15 minutes cette semaine ?", available_slots=slots)
    assert r.passed


def test_cta_slots_real_passes_when_email_slot_matches_calcom() -> None:
    slots = [
        {"day_fr": "mardi", "date_fr": "27 mai",
         "times": ["14h", "15h", "16h30"]},
    ]
    body = "Mardi 27 mai à 14h ou mardi 27 mai à 16h30, 15 minutes ?"
    r = cc.check_cta_slots_real(body, available_slots=slots)
    assert r.passed, f"créneaux légit devraient passer: {r.matches}"


def test_cta_slots_real_blocks_when_email_invents_slot() -> None:
    """L'email mentionne un créneau absent de Cal.com = mensonge bloqué."""
    slots = [{"day_fr": "mardi", "date_fr": "27 mai", "times": ["14h", "15h"]}]
    body = "Mardi 27 mai à 9h, 15 minutes ?"  # 9h pas dans Cal.com
    r = cc.check_cta_slots_real(body, available_slots=slots)
    assert not r.passed
    assert r.severity == "block"


def test_cta_slots_real_blocks_wrong_day() -> None:
    slots = [{"day_fr": "mardi", "date_fr": "27 mai", "times": ["14h"]}]
    body = "Mercredi 28 mai à 14h ?"  # mercredi pas dans Cal.com
    r = cc.check_cta_slots_real(body, available_slots=slots)
    assert not r.passed


# ---------------- 5. vouvoiement (culture business QC) ----------------

def test_vouvoiement_passes_proper_form() -> None:
    body = "Bonjour, votre clinique m'intéresse. Avez-vous 15 minutes ?"
    r = cc.check_vouvoiement(body)
    assert r.passed


def test_vouvoiement_blocks_tutoiement() -> None:
    body = "Salut, ta clinique m'intéresse. T'as 15 minutes ?"
    r = cc.check_vouvoiement(body)
    assert not r.passed
    assert r.severity == "block"


def test_vouvoiement_blocks_insufficient_vous() -> None:
    """Moins de 2 occurrences vous/votre → ton trop neutre, pas business."""
    body = "Bonjour, intéressant. 15 minutes ?"  # 0 vous/votre
    r = cc.check_vouvoiement(body)
    assert not r.passed


def test_vouvoiement_blocks_mixed_with_tu() -> None:
    """Même si 'vous' présent, un seul 'tu' brise le registre."""
    body = "Bonjour, votre site est super. Tu as 15 minutes ? Votre équipe."
    r = cc.check_vouvoiement(body)
    assert not r.passed


# ---------------- 6. warmup_window (gate délivrabilité) ----------------

def test_warmup_window_blocks_before_end_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARMUP_END_DATE", "2099-12-31")  # toujours futur
    r = cc.check_warmup_window()
    assert not r.passed
    assert r.severity == "block"
    assert "INTERDIT" in r.message


def test_warmup_window_passes_after_end_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARMUP_END_DATE", "2020-01-01")  # déjà passé
    r = cc.check_warmup_window()
    assert r.passed


def test_warmup_window_passes_when_env_var_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pas de WARMUP_END_DATE = gate désactivé (pas de blocage par défaut)."""
    monkeypatch.delenv("WARMUP_END_DATE", raising=False)
    r = cc.check_warmup_window()
    assert r.passed


def test_warmup_window_passes_when_env_var_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Date malformée ne doit PAS bloquer la prod (fail-open intentionnel)."""
    monkeypatch.setenv("WARMUP_END_DATE", "pas-une-date")
    r = cc.check_warmup_window()
    assert r.passed


def test_warmup_window_exact_boundary_today_equals_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """today == end_date → autorisé (>= dans le code, pas >)."""
    today = date(2026, 5, 27)
    monkeypatch.setenv("WARMUP_END_DATE", "2026-05-27")
    r = cc.check_warmup_window(today=today)
    assert r.passed


# ---------------- 7. banned_words ----------------

@pytest.mark.parametrize("word", [
    "intelligence artificielle",
    "automatisation",
    "automatiser",
    "solution",
    "synergie",
    "stratégique",
    "innovation",
    "leviers",
])
def test_banned_words_blocks_corporate_jargon(word: str) -> None:
    body = f"Bonjour,\nNotre approche {word} pour votre clinique."
    r = cc.check_banned_words(body)
    assert not r.passed, f"devait bloquer: {word!r}"


def test_banned_words_passes_clean_copy() -> None:
    body = (
        "Bonjour,\n\nVotre clinique m'intéresse. Auriez-vous 15 minutes "
        "pour un café ?\n\n—\nWilliam"
    )
    r = cc.check_banned_words(body)
    assert r.passed


def test_banned_words_isolated_ia_blocked_but_couture_ia_allowed() -> None:
    """'IA' isolé (jargon) doit être bloqué, sauf dans 'Couture IA' (le nom)."""
    body_bad = "Notre IA va vous aider"
    r_bad = cc.check_banned_words(body_bad)
    assert not r_bad.passed

    body_ok = "Bonjour de la part de Couture IA, votre clinique m'intéresse."
    r_ok = cc.check_banned_words(body_ok)
    assert r_ok.passed, f"'Couture IA' doit passer: {r_ok.matches}"


# ---------------- run_all integration ----------------

def test_run_all_returns_13_checks() -> None:
    """run_all doit toujours retourner tous les checks (pour audit), même
    quand certains sont 'passed=True ignoré'."""
    results = cc.run_all(
        email_body="Bonjour,\nVotre clinique m'intéresse. 15 minutes ?\n\n—\nWilliam",
        social_proof_count=0,
        available_slots=None,
        template="A",
        email_subject="Question rapide",
    )
    # 13 checks expected: warmup + 6 body + 3 subject + length + cta_present
    # + cta_slots_real + vouvoiement
    assert len(results) == 13, f"attendu 13 checks, eu {len(results)}"
    names = [r.name for r in results]
    # Sanity: pas de doublon
    assert len(set(names)) == 13


def test_run_all_clean_legit_email_no_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Email propre + env vars LCAP set → 0 blocker."""
    monkeypatch.setenv("LEGAL_COMPANY_NAME", "William Couture Pilote")
    monkeypatch.setenv("LEGAL_COMPANY_ADDRESS", "193 rue de l'Anse, Lévis")
    monkeypatch.setenv("UNSUBSCRIBE_URL", "https://couture-ia.com/unsubscribe")
    monkeypatch.setenv("WARMUP_END_DATE", "2020-01-01")  # passé

    body = (
        "Bonjour,\n\n"
        "Votre clinique de physiothérapie à Montréal m'a marqué — vos avis "
        "Google soulignent l'écoute de votre équipe. Une question : comment "
        "gérez-vous les demandes de RDV reçues le soir ? Auriez-vous "
        "15 minutes mardi 27 mai à 14h pour en discuter ?\n\n"
        "—\nWilliam Couture\n"
        "William Couture Pilote — 193 rue de l'Anse, Lévis · "
        "https://couture-ia.com/unsubscribe"
    )
    results = cc.run_all(
        email_body=body,
        social_proof_count=0,
        available_slots=[{
            "day_fr": "mardi", "date_fr": "27 mai", "times": ["14h", "15h"]
        }],
        template="A",
        email_subject="Question gestion RDV",
    )
    blockers = [r for r in results if not r.passed and r.severity == "block"]
    assert not blockers, f"email propre devrait avoir 0 blockers: {[(b.name, b.message) for b in blockers]}"
