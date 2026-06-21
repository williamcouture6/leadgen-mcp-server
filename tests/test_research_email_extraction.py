"""Tests extraction d'emails du scraper Research (WF-3).

Régression terrain (Lajoie Paysagistes, 2026-06-21) : le scraper trouvait
0 email alors que le site en publie un. Trois causes empilées :
  1. la page /contactez-nous (où vit l'email) n'était jamais fetchée — budget de
     pages épuisé par des pages moins utiles en ordre DOM ;
  2. l'email était obfusqué par Cloudflare (`<span data-cfemail=...>`) → invisible
     à EMAIL_REGEX ;
  3. le courriel vit sur un domaine-frère (info@fermehorticolelajoie.com sur le
     site famillelajoie.com) → jeté par le filtre same-domain.
"""
from __future__ import annotations

from src.tools import research


# --- (B) décodage Cloudflare cfemail ---------------------------------------

# hex réel capturé sur famillelajoie.com/contactez-nous → info@fermehorticolelajoie.com
_LAJOIE_CFEMAIL = "3f565159507f595a4d525a57504d4b565c50535a535e5550565a115c5052"


def test_decode_cfemail_decodes_real_cloudflare_hash() -> None:
    assert research._decode_cfemail(_LAJOIE_CFEMAIL) == "info@fermehorticolelajoie.com"


def test_decode_cfemail_rejects_garbage() -> None:
    assert research._decode_cfemail("zzz") is None
    assert research._decode_cfemail("") is None
    assert research._decode_cfemail("3f") is None  # trop court (clé seule)


def test_extract_decodes_cfemail_sibling_domain() -> None:
    # Cas Lajoie complet : cfemail obfusqué + domaine-frère, sur le site famillelajoie.
    html = f"""
    <a href="/cdn-cgi/l/email-protection"><span class="__cf_email__"
       data-cfemail="{_LAJOIE_CFEMAIL}">[email&#160;protected]</span></a>
    """
    emails = research._extract_emails_from_html(html, "https://famillelajoie.com/contactez-nous/")
    addrs = {e["email"] for e in emails}
    assert "info@fermehorticolelajoie.com" in addrs


# --- (C) politique cross-domain : domaine-frère vs tiers --------------------

def test_explicit_cross_domain_non_affine_is_rejected() -> None:
    # mailto vers un domaine tiers sans radical commun (cas SET Jardin → unionmd) → rejeté.
    html = '<a href="mailto:mtlestinfo@unionmd.ca">écrivez-nous</a>'
    emails = research._extract_emails_from_html(html, "https://www.setjardin.ca/")
    assert emails == []


def test_freetext_cross_domain_is_rejected() -> None:
    # Email partenaire cité en prose (pas un lien) sur un autre domaine → jamais scrapé.
    html = "<p>Réalisé en partenariat avec partenaire@autreboite.com pour le design.</p>"
    emails = research._extract_emails_from_html(html, "https://maboite.com/")
    assert emails == []


def test_same_domain_generic_still_kept() -> None:
    html = '<a href="mailto:info@maboite.com">contact</a>'
    emails = research._extract_emails_from_html(html, "https://maboite.com/contact")
    assert {e["email"] for e in emails} == {"info@maboite.com"}


def test_personal_nominative_still_kept() -> None:
    html = "<p>Écrivez à jean.tremblay@gmail.com</p>"
    emails = research._extract_emails_from_html(html, "https://maboite.com/")
    assert {e["email"] for e in emails} == {"jean.tremblay@gmail.com"}


# --- (A) priorisation des pages internes -----------------------------------

def test_rank_pages_prioritizes_contact_over_services() -> None:
    # contact apparaît APRÈS services dans le DOM, mais doit passer en premier.
    html = """
    <a href="/nos-services/">Nos services</a>
    <a href="/a-propos/">À propos</a>
    <a href="/contactez-nous/">Contact</a>
    """
    ranked = research._rank_internal_pages("https://x.ca/", html, max_links=2)
    assert ranked[0].endswith("/contactez-nous/")
    assert any(u.endswith("/a-propos/") for u in ranked)  # équipe/à-propos prioritaire sur services


def test_rank_pages_dedupes_fragments() -> None:
    html = """
    <a href="/contactez-nous/">Contact</a>
    <a href="/contactez-nous/#horaire">Horaire</a>
    <a href="/contactez-nous/#coordonnees">Coordonnées</a>
    """
    ranked = research._rank_internal_pages("https://x.ca/", html, max_links=5)
    assert ranked == ["https://x.ca/contactez-nous/"]


def test_rank_pages_skips_external_hosts() -> None:
    html = '<a href="https://plannit.io/merchants/x">Réserver</a><a href="/contact/">Contact</a>'
    ranked = research._rank_internal_pages("https://x.ca/", html, max_links=5)
    assert all("plannit.io" not in u for u in ranked)
    assert ranked == ["https://x.ca/contact/"]
