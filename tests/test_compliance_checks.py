"""Tests for the deterministic compliance checks in lib/compliance_checks.py.

Focus on the LCAP / Loi 25 / anti-mensonge invariants that ABSOLUTELY must
not regress, in priority order :

1. legal_footer       — LCAP: nom légal + adresse + unsubscribe obligatoires
2. first_person_actions — anti-mensonge: "j'ai testé/appelé/visité" bloqué
3. fake_social_proof  — anti-preuve-sociale-inventée quand social_proof_count=0
4. cta_slots_real     — anti-créneau-inventé (doit matcher Cal.com)
5. registre           — cohérence tu/vous dérivée du track, pas un registre imposé
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
from src.lib.compliance_checks import check_length, check_tics_de_langage, run_all
from tests.fixtures.corps_ac1 import (
    CORPS_A,
    CORPS_A_SANS_CTA,
    CORPS_A_SANS_RENVOI,
    CORPS_B,
)


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


# ---------------- check_cta_present (forme réelle du prompt) ----------------

def test_cta_present_passes_on_generic_call_invite() -> None:
    """Le prompt génère 'un appel rapide ?' (fallback sans créneaux) — doit passer."""
    body = "Bonjour, votre clinique m'intéresse.\n\nUn appel rapide cette semaine ?\n\n—\nWilliam"
    r = cc.check_cta_present(body)
    assert r.passed


def test_cta_present_passes_on_slotted_call_invite() -> None:
    """CTA avec jour/heure Cal.com + 'un appel rapide ?' — doit passer."""
    body = "Mercredi 13 mai à 18h ou jeudi 14 mai à 18h30, un appel rapide ?\n\n—\nWilliam"
    r = cc.check_cta_present(body)
    assert r.passed


def test_cta_present_still_passes_on_explicit_minutes() -> None:
    """Rétrocompat: l'ancienne forme 'X minutes ?' reste acceptée."""
    r = cc.check_cta_present("Bonjour, 15 minutes ?\n\n—\nWilliam")
    assert r.passed


def test_cta_present_fails_when_no_invite_and_no_question() -> None:
    """Pas d'invitation à un appel ni de question → CTA faible."""
    r = cc.check_cta_present("Bonjour, voici une idée pour votre clinique.\n\n—\nWilliam")
    assert not r.passed


# ---------------- check_cta_present — faux vert à deux moitiés (CORPS_A) ----------------
#
# Mesuré : le check passait sur CORPS_A pour deux mauvaises raisons — le bloc
# service ("Un appel que tu peux pas prendre, un texto...") satisfaisait
# has_call_invite, et la ligne de renvoi ("...tu peux-tu me pointer la bonne
# personne?") satisfaisait has_question. Le vrai CTA ("Dis-moi juste si tu
# veux le voir.") ne portait ni l'un ni l'autre et n'était jamais regardé.

def test_cta_passe_sur_le_corps_complet() -> None:
    assert cc.check_cta_present(CORPS_A).passed


def test_cta_passe_meme_sans_la_ligne_de_renvoi() -> None:
    """Le CTA seul doit suffire — sinon le check verdit sur la mauvaise phrase."""
    assert cc.check_cta_present(CORPS_A_SANS_RENVOI).passed


def test_cta_echoue_quand_on_retire_le_vrai_CTA() -> None:
    """La ligne de renvoi porte un « ? » : si le check passe encore, il est faux vert."""
    assert not cc.check_cta_present(CORPS_A_SANS_CTA).passed


def test_cta_retrocompat_opt_avec_question() -> None:
    corps = "Bonjour,\n\nUn appel rapide pour en parler?\n\n---\nWilliam"
    assert cc.check_cta_present(corps).passed


