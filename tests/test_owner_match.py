"""Tests de la logique pure de confiance sur le décideur (scraping WF-3).

Aucune DB ni réseau : on teste `classify_scraped_contact` et ses helpers,
qui décident du `owner_confidence` d'un email scrapé à partir des
`decideur_candidats` extraits par le Research Agent.
"""
from __future__ import annotations

from src.lib.owner_match import (
    classify_scraped_contact,
    email_matches_name,
    summarize_company_decideur,
)


def _email(local, kind):
    return {"email": f"{local}@plomberie.com", "local": local, "kind": kind}


def test_email_matches_name_variants():
    assert email_matches_name("jean.tremblay", "Jean Tremblay")
    assert email_matches_name("tremblay.jean", "Jean Tremblay")   # ordre libre
    assert email_matches_name("jeantremblay", "Jean Tremblay")    # collé
    assert email_matches_name("j.tremblay", "Jean Tremblay")      # initiale + nom
    assert email_matches_name("jtremblay", "Jean Tremblay")
    assert email_matches_name("jean.tremblay", "Jean Tremblay")   # idempotent
    assert email_matches_name("jean.tremblay", "Jean Trembläy")   # accents ignorés


def test_email_does_not_match_other_name():
    assert not email_matches_name("info", "Jean Tremblay")
    assert not email_matches_name("marie.gagnon", "Jean Tremblay")
    assert not email_matches_name("jean", "Jean Tremblay")        # prénom seul insuffisant


def test_confirmed_via_nominative_match():
    decideurs = [{"nom_complet": "Jean Tremblay", "titre": "Propriétaire",
                  "source_url": "https://plomberie.com/a-propos", "confidence": "medium"}]
    d = classify_scraped_contact(_email("jean.tremblay", "nominative"), decideurs)
    assert d.owner_confidence == "confirmed"
    assert d.first_name == "Jean"
    assert d.last_name == "Tremblay"
    assert d.title == "Propriétaire"
    assert d.potential_owner is None


def test_confirmed_via_high_confidence_decideur_even_generic_email():
    # Cas (b) : on est sûr du nom, email générique -> confirmed quand même.
    decideurs = [{"nom_complet": "Marie Gagnon", "titre": "Présidente",
                  "source_url": "https://x.com", "confidence": "high"}]
    d = classify_scraped_contact(_email("info", "generic"), decideurs)
    assert d.owner_confidence == "confirmed"
    assert d.first_name == "Marie"
    assert d.last_name == "Gagnon"


def test_two_high_confidence_decideurs_is_not_confirmed():
    # Ambiguïté : 2 candidats 'high' -> on ne sait pas à qui s'adresser -> potential.
    decideurs = [
        {"nom_complet": "Marie Gagnon", "titre": "Co-propriétaire", "confidence": "high"},
        {"nom_complet": "Luc Roy", "titre": "Co-propriétaire", "confidence": "high"},
    ]
    d = classify_scraped_contact(_email("info", "generic"), decideurs)
    assert d.owner_confidence == "potential"
    assert d.first_name is None
    assert d.potential_owner is not None


def test_potential_from_low_confidence_decideur():
    decideurs = [{"nom_complet": "Paul Côté", "titre": "Mentionné en avis",
                  "source_url": "google_review", "confidence": "low"}]
    d = classify_scraped_contact(_email("info", "generic"), decideurs)
    assert d.owner_confidence == "potential"
    assert d.first_name is None
    assert d.potential_owner == {
        "nom_complet": "Paul Côté", "titre": "Mentionné en avis", "source_url": "google_review",
    }


def test_potential_from_unmatched_nominative_local():
    # Email nominatif non corroboré par un décideur -> nom dérivé du local, potential.
    d = classify_scraped_contact(_email("sophie.lavoie", "nominative"), [])
    assert d.owner_confidence == "potential"
    assert d.first_name is None
    assert d.potential_owner["nom_complet"] == "Sophie Lavoie"
    assert d.potential_owner["titre"] is None
    assert d.potential_owner["source_url"] is None


def test_unknown_generic_no_decideur():
    d = classify_scraped_contact(_email("contact", "generic"), [])
    assert d.owner_confidence == "unknown"
    assert d.first_name is None
    assert d.last_name is None
    assert d.potential_owner is None


def test_decision_maps_to_contactin_fields():
    # Garde-fou : la décision se mappe proprement sur les champs ContactIn.
    decideurs = [{"nom_complet": "Jean Tremblay", "titre": "Propriétaire", "confidence": "high"}]
    d = classify_scraped_contact(_email("jean.tremblay", "nominative"), decideurs)
    assert (d.first_name, d.last_name, d.title) == ("Jean", "Tremblay", "Propriétaire")
    assert (d.owner_confidence == "confirmed") is True   # -> is_decision_maker True


# ----------------------------------------------- Résumé décideur (companies)

def test_summary_confirme_via_nominative_match():
    decideurs = [{"nom_complet": "Jean Tremblay", "titre": "Propriétaire",
                  "source_url": "https://x.com/a-propos", "confidence": "medium"}]
    emails = [_email("jean.tremblay", "nominative")]
    confirme, potentiel = summarize_company_decideur(decideurs, emails)
    assert confirme == {"nom_complet": "Jean Tremblay", "titre": "Propriétaire",
                        "source_url": "https://x.com/a-propos"}
    assert potentiel is None


def test_summary_confirme_via_single_high():
    decideurs = [{"nom_complet": "Marie Gagnon", "titre": "Présidente",
                  "source_url": "https://x.com", "confidence": "high"}]
    confirme, potentiel = summarize_company_decideur(decideurs, [_email("info", "generic")])
    assert confirme["nom_complet"] == "Marie Gagnon"
    assert "confidence" not in confirme        # confirmé = pas de niveau, c'est un fait
    assert potentiel is None


def test_summary_potentiel_best_candidate_with_confidence():
    # Pas de match nominatif, pas de high unique -> meilleur candidat en potentiel.
    decideurs = [
        {"nom_complet": "Paul Côté", "titre": "Gérant", "source_url": "g", "confidence": "low"},
        {"nom_complet": "Luc Roy", "titre": "Directeur", "source_url": "s", "confidence": "medium"},
    ]
    confirme, potentiel = summarize_company_decideur(decideurs, [_email("info", "generic")])
    assert confirme is None
    assert potentiel == {"nom_complet": "Luc Roy", "titre": "Directeur",
                         "source_url": "s", "confidence": "medium"}


def test_summary_two_highs_ambiguous_goes_potentiel():
    decideurs = [
        {"nom_complet": "Marie Gagnon", "titre": "Co-propriétaire", "source_url": "a", "confidence": "high"},
        {"nom_complet": "Luc Roy", "titre": "Co-propriétaire", "source_url": "b", "confidence": "high"},
    ]
    confirme, potentiel = summarize_company_decideur(decideurs, [_email("info", "generic")])
    assert confirme is None
    assert potentiel["confidence"] == "high"   # fort, mais ambigu donc pas confirmé


def test_summary_nothing_when_no_decideur():
    confirme, potentiel = summarize_company_decideur([], [_email("info", "generic")])
    assert confirme is None and potentiel is None
    confirme2, potentiel2 = summarize_company_decideur(None, None)
    assert confirme2 is None and potentiel2 is None