def test_cta_echoue_sur_une_question_rhetorique_sans_demande() -> None:
    """Un « ? » dans l'ouvreur généré ne prouve pas qu'on demande quelque chose."""
    corps = (
        "Bonjour,\n\n"
        "Est-ce que ça t'arrive souvent de manquer des appels le soir?\n\n"
        "Si c'est pas toi qui gères ça, tu peux-tu me pointer la bonne personne?\n"
        "\n---\nWilliam"
    )
    assert not cc.check_cta_present(corps).passed


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


# ---------------- 5. registre (cohérence tu/vous, pas un registre imposé) ----------------

def test_registre_agence_ia_accepte_le_tutoiement_coherent() -> None:
    r = cc.check_registre(CORPS_A, track="agence-ia")
    assert r.passed, r.message


def test_registre_agence_ia_accepte_le_template_b() -> None:
    assert cc.check_registre(CORPS_B, track="agence-ia").passed


def test_registre_opt_exige_le_vouvoiement() -> None:
    corps = "Bonjour,\n\nVous avez sûrement remarqué que votre site.\n\n---\nWilliam"
    assert cc.check_registre(corps, track="OPT").passed


def test_registre_bloque_le_melange() -> None:
    corps = "Bonjour,\n\nTon site est beau mais vous pouvez faire mieux, votre équipe.\n\n---\nWilliam"
    r = cc.check_registre(corps, track="agence-ia")
    assert not r.passed
    assert r.severity == "block"


def test_registre_bloque_le_vouvoiement_sur_agence_ia() -> None:
    corps = "Bonjour,\n\nVous avez sûrement remarqué que votre site.\n\n---\nWilliam"
    assert not cc.check_registre(corps, track="agence-ia").passed


def test_registre_sans_track_retombe_sur_vous() -> None:
    corps = "Bonjour,\n\nVous avez sûrement remarqué que votre site.\n\n---\nWilliam"
    assert cc.check_registre(corps, track=None).passed


def test_registre_ne_bloque_pas_sur_rendez_vous() -> None:
    """« rendez-vous » contient le token « vous » — il ne doit pas compter."""
    corps = "Bonjour,\n\nTon client veut un rendez-vous pis tu peux pas répondre.\n\n---\nWilliam"
    assert cc.check_registre(corps, track="agence-ia").passed


def test_registre_compte_toujours_un_vrai_vous_apres_rendez_vous() -> None:
    corps = "Bonjour,\n\nTon rendez-vous, vous pouvez le déplacer, votre équipe.\n\n---\nWilliam"
    assert not cc.check_registre(corps, track="agence-ia").passed


def test_registre_reconnait_te_toi_et_ten() -> None:
    corps = "Bonjour,\n\nÇa te permet d'en faire plus. Toi, t'en profites.\n\n---\nWilliam"
    assert cc.check_registre(corps, track="agence-ia").passed


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


def test_warmup_window_blocks_when_env_var_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAIL-CLOSED : pas de WARMUP_END_DATE (ni WARMUP_DISABLED) = on BLOQUE.
    Une barrière de sécurité ne doit jamais s'ouvrir par oubli d'une env var."""
    monkeypatch.delenv("WARMUP_END_DATE", raising=False)
    monkeypatch.delenv("WARMUP_DISABLED", raising=False)
    r = cc.check_warmup_window()
    assert not r.passed
    assert r.severity == "block"


def test_warmup_window_blocks_when_env_var_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAIL-CLOSED : date malformée = config douteuse = on BLOQUE."""
    monkeypatch.delenv("WARMUP_DISABLED", raising=False)
    monkeypatch.setenv("WARMUP_END_DATE", "pas-une-date")
    r = cc.check_warmup_window()
    assert not r.passed
    assert r.severity == "block"


def test_warmup_window_passes_when_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Échappatoire explicite : WARMUP_DISABLED=true autorise l'envoi même sans
    WARMUP_END_DATE (cas warmup terminé / aucun warmup requis)."""
    monkeypatch.delenv("WARMUP_END_DATE", raising=False)
    monkeypatch.setenv("WARMUP_DISABLED", "true")
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


# ---------------- 8. check_length (bornes par (track, gabarit), mesurées 2026-08-30) ----------------

def test_length_accepte_le_corps_a_du_pivot():
    assert check_length(CORPS_A, template="A", track="agence-ia").passed


def test_length_accepte_le_corps_b_du_pivot():
    assert check_length(CORPS_B, template="B", track="agence-ia").passed


def test_length_accepte_une_relance():
    corps = "Bonjour,\n\n" + " ".join(["mot"] * 70) + "\n\n---\nWilliam"
    assert check_length(corps, template="RELANCE", track="agence-ia").passed


def test_length_refuse_une_relance_trop_longue():
    corps = "Bonjour,\n\n" + " ".join(["mot"] * 150) + "\n\n---\nWilliam"
    assert not check_length(corps, template="RELANCE", track="agence-ia").passed


def test_length_refuse_un_corps_de_tri_trop_court():
    corps = "Bonjour,\n\n" + " ".join(["mot"] * 100) + "\n\n---\nWilliam"
    assert not check_length(corps, template="A", track="agence-ia").passed


def test_length_opt_garde_ses_anciennes_bornes():
    """Le prompt OPT vise 60-90 mots avec des gabarits nommes A et B, comme le
    pivot. Indexer sur le seul gabarit ferait echouer 100 % des brouillons OPT."""
    corps = "Bonjour,\n\n" + " ".join(["mot"] * 75) + "\n\n---\nWilliam"
    assert check_length(corps, template="A", track="OPT").passed


def test_length_opt_refuse_un_corps_du_pivot():
    """Symetrique : 206 mots est hors bornes pour OPT."""
    assert not check_length(CORPS_A, template="A", track="OPT").passed


def test_length_track_inconnu_retombe_sur_les_bornes_historiques():
    corps = "Bonjour,\n\n" + " ".join(["mot"] * 75) + "\n\n---\nWilliam"
    assert check_length(corps, template="A", track=None).passed


# ---------------- 9. check_tics_de_langage (budget du « pis ») ----------------

def test_tics_accepte_les_corps_du_pivot():
    assert check_tics_de_langage(CORPS_A).passed
    assert check_tics_de_langage(CORPS_B).passed


def test_tics_bloque_au_dela_de_quatre_pis():
    corps = "Bonjour,\n\npis pis pis pis pis\n\n---\nWilliam"
    r = check_tics_de_langage(corps)
    assert not r.passed
    assert r.severity == "block"


def test_tics_accepte_exactement_quatre():
    corps = "Bonjour,\n\npis pis pis pis\n\n---\nWilliam"
    assert check_tics_de_langage(corps).passed


# ---------------- run_all integration ----------------

def test_run_all_returns_13_checks() -> None:
    """run_all doit toujours retourner tous les checks (pour audit), même
    quand certains sont 'passed=True ignoré'.

    MAJ 2026-08-30 : 14 checks depuis l'ajout de `check_tics_de_langage`
    (tâche AC1a, garde-fou sur le paragraphe généré)."""
    results = cc.run_all(
        email_body="Bonjour,\nVotre clinique m'intéresse. 15 minutes ?\n\n—\nWilliam",
        social_proof_count=0,
        available_slots=None,
        template="A",
        email_subject="Question rapide",
    )
    # 14 checks expected: warmup + 6 body + 3 subject + length + cta_present
    # + cta_slots_real + registre + tics_de_langage
    assert len(results) == 14, f"attendu 14 checks, eu {len(results)}"
    names = [r.name for r in results]
    # Sanity: pas de doublon
    assert len(set(names)) == 14


def test_run_all_retourne_14_checks():
    assert len(run_all(CORPS_A, 0, template="A", track="agence-ia")) == 14


def test_run_all_ne_bloque_pas_le_corps_du_pivot():
    resultats = run_all(CORPS_A, 0, template="A", track="agence-ia")
    bloquants = [r.name for r in resultats if not r.passed and r.severity == "block"]
    assert bloquants == ["warmup_window", "legal_footer"], bloquants


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
